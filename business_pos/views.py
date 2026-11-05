import uuid
import csv
from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum, Count, F, Q
from django.http import Http404, HttpResponse
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from .models import (
    Shift, BarTab, Customer, Order, OrderItem, Payment,
    CustomerCreditTransaction, Supplier, PurchaseOrder, SupplierPayment
)
from .serializers import (
    ShiftSerializer, BarTabSerializer, CustomerSerializer,
    OrderSerializer, CustomerCreditTransactionSerializer,
    SupplierSerializer, PurchaseOrderSerializer, SupplierPaymentSerializer
)
from business_catalog.models import Product
from business_core.audit import record_audit
from business_core.models import User


class ShiftViewSet(viewsets.ModelViewSet):
    """
    Shift and Cash Reconciliation management with Z-Report and End-of-Day generation.
    """
    serializer_class = ShiftSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Shift.objects.for_request(self.request)

    @action(detail=False, methods=['get'])
    def current(self, request):
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = request.user.business_unit_id or 'bar'
            
        user = request.user
        is_manager = user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER)
        
        if is_manager:
            # Managers and Owner see active cashier shift for their unit
            shift = Shift.objects.filter(
                business_unit_id=unit_id,
                status=Shift.STATUS_OPEN
            ).first()
            active_shifts = list(Shift.objects.filter(
                business_unit_id=unit_id,
                status=Shift.STATUS_OPEN
            ).values(
                'id', 'cashier__username', 'cashier__first_name', 'cashier__last_name',
                'opening_float', 'total_sales_cash', 'total_sales_mobile', 'opened_at'
            ))
            return Response({
                'active_shift': ShiftSerializer(shift).data if shift else None,
                'active_shifts_list': active_shifts,
                'is_manager_oversight': True
            })
        else:
            shift = Shift.objects.filter(
                business_unit_id=unit_id,
                cashier=user,
                status=Shift.STATUS_OPEN
            ).first()
            return Response({
                'active_shift': ShiftSerializer(shift).data if shift else None,
                'is_manager_oversight': False
            })

    @action(detail=False, methods=['post'])
    def start_shift(self, request):
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all':
            unit_id = request.user.business_unit_id or 'bar'

        user = request.user
        # Enforce role boundaries: Only frontline cashier staff operate cash drawers
        if user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            return Response({
                'error': 'Permission Denied: Managers and the Business Owner do not operate cashier register drawers. Register shifts can only be opened by designated cashiers (Bartender / Stationery Cashier).',
                'code': 'ROLE_SHIFT_RESTRICTED'
            }, status=status.HTTP_403_FORBIDDEN)

        if unit_id == 'bar' and user.role != User.ROLE_BARTENDER:
            return Response({
                'error': 'Only a Bartender can open a register shift for Bar & Lounge.',
                'code': 'ROLE_UNIT_MISMATCH'
            }, status=status.HTTP_403_FORBIDDEN)

        if unit_id == 'stationery' and user.role != User.ROLE_STATIONERY_CASHIER:
            return Response({
                'error': 'Only a Stationery Cashier can open a register shift for Stationery & Retail.',
                'code': 'ROLE_UNIT_MISMATCH'
            }, status=status.HTTP_403_FORBIDDEN)

        existing = Shift.objects.filter(
            business_unit_id=unit_id,
            cashier=request.user,
            status=Shift.STATUS_OPEN
        ).first()
        if existing:
            return Response(
                {'error': 'You already have an active open shift.', 'shift': ShiftSerializer(existing).data},
                status=status.HTTP_400_BAD_REQUEST
            )

        opening_float = Decimal(str(request.data.get('opening_float', '0.00')))
        notes = request.data.get('notes', '')

        shift = Shift.objects.create(
            business_unit_id=unit_id,
            cashier=request.user,
            opening_float=opening_float,
            expected_cash=opening_float,
            notes=notes,
            status=Shift.STATUS_OPEN
        )

        record_audit(
            request=request,
            action='SHIFT_OPENED',
            entity_type='Shift',
            entity_id=shift.id,
            after_value={'opening_float': str(opening_float)}
        )

        return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def close_shift(self, request, pk=None):
        shift = self.get_object()
        if shift.status == Shift.STATUS_CLOSED:
            return Response({'error': 'Shift is already closed.'}, status=status.HTTP_400_BAD_REQUEST)

        counted_cash = Decimal(str(request.data.get('counted_cash', '0.00')))
        variance_reason = request.data.get('variance_reason', '')

        # Expected cash in drawer = opening float + cash sales + debt payments collected in cash
        expected_cash = shift.opening_float + shift.total_sales_cash + shift.total_debt_recovered
        variance = counted_cash - expected_cash

        shift.counted_cash = counted_cash
        shift.expected_cash = expected_cash
        shift.variance = variance
        shift.variance_reason = variance_reason
        shift.ended_at = timezone.now()
        shift.status = Shift.STATUS_CLOSED
        shift.save()

        action_name = 'SHIFT_CLOSED_Z_REPORT' if shift.business_unit_id == 'bar' else 'END_OF_DAY_RECONCILIATION'

        record_audit(
            request=request,
            action=action_name,
            entity_type='Shift',
            entity_id=shift.id,
            before_value={'expected_cash': str(expected_cash)},
            after_value={
                'counted_cash': str(counted_cash),
                'variance': str(variance),
                'total_sales_cash': str(shift.total_sales_cash),
                'total_sales_mobile': str(shift.total_sales_mobile),
                'total_sales_credit': str(shift.total_sales_credit),
            },
            metadata={'variance_reason': variance_reason}
        )

        return Response(ShiftSerializer(shift).data)

    @action(detail=True, methods=['get'])
    def z_report(self, request, pk=None):
        shift = self.get_object()
        return Response({
            'shift_id': shift.id,
            'business_unit': shift.business_unit_id,
            'cashier': shift.cashier.username,
            'started_at': shift.started_at,
            'ended_at': shift.ended_at,
            'status': shift.status,
            'opening_float': str(shift.opening_float),
            'total_sales_cash': str(shift.total_sales_cash),
            'total_sales_mobile': str(shift.total_sales_mobile),
            'total_sales_credit': str(shift.total_sales_credit),
            'total_debt_recovered': str(shift.total_debt_recovered),
            'total_sales_gross': str(shift.total_sales_cash + shift.total_sales_mobile + shift.total_sales_credit),
            'expected_cash_in_drawer': str(shift.opening_float + shift.total_sales_cash + shift.total_debt_recovered),
            'counted_cash': str(shift.counted_cash or '0.00'),
            'variance': str(shift.variance),
            'variance_reason': shift.variance_reason,
            'order_count': shift.order_count,
            'generated_at': timezone.now().isoformat(),
        })


class BarTabViewSet(viewsets.ModelViewSet):
    serializer_class = BarTabSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BarTab.objects.for_request(self.request).filter(business_unit_id='bar')

    def perform_create(self, serializer):
        user = self.request.user
        tab = serializer.save(
            business_unit_id='bar',
            opened_by=user,
            status=BarTab.STATUS_OPEN
        )
        record_audit(
            request=self.request,
            action='TAB_OPENED',
            entity_type='BarTab',
            entity_id=tab.id,
            after_value={'tab_name': tab.tab_name, 'customer_name': tab.customer_name}
        )

    @action(detail=True, methods=['post'])
    def reopen(self, request, pk=None):
        tab = self.get_object()
        approver_id = request.data.get('approver_id')
        manager_pin = request.data.get('manager_pin')

        approver = None
        if manager_pin:
            managers = User.objects.filter(
                role__in=[User.ROLE_OWNER, User.ROLE_BAR_MANAGER], is_active=True
            )
            for m in managers:
                if m.check_pin(manager_pin):
                    approver = m
                    break
        elif approver_id:
            approver = User.objects.filter(
                id=approver_id, role__in=[User.ROLE_OWNER, User.ROLE_BAR_MANAGER], is_active=True
            ).first()

        if not approver and not request.user.is_bar_manager_or_owner:
            record_audit(
                request=request,
                action='TAB_REOPEN_DENIED',
                entity_type='BarTab',
                entity_id=tab.id
            )
            return Response(
                {'error': 'Reopening a tab strictly requires Bar Manager or Owner approval.'},
                status=status.HTTP_403_FORBIDDEN
            )

        tab.status = BarTab.STATUS_OPEN
        tab.closed_at = None
        tab.save()

        record_audit(
            request=request,
            user=approver or request.user,
            action='TAB_REOPENED',
            entity_type='BarTab',
            entity_id=tab.id,
            metadata={'reopened_by': request.user.username}
        )

        return Response({'message': f"Tab '{tab.tab_name}' successfully reopened.", 'tab': BarTabSerializer(tab).data})


class CustomerViewSet(viewsets.ModelViewSet):
    """
    Stationery customer registry with credit balances and debt repayments.
    """
    serializer_class = CustomerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Customer.objects.for_request(self.request).filter(business_unit_id='stationery')

    def perform_create(self, serializer):
        user = self.request.user
        customer = serializer.save(business_unit_id='stationery')
        record_audit(
            request=self.request,
            action='CUSTOMER_REGISTERED',
            entity_type='Customer',
            entity_id=customer.id,
            after_value={'name': customer.name, 'credit_limit': str(customer.credit_limit)}
        )

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def record_payment(self, request, pk=None):
        """
        Record a customer debt repayment (Cash, Mobile Money, Bank Transfer).
        Decrements customer.current_balance and updates active shift drawer if cash.
        """
        customer = self.get_object()
        amount = Decimal(str(request.data.get('amount', '0.00')))
        payment_method = request.data.get('payment_method', 'CASH').upper()
        reference = request.data.get('reference', '')
        notes = request.data.get('notes', '')

        if amount <= 0:
            return Response({'error': 'Payment amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

        if amount > customer.current_balance:
            return Response({
                'error': f"Payment amount (TSH {amount}) exceeds current balance due (TSH {customer.current_balance})."
            }, status=status.HTTP_400_BAD_REQUEST)

        balance_before = customer.current_balance
        customer.current_balance = F('current_balance') - amount
        customer.save(update_fields=['current_balance'])
        customer.refresh_from_db()

        # Create credit transaction record
        tx = CustomerCreditTransaction.objects.create(
            business_unit_id='stationery',
            customer=customer,
            transaction_type=CustomerCreditTransaction.TYPE_PAYMENT,
            amount=amount,
            balance_before=balance_before,
            balance_after=customer.current_balance,
            payment_method=payment_method,
            reference=reference,
            recorded_by=request.user,
            notes=notes
        )

        # Update shift totals if cashier has active open shift
        shift = Shift.objects.filter(
            business_unit_id='stationery', cashier=request.user, status=Shift.STATUS_OPEN
        ).first()
        if shift:
            shift.total_debt_recovered = F('total_debt_recovered') + amount
            shift.save(update_fields=['total_debt_recovered'])

        record_audit(
            request=request,
            action='CUSTOMER_DEBT_REPAID',
            entity_type='Customer',
            entity_id=customer.id,
            before_value={'current_balance': str(balance_before)},
            after_value={'current_balance': str(customer.current_balance)},
            metadata={'amount_paid': str(amount), 'payment_method': payment_method}
        )

        return Response({
            'message': f"Payment of TSH {amount} recorded for {customer.name}.",
            'customer': CustomerSerializer(customer).data,
            'transaction': CustomerCreditTransactionSerializer(tx).data
        })

    @action(detail=True, methods=['get'])
    def statement(self, request, pk=None):
        """Statement of account for a customer showing all credit sales and payments."""
        customer = self.get_object()
        txs = customer.credit_transactions.all()
        return Response({
            'customer': CustomerSerializer(customer).data,
            'transactions': CustomerCreditTransactionSerializer(txs, many=True).data
        })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def debtors_aging_report_view(request):
    """
    Debtors Aging Report for Stationery business:
    Categories: Current (0-30 days), 31-60 days, 61-90 days, and 90+ days.
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('stationery', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    customers = Customer.objects.filter(business_unit_id='stationery', current_balance__gt=0)
    now = timezone.now()

    aging_rows = []
    total_current = Decimal('0.00')
    total_30_60 = Decimal('0.00')
    total_61_90 = Decimal('0.00')
    total_90_plus = Decimal('0.00')
    grand_total_debt = Decimal('0.00')

    for cust in customers:
        # Age unpaid credit sales
        sales = CustomerCreditTransaction.objects.filter(
            customer=cust, transaction_type='CREDIT_SALE'
        ).order_by('-created_at')

        b_current = Decimal('0.00')
        b_30_60 = Decimal('0.00')
        b_61_90 = Decimal('0.00')
        b_90_plus = Decimal('0.00')

        rem_balance = cust.current_balance

        for sale in sales:
            if rem_balance <= 0:
                break
            amt = min(sale.amount, rem_balance)
            days_old = (now - sale.created_at).days

            if days_old <= 30:
                b_current += amt
            elif days_old <= 60:
                b_30_60 += amt
            elif days_old <= 90:
                b_61_90 += amt
            else:
                b_90_plus += amt

            rem_balance -= amt

        total_current += b_current
        total_30_60 += b_30_60
        total_61_90 += b_61_90
        total_90_plus += b_90_plus
        grand_total_debt += cust.current_balance

        aging_rows.append({
            'customer_id': cust.id,
            'customer_name': cust.name,
            'phone': cust.phone,
            'credit_limit': str(cust.credit_limit),
            'total_balance': str(cust.current_balance),
            'current_0_30': str(b_current),
            'aging_31_60': str(b_30_60),
            'aging_61_90': str(b_61_90),
            'aging_90_plus': str(b_90_plus),
        })

    return Response({
        'generated_at': now.isoformat(),
        'business_unit': 'stationery',
        'debtors': aging_rows,
        'summary': {
            'total_current': str(total_current),
            'total_31_60': str(total_30_60),
            'total_61_90': str(total_61_90),
            'total_90_plus': str(total_90_plus),
            'grand_total_debt': str(grand_total_debt),
            'debtors_count': len(aging_rows),
        }
    })


class SupplierViewSet(viewsets.ModelViewSet):
    serializer_class = SupplierSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Supplier.objects.for_request(self.request)

    def perform_create(self, serializer):
        unit_id = getattr(self.request, 'business_unit', self.request.user.business_unit_id) or 'stationery'
        instance = serializer.save(business_unit_id=unit_id)
        record_audit(
            request=self.request,
            action='SUPPLIER_CREATED',
            entity_type='Supplier',
            entity_id=instance.id,
            after_value={'name': instance.name}
        )


class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """
    Receiving inventory and recording supplier payables.
    """
    serializer_class = PurchaseOrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return PurchaseOrder.objects.for_request(self.request)

    @transaction.atomic
    def perform_create(self, serializer):
        user = self.request.user
        unit_id = getattr(self.request, 'business_unit', user.business_unit_id) or 'stationery'
        order_number = f"PO-{timezone.now().strftime('%y%m%d%H%M')}-{uuid.uuid4().hex[:4].upper()}"

        po = serializer.save(
            business_unit_id=unit_id,
            order_number=order_number,
            received_by=user,
            status='UNPAID'
        )

        # Increase supplier payable balance
        supplier = po.supplier
        supplier.balance_payable = F('balance_payable') + po.total_cost
        supplier.save(update_fields=['balance_payable'])

        record_audit(
            request=self.request,
            action='PURCHASE_ORDER_RECEIVED',
            entity_type='PurchaseOrder',
            entity_id=po.id,
            after_value={'total_cost': str(po.total_cost), 'supplier': supplier.name}
        )

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def record_payment(self, request, pk=None):
        """Pay supplier against a purchase order."""
        po = self.get_object()
        amount = Decimal(str(request.data.get('amount', '0.00')))
        payment_method = request.data.get('payment_method', 'BANK_TRANSFER')
        reference = request.data.get('reference', '')
        notes = request.data.get('notes', '')

        if amount <= 0:
            return Response({'error': 'Amount must be positive.'}, status=status.HTTP_400_BAD_REQUEST)

        supplier = po.supplier
        supplier.balance_payable = F('balance_payable') - amount
        supplier.save(update_fields=['balance_payable'])

        po.amount_paid = F('amount_paid') + amount
        po.save(update_fields=['amount_paid'])
        po.refresh_from_db()

        if po.amount_paid >= po.total_cost:
            po.status = 'PAID'
        elif po.amount_paid > 0:
            po.status = 'PARTIALLY_PAID'
        po.save(update_fields=['status'])

        sp = SupplierPayment.objects.create(
            business_unit=po.business_unit,
            supplier=supplier,
            purchase_order=po,
            amount=amount,
            payment_method=payment_method,
            reference=reference,
            recorded_by=request.user,
            notes=notes
        )

        record_audit(
            request=request,
            action='SUPPLIER_PAYMENT_RECORDED',
            entity_type='SupplierPayment',
            entity_id=sp.id,
            after_value={'amount': str(amount), 'supplier': supplier.name}
        )

        return Response({
            'message': f"Payment of TSH {amount} recorded to {supplier.name}.",
            'purchase_order': PurchaseOrderSerializer(po).data
        })


class OrderViewSet(viewsets.ModelViewSet):
    """
    POS Order Processing:
    - Supports Bar and Stationery.
    - Automatic Unit vs Pack conversion for Stationery.
    - Customer Credit authorization for registered stationery customers.
    - Non-bypassable alcohol age check for Bar.
    """
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.for_request(self.request)

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = request.user.business_unit_id or 'bar'

        user = request.user

        # ENFORCE STRICT CORE ROLES: Managers, Business Owners, and Stock Clerks cannot record sales
        if user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            return Response({
                'error': 'Sales recording is strictly reserved for frontline staff (Bartender / Stationery Cashier). Managers and Business Owners perform oversight, inventory, reporting, and approvals only.',
                'code': 'SALES_RECORDING_RESTRICTED'
            }, status=status.HTTP_403_FORBIDDEN)

        if unit_id == 'bar' and user.role != User.ROLE_BARTENDER:
            return Response({
                'error': 'Only a designated Bartender can record sales in the Bar & Lounge.',
                'code': 'ROLE_UNIT_MISMATCH'
            }, status=status.HTTP_403_FORBIDDEN)

        if unit_id == 'stationery' and user.role != User.ROLE_STATIONERY_CASHIER:
            return Response({
                'error': 'Only a designated Stationery Cashier can record sales in Stationery & Retail.',
                'code': 'ROLE_UNIT_MISMATCH'
            }, status=status.HTTP_403_FORBIDDEN)

        items_data = request.data.get('items', [])
        payments_data = request.data.get('payments', [])
        tab_id = request.data.get('tab_id')
        customer_id = request.data.get('customer_id')
        shift_id = request.data.get('shift_id')
        is_age_verified = bool(request.data.get('is_age_verified', False))
        discount_amount = Decimal(str(request.data.get('discount_amount', '0.00')))
        discount_reason = request.data.get('discount_reason', '')
        discount_approver_id = request.data.get('discount_approved_by')

        if not items_data:
            return Response({'error': 'Order must contain at least one item.'}, status=status.HTTP_400_BAD_REQUEST)

        has_alcoholic_item = False
        parsed_items = []
        subtotal = Decimal('0.00')

        # 1. PARSE ITEMS & HANDLE UNIT VS PACK PRICING
        for item_data in items_data:
            product_id = item_data.get('product_id')
            qty = Decimal(str(item_data.get('quantity', 1)))
            is_pack_sale = bool(item_data.get('is_pack_sale', False))
            
            try:
                product = Product.objects.select_for_update().get(id=product_id, business_unit_id=unit_id)
            except Product.DoesNotExist:
                raise Http404(f"Product ID {product_id} not found in {unit_id}.")

            if product.is_alcoholic:
                has_alcoholic_item = True

            # Determine price and stock deduction ratio
            if product.is_pack and not is_pack_sale and product.allow_split_sale:
                # Sold as individual units/pieces
                line_price = product.split_unit_price or (product.selling_price / Decimal(product.pack_unit_ratio))
                stock_deduction = qty / Decimal(product.pack_unit_ratio)
            else:
                # Sold as full pack or standard product
                line_price = product.selling_price
                stock_deduction = qty

            line_subtotal = line_price * qty
            subtotal += line_subtotal

            parsed_items.append({
                'product': product,
                'quantity': qty,
                'unit_price': line_price,
                'subtotal': line_subtotal,
                'is_alcoholic': product.is_alcoholic,
                'is_pack_sale': is_pack_sale,
                'pack_unit_ratio': product.pack_unit_ratio,
                'stock_deduction': stock_deduction
            })

        # 2. NON-BYPASSABLE AGE CHECK (Bar)
        if has_alcoholic_item and not is_age_verified:
            record_audit(
                request=request,
                action='AGE_CHECK_BLOCKED_SALE',
                entity_type='Order',
                metadata={'items_count': len(parsed_items)}
            )
            return Response({
                'error': 'NON-BYPASSABLE: Alcoholic items present in cart. Customer age (18+) verification is legally required before sale.',
                'code': 'AGE_VERIFICATION_REQUIRED'
            }, status=status.HTTP_400_BAD_REQUEST)

        final_amount = max(Decimal('0.00'), subtotal - discount_amount)

        # 3. PAYMENT VALIDATION & CREDIT RULES
        total_paid = Decimal('0.00')
        parsed_payments = []
        customer = None

        if not payments_data and not tab_id:
            return Response({'error': 'Payment information or tab attachment is required.'}, status=status.HTTP_400_BAD_REQUEST)

        for p_data in payments_data:
            method = p_data.get('payment_method', 'CASH').upper()
            amt = Decimal(str(p_data.get('amount', '0.00')))
            ref = p_data.get('reference', '')

            if unit_id == 'bar' and method == 'CREDIT':
                return Response(
                    {'error': 'Bar sales accept Cash and Mobile Money only. Credit is strictly prohibited in the Bar unit.'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if method == 'CREDIT':
                if not customer_id:
                    return Response({'error': 'Credit sales require selecting a registered customer.'}, status=status.HTTP_400_BAD_REQUEST)
                try:
                    customer = Customer.objects.select_for_update().get(id=customer_id, business_unit_id='stationery', is_active=True)
                except Customer.DoesNotExist:
                    raise Http404("Registered customer not found in Stationery.")

                # Check credit limit
                new_balance = customer.current_balance + amt
                if new_balance > customer.credit_limit:
                    manager_pin = request.data.get('manager_pin')
                    approver = None
                    if manager_pin:
                        managers = User.objects.filter(is_active=True).exclude(pin_hash__isnull=True).filter(
                            role__in=[User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER]
                        )
                        for m in managers:
                            if m.check_pin(manager_pin):
                                approver = m
                                break
                    if not approver and not user.is_stationery_manager_or_owner:
                        return Response({
                            'error': f"Credit limit exceeded! Limit: TSH {customer.credit_limit}, Current Owed: TSH {customer.current_balance}. Manager authorization required.",
                            'code': 'CREDIT_LIMIT_EXCEEDED'
                        }, status=status.HTTP_403_FORBIDDEN)

            total_paid += amt
            parsed_payments.append({'method': method, 'amount': amt, 'reference': ref})

        # Validate against open tab or full payment
        tab = None
        if tab_id:
            try:
                tab = BarTab.objects.get(id=tab_id, business_unit_id='bar', status=BarTab.STATUS_OPEN)
            except BarTab.DoesNotExist:
                return Response({'error': 'Active open bar tab not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not tab and total_paid < final_amount:
            return Response({
                'error': f"Total paid ({total_paid}) is less than final amount due ({final_amount})."
            }, status=status.HTTP_400_BAD_REQUEST)

        # 4. RESOLVE SHIFT
        shift = None
        if shift_id:
            shift = Shift.objects.filter(id=shift_id, status=Shift.STATUS_OPEN).first()
        if not shift:
            shift = Shift.objects.filter(
                business_unit_id=unit_id, cashier=user, status=Shift.STATUS_OPEN
            ).first()

        # 5. CREATE ORDER
        order = Order.objects.create(
            business_unit_id=unit_id,
            order_number=Order.generate_order_number(unit_id),
            shift=shift,
            tab=tab,
            customer=customer,
            cashier=user,
            subtotal=subtotal,
            discount_amount=discount_amount,
            discount_reason=discount_reason,
            discount_approved_by_id=discount_approver_id if discount_amount > 0 else None,
            final_amount=final_amount,
            status=Order.STATUS_COMPLETED,
            is_age_verified=is_age_verified,
            age_verified_by=user if is_age_verified else None,
            age_verified_at=timezone.now() if is_age_verified else None,
        )

        # 6. CREATE ORDER ITEMS & DEDUCT INVENTORY
        for p_item in parsed_items:
            product = p_item['product']
            qty = p_item['quantity']

            OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                unit_price=p_item['unit_price'],
                quantity=qty,
                subtotal=p_item['subtotal'],
                is_alcoholic=p_item['is_alcoholic'],
                is_pack_sale=p_item['is_pack_sale'],
                pack_unit_ratio=p_item['pack_unit_ratio']
            )

            # Atomic stock deduction
            product.stock_quantity = F('stock_quantity') - p_item['stock_deduction']
            product.save(update_fields=['stock_quantity'])

        # 7. RECORD PAYMENTS & UPDATE RELEVANT LEDGERS
        for p in parsed_payments:
            Payment.objects.create(
                order=order,
                payment_method=p['method'],
                amount=p['amount'],
                reference=p['reference']
            )

            if p['method'] == 'CREDIT' and customer:
                balance_before = customer.current_balance
                customer.current_balance = F('current_balance') + p['amount']
                customer.save(update_fields=['current_balance'])
                customer.refresh_from_db()

                CustomerCreditTransaction.objects.create(
                    business_unit_id='stationery',
                    customer=customer,
                    order=order,
                    transaction_type=CustomerCreditTransaction.TYPE_SALE,
                    amount=p['amount'],
                    balance_before=balance_before,
                    balance_after=customer.current_balance,
                    payment_method='CREDIT',
                    recorded_by=user,
                    notes=f"Order #{order.order_number}"
                )

            if shift:
                if p['method'] == 'CASH':
                    shift.total_sales_cash = F('total_sales_cash') + p['amount']
                elif p['method'] == 'MOBILE_MONEY':
                    shift.total_sales_mobile = F('total_sales_mobile') + p['amount']
                elif p['method'] == 'CREDIT':
                    shift.total_sales_credit = F('total_sales_credit') + p['amount']

        if shift:
            shift.order_count = F('order_count') + 1
            shift.save(update_fields=['total_sales_cash', 'total_sales_mobile', 'total_sales_credit', 'order_count'])

        # 8. AUDIT RECORD
        record_audit(
            request=request,
            action='SALE_COMPLETED',
            entity_type='Order',
            entity_id=order.id,
            after_value={
                'order_number': order.order_number,
                'final_amount': str(order.final_amount),
                'items_count': len(parsed_items),
                'business_unit': unit_id,
            },
            metadata={
                'payments': [
                    {'method': p['method'], 'amount': str(p['amount']), 'reference': p['reference']}
                    for p in parsed_payments
                ]
            }
        )

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def void_order(self, request, pk=None):
        order = self.get_object()
        if order.status == Order.STATUS_VOIDED:
            return Response({'error': 'Order is already voided.'}, status=status.HTTP_400_BAD_REQUEST)

        void_reason = request.data.get('reason', 'Cashier request')
        approver_id = request.data.get('approver_id')
        manager_pin = request.data.get('manager_pin')

        approver = None
        if manager_pin:
            managers = User.objects.filter(is_active=True).exclude(pin_hash__isnull=True)
            if order.business_unit_id == 'bar':
                managers = managers.filter(role__in=[User.ROLE_OWNER, User.ROLE_BAR_MANAGER])
            else:
                managers = managers.filter(role__in=[User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER])

            for m in managers:
                if m.check_pin(manager_pin):
                    approver = m
                    break
        elif approver_id:
            approver = User.objects.filter(id=approver_id, is_active=True).first()

        if not approver and not request.user.is_manager_or_owner:
            record_audit(
                request=request,
                action='VOID_ORDER_DENIED',
                entity_type='Order',
                entity_id=order.id,
                metadata={'reason': void_reason}
            )
            return Response(
                {'error': 'Voiding an order strictly requires Manager or Owner PIN authorization.'},
                status=status.HTTP_403_FORBIDDEN
            )

        # Restore product inventory
        for item in order.items.all():
            product = item.product
            stock_restore = item.quantity
            if item.product.is_pack and not item.is_pack_sale and item.product.allow_split_sale:
                stock_restore = item.quantity / Decimal(item.product.pack_unit_ratio)
            product.stock_quantity = F('stock_quantity') + stock_restore
            product.save(update_fields=['stock_quantity'])

        # Adjust shift cash / credit totals
        if order.shift:
            for payment in order.payments.all():
                if payment.payment_method == 'CASH':
                    order.shift.total_sales_cash = F('total_sales_cash') - payment.amount
                elif payment.payment_method == 'MOBILE_MONEY':
                    order.shift.total_sales_mobile = F('total_sales_mobile') - payment.amount
                elif payment.payment_method == 'CREDIT':
                    order.shift.total_sales_credit = F('total_sales_credit') - payment.amount
            order.shift.order_count = F('order_count') - 1
            order.shift.save(update_fields=['total_sales_cash', 'total_sales_mobile', 'total_sales_credit', 'order_count'])

        # Adjust customer debt balance if credit sale
        if order.customer:
            for payment in order.payments.filter(payment_method='CREDIT'):
                order.customer.current_balance = F('current_balance') - payment.amount
                order.customer.save(update_fields=['current_balance'])

        order.status = Order.STATUS_VOIDED
        order.void_reason = void_reason
        order.void_approved_by = approver or request.user
        order.voided_at = timezone.now()
        order.save()

        record_audit(
            request=request,
            user=approver or request.user,
            action='VOID_APPROVED',
            entity_type='Order',
            entity_id=order.id,
            before_value={'status': 'COMPLETED'},
            after_value={'status': 'VOIDED'},
            metadata={'void_reason': void_reason, 'cashier': order.cashier.username}
        )

        return Response({'message': f"Order #{order.order_number} has been voided and stock restored.", 'order': OrderSerializer(order).data})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def bar_reports_view(request):
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('bar', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    completed_orders = Order.objects.filter(business_unit_id='bar', status=Order.STATUS_COMPLETED)
    total_revenue = completed_orders.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')
    total_orders = completed_orders.count()
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else Decimal('0.00')

    # Payments breakdown
    payments = Payment.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    cash_payments = payments.filter(payment_method='CASH')
    momo_payments = payments.filter(payment_method='MOBILE_MONEY')
    cash_total = cash_payments.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    cash_count = cash_payments.count()
    momo_total = momo_payments.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    momo_count = momo_payments.count()

    # Product Sales breakdown
    items = OrderItem.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED).select_related('product')
    total_drinks_sold = Decimal('0.00')
    alcoholic_revenue = Decimal('0.00')
    alcoholic_count = Decimal('0.00')
    non_alcoholic_revenue = Decimal('0.00')
    non_alcoholic_count = Decimal('0.00')
    cogs = Decimal('0.00')

    product_dict = {}
    for itm in items:
        pid = itm.product_id
        p_name = itm.product.name
        is_alc = itm.product.is_alcoholic
        cost_price = itm.product.cost_price
        selling_price = itm.unit_price
        qty = itm.quantity
        subtotal = itm.subtotal
        item_cogs = cost_price * qty
        cogs += item_cogs
        total_drinks_sold += qty

        if is_alc:
            alcoholic_revenue += subtotal
            alcoholic_count += qty
        else:
            non_alcoholic_revenue += subtotal
            non_alcoholic_count += qty

        if pid not in product_dict:
            product_dict[pid] = {
                'product__id': pid,
                'product__name': p_name,
                'product__is_alcoholic': is_alc,
                'unit_cost': str(cost_price),
                'unit_price': str(selling_price),
                'total_quantity': Decimal('0.00'),
                'total_revenue': Decimal('0.00'),
                'total_cogs': Decimal('0.00'),
                'line_profit': Decimal('0.00'),
            }
        product_dict[pid]['total_quantity'] += qty
        product_dict[pid]['total_revenue'] += subtotal
        product_dict[pid]['total_cogs'] += item_cogs
        product_dict[pid]['line_profit'] += (subtotal - item_cogs)

    product_sales = []
    for p in product_dict.values():
        rev = p['total_revenue']
        profit = p['line_profit']
        margin = ((profit / rev) * 100) if rev > 0 else Decimal('0.00')
        product_sales.append({
            'product__id': p['product__id'],
            'product__name': p['product__name'],
            'product__is_alcoholic': p['product__is_alcoholic'],
            'unit_cost': p['unit_cost'],
            'unit_price': p['unit_price'],
            'total_quantity': int(p['total_quantity']),
            'total_revenue': str(rev),
            'total_cogs': str(p['total_cogs']),
            'line_profit': str(profit),
            'margin_percent': f"{margin:.1f}%"
        })
    product_sales.sort(key=lambda x: Decimal(x['total_revenue']), reverse=True)

    gross_profit = total_revenue - cogs
    gross_margin = ((gross_profit / total_revenue) * 100) if total_revenue > 0 else Decimal('0.00')

    # Inventory Valuation
    bar_products = Product.objects.filter(business_unit_id='bar', is_active=True)
    val_at_cost = Decimal('0.00')
    val_at_retail = Decimal('0.00')
    total_stock_units = Decimal('0.00')
    low_stock_count = 0
    out_of_stock_count = 0
    stock_on_hand = []

    for bp in bar_products:
        val_at_cost += bp.stock_quantity * bp.cost_price
        val_at_retail += bp.stock_quantity * bp.selling_price
        total_stock_units += bp.stock_quantity
        is_low = bp.stock_quantity <= bp.reorder_threshold
        is_out = bp.stock_quantity <= 0
        if is_out:
            out_of_stock_count += 1
        elif is_low:
            low_stock_count += 1

        stock_on_hand.append({
            'id': bp.id,
            'name': bp.name,
            'stock_quantity': str(bp.stock_quantity),
            'reorder_threshold': str(bp.reorder_threshold),
            'cost_price': str(bp.cost_price),
            'selling_price': str(bp.selling_price),
            'is_alcoholic': bp.is_alcoholic,
            'valuation_cost': str(bp.stock_quantity * bp.cost_price),
            'valuation_retail': str(bp.stock_quantity * bp.selling_price),
            'status': 'Out of Stock' if is_out else ('Low Stock' if is_low else 'Optimal')
        })
    stock_on_hand.sort(key=lambda x: Decimal(x['stock_quantity']))

    # Recent Shifts
    recent_shifts = []
    for s in Shift.objects.filter(business_unit_id='bar').select_related('cashier').order_by('-started_at')[:10]:
        recent_shifts.append({
            'id': s.id,
            'cashier': s.cashier.username,
            'started_at': s.started_at.isoformat() if s.started_at else None,
            'ended_at': s.ended_at.isoformat() if s.ended_at else None,
            'opening_float': str(s.opening_float),
            'total_sales_cash': str(s.total_sales_cash),
            'total_sales_mobile': str(s.total_sales_mobile),
            'expected_cash': str(s.expected_cash),
            'counted_cash': str(s.counted_cash) if s.counted_cash is not None else None,
            'variance': str(s.variance) if s.variance is not None else '0.00',
            'status': s.status
        })

    # CSV Download Check
    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="bar_comprehensive_report_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)
        
        writer.writerow(['THE FRINGE BAR & LOUNGE - COMPREHENSIVE BUSINESS REPORT'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S'), 'Currency', 'TSH'])
        writer.writerow([])

        writer.writerow(['EXECUTIVE FINANCIAL SUMMARY'])
        writer.writerow(['Total Revenue (TSH)', f"{total_revenue:.2f}"])
        writer.writerow(['Total Orders Completed', total_orders])
        writer.writerow(['Average Order Value (TSH)', f"{avg_order_value:.2f}"])
        writer.writerow(['Cost of Goods Sold (COGS) (TSH)', f"{cogs:.2f}"])
        writer.writerow(['Gross Profit (TSH)', f"{gross_profit:.2f}"])
        writer.writerow(['Gross Margin', f"{gross_margin:.1f}%"])
        writer.writerow(['Alcoholic Drinks Revenue (TSH)', f"{alcoholic_revenue:.2f}"])
        writer.writerow(['Non-Alcoholic Drinks Revenue (TSH)', f"{non_alcoholic_revenue:.2f}"])
        writer.writerow([])

        writer.writerow(['PAYMENT TENDERS BREAKDOWN'])
        writer.writerow(['Tender Method', 'Transactions Count', 'Total Amount (TSH)'])
        writer.writerow(['Cash', cash_count, f"{cash_total:.2f}"])
        writer.writerow(['Mobile Money (M-Pesa / Airtel)', momo_count, f"{momo_total:.2f}"])
        writer.writerow([])

        writer.writerow(['INVENTORY VALUATION'])
        writer.writerow(['Total Catalog Items', len(bar_products)])
        writer.writerow(['Total Stock Units on Hand', f"{total_stock_units:.0f}"])
        writer.writerow(['Inventory Valuation at Cost (TSH)', f"{val_at_cost:.2f}"])
        writer.writerow(['Inventory Valuation at Retail (TSH)', f"{val_at_retail:.2f}"])
        writer.writerow(['Potential Unrealized Gross Profit (TSH)', f"{(val_at_retail - val_at_cost):.2f}"])
        writer.writerow(['Low Stock Alert Items', low_stock_count])
        writer.writerow(['Out of Stock Items', out_of_stock_count])
        writer.writerow([])

        writer.writerow(['PRODUCT SALES BREAKDOWN'])
        writer.writerow(['Product Name', 'Classification', 'Units Sold', 'Unit Cost (TSH)', 'Selling Price (TSH)', 'Total Revenue (TSH)', 'Gross Profit (TSH)', 'Margin %'])
        for p in product_sales:
            writer.writerow([
                p['product__name'],
                'Alcoholic (18+)' if p['product__is_alcoholic'] else 'Non-Alcoholic',
                p['total_quantity'],
                p['unit_cost'],
                p['unit_price'],
                p['total_revenue'],
                p['line_profit'],
                p['margin_percent']
            ])
        writer.writerow([])

        writer.writerow(['STOCK-ON-HAND STATUS'])
        writer.writerow(['Product Name', 'Stock Qty', 'Reorder Threshold', 'Cost Price (TSH)', 'Selling Price (TSH)', 'Value at Cost (TSH)', 'Status'])
        for s in stock_on_hand:
            writer.writerow([
                s['name'],
                s['stock_quantity'],
                s['reorder_threshold'],
                s['cost_price'],
                s['selling_price'],
                s['valuation_cost'],
                s['status']
            ])

        return response

    return Response({
        'business_unit': 'bar',
        'currency': 'TSH',
        'generated_at': timezone.now().isoformat(),
        'summary': {
            'total_revenue': str(total_revenue),
            'total_orders': total_orders,
            'avg_order_value': f"{avg_order_value:.2f}",
            'total_drinks_sold': int(total_drinks_sold),
            'cogs': str(cogs),
            'gross_profit': str(gross_profit),
            'gross_margin_percent': f"{gross_margin:.1f}%",
            'alcoholic_revenue': str(alcoholic_revenue),
            'alcoholic_count': int(alcoholic_count),
            'non_alcoholic_revenue': str(non_alcoholic_revenue),
            'non_alcoholic_count': int(non_alcoholic_count),
        },
        'payment_tenders': {
            'cash_total': str(cash_total),
            'cash_count': cash_count,
            'mobile_money_total': str(momo_total),
            'mobile_money_count': momo_count,
            'total_tendered': str(cash_total + momo_total)
        },
        'inventory_valuation': {
            'total_items': len(bar_products),
            'total_units': int(total_stock_units),
            'valuation_at_cost': str(val_at_cost),
            'valuation_at_retail': str(val_at_retail),
            'potential_gross_profit': str(val_at_retail - val_at_cost),
            'low_stock_count': low_stock_count,
            'out_of_stock_count': out_of_stock_count,
        },
        'product_sales': product_sales,
        'stock_on_hand': stock_on_hand,
        'recent_shifts': recent_shifts,
        'profit_snapshot': {
            'total_revenue': str(total_revenue),
            'cogs': str(cogs),
            'gross_profit': str(gross_profit),
            'gross_margin_percent': f"{gross_margin:.1f}%",
        }
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def stationery_reports_view(request):
    """
    Stationery comprehensive reports:
    - End-of-Day Financial Totals (Cash, Mobile, Credit, Recoveries, Expected Drawer)
    - Pack vs Single Sales Split
    - Product Sales Breakdown with Margins
    - Debtors Aging (0-30, 31-60, 61-90, 90+ days)
    - Supplier Payables
    - Inventory Valuation at Cost and Retail
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('stationery', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    completed = Order.objects.filter(business_unit_id='stationery', status=Order.STATUS_COMPLETED)
    total_revenue = completed.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')
    total_orders = completed.count()
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else Decimal('0.00')

    payments = Payment.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED)
    cash_sales = payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    cash_count = payments.filter(payment_method='CASH').count()
    mobile_sales = payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    mobile_count = payments.filter(payment_method='MOBILE_MONEY').count()
    credit_sales = payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    credit_count = payments.filter(payment_method='CREDIT').count()

    # Recovered customer debts
    debt_recovered = CustomerCreditTransaction.objects.filter(
        business_unit_id='stationery', transaction_type='PAYMENT_RECEIVED'
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    # Pack vs Single split & COGS
    items = OrderItem.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED).select_related('product')
    cogs = Decimal('0.00')
    pack_sales_rev = Decimal('0.00')
    pack_sales_count = Decimal('0.00')
    single_sales_rev = Decimal('0.00')
    single_sales_count = Decimal('0.00')
    product_dict = {}

    for itm in items:
        p = itm.product
        qty = itm.quantity
        subtotal = itm.subtotal
        is_pack = itm.is_pack_sale

        if p.is_pack and not is_pack and p.allow_split_sale:
            item_cost = (p.cost_price / Decimal(p.pack_unit_ratio)) * qty
            single_sales_rev += subtotal
            single_sales_count += qty
        else:
            item_cost = p.cost_price * qty
            pack_sales_rev += subtotal
            pack_sales_count += qty

        cogs += item_cost
        key = (p.id, is_pack)
        if key not in product_dict:
            product_dict[key] = {
                'product__id': p.id,
                'product__name': p.name,
                'is_pack_sale': is_pack,
                'unit_cost': str(p.cost_price),
                'total_quantity': Decimal('0.00'),
                'total_revenue': Decimal('0.00'),
                'total_cogs': Decimal('0.00'),
                'line_profit': Decimal('0.00')
            }
        product_dict[key]['total_quantity'] += qty
        product_dict[key]['total_revenue'] += subtotal
        product_dict[key]['total_cogs'] += item_cost
        product_dict[key]['line_profit'] += (subtotal - item_cost)

    product_sales = []
    for p in product_dict.values():
        rev = p['total_revenue']
        profit = p['line_profit']
        margin = ((profit / rev) * 100) if rev > 0 else Decimal('0.00')
        product_sales.append({
            'product__id': p['product__id'],
            'product__name': p['product__name'],
            'is_pack_sale': p['is_pack_sale'],
            'sale_type': 'Whole Pack' if p['is_pack_sale'] else 'Single Piece',
            'unit_cost': p['unit_cost'],
            'total_quantity': int(p['total_quantity']),
            'total_revenue': str(rev),
            'total_cogs': str(p['total_cogs']),
            'line_profit': str(profit),
            'margin_percent': f"{margin:.1f}%"
        })
    product_sales.sort(key=lambda x: Decimal(x['total_revenue']), reverse=True)

    gross_profit = total_revenue - cogs
    gross_margin = ((gross_profit / total_revenue) * 100) if total_revenue > 0 else Decimal('0.00')

    # Debtors Aging
    now = timezone.now()
    active_customers = Customer.objects.filter(business_unit_id='stationery', is_active=True)
    total_debtors = active_customers.aggregate(t=Sum('current_balance'))['t'] or Decimal('0.00')
    active_debtors_count = active_customers.filter(current_balance__gt=0).count()

    aging_0_30 = Decimal('0.00')
    aging_31_60 = Decimal('0.00')
    aging_61_90 = Decimal('0.00')
    aging_90_plus = Decimal('0.00')

    unsettled_orders = Order.objects.filter(
        business_unit_id='stationery',
        status=Order.STATUS_COMPLETED,
        payments__payment_method='CREDIT'
    ).distinct()

    for ord in unsettled_orders:
        credit_amt = ord.payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
        age_days = (now - ord.created_at).days
        if age_days <= 30:
            aging_0_30 += credit_amt
        elif age_days <= 60:
            aging_31_60 += credit_amt
        elif age_days <= 90:
            aging_61_90 += credit_amt
        else:
            aging_90_plus += credit_amt

    top_debtors = []
    for c in active_customers.filter(current_balance__gt=0).order_by('-current_balance')[:10]:
        top_debtors.append({
            'id': c.id,
            'name': c.name,
            'phone': c.phone,
            'current_balance': str(c.current_balance),
            'credit_limit': str(c.credit_limit),
            'available_credit': str(c.available_credit)
        })

    # Supplier Payables
    suppliers_count = Supplier.objects.filter(business_unit_id='stationery', is_active=True).count()
    pos = PurchaseOrder.objects.filter(business_unit_id='stationery')
    total_po_count = pos.count()
    total_po_cost = pos.aggregate(t=Sum('total_cost'))['t'] or Decimal('0.00')
    total_paid_to_suppliers = SupplierPayment.objects.filter(purchase_order__business_unit_id='stationery').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    total_payables_outstanding = max(Decimal('0.00'), total_po_cost - total_paid_to_suppliers)

    # Inventory Valuation
    stat_products = Product.objects.filter(business_unit_id='stationery', is_active=True)
    val_at_cost = Decimal('0.00')
    val_at_retail = Decimal('0.00')
    low_stock_count = 0
    out_of_stock_count = 0
    stock_on_hand = []

    for sp in stat_products:
        val_at_cost += sp.stock_quantity * sp.cost_price
        val_at_retail += sp.stock_quantity * sp.selling_price
        is_low = sp.stock_quantity <= sp.reorder_threshold
        is_out = sp.stock_quantity <= 0
        if is_out:
            out_of_stock_count += 1
        elif is_low:
            low_stock_count += 1

        stock_on_hand.append({
            'id': sp.id,
            'name': sp.name,
            'stock_quantity': str(sp.stock_quantity),
            'reorder_threshold': str(sp.reorder_threshold),
            'cost_price': str(sp.cost_price),
            'selling_price': str(sp.selling_price),
            'is_pack': sp.is_pack,
            'pack_name': sp.pack_name,
            'valuation_cost': str(sp.stock_quantity * sp.cost_price),
            'valuation_retail': str(sp.stock_quantity * sp.selling_price),
            'status': 'Out of Stock' if is_out else ('Low Stock' if is_low else 'Optimal')
        })
    stock_on_hand.sort(key=lambda x: Decimal(x['stock_quantity']))

    # CSV Download Check
    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="stationery_comprehensive_report_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)

        writer.writerow(['THE FRINGE STATIONERY & RETAIL - COMPREHENSIVE BUSINESS REPORT'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S'), 'Currency', 'TSH'])
        writer.writerow([])

        writer.writerow(['END-OF-DAY FINANCIAL SUMMARY'])
        writer.writerow(['Total Gross Revenue (TSH)', f"{total_revenue:.2f}"])
        writer.writerow(['Total Orders Completed', total_orders])
        writer.writerow(['Average Order Value (TSH)', f"{avg_order_value:.2f}"])
        writer.writerow(['Cash Sales (TSH)', f"{cash_sales:.2f}"])
        writer.writerow(['Mobile Money Sales (TSH)', f"{mobile_sales:.2f}"])
        writer.writerow(['Customer Credit Sales (TSH)', f"{credit_sales:.2f}"])
        writer.writerow(['Customer Debt Recovered (TSH)', f"{debt_recovered:.2f}"])
        writer.writerow(['Expected Cash in Drawer (TSH)', f"{(cash_sales + debt_recovered):.2f}"])
        writer.writerow(['COGS (TSH)', f"{cogs:.2f}"])
        writer.writerow(['Gross Profit (TSH)', f"{gross_profit:.2f}"])
        writer.writerow(['Gross Margin', f"{gross_margin:.1f}%"])
        writer.writerow([])

        writer.writerow(['PACK VS SINGLE SALE RATIO'])
        writer.writerow(['Type', 'Units Sold', 'Revenue (TSH)'])
        writer.writerow(['Whole Packs', f"{pack_sales_count:.0f}", f"{pack_sales_rev:.2f}"])
        writer.writerow(['Single Pieces', f"{single_sales_count:.0f}", f"{single_sales_rev:.2f}"])
        writer.writerow([])

        writer.writerow(['DEBTORS AGING SUMMARY'])
        writer.writerow(['Total Outstanding Customer Debt (TSH)', f"{total_debtors:.2f}"])
        writer.writerow(['Active Debtors Count', active_debtors_count])
        writer.writerow(['Current (0-30 days) (TSH)', f"{aging_0_30:.2f}"])
        writer.writerow(['Aging (31-60 days) (TSH)', f"{aging_31_60:.2f}"])
        writer.writerow(['Aging (61-90 days) (TSH)', f"{aging_61_90:.2f}"])
        writer.writerow(['Aging (90+ days) (TSH)', f"{aging_90_plus:.2f}"])
        writer.writerow([])

        writer.writerow(['SUPPLIER PAYABLES OVERVIEW'])
        writer.writerow(['Registered Suppliers Count', suppliers_count])
        writer.writerow(['Purchase Orders Count', total_po_count])
        writer.writerow(['Total PO Cost (TSH)', f"{total_po_cost:.2f}"])
        writer.writerow(['Total Paid to Suppliers (TSH)', f"{total_paid_to_suppliers:.2f}"])
        writer.writerow(['Outstanding Payables Owed (TSH)', f"{total_payables_outstanding:.2f}"])
        writer.writerow([])

        writer.writerow(['INVENTORY VALUATION'])
        writer.writerow(['Catalog Items Count', len(stat_products)])
        writer.writerow(['Inventory Valuation at Cost (TSH)', f"{val_at_cost:.2f}"])
        writer.writerow(['Inventory Valuation at Retail (TSH)', f"{val_at_retail:.2f}"])
        writer.writerow(['Potential Gross Profit (TSH)', f"{(val_at_retail - val_at_cost):.2f}"])
        writer.writerow(['Low Stock Alert Items', low_stock_count])
        writer.writerow(['Out of Stock Items', out_of_stock_count])
        writer.writerow([])

        writer.writerow(['PRODUCT SALES BREAKDOWN'])
        writer.writerow(['Product Name', 'Sale Mode', 'Units Sold', 'Revenue (TSH)', 'COGS (TSH)', 'Gross Profit (TSH)', 'Margin %'])
        for p in product_sales:
            writer.writerow([
                p['product__name'],
                p['sale_type'],
                p['total_quantity'],
                p['total_revenue'],
                p['total_cogs'],
                p['line_profit'],
                p['margin_percent']
            ])

        return response

    return Response({
        'business_unit': 'stationery',
        'currency': 'TSH',
        'generated_at': timezone.now().isoformat(),
        'end_of_day': {
            'total_revenue': str(total_revenue),
            'cash_sales': str(cash_sales),
            'mobile_sales': str(mobile_sales),
            'credit_sales': str(credit_sales),
            'debt_recovered': str(debt_recovered),
            'expected_cash_drawer': str(cash_sales + debt_recovered),
            'total_debtors_balance': str(total_debtors),
            'total_orders': total_orders,
            'avg_order_value': f"{avg_order_value:.2f}",
            'pack_sales_rev': str(pack_sales_rev),
            'pack_sales_count': int(pack_sales_count),
            'single_sales_rev': str(single_sales_rev),
            'single_sales_count': int(single_sales_count),
            'cogs': str(cogs),
            'gross_profit': str(gross_profit),
            'gross_margin_percent': f"{gross_margin:.1f}%",
        },
        'debtors': {
            'total_balance': str(total_debtors),
            'active_debtors_count': active_debtors_count,
            'aging_0_30': str(aging_0_30),
            'aging_31_60': str(aging_31_60),
            'aging_61_90': str(aging_61_90),
            'aging_90_plus': str(aging_90_plus),
            'top_debtors': top_debtors
        },
        'payables': {
            'suppliers_count': suppliers_count,
            'total_po_count': total_po_count,
            'total_po_cost': str(total_po_cost),
            'total_paid': str(total_paid_to_suppliers),
            'outstanding_payables': str(total_payables_outstanding)
        },
        'inventory_valuation': {
            'total_items': len(stat_products),
            'valuation_at_cost': str(val_at_cost),
            'valuation_at_retail': str(val_at_retail),
            'potential_gross_profit': str(val_at_retail - val_at_cost),
            'low_stock_count': low_stock_count,
            'out_of_stock_count': out_of_stock_count,
        },
        'product_sales': product_sales,
        'stock_on_hand': stock_on_hand,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def consolidated_reports_view(request):
    """
    Consolidated Executive Report combining Bar & Lounge + Stationery & Retail:
    - Group Revenue, Orders, and Gross Profit
    - Share of Business %
    - Total Cash Expected in Drawers
    - Total Working Capital in Stock
    - Receivables vs Payables & Net Working Capital Position
    """
    user = request.user
    if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER):
        raise Http404("Consolidated reports require Manager or Owner authorization.")

    # 1. Bar Metrics
    bar_orders = Order.objects.filter(business_unit_id='bar', status=Order.STATUS_COMPLETED)
    bar_revenue = bar_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
    bar_orders_count = bar_orders.count()
    bar_items = OrderItem.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED).select_related('product')
    bar_cogs = Decimal('0.00')
    for itm in bar_items:
        bar_cogs += itm.product.cost_price * itm.quantity
    bar_profit = bar_revenue - bar_cogs
    bar_margin = ((bar_profit / bar_revenue) * 100) if bar_revenue > 0 else Decimal('0.00')

    bar_payments = Payment.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    bar_cash = bar_payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    bar_momo = bar_payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    bar_stock = Product.objects.filter(business_unit_id='bar', is_active=True)
    bar_stock_cost = Decimal('0.00')
    bar_stock_retail = Decimal('0.00')
    for bp in bar_stock:
        bar_stock_cost += bp.stock_quantity * bp.cost_price
        bar_stock_retail += bp.stock_quantity * bp.selling_price

    # 2. Stationery Metrics
    stat_orders = Order.objects.filter(business_unit_id='stationery', status=Order.STATUS_COMPLETED)
    stat_revenue = stat_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
    stat_orders_count = stat_orders.count()
    stat_items = OrderItem.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED).select_related('product')
    stat_cogs = Decimal('0.00')
    for itm in stat_items:
        p = itm.product
        if p.is_pack and not itm.is_pack_sale and p.allow_split_sale:
            stat_cogs += (p.cost_price / Decimal(p.pack_unit_ratio)) * itm.quantity
        else:
            stat_cogs += p.cost_price * itm.quantity
    stat_profit = stat_revenue - stat_cogs
    stat_margin = ((stat_profit / stat_revenue) * 100) if stat_revenue > 0 else Decimal('0.00')

    stat_payments = Payment.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED)
    stat_cash = stat_payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_momo = stat_payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_credit = stat_payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_debt_rec = CustomerCreditTransaction.objects.filter(
        business_unit_id='stationery', transaction_type='PAYMENT_RECEIVED'
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_drawer_cash = stat_cash + stat_debt_rec

    stat_stock = Product.objects.filter(business_unit_id='stationery', is_active=True)
    stat_stock_cost = Decimal('0.00')
    stat_stock_retail = Decimal('0.00')
    for sp in stat_stock:
        stat_stock_cost += sp.stock_quantity * sp.cost_price
        stat_stock_retail += sp.stock_quantity * sp.selling_price

    # Receivables & Payables
    total_receivables = Customer.objects.filter(business_unit_id='stationery', is_active=True).aggregate(t=Sum('current_balance'))['t'] or Decimal('0.00')
    pos = PurchaseOrder.objects.filter(business_unit_id='stationery')
    total_po_cost = pos.aggregate(t=Sum('total_cost'))['t'] or Decimal('0.00')
    total_po_paid = SupplierPayment.objects.filter(purchase_order__business_unit_id='stationery').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    total_payables = max(Decimal('0.00'), total_po_cost - total_po_paid)

    # Combined Group Totals
    group_revenue = bar_revenue + stat_revenue
    group_cogs = bar_cogs + stat_cogs
    group_profit = bar_profit + stat_profit
    group_margin = ((group_profit / group_revenue) * 100) if group_revenue > 0 else Decimal('0.00')
    group_drawer_cash = bar_cash + stat_drawer_cash
    group_inventory_cost = bar_stock_cost + stat_stock_cost
    group_inventory_retail = bar_stock_retail + stat_stock_retail

    bar_share = ((bar_revenue / group_revenue) * 100) if group_revenue > 0 else Decimal('0.00')
    stat_share = ((stat_revenue / group_revenue) * 100) if group_revenue > 0 else Decimal('0.00')

    net_working_capital = (group_drawer_cash + total_receivables + group_inventory_cost) - total_payables

    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="fringe_consolidated_report_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)

        writer.writerow(['THE FRINGE ENTERPRISES - CONSOLIDATED EXECUTIVE REPORT'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S'), 'Currency', 'TSH'])
        writer.writerow([])

        writer.writerow(['GROUP EXECUTIVE SUMMARY'])
        writer.writerow(['Total Group Revenue (TSH)', f"{group_revenue:.2f}"])
        writer.writerow(['Total Cost of Goods Sold (COGS) (TSH)', f"{group_cogs:.2f}"])
        writer.writerow(['Total Gross Profit (TSH)', f"{group_profit:.2f}"])
        writer.writerow(['Group Gross Margin', f"{group_margin:.1f}%"])
        writer.writerow(['Total Drawer Cash (TSH)', f"{group_drawer_cash:.2f}"])
        writer.writerow(['Working Capital in Inventory (TSH)', f"{group_inventory_cost:.2f}"])
        writer.writerow(['Accounts Receivable (Customer Debt) (TSH)', f"{total_receivables:.2f}"])
        writer.writerow(['Accounts Payable (Owed to Suppliers) (TSH)', f"{total_payables:.2f}"])
        writer.writerow(['Net Working Capital Position (TSH)', f"{net_working_capital:.2f}"])
        writer.writerow([])

        writer.writerow(['BUSINESS UNIT BREAKDOWN'])
        writer.writerow(['Unit', 'Revenue (TSH)', 'Share of Business', 'Orders', 'COGS (TSH)', 'Gross Profit (TSH)', 'Margin %', 'Drawer Cash (TSH)', 'Stock Valuation at Cost (TSH)'])
        writer.writerow(['Bar & Lounge', f"{bar_revenue:.2f}", f"{bar_share:.1f}%", bar_orders_count, f"{bar_cogs:.2f}", f"{bar_profit:.2f}", f"{bar_margin:.1f}%", f"{bar_cash:.2f}", f"{bar_stock_cost:.2f}"])
        writer.writerow(['Stationery & Retail', f"{stat_revenue:.2f}", f"{stat_share:.1f}%", stat_orders_count, f"{stat_cogs:.2f}", f"{stat_profit:.2f}", f"{stat_margin:.1f}%", f"{stat_drawer_cash:.2f}", f"{stat_stock_cost:.2f}"])
        writer.writerow(['TOTAL GROUP', f"{group_revenue:.2f}", '100%', bar_orders_count + stat_orders_count, f"{group_cogs:.2f}", f"{group_profit:.2f}", f"{group_margin:.1f}%", f"{group_drawer_cash:.2f}", f"{group_inventory_cost:.2f}"])

        return response

    return Response({
        'organization': 'The Fringe Enterprises Tanzania',
        'currency': 'TSH',
        'generated_at': timezone.now().isoformat(),
        'group_summary': {
            'group_revenue': str(group_revenue),
            'group_cogs': str(group_cogs),
            'group_profit': str(group_profit),
            'group_margin_percent': f"{group_margin:.1f}%",
            'group_drawer_cash': str(group_drawer_cash),
            'group_inventory_cost': str(group_inventory_cost),
            'group_inventory_retail': str(group_inventory_retail),
            'total_receivables': str(total_receivables),
            'total_payables': str(total_payables),
            'net_working_capital': str(net_working_capital),
        },
        'units': {
            'bar': {
                'name': 'Bar & Lounge',
                'revenue': str(bar_revenue),
                'share_percent': f"{bar_share:.1f}%",
                'orders_count': bar_orders_count,
                'cogs': str(bar_cogs),
                'gross_profit': str(bar_profit),
                'gross_margin_percent': f"{bar_margin:.1f}%",
                'cash_in_drawer': str(bar_cash),
                'mobile_money': str(bar_momo),
                'inventory_cost': str(bar_stock_cost),
                'inventory_retail': str(bar_stock_retail)
            },
            'stationery': {
                'name': 'Stationery & Retail',
                'revenue': str(stat_revenue),
                'share_percent': f"{stat_share:.1f}%",
                'orders_count': stat_orders_count,
                'cogs': str(stat_cogs),
                'gross_profit': str(stat_profit),
                'gross_margin_percent': f"{stat_margin:.1f}%",
                'cash_in_drawer': str(stat_drawer_cash),
                'cash_sales': str(stat_cash),
                'mobile_money': str(stat_momo),
                'credit_sales': str(stat_credit),
                'debt_recovered': str(stat_debt_rec),
                'receivables_owed': str(total_receivables),
                'payables_owed': str(total_payables),
                'inventory_cost': str(stat_stock_cost),
                'inventory_retail': str(stat_stock_retail)
            }
        }
    })
