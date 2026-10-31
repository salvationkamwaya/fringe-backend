import uuid
from decimal import Decimal
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum, Count, F, Q
from django.http import Http404
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
        if unit_id == 'all':
            unit_id = request.user.business_unit_id or 'bar'
            
        shift = Shift.objects.filter(
            business_unit_id=unit_id,
            cashier=request.user,
            status=Shift.STATUS_OPEN
        ).first()

        if not shift:
            return Response({'active_shift': None})
        return Response({'active_shift': ShiftSerializer(shift).data})

    @action(detail=False, methods=['post'])
    def start_shift(self, request):
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all':
            unit_id = request.user.business_unit_id or 'bar'

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
                'error': f"Payment amount (ZMW {amount}) exceeds current balance due (ZMW {customer.current_balance})."
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
            'message': f"Payment of ZMW {amount} recorded for {customer.name}.",
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
            'message': f"Payment of ZMW {amount} recorded to {supplier.name}.",
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
                            'error': f"Credit limit exceeded! Limit: ZMW {customer.credit_limit}, Current Owed: ZMW {customer.current_balance}. Manager authorization required.",
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

    product_sales = OrderItem.objects.filter(
        order__business_unit_id='bar',
        order__status='COMPLETED'
    ).values(
        'product__id', 'product__name', 'product__is_alcoholic'
    ).annotate(
        total_quantity=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_revenue')

    stock_on_hand = Product.objects.filter(
        business_unit_id='bar',
        is_active=True
    ).values(
        'id', 'name', 'stock_quantity', 'reorder_threshold', 'selling_price', 'cost_price', 'is_alcoholic'
    ).order_by('stock_quantity')

    completed_orders = Order.objects.filter(
        business_unit_id='bar',
        status='COMPLETED'
    )
    total_revenue = completed_orders.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')

    items = OrderItem.objects.filter(order__business_unit_id='bar', order__status='COMPLETED')
    cogs = Decimal('0.00')
    for itm in items:
        cogs += itm.product.cost_price * itm.quantity

    gross_profit = total_revenue - cogs
    gross_margin = ((gross_profit / total_revenue) * 100) if total_revenue > 0 else Decimal('0.00')

    return Response({
        'business_unit': 'bar',
        'product_sales': list(product_sales),
        'stock_on_hand': list(stock_on_hand),
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
    Stationery reports:
    - End-of-Day Report (Cash, Mobile, Credit, Debt Recoveries, Total)
    - Product Sales Breakdown
    - Stock on Hand with Reorder Alerts
    - Debtors Summary
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('stationery', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    # 1. Product Sales
    product_sales = OrderItem.objects.filter(
        order__business_unit_id='stationery',
        order__status='COMPLETED'
    ).values(
        'product__id', 'product__name', 'is_pack_sale'
    ).annotate(
        total_quantity=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_revenue')

    # 2. Stock on Hand
    stock_on_hand = Product.objects.filter(
        business_unit_id='stationery',
        is_active=True
    ).values(
        'id', 'name', 'stock_quantity', 'reorder_threshold', 'selling_price', 'cost_price', 'is_pack', 'pack_name'
    ).order_by('stock_quantity')

    # 3. Financial Totals
    completed = Order.objects.filter(business_unit_id='stationery', status='COMPLETED')
    total_revenue = completed.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')

    payments = Payment.objects.filter(order__business_unit_id='stationery', order__status='COMPLETED')
    cash_sales = payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    mobile_sales = payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    credit_sales = payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    # Recovered customer debts
    debt_recovered = CustomerCreditTransaction.objects.filter(
        business_unit_id='stationery', transaction_type='PAYMENT_RECEIVED'
    ).aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    # Total Debtors Balance
    total_debtors = Customer.objects.filter(business_unit_id='stationery').aggregate(t=Sum('current_balance'))['t'] or Decimal('0.00')

    return Response({
        'business_unit': 'stationery',
        'product_sales': list(product_sales),
        'stock_on_hand': list(stock_on_hand),
        'end_of_day': {
            'total_revenue': str(total_revenue),
            'cash_sales': str(cash_sales),
            'mobile_sales': str(mobile_sales),
            'credit_sales': str(credit_sales),
            'debt_recovered': str(debt_recovered),
            'expected_cash_drawer': str(cash_sales + debt_recovered),
            'total_debtors_balance': str(total_debtors),
        }
    })
