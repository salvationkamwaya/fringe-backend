import uuid
import csv
from decimal import Decimal
from datetime import datetime, date, time, timedelta
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum, Count, F, Q
from django.http import Http404, HttpResponse
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .models import (
    Shift, BarTab, Customer, Order, OrderItem, Payment,
    CustomerCreditTransaction, Supplier, PurchaseOrder, SupplierPayment
)
from .serializers import (
    ShiftSerializer, BarTabSerializer, CustomerSerializer,
    OrderSerializer, CustomerCreditTransactionSerializer,
    SupplierSerializer, PurchaseOrderSerializer, SupplierPaymentSerializer
)
from .pdf_generator import ReportPDFBuilder
from business_catalog.models import Product, Category
from business_core.audit import record_audit
from business_core.models import User
from .stationery_store import (
    list_expenses, add_expense, delete_expense,
    list_quotations, get_quotation, create_quotation, update_quotation_status,
    list_returns, create_return, EXPENSE_CATEGORIES
)


class ShiftViewSet(viewsets.ModelViewSet):
    """
    Shift and Cash Reconciliation management with Z-Report and End-of-Day generation.
    """
    serializer_class = ShiftSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Shift.objects.for_request(self.request)

    def destroy(self, request, *args, **kwargs):
        return Response({
            'error': 'Cash register shifts cannot be deleted for audit and financial integrity.',
            'code': 'SHIFT_DELETE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        return Response({
            'error': 'Shifts cannot be directly updated. Use close_shift to complete drawer reconciliation.',
            'code': 'SHIFT_UPDATE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        return Response({
            'error': 'Shifts cannot be directly updated. Use close_shift to complete drawer reconciliation.',
            'code': 'SHIFT_UPDATE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=False, methods=['get'])
    def current(self, request):
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = request.user.business_unit_id or 'bar'
            
        user = request.user
        is_manager = user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER)
        
        branch_id = getattr(request, 'branch_id', None)
        if not branch_id or branch_id == 'all':
            branch_id = getattr(user, 'branch_id', None)
        if branch_id and str(branch_id).isdigit():
            branch_id = int(branch_id)
        else:
            branch_id = None

        if is_manager:
            shift_filter = {'business_unit_id': unit_id, 'status': Shift.STATUS_OPEN}
            if branch_id:
                shift_filter['branch_id'] = branch_id
            shift = Shift.objects.filter(**shift_filter).first()
            active_shifts = list(Shift.objects.filter(**shift_filter).values(
                'id', 'cashier__username', 'cashier__first_name', 'cashier__last_name',
                'opening_float', 'total_sales_cash', 'total_sales_mobile', 'started_at'
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

        branch_id = getattr(request, 'branch_id', None)
        if not branch_id or branch_id == 'all':
            branch_id = getattr(user, 'branch_id', None)
        if branch_id and str(branch_id).isdigit():
            branch_id = int(branch_id)
        else:
            branch_id = None

        shift = Shift.objects.create(
            business_unit_id=unit_id,
            branch_id=branch_id,
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

    def destroy(self, request, *args, **kwargs):
        return Response({
            'error': 'Bar tabs cannot be hard deleted. Settle or close the tab, or void the attached orders with manager approval.',
            'code': 'TAB_DELETE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def perform_create(self, serializer):
        user = self.request.user
        if not (user.is_owner or user.role in (User.ROLE_BAR_MANAGER, User.ROLE_BARTENDER)):
            self.permission_denied(self.request, message="Only Bar & Grocery staff or Owner can open tabs.")

        branch_id = getattr(self.request, 'branch_id', None)
        if not branch_id or branch_id == 'all':
            branch_id = getattr(user, 'branch_id', None)
        if branch_id and str(branch_id).isdigit():
            branch_id = int(branch_id)
        else:
            branch_id = None

        tab = serializer.save(
            business_unit_id='bar',
            branch_id=branch_id,
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
    def close_tab(self, request, pk=None):
        tab = self.get_object()
        if tab.status == BarTab.STATUS_CLOSED:
            return Response({'error': 'Tab is already closed.'}, status=status.HTTP_400_BAD_REQUEST)

        tab.status = BarTab.STATUS_CLOSED
        tab.closed_by = request.user
        tab.closed_at = timezone.now()
        tab.save()

        record_audit(
            request=request,
            action='TAB_CLOSED',
            entity_type='BarTab',
            entity_id=tab.id,
            metadata={'closed_by': request.user.username}
        )
        return Response({
            'message': f"Tab '{tab.tab_name}' successfully closed.",
            'tab': BarTabSerializer(tab).data
        })

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def settle(self, request, pk=None):
        tab = self.get_object()
        if tab.status == BarTab.STATUS_CLOSED:
            return Response({'error': 'Tab is already closed.'}, status=status.HTTP_400_BAD_REQUEST)

        unvoided_orders = tab.orders.exclude(status=Order.STATUS_VOIDED)
        total_tab_amount = sum(order.final_amount for order in unvoided_orders)

        existing_paid = Payment.objects.filter(order__in=unvoided_orders).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        balance_due = max(Decimal('0.00'), total_tab_amount - existing_paid)

        payments_data = request.data.get('payments', [])
        if not payments_data and balance_due > Decimal('0.00'):
            return Response({'error': 'Payment information is required to settle the tab.'}, status=status.HTTP_400_BAD_REQUEST)

        total_paid_incoming = Decimal('0.00')
        parsed_payments = []
        for p in payments_data:
            method = p.get('payment_method', 'CASH').upper()
            amt = Decimal(str(p.get('amount', '0.00')))
            ref = p.get('reference', '')

            if method not in ('CASH', 'MOBILE_MONEY'):
                return Response({'error': 'Bar tabs accept Cash or Mobile Money only.'}, status=status.HTTP_400_BAD_REQUEST)

            if amt <= Decimal('0.00'):
                return Response({'error': 'Payment amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

            total_paid_incoming += amt
            parsed_payments.append({'method': method, 'amount': amt, 'reference': ref})

        if balance_due > Decimal('0.00') and total_paid_incoming < balance_due:
            return Response({
                'error': f"Total paid (TSH {total_paid_incoming}) is less than tab balance due (TSH {balance_due})."
            }, status=status.HTTP_400_BAD_REQUEST)

        # Attach payments to latest order if orders exist
        target_order = unvoided_orders.last()
        shift = Shift.objects.filter(
            business_unit_id='bar', cashier=request.user, status=Shift.STATUS_OPEN
        ).first()

        if target_order and parsed_payments:
            for p in parsed_payments:
                Payment.objects.create(
                    order=target_order,
                    payment_method=p['method'],
                    amount=p['amount'],
                    reference=p['reference']
                )
                if shift:
                    if p['method'] == 'CASH':
                        shift.total_sales_cash = F('total_sales_cash') + p['amount']
                    elif p['method'] == 'MOBILE_MONEY':
                        shift.total_sales_mobile = F('total_sales_mobile') + p['amount']

            if shift:
                shift.save(update_fields=['total_sales_cash', 'total_sales_mobile'])

        tab.status = BarTab.STATUS_CLOSED
        tab.closed_by = request.user
        tab.closed_at = timezone.now()
        tab.save()

        record_audit(
            request=request,
            action='TAB_SETTLED',
            entity_type='BarTab',
            entity_id=tab.id,
            after_value={'settled_amount': str(total_tab_amount), 'payments': parsed_payments},
            metadata={'settled_by': request.user.username, 'tab_name': tab.tab_name}
        )

        return Response({
            'message': f"Tab '{tab.tab_name}' successfully settled and closed!",
            'tab': BarTabSerializer(tab).data,
            'settled_amount': str(total_tab_amount),
            'payments': parsed_payments
        })

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
        if not (user.is_owner or user.role in (User.ROLE_STATIONERY_MANAGER, User.ROLE_STATIONERY_CASHIER)):
            self.permission_denied(self.request, message="Permission Denied: Only Stationery staff or Business Owner can register customers.")

        customer = serializer.save(business_unit_id='stationery')
        record_audit(
            request=self.request,
            action='CUSTOMER_REGISTERED',
            entity_type='Customer',
            entity_id=customer.id,
            after_value={'name': customer.name, 'credit_limit': str(customer.credit_limit)}
        )

    def perform_update(self, serializer):
        user = self.request.user
        if not (user.is_owner or user.role in (User.ROLE_STATIONERY_MANAGER, User.ROLE_STATIONERY_CASHIER)):
            self.permission_denied(self.request, message="Permission Denied: Only Stationery staff or Business Owner can update customer records.")

        old_instance = self.get_object()
        new_limit = serializer.validated_data.get('credit_limit')
        if new_limit is not None and Decimal(str(new_limit)) != old_instance.credit_limit:
            if not user.is_stationery_manager_or_owner:
                self.permission_denied(self.request, message="Permission Denied: Modifying customer credit limits is strictly restricted to Stationery Managers and Business Owners.")

        customer = serializer.save()
        record_audit(
            request=self.request,
            action='CUSTOMER_UPDATED',
            entity_type='Customer',
            entity_id=customer.id,
            after_value={'name': customer.name, 'credit_limit': str(customer.credit_limit)}
        )

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_stationery_manager_or_owner:
            self.permission_denied(self.request, message="Permission Denied: Only Stationery Managers and Business Owners can delete customer accounts.")

        if instance.current_balance > Decimal('0.00'):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'error': f"Cannot delete customer '{instance.name}' because they have an active outstanding debt of TSH {instance.current_balance}."})

        cust_id = instance.id
        cust_name = instance.name
        instance.delete()
        record_audit(
            request=self.request,
            action='CUSTOMER_DELETED',
            entity_type='Customer',
            entity_id=cust_id,
            metadata={'name': cust_name, 'deleted_by': user.username}
        )

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def record_payment(self, request, pk=None):
        """
        Record a customer debt repayment (Cash, Mobile Money, Bank Transfer).
        Decrements customer.current_balance and updates active shift drawer if cash.
        """
        user = request.user
        if not (user.is_owner or user.role in (User.ROLE_STATIONERY_MANAGER, User.ROLE_STATIONERY_CASHIER)):
            return Response({'error': 'Permission Denied: Only Stationery Cashier, Manager, or Owner can record customer repayments.'}, status=status.HTTP_403_FORBIDDEN)
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
        user = self.request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            self.permission_denied(self.request, message="Permission Denied: Only Managers, Stock Clerks, or Business Owner can register suppliers.")

        unit_id = getattr(self.request, 'business_unit', user.business_unit_id) or 'bar'
        instance = serializer.save(business_unit_id=unit_id)
        record_audit(
            request=self.request,
            action='SUPPLIER_CREATED',
            entity_type='Supplier',
            entity_id=instance.id,
            after_value={'name': instance.name}
        )

    def perform_update(self, serializer):
        user = self.request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            self.permission_denied(self.request, message="Permission Denied: Only Managers, Stock Clerks, or Business Owner can update supplier details.")

        old_name = self.get_object().name
        instance = serializer.save()
        record_audit(
            request=self.request,
            action='SUPPLIER_UPDATED',
            entity_type='Supplier',
            entity_id=instance.id,
            before_value={'name': old_name},
            after_value={'name': instance.name}
        )

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_manager_or_owner:
            self.permission_denied(self.request, message="Permission Denied: Only Managers or Business Owner can delete suppliers.")

        if instance.balance_payable > Decimal('0.00'):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'error': f"Cannot delete supplier '{instance.name}' because there is an outstanding payable of TSH {instance.balance_payable}."})

        sup_id = instance.id
        sup_name = instance.name
        instance.delete()
        record_audit(
            request=self.request,
            action='SUPPLIER_DELETED',
            entity_type='Supplier',
            entity_id=sup_id,
            metadata={'name': sup_name, 'deleted_by': user.username}
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
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            self.permission_denied(self.request, message="Permission Denied: Only Managers, Stock Clerks, or Business Owner can create purchase orders.")

        unit_id = getattr(self.request, 'business_unit', user.business_unit_id) or 'bar'
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

    def perform_update(self, serializer):
        user = self.request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            self.permission_denied(self.request, message="Permission Denied: Only Managers, Stock Clerks, or Business Owner can update purchase orders.")

        instance = serializer.save()
        record_audit(
            request=self.request,
            action='PURCHASE_ORDER_UPDATED',
            entity_type='PurchaseOrder',
            entity_id=instance.id,
            after_value={'status': instance.status}
        )

    def perform_destroy(self, instance):
        user = self.request.user
        if user.role != User.ROLE_OWNER:
            self.permission_denied(self.request, message="Permission Denied: Only the Business Owner can delete purchase orders.")

        if instance.amount_paid > Decimal('0.00'):
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'error': 'Cannot delete a purchase order that has registered payments.'})

        po_id = instance.id
        po_num = instance.order_number
        instance.delete()
        record_audit(
            request=self.request,
            action='PURCHASE_ORDER_DELETED',
            entity_type='PurchaseOrder',
            entity_id=po_id,
            metadata={'order_number': po_num, 'deleted_by': user.username}
        )

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def record_payment(self, request, pk=None):
        """Pay supplier against a purchase order."""
        user = request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            return Response({'error': 'Permission Denied: Only Managers, Stock Clerks, or Owner can record supplier payments.'}, status=status.HTTP_403_FORBIDDEN)

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


class OrderPagination(PageNumberPagination):
    page_size = 15
    page_size_query_param = 'page_size'
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response({
            'count': self.page.paginator.count,
            'total_pages': self.page.paginator.num_pages,
            'current_page': self.page.number,
            'page_size': self.get_page_size(self.request),
            'next': self.get_next_link(),
            'previous': self.get_previous_link(),
            'results': data
        })


class OrderViewSet(viewsets.ModelViewSet):
    """
    POS Order Processing and Sales History Records:
    - Supports Bar and Stationery with strict tenant isolation.
    - Automatic Unit vs Pack conversion for Stationery.
    - Customer Credit authorization for registered stationery customers.
    - Non-bypassable alcohol age check for Bar.
    - Filterable by cashier/bartender, drink/product type, date range, payment method.
    - Paginated with newest transactions first.
    """
    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = OrderPagination

    def get_queryset(self):
        qs = Order.objects.for_request(self.request).select_related(
            'cashier', 'shift', 'tab', 'customer'
        ).prefetch_related('items__product', 'payments').order_by('-created_at')

        user = self.request.user
        params = self.request.query_params

        # Filter by Cashier / Bartender
        my_sales = params.get('my_sales')
        cashier = params.get('cashier') or params.get('cashier_id')
        if user.role in (User.ROLE_BARTENDER, User.ROLE_STATIONERY_CASHIER):
            # Frontline staff can ONLY view sales they personally recorded
            qs = qs.filter(cashier=user)
        elif my_sales in ('true', '1'):
            qs = qs.filter(cashier=user)
        elif cashier:
            if str(cashier).isdigit():
                qs = qs.filter(cashier_id=int(cashier))
            else:
                qs = qs.filter(cashier__username__iexact=str(cashier))

        # Filter by Shift
        shift_id = params.get('shift') or params.get('shift_id')
        if shift_id:
            qs = qs.filter(shift_id=shift_id)

        # Filter by Date Range (today, this_week, this_month, last_month, this_year, last_year, custom)
        range_param = params.get('range')
        start_date_param = params.get('start_date')
        end_date_param = params.get('end_date')
        if range_param or (start_date_param and end_date_param):
            start, end, label, range_type, s_str, e_str = parse_date_range(self.request)
            if start and end and range_type != 'all':
                qs = qs.filter(created_at__range=(start, end))

        # Filter by Drink / Product Category
        category_id = params.get('category') or params.get('category_id')
        if category_id:
            qs = qs.filter(items__product__category_id=category_id)

        # Filter by Specific Product
        product_id = params.get('product') or params.get('product_id')
        if product_id:
            qs = qs.filter(items__product_id=product_id)

        # Filter by Product Name
        product_name = params.get('product_name')
        if product_name:
            qs = qs.filter(items__product_name__icontains=product_name)

        # Filter by Alcoholic Drinks
        is_alcoholic = params.get('is_alcoholic')
        if is_alcoholic in ('true', '1'):
            qs = qs.filter(items__is_alcoholic=True)

        # Filter by Payment Method
        payment_method = params.get('payment_method')
        if payment_method:
            qs = qs.filter(payments__payment_method=payment_method.upper())

        # Filter by Order Status
        status_filter = params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        # General Search (Order #, Customer, Tab, Cashier, Item Name)
        search = params.get('search')
        if search:
            search = search.strip()
            qs = qs.filter(
                Q(order_number__icontains=search) |
                Q(customer__name__icontains=search) |
                Q(tab__tab_name__icontains=search) |
                Q(cashier__username__icontains=search) |
                Q(cashier__first_name__icontains=search) |
                Q(cashier__last_name__icontains=search) |
                Q(items__product_name__icontains=search)
            )

        return qs.distinct()

    def destroy(self, request, *args, **kwargs):
        return Response({
            'error': 'Completed sales and orders are immutable legal records and cannot be deleted. If an order needs cancellation, use manager void authorization.',
            'code': 'ORDER_DELETE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        return Response({
            'error': 'Orders are immutable records and cannot be modified after transaction completion.',
            'code': 'ORDER_UPDATE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        return Response({
            'error': 'Orders are immutable records and cannot be modified after transaction completion.',
            'code': 'ORDER_UPDATE_FORBIDDEN'
        }, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        # Summary calculations across the entire filtered set (not just current page)
        completed_qs = queryset.filter(status=Order.STATUS_COMPLETED)
        aggregates = completed_qs.aggregate(
            total_sales=Sum('final_amount'),
            total_orders=Count('id', distinct=True)
        )

        cash_total = Payment.objects.filter(
            order__in=completed_qs, payment_method=Payment.METHOD_CASH
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        mobile_total = Payment.objects.filter(
            order__in=completed_qs, payment_method=Payment.METHOD_MOBILE_MONEY
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        credit_total = Payment.objects.filter(
            order__in=completed_qs, payment_method=Payment.METHOD_CREDIT
        ).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')

        summary = {
            'total_sales': str(aggregates['total_sales'] or Decimal('0.00')),
            'total_orders': aggregates['total_orders'] or 0,
            'cash_total': str(cash_total),
            'mobile_total': str(mobile_total),
            'credit_total': str(credit_total),
            'currency': 'TSH'
        }

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response = self.get_paginated_response(serializer.data)
            response.data['summary'] = summary
            return response

        serializer = self.get_serializer(queryset, many=True)
        return Response({'results': serializer.data, 'summary': summary})

    @action(detail=False, methods=['get'])
    def filter_options(self, request):
        """
        Returns dynamic dropdown lists for sales record filtering:
        - Staff / Cashiers / Bartenders who have recorded sales
        - Categories (Drink classes / Stationery types)
        """
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = request.user.business_unit_id or 'bar'

        if request.user.role in (User.ROLE_BARTENDER, User.ROLE_STATIONERY_CASHIER):
            cashiers = User.objects.filter(id=request.user.id).values('id', 'username', 'first_name', 'last_name', 'role')
        else:
            cashiers = User.objects.filter(
                orders__business_unit_id=unit_id
            ).distinct().values('id', 'username', 'first_name', 'last_name', 'role')

        from business_catalog.models import Category
        categories = Category.objects.filter(
            business_unit_id=unit_id
        ).values('id', 'name', 'slug')

        return Response({
            'cashiers': list(cashiers),
            'categories': list(categories),
            'current_user_id': request.user.id,
            'current_username': request.user.username,
            'can_record_sales': request.user.can_record_sales
        })

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
        order_type = request.data.get('order_type', 'RETAIL').upper()
        quotation_id = request.data.get('quotation_id')

        if not items_data:
            return Response({'error': 'Order must contain at least one item.'}, status=status.HTTP_400_BAD_REQUEST)

        # In Wholesale mode, customer is required (shops, schools, offices)
        if unit_id == 'stationery' and order_type == 'WHOLESALE' and not customer_id:
            return Response({
                'error': 'Wholesale orders require selecting a registered customer (school, office, or retail shop).',
                'code': 'WHOLESALE_CUSTOMER_REQUIRED'
            }, status=status.HTTP_400_BAD_REQUEST)

        has_alcoholic_item = False
        parsed_items = []
        subtotal = Decimal('0.00')

        # 1. PARSE ITEMS & HANDLE UNIT VS PACK & WHOLESALE PRICING
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
            if order_type == 'WHOLESALE':
                if item_data.get('unit_price'):
                    line_price = Decimal(str(item_data['unit_price']))
                elif product.is_pack and not is_pack_sale and product.allow_split_sale:
                    base_split = product.split_unit_price or (product.selling_price / Decimal(product.pack_unit_ratio))
                    line_price = (base_split * Decimal('0.85')).quantize(Decimal('0.01'))
                else:
                    line_price = (product.selling_price * Decimal('0.85')).quantize(Decimal('0.01'))

                if product.is_pack and not is_pack_sale and product.allow_split_sale:
                    stock_deduction = qty / Decimal(product.pack_unit_ratio)
                else:
                    stock_deduction = qty
            elif product.is_pack and not is_pack_sale and product.allow_split_sale:
                # Sold as individual units/pieces (Retail)
                line_price = product.split_unit_price or (product.selling_price / Decimal(product.pack_unit_ratio))
                stock_deduction = qty / Decimal(product.pack_unit_ratio)
            else:
                # Sold as full pack or standard product (Retail)
                line_price = product.selling_price
                stock_deduction = qty

            # F4 Out-of-stock blockage check
            if product.stock_quantity < stock_deduction and not request.data.get('allow_negative_stock', False):
                return Response({
                    'error': f"Product '{product.name}' is out of stock (Available: {product.stock_quantity}, Requested: {stock_deduction}).",
                    'code': 'OUT_OF_STOCK'
                }, status=status.HTTP_400_BAD_REQUEST)

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

        # 5. CREATE ORDER (With Retail vs Wholesale identifier and Branch code)
        branch_id = getattr(request, 'branch_id', None)
        if not branch_id or branch_id == 'all':
            branch_id = getattr(user, 'branch_id', None)
        if branch_id and str(branch_id).isdigit():
            branch_id = int(branch_id)
        else:
            branch_id = None

        branch_code = None
        if branch_id:
            from business_core.models import Branch
            b = Branch.objects.filter(id=branch_id).first()
            if b:
                branch_code = b.code

        if branch_code:
            prefix = branch_code.upper()
        else:
            prefix = 'BAR' if unit_id == 'bar' else ('STN-WS' if order_type == 'WHOLESALE' else 'STN')
        order_number = f"{prefix}-{timezone.now().strftime('%y%m%d%H%M')}-{uuid.uuid4().hex[:4].upper()}"

        order = Order.objects.create(
            business_unit_id=unit_id,
            branch_id=branch_id,
            order_number=order_number,
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

        if quotation_id and unit_id == 'stationery':
            update_quotation_status(quotation_id, 'CONVERTED', converted_order_number=order.order_number, unit_id='stationery')

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


def parse_date_range(request):
    """
    Parses 'range', 'start_date', 'end_date' query parameters.
    Returns:
        (start_datetime, end_datetime, range_label, range_type, start_date_str, end_date_str)
    """
    range_type = request.query_params.get('range', 'all').lower()
    start_str = request.query_params.get('start_date')
    end_str = request.query_params.get('end_date')
    now = timezone.now()
    today = now.date()

    if range_type == 'today':
        start = timezone.make_aware(datetime.combine(today, time.min))
        end = timezone.make_aware(datetime.combine(today, time.max))
        label = f"Today ({today.strftime('%d %b %Y')})"
        s_str, e_str = today.isoformat(), today.isoformat()
    elif range_type == 'this_week':
        monday = today - timedelta(days=today.weekday())
        start = timezone.make_aware(datetime.combine(monday, time.min))
        end = now
        label = f"This Week ({monday.strftime('%d %b')} - {today.strftime('%d %b %Y')})"
        s_str, e_str = monday.isoformat(), today.isoformat()
    elif range_type == 'this_month':
        first_day = today.replace(day=1)
        start = timezone.make_aware(datetime.combine(first_day, time.min))
        end = now
        label = f"This Month ({first_day.strftime('%B %Y')})"
        s_str, e_str = first_day.isoformat(), today.isoformat()
    elif range_type == 'last_month':
        first_of_this_month = today.replace(day=1)
        last_day_last_month = first_of_this_month - timedelta(days=1)
        first_day_last_month = last_day_last_month.replace(day=1)
        start = timezone.make_aware(datetime.combine(first_day_last_month, time.min))
        end = timezone.make_aware(datetime.combine(last_day_last_month, time.max))
        label = f"Last Month ({first_day_last_month.strftime('%B %Y')})"
        s_str, e_str = first_day_last_month.isoformat(), last_day_last_month.isoformat()
    elif range_type == 'this_year':
        first_day = today.replace(month=1, day=1)
        start = timezone.make_aware(datetime.combine(first_day, time.min))
        end = now
        label = f"This Year ({today.year})"
        s_str, e_str = first_day.isoformat(), today.isoformat()
    elif range_type == 'last_year':
        last_year = today.year - 1
        first_day = date(last_year, 1, 1)
        last_day = date(last_year, 12, 31)
        start = timezone.make_aware(datetime.combine(first_day, time.min))
        end = timezone.make_aware(datetime.combine(last_day, time.max))
        label = f"Last Year ({last_year})"
        s_str, e_str = first_day.isoformat(), last_day.isoformat()
    elif range_type == 'custom' or (start_str and end_str):
        range_type = 'custom'
        try:
            s_date = datetime.strptime(start_str, '%Y-%m-%d').date() if start_str else today
            e_date = datetime.strptime(end_str, '%Y-%m-%d').date() if end_str else today
            if s_date > e_date:
                s_date, e_date = e_date, s_date
            start = timezone.make_aware(datetime.combine(s_date, time.min))
            end = timezone.make_aware(datetime.combine(e_date, time.max))
            label = f"Custom ({s_date.strftime('%d %b %Y')} to {e_date.strftime('%d %b %Y')})"
            s_str, e_str = s_date.isoformat(), e_date.isoformat()
        except ValueError:
            start, end, label, range_type, s_str, e_str = None, None, "All Time", "all", None, None
    else:
        start, end, label, range_type, s_str, e_str = None, None, "All Time", "all", None, None

    return start, end, label, range_type, s_str, e_str


def compute_inventory_valuation(products_qs):
    """
    Accurately computes Business Stock Worth across all 4 product situations:
    1. Single-Item Only (is_pack=False)
    2. Pack-Only (is_pack=True, allow_split_sale=False)
    3. Dual Pack + Split Singles (is_pack=True, allow_split_sale=True)
    4. Partially opened packs (whole packs at pack price + loose pieces at single price)
    Returns (valuation_dict, stock_on_hand_list).
    """
    val_at_cost = Decimal('0.00')
    val_at_standard = Decimal('0.00')
    val_at_max_split = Decimal('0.00')
    total_stock_units = Decimal('0.00')
    low_stock_count = 0
    out_of_stock_count = 0

    pack_count = 0
    pack_total_packs = Decimal('0.00')
    pack_total_pieces = Decimal('0.00')
    pack_buying = Decimal('0.00')
    pack_selling_std = Decimal('0.00')
    pack_selling_max = Decimal('0.00')

    single_count = 0
    single_total_units = Decimal('0.00')
    single_buying = Decimal('0.00')
    single_selling = Decimal('0.00')

    stock_on_hand = []

    for prod in products_qs:
        raw_qty = prod.stock_quantity or Decimal('0.00')
        qty = max(Decimal('0.00'), raw_qty)
        cost_price = prod.cost_price or Decimal('0.00')
        selling_price = prod.selling_price or Decimal('0.00')
        reorder = prod.reorder_threshold or Decimal('0.00')

        is_out = raw_qty <= 0
        is_low = (not is_out) and (raw_qty <= reorder)
        if is_out:
            out_of_stock_count += 1
        elif is_low:
            low_stock_count += 1

        item_cost = qty * cost_price
        total_stock_units += qty

        if prod.is_pack:
            ratio = max(1, int(prod.pack_unit_ratio or 1))
            ratio_dec = Decimal(ratio)
            pieces = qty * ratio_dec
            if prod.allow_split_sale:
                product_type_code = 'dual_split'
                split_price = (
                    prod.split_unit_price
                    if (prod.split_unit_price and prod.split_unit_price > 0)
                    else (selling_price / ratio_dec)
                )
                whole_packs = int(qty)
                loose_pieces = (qty - Decimal(whole_packs)) * ratio_dec
                item_std_sell = (Decimal(whole_packs) * selling_price) + (loose_pieces * split_price)
                item_max_sell = pieces * split_price
            else:
                product_type_code = 'pack_only'
                item_std_sell = qty * selling_price
                item_max_sell = item_std_sell

            pack_count += 1
            pack_total_packs += qty
            pack_total_pieces += pieces
            pack_buying += item_cost
            pack_selling_std += item_std_sell
            pack_selling_max += item_max_sell
        else:
            product_type_code = 'single_only'
            item_std_sell = qty * selling_price
            item_max_sell = item_std_sell

            single_count += 1
            single_total_units += qty
            single_buying += item_cost
            single_selling += item_std_sell

        val_at_cost += item_cost
        val_at_standard += item_std_sell
        val_at_max_split += item_max_sell

        stock_on_hand.append({
            'id': prod.id,
            'name': prod.name,
            'branch_id': prod.branch_id,
            'branch_name': getattr(prod.branch, 'name', None) if prod.branch_id else None,
            'stock_quantity': str(raw_qty),
            'reorder_threshold': str(reorder),
            'cost_price': str(cost_price),
            'selling_price': str(selling_price),
            'split_unit_price': str(prod.split_unit_price) if prod.split_unit_price else None,
            'pack_unit_ratio': int(prod.pack_unit_ratio or 1),
            'is_pack': bool(prod.is_pack),
            'allow_split_sale': bool(prod.allow_split_sale),
            'pack_name': prod.pack_name or 'Pack',
            'unit_name': prod.unit_name or 'Piece',
            'is_alcoholic': bool(prod.is_alcoholic),
            'product_type_code': product_type_code,
            'valuation_cost': str(item_cost.quantize(Decimal('0.01'))),
            'valuation_retail': str(item_std_sell.quantize(Decimal('0.01'))),
            'valuation_max_split': str(item_max_sell.quantize(Decimal('0.01'))),
            'status': 'Out of Stock' if is_out else ('Low Stock' if is_low else 'Optimal')
        })

    stock_on_hand.sort(key=lambda x: Decimal(x['stock_quantity']))

    std_profit = val_at_standard - val_at_cost
    max_profit = val_at_max_split - val_at_cost
    split_upside = max(Decimal('0.00'), val_at_max_split - val_at_standard)
    std_margin = ((std_profit / val_at_standard) * 100) if val_at_standard > 0 else Decimal('0.00')
    max_margin = ((max_profit / val_at_max_split) * 100) if val_at_max_split > 0 else Decimal('0.00')

    pack_profit_std = pack_selling_std - pack_buying
    pack_profit_max = pack_selling_max - pack_buying
    single_profit = single_selling - single_buying
    single_margin = ((single_profit / single_selling) * 100) if single_selling > 0 else Decimal('0.00')

    valuation = {
        'total_items': pack_count + single_count,
        'total_units': int(total_stock_units),
        'total_stock_units': str(total_stock_units.quantize(Decimal('0.01'))),
        'valuation_at_cost': str(val_at_cost.quantize(Decimal('0.01'))),
        'valuation_at_retail': str(val_at_standard.quantize(Decimal('0.01'))),
        'valuation_at_max_split': str(val_at_max_split.quantize(Decimal('0.01'))),
        'potential_gross_profit': str(std_profit.quantize(Decimal('0.01'))),
        'potential_max_profit': str(max_profit.quantize(Decimal('0.01'))),
        'split_upside': str(split_upside.quantize(Decimal('0.01'))),
        'expected_margin_percent': f"{std_margin:.1f}%",
        'max_margin_percent': f"{max_margin:.1f}%",
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'pack_products': {
            'count': pack_count,
            'total_packs': str(pack_total_packs.quantize(Decimal('0.01'))),
            'total_pieces': str(pack_total_pieces.quantize(Decimal('0.01'))),
            'buying_worth': str(pack_buying.quantize(Decimal('0.01'))),
            'selling_standard': str(pack_selling_std.quantize(Decimal('0.01'))),
            'selling_max_split': str(pack_selling_max.quantize(Decimal('0.01'))),
            'profit_standard': str(pack_profit_std.quantize(Decimal('0.01'))),
            'profit_max_split': str(pack_profit_max.quantize(Decimal('0.01'))),
            'split_upside': str(max(Decimal('0.00'), pack_selling_max - pack_selling_std).quantize(Decimal('0.01'))),
        },
        'single_products': {
            'count': single_count,
            'total_units': str(single_total_units.quantize(Decimal('0.01'))),
            'buying_worth': str(single_buying.quantize(Decimal('0.01'))),
            'selling_worth': str(single_selling.quantize(Decimal('0.01'))),
            'expected_profit': str(single_profit.quantize(Decimal('0.01'))),
            'margin_percent': f"{single_margin:.1f}%",
        },
    }
    return valuation, stock_on_hand


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def bar_reports_view(request):
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('bar', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    start_dt, end_dt, range_label, range_type, s_str, e_str = parse_date_range(request)

    branch_id = getattr(request, 'branch_id', None)
    if not branch_id or branch_id == 'all':
        if request.user.role != User.ROLE_OWNER:
            branch_id = request.user.branch_id
    if branch_id and str(branch_id).isdigit():
        branch_id = int(branch_id)
    else:
        branch_id = None

    completed_orders = Order.objects.filter(business_unit_id='bar', status=Order.STATUS_COMPLETED)
    if branch_id:
        completed_orders = completed_orders.filter(branch_id=branch_id)
    if start_dt and end_dt:
        completed_orders = completed_orders.filter(created_at__range=(start_dt, end_dt))

    total_revenue = completed_orders.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')
    total_orders = completed_orders.count()
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else Decimal('0.00')

    # Payments breakdown
    payments = Payment.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    if branch_id:
        payments = payments.filter(order__branch_id=branch_id)
    if start_dt and end_dt:
        payments = payments.filter(order__created_at__range=(start_dt, end_dt))

    cash_payments = payments.filter(payment_method='CASH')
    momo_payments = payments.filter(payment_method='MOBILE_MONEY')
    cash_total = cash_payments.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    cash_count = cash_payments.count()
    momo_total = momo_payments.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    momo_count = momo_payments.count()

    # Product Sales breakdown
    items = OrderItem.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    if branch_id:
        items = items.filter(order__branch_id=branch_id)
    if start_dt and end_dt:
        items = items.filter(order__created_at__range=(start_dt, end_dt))
    items = items.select_related('product')

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

    # Bar-only Refunds & Operating Expenses (strictly scoped to Bar & active branch)
    bar_returns_list = list_returns(unit_id='bar', start_date=s_str, end_date=e_str, branch_id=branch_id)
    bar_total_refunds = sum(Decimal(str(r.get('total_refund', 0))) for r in bar_returns_list)
    bar_cash_refunds = sum(
        Decimal(str(r.get('total_refund', 0)))
        for r in bar_returns_list
        if str(r.get('refund_method', 'CASH')).upper() == 'CASH'
    )
    bar_expenses_list = list_expenses(unit_id='bar', start_date=s_str, end_date=e_str, branch_id=branch_id)
    bar_total_expenses = sum(Decimal(str(e.get('amount', 0))) for e in bar_expenses_list)

    actual_revenue = total_revenue - bar_total_refunds
    gross_profit = actual_revenue - cogs
    gross_margin = ((gross_profit / actual_revenue) * 100) if actual_revenue > 0 else Decimal('0.00')
    bar_net_profit = gross_profit - bar_total_expenses
    bar_net_margin = ((bar_net_profit / actual_revenue) * 100) if actual_revenue > 0 else Decimal('0.00')
    actual_net_cash = actual_revenue - bar_total_expenses

    # Inventory Valuation (separated Pack/Bulk vs Single-Item Only)
    bar_products = Product.objects.filter(business_unit_id='bar', is_active=True).select_related('branch')
    if branch_id:
        bar_products = bar_products.filter(branch_id=branch_id)
    bar_valuation, stock_on_hand = compute_inventory_valuation(bar_products)
    val_at_cost = Decimal(bar_valuation['valuation_at_cost'])
    val_at_retail = Decimal(bar_valuation['valuation_at_retail'])
    total_stock_units = Decimal(bar_valuation['total_stock_units'])
    low_stock_count = bar_valuation['low_stock_count']
    out_of_stock_count = bar_valuation['out_of_stock_count']

    # Recent Shifts
    shifts_query = Shift.objects.filter(business_unit_id='bar')
    if branch_id:
        shifts_query = shifts_query.filter(branch_id=branch_id)
    if start_dt and end_dt:
        shifts_query = shifts_query.filter(started_at__range=(start_dt, end_dt))
    recent_shifts = []
    for s in shifts_query.select_related('cashier').order_by('-started_at')[:10]:
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

    # PDF Download Check
    if request.query_params.get('export') == 'pdf':
        pdf = ReportPDFBuilder(
            title="Bar Management & Financial Report",
            subtitle="The Fringe Bar & Lounge",
            date_range_label=range_label,
            business_unit="Bar & Lounge"
        )
        
        pdf.add_kpi_grid([
            {'label': 'Total Revenue', 'value': f"TSH {total_revenue:,.2f}", 'sub': f"{total_orders} completed orders", 'color': (0.85, 0.55, 0.05)},
            {'label': 'Cost of Goods (COGS)', 'value': f"TSH {cogs:,.2f}", 'sub': 'Drink purchase costs', 'color': (0.4, 0.45, 0.55)},
            {'label': 'Gross Profit', 'value': f"TSH {gross_profit:,.2f}", 'sub': f"Margin: {gross_margin:.1f}%", 'color': (0.08, 0.6, 0.35)},
            {'label': 'Drinks Sold', 'value': f"{int(total_drinks_sold)} units", 'sub': f"Avg: TSH {avg_order_value:,.2f}", 'color': (0.1, 0.4, 0.8)},
        ])

        pdf.add_section_header("Payment Tenders & Category Breakdown")
        tender_rows = [
            ['Cash Collections', f"{cash_count} transactions", f"TSH {cash_total:,.2f}", f"{(cash_total/total_revenue*100):.1f}%" if total_revenue > 0 else '0%'],
            ['Mobile Money (M-Pesa / Airtel)', f"{momo_count} transactions", f"TSH {momo_total:,.2f}", f"{(momo_total/total_revenue*100):.1f}%" if total_revenue > 0 else '0%'],
            ['Alcoholic Beverages (18+ Statutory)', f"{int(alcoholic_count)} drinks", f"TSH {alcoholic_revenue:,.2f}", f"{(alcoholic_revenue/total_revenue*100):.1f}%" if total_revenue > 0 else '0%'],
            ['Non-Alcoholic Soft Drinks', f"{int(non_alcoholic_count)} drinks", f"TSH {non_alcoholic_revenue:,.2f}", f"{(non_alcoholic_revenue/total_revenue*100):.1f}%" if total_revenue > 0 else '0%'],
        ]
        pdf.add_table(
            headers=['Tender / Product Category', 'Volume / Transactions', 'Amount (TSH)', 'Share %'],
            rows=tender_rows,
            col_widths=[215, 120, 110, 70],
            alignments=['L', 'L', 'R', 'R']
        )

        pdf.add_section_header("Product Sales Performance Breakdown")
        prod_rows = []
        for p in product_sales:
            prod_rows.append([
                p['product__name'],
                '18+ Alcohol' if p['product__is_alcoholic'] else 'Soft Drink',
                str(p['total_quantity']),
                f"TSH {Decimal(p['unit_price']):,.2f}",
                f"TSH {Decimal(p['total_revenue']):,.2f}",
                f"TSH {Decimal(p['line_profit']):,.2f}",
                p['margin_percent']
            ])
        pdf.add_table(
            headers=['Product Name', 'Classification', 'Units', 'Price', 'Revenue', 'Profit', 'Margin'],
            rows=prod_rows,
            col_widths=[145, 75, 45, 65, 70, 65, 50],
            alignments=['L', 'L', 'R', 'R', 'R', 'R', 'R']
        )

        pdf.add_section_header("Stock Valuation & Inventory Status")
        stock_rows = []
        for s in stock_on_hand:
            stock_rows.append([
                s['name'],
                str(Decimal(s['stock_quantity'])),
                str(Decimal(s['reorder_threshold'])),
                f"TSH {Decimal(s['cost_price']):,.2f}",
                f"TSH {Decimal(s['selling_price']):,.2f}",
                f"TSH {Decimal(s['valuation_cost']):,.2f}",
                s['status']
            ])
        pdf.add_table(
            headers=['Product Name', 'Stock Qty', 'Reorder', 'Cost Price', 'Retail Price', 'Value at Cost', 'Status'],
            rows=stock_rows,
            col_widths=[150, 55, 50, 65, 65, 75, 55],
            alignments=['L', 'R', 'R', 'R', 'R', 'R', 'L']
        )

        pdf_bytes = pdf.build()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f"bar_report_{range_type}_{timezone.now().strftime('%Y%m%d_%H%M')}.pdf"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # CSV Download Check
    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="bar_comprehensive_report_{range_type}_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)
        
        writer.writerow(['THE FRINGE BAR & LOUNGE - COMPREHENSIVE BUSINESS REPORT'])
        writer.writerow(['Report Period', range_label, 'Currency', 'TSH'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S')])
        writer.writerow([])

        writer.writerow(['EXECUTIVE FINANCIAL SUMMARY'])
        writer.writerow(['Report Period', range_label])
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
        'date_range': {
            'range_type': range_type,
            'label': range_label,
            'start_date': s_str,
            'end_date': e_str,
        },
        'summary': {
            'total_revenue': str(total_revenue),
            'total_orders': total_orders,
            'avg_order_value': f"{avg_order_value:.2f}",
            'total_drinks_sold': int(total_drinks_sold),
            'cogs': str(cogs),
            'gross_profit': str(gross_profit),
            'gross_margin_percent': f"{gross_margin:.1f}%",
            'total_refunds': str(bar_total_refunds),
            'actual_revenue': str(actual_revenue),
            'total_expenses': str(bar_total_expenses),
            'total_liabilities': str(bar_total_refunds + bar_total_expenses),
            'actual_net_cash': str(actual_net_cash),
            'net_profit': str(bar_net_profit),
            'net_margin_percent': f"{bar_net_margin:.1f}%",
            'expected_cash_drawer': str(cash_total - bar_cash_refunds),
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
        'inventory_valuation': bar_valuation,
        'product_sales': product_sales,
        'stock_on_hand': stock_on_hand,
        'recent_shifts': recent_shifts,
        'branch_breakdown': [
            {
                'id': b.id,
                'name': b.name,
                'code': b.code,
                'address': b.address,
                'phone': b.phone,
                'total_revenue': str(
                    Order.objects.filter(
                        business_unit_id='bar', branch_id=b.id, status=Order.STATUS_COMPLETED
                    ).filter(
                        **({'created_at__range': (start_dt, end_dt)} if start_dt and end_dt else {})
                    ).aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
                ),
                'total_orders': Order.objects.filter(
                    business_unit_id='bar', branch_id=b.id, status=Order.STATUS_COMPLETED
                ).filter(
                    **({'created_at__range': (start_dt, end_dt)} if start_dt and end_dt else {})
                ).count(),
            }
            for b in __import__('business_core.models', fromlist=['Branch']).Branch.objects.filter(
                business_unit_id='bar', is_active=True
            )
        ] if request.user.role == User.ROLE_OWNER else [],
        'expenses': {
            'total_expenses': str(bar_total_expenses),
            'count': len(bar_expenses_list),
            'categories': {
                cat: str(sum(Decimal(str(e.get('amount', 0))) for e in bar_expenses_list if e.get('category') == cat))
                for cat in EXPENSE_CATEGORIES if any(e.get('category') == cat for e in bar_expenses_list)
            },
            'recent': bar_expenses_list[:25]
        },
        'profit_snapshot': {
            'total_revenue': str(total_revenue),
            'cogs': str(cogs),
            'gross_profit': str(gross_profit),
            'gross_margin_percent': f"{gross_margin:.1f}%",
            'total_expenses': str(bar_total_expenses),
            'net_profit': str(bar_net_profit),
            'net_margin_percent': f"{bar_net_margin:.1f}%",
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

    start_dt, end_dt, range_label, range_type, s_str, e_str = parse_date_range(request)

    branch_id = getattr(request, 'branch_id', None)
    if unit_id == 'all':
        branch_id = None
    elif not branch_id or branch_id == 'all':
        if request.user.role != User.ROLE_OWNER:
            branch_id = request.user.branch_id
        else:
            branch_id = None
    if branch_id and str(branch_id).isdigit():
        branch_id = int(branch_id)
    else:
        branch_id = None

    completed = Order.objects.filter(business_unit_id='stationery', status=Order.STATUS_COMPLETED)
    if branch_id:
        completed = completed.filter(branch_id=branch_id)
    if start_dt and end_dt:
        completed = completed.filter(created_at__range=(start_dt, end_dt))

    total_revenue = completed.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')
    total_orders = completed.count()
    avg_order_value = (total_revenue / total_orders) if total_orders > 0 else Decimal('0.00')

    payments = Payment.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED)
    if branch_id:
        payments = payments.filter(order__branch_id=branch_id)
    if start_dt and end_dt:
        payments = payments.filter(order__created_at__range=(start_dt, end_dt))

    cash_sales = payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    cash_count = payments.filter(payment_method='CASH').count()
    mobile_sales = payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    mobile_count = payments.filter(payment_method='MOBILE_MONEY').count()
    credit_sales = payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    credit_count = payments.filter(payment_method='CREDIT').count()

    # Recovered customer debts
    debt_rec_query = CustomerCreditTransaction.objects.filter(
        business_unit_id='stationery', transaction_type='PAYMENT_RECEIVED'
    )
    if start_dt and end_dt:
        debt_rec_query = debt_rec_query.filter(created_at__range=(start_dt, end_dt))
    debt_recovered = debt_rec_query.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    # Pack vs Single split & COGS
    items = OrderItem.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED)
    if branch_id:
        items = items.filter(order__branch_id=branch_id)
    if start_dt and end_dt:
        items = items.filter(order__created_at__range=(start_dt, end_dt))
    items = items.select_related('product')

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

    # Stationery-only Refunds & Operating Expenses (strictly scoped to Stationery & active branch)
    returns_list = list_returns(unit_id='stationery', start_date=s_str, end_date=e_str, branch_id=branch_id)
    total_refunds = sum(Decimal(str(r.get('total_refund', 0))) for r in returns_list)
    cash_refunds = sum(
        Decimal(str(r.get('total_refund', 0)))
        for r in returns_list
        if str(r.get('refund_method', 'CASH')).upper() == 'CASH'
    )
    expenses_list = list_expenses(unit_id='stationery', start_date=s_str, end_date=e_str, branch_id=branch_id)
    total_expenses = sum(Decimal(str(e.get('amount', 0))) for e in expenses_list)

    actual_revenue = total_revenue - total_refunds
    gross_profit = actual_revenue - cogs
    gross_margin = ((gross_profit / actual_revenue) * 100) if actual_revenue > 0 else Decimal('0.00')
    net_profit = gross_profit - total_expenses
    net_margin = ((net_profit / actual_revenue) * 100) if actual_revenue > 0 else Decimal('0.00')
    actual_net_cash = actual_revenue - total_expenses

    # Retail vs Wholesale Breakdown (F1.2 & F4 & F5)
    wholesale_orders = completed.filter(order_number__contains='-WS-')
    retail_orders = completed.exclude(order_number__contains='-WS-')
    wholesale_rev = wholesale_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
    retail_rev = retail_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')

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

    # Inventory Valuation (scoped to active branch, or all branches in Combined view)
    stat_products = Product.objects.filter(business_unit_id='stationery', is_active=True).select_related('branch')
    if branch_id:
        stat_products = stat_products.filter(branch_id=branch_id)
    stat_valuation, stock_on_hand = compute_inventory_valuation(stat_products)
    val_at_cost = Decimal(stat_valuation['valuation_at_cost'])
    val_at_retail = Decimal(stat_valuation['valuation_at_retail'])
    total_stock_units = Decimal(stat_valuation['total_stock_units'])
    low_stock_count = stat_valuation['low_stock_count']
    out_of_stock_count = stat_valuation['out_of_stock_count']

    # PDF Download Check
    if request.query_params.get('export') == 'pdf':
        pdf = ReportPDFBuilder(
            title="Stationery & Retail Business Report",
            subtitle="The Fringe Stationery & Retail",
            date_range_label=range_label,
            business_unit="Stationery & Retail"
        )
        
        pdf.add_kpi_grid([
            {'label': 'Total Revenue', 'value': f"TSH {total_revenue:,.2f}", 'sub': f"{total_orders} completed orders", 'color': (0.1, 0.5, 0.7)},
            {'label': 'Expected Drawer Cash', 'value': f"TSH {(cash_sales + debt_recovered):,.2f}", 'sub': 'Cash + Recovered Debts', 'color': (0.1, 0.65, 0.35)},
            {'label': 'Gross Profit', 'value': f"TSH {gross_profit:,.2f}", 'sub': f"Margin: {gross_margin:.1f}%", 'color': (0.05, 0.6, 0.3)},
            {'label': 'Customer Debtors', 'value': f"TSH {total_debtors:,.2f}", 'sub': f"{active_debtors_count} active accounts", 'color': (0.8, 0.3, 0.2)},
        ])

        pdf.add_section_header("Financial Breakdown & Pack vs Single Split")
        summary_rows = [
            ['Cash Sales', f"TSH {cash_sales:,.2f}", 'Whole Pack Sales Revenue', f"TSH {pack_sales_rev:,.2f} ({int(pack_sales_count)} packs)"],
            ['Mobile Money Sales', f"TSH {mobile_sales:,.2f}", 'Single Split Sales Revenue', f"TSH {single_sales_rev:,.2f} ({int(single_sales_count)} pieces)"],
            ['Customer Credit Sales', f"TSH {credit_sales:,.2f}", 'Cost of Goods Sold (COGS)', f"TSH {cogs:,.2f}"],
            ['Customer Debt Recoveries', f"TSH {debt_recovered:,.2f}", 'Gross Margin Percentage', f"{gross_margin:.1f}%"],
        ]
        pdf.add_table(
            headers=['Payment Tender', 'Amount (TSH)', 'Product Split / Metric', 'Value / Details'],
            rows=summary_rows,
            col_widths=[130, 115, 140, 130],
            alignments=['L', 'R', 'L', 'R']
        )

        pdf.add_section_header("Debtors Aging Breakdown (Customer Receivables)")
        aging_rows = [
            ['Current (0 - 30 days)', f"TSH {aging_0_30:,.2f}", 'Standard credit window'],
            ['Overdue (31 - 60 days)', f"TSH {aging_31_60:,.2f}", 'Payment follow-up required'],
            ['Overdue (61 - 90 days)', f"TSH {aging_61_90:,.2f}", 'Grace period expired'],
            ['Critical Overdue (90+ days)', f"TSH {aging_90_plus:,.2f}", 'Urgent legal/management recovery'],
        ]
        pdf.add_table(
            headers=['Aging Category', 'Outstanding Balance (TSH)', 'Recovery Status'],
            rows=aging_rows,
            col_widths=[180, 155, 180],
            alignments=['L', 'R', 'L']
        )

        if top_debtors:
            pdf.add_section_header("Top Customer Debt Accounts")
            deb_rows = []
            for d in top_debtors:
                deb_rows.append([
                    d['name'],
                    d['phone'] or 'N/A',
                    f"TSH {Decimal(d['credit_limit']):,.2f}",
                    f"TSH {Decimal(d['available_credit']):,.2f}",
                    f"TSH {Decimal(d['current_balance']):,.2f}"
                ])
            pdf.add_table(
                headers=['Customer / School Name', 'Contact Phone', 'Credit Limit', 'Available', 'Owed Balance'],
                rows=deb_rows,
                col_widths=[165, 85, 90, 85, 90],
                alignments=['L', 'L', 'R', 'R', 'R']
            )

        pdf.add_section_header("Product Sales Performance Breakdown")
        prod_rows = []
        for p in product_sales:
            prod_rows.append([
                p['product__name'],
                p['sale_type'],
                str(p['total_quantity']),
                f"TSH {Decimal(p['total_revenue']):,.2f}",
                f"TSH {Decimal(p['total_cogs']):,.2f}",
                f"TSH {Decimal(p['line_profit']):,.2f}",
                p['margin_percent']
            ])
        pdf.add_table(
            headers=['Product Name', 'Sale Mode', 'Units', 'Revenue', 'COGS', 'Profit', 'Margin'],
            rows=prod_rows,
            col_widths=[165, 80, 45, 65, 60, 55, 45],
            alignments=['L', 'L', 'R', 'R', 'R', 'R', 'R']
        )

        pdf_bytes = pdf.build()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f"stationery_report_{range_type}_{timezone.now().strftime('%Y%m%d_%H%M')}.pdf"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # CSV Download Check
    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="stationery_comprehensive_report_{range_type}_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)

        writer.writerow(['THE FRINGE STATIONERY & RETAIL - COMPREHENSIVE BUSINESS REPORT'])
        writer.writerow(['Report Period', range_label, 'Currency', 'TSH'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S')])
        writer.writerow([])

        writer.writerow(['END-OF-DAY FINANCIAL SUMMARY'])
        writer.writerow(['Report Period', range_label])
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
        'date_range': {
            'range_type': range_type,
            'label': range_label,
            'start_date': s_str,
            'end_date': e_str,
        },
        'end_of_day': {
            'total_revenue': str(total_revenue),
            'total_refunds': str(total_refunds),
            'actual_revenue': str(actual_revenue),
            'total_liabilities': str(total_refunds + total_expenses),
            'actual_net_cash': str(actual_net_cash),
            'cash_sales': str(cash_sales),
            'mobile_sales': str(mobile_sales),
            'credit_sales': str(credit_sales),
            'debt_recovered': str(debt_recovered),
            'expected_cash_drawer': str(cash_sales + debt_recovered - cash_refunds),
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
            'total_expenses': str(total_expenses),
            'net_profit': str(net_profit),
            'net_margin_percent': f"{net_margin:.1f}%",
            'retail_sales_rev': str(retail_rev),
            'retail_sales_count': retail_orders.count(),
            'wholesale_sales_rev': str(wholesale_rev),
            'wholesale_sales_count': wholesale_orders.count(),
        },
        'expenses': {
            'total_expenses': str(total_expenses),
            'count': len(expenses_list),
            'categories': {
                cat: str(sum(Decimal(str(e.get('amount', 0))) for e in expenses_list if e.get('category') == cat))
                for cat in EXPENSE_CATEGORIES if any(e.get('category') == cat for e in expenses_list)
            },
            'recent': expenses_list[:25]
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
        'inventory_valuation': stat_valuation,
        'branch_breakdown': [
            {
                'id': b.id,
                'name': b.name,
                'code': b.code,
                'total_revenue': str(
                    Order.objects.filter(
                        business_unit_id='stationery', branch_id=b.id, status=Order.STATUS_COMPLETED
                    ).filter(
                        **({'created_at__range': (start_dt, end_dt)} if start_dt and end_dt else {})
                    ).aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
                ),
                'total_orders': Order.objects.filter(
                    business_unit_id='stationery', branch_id=b.id, status=Order.STATUS_COMPLETED
                ).filter(
                    **({'created_at__range': (start_dt, end_dt)} if start_dt and end_dt else {})
                ).count(),
                'total_expenses': str(
                    sum(Decimal(str(e.get('amount', 0))) for e in list_expenses(unit_id='stationery', start_date=s_str, end_date=e_str, branch_id=b.id))
                ),
                'total_refunds': str(
                    sum(Decimal(str(r.get('total_refund', 0))) for r in list_returns(unit_id='stationery', start_date=s_str, end_date=e_str, branch_id=b.id))
                ),
                'valuation': compute_inventory_valuation(
                    Product.objects.filter(business_unit_id='stationery', branch_id=b.id, is_active=True)
                )[0],
            }
            for b in __import__('business_core.models', fromlist=['Branch']).Branch.objects.filter(
                business_unit_id='stationery', is_active=True
            )
        ] if request.user.role == User.ROLE_OWNER else [],
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

    start_dt, end_dt, range_label, range_type, s_str, e_str = parse_date_range(request)

    # 1. Bar Metrics
    bar_orders = Order.objects.filter(business_unit_id='bar', status=Order.STATUS_COMPLETED)
    if start_dt and end_dt:
        bar_orders = bar_orders.filter(created_at__range=(start_dt, end_dt))

    bar_revenue = bar_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
    bar_orders_count = bar_orders.count()

    bar_items = OrderItem.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    if start_dt and end_dt:
        bar_items = bar_items.filter(order__created_at__range=(start_dt, end_dt))
    bar_items = bar_items.select_related('product')

    bar_cogs = Decimal('0.00')
    for itm in bar_items:
        bar_cogs += itm.product.cost_price * itm.quantity
    bar_profit = bar_revenue - bar_cogs
    bar_margin = ((bar_profit / bar_revenue) * 100) if bar_revenue > 0 else Decimal('0.00')

    bar_payments = Payment.objects.filter(order__business_unit_id='bar', order__status=Order.STATUS_COMPLETED)
    if start_dt and end_dt:
        bar_payments = bar_payments.filter(order__created_at__range=(start_dt, end_dt))

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
    if start_dt and end_dt:
        stat_orders = stat_orders.filter(created_at__range=(start_dt, end_dt))

    stat_revenue = stat_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
    stat_orders_count = stat_orders.count()

    stat_items = OrderItem.objects.filter(order__business_unit_id='stationery', order__status=Order.STATUS_COMPLETED)
    if start_dt and end_dt:
        stat_items = stat_items.filter(order__created_at__range=(start_dt, end_dt))
    stat_items = stat_items.select_related('product')

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
    if start_dt and end_dt:
        stat_payments = stat_payments.filter(order__created_at__range=(start_dt, end_dt))

    stat_cash = stat_payments.filter(payment_method='CASH').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_momo = stat_payments.filter(payment_method='MOBILE_MONEY').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
    stat_credit = stat_payments.filter(payment_method='CREDIT').aggregate(t=Sum('amount'))['t'] or Decimal('0.00')

    stat_debt_query = CustomerCreditTransaction.objects.filter(
        business_unit_id='stationery', transaction_type='PAYMENT_RECEIVED'
    )
    if start_dt and end_dt:
        stat_debt_query = stat_debt_query.filter(created_at__range=(start_dt, end_dt))
    stat_debt_rec = stat_debt_query.aggregate(t=Sum('amount'))['t'] or Decimal('0.00')
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

    # Combined Group Valuations (Pack/Bulk vs Single-Item Only across all branches)
    bar_valuation, _ = compute_inventory_valuation(bar_stock)
    stat_valuation, _ = compute_inventory_valuation(stat_stock)
    all_active_stock = Product.objects.filter(business_unit_id__in=['bar', 'stationery'], is_active=True).select_related('branch')
    group_valuation, _ = compute_inventory_valuation(all_active_stock)

    # Combined Refunds & Operating Expenses (Bar + Stationery separated & totaled)
    bar_returns_list = list_returns(unit_id='bar', start_date=s_str, end_date=e_str)
    stat_returns_list = list_returns(unit_id='stationery', start_date=s_str, end_date=e_str)
    bar_refunds_total = sum(Decimal(str(r.get('total_refund', 0))) for r in bar_returns_list)
    stat_refunds_total = sum(Decimal(str(r.get('total_refund', 0))) for r in stat_returns_list)
    group_refunds_total = bar_refunds_total + stat_refunds_total

    bar_expenses_list = list_expenses(unit_id='bar', start_date=s_str, end_date=e_str)
    stat_expenses_list = list_expenses(unit_id='stationery', start_date=s_str, end_date=e_str)
    bar_expenses_total = sum(Decimal(str(e.get('amount', 0))) for e in bar_expenses_list)
    stat_expenses_total = sum(Decimal(str(e.get('amount', 0))) for e in stat_expenses_list)
    group_expenses_total = bar_expenses_total + stat_expenses_total

    bar_actual_revenue = bar_revenue - bar_refunds_total
    stat_actual_revenue = stat_revenue - stat_refunds_total
    bar_profit = bar_actual_revenue - bar_cogs
    stat_profit = stat_actual_revenue - stat_cogs
    bar_margin = ((bar_profit / bar_actual_revenue) * 100) if bar_actual_revenue > 0 else Decimal('0.00')
    stat_margin = ((stat_profit / stat_actual_revenue) * 100) if stat_actual_revenue > 0 else Decimal('0.00')

    # Combined Group Totals
    group_revenue = bar_revenue + stat_revenue
    group_actual_revenue = bar_actual_revenue + stat_actual_revenue
    group_cogs = bar_cogs + stat_cogs
    group_profit = bar_profit + stat_profit
    group_margin = ((group_profit / group_actual_revenue) * 100) if group_actual_revenue > 0 else Decimal('0.00')
    group_drawer_cash = bar_cash + stat_drawer_cash
    group_inventory_cost = Decimal(group_valuation['valuation_at_cost'])
    group_inventory_retail = Decimal(group_valuation['valuation_at_retail'])
    group_liabilities_total = group_refunds_total + group_expenses_total
    group_net_profit = group_profit - group_expenses_total
    group_net_margin = ((group_net_profit / group_actual_revenue) * 100) if group_actual_revenue > 0 else Decimal('0.00')
    group_actual_net_cash = group_actual_revenue - group_expenses_total

    bar_share = ((bar_actual_revenue / group_actual_revenue) * 100) if group_actual_revenue > 0 else Decimal('0.00')
    stat_share = ((stat_actual_revenue / group_actual_revenue) * 100) if group_actual_revenue > 0 else Decimal('0.00')

    net_working_capital = (group_drawer_cash + total_receivables + group_inventory_cost) - total_payables

    # All-Branches Breakdown (Bar + Stationery branches)
    BranchModel = __import__('business_core.models', fromlist=['Branch']).Branch
    all_branches_breakdown = []
    for br in BranchModel.objects.filter(business_unit_id__in=['bar', 'stationery'], is_active=True).order_by('business_unit_id', 'name'):
        br_orders = Order.objects.filter(
            business_unit_id=br.business_unit_id, branch_id=br.id, status=Order.STATUS_COMPLETED
        )
        if start_dt and end_dt:
            br_orders = br_orders.filter(created_at__range=(start_dt, end_dt))
        br_rev = br_orders.aggregate(t=Sum('final_amount'))['t'] or Decimal('0.00')
        br_count = br_orders.count()
        br_refunds = sum(
            Decimal(str(r.get('total_refund', 0)))
            for r in list_returns(unit_id=br.business_unit_id, start_date=s_str, end_date=e_str, branch_id=br.id)
        )
        br_expenses = sum(
            Decimal(str(e.get('amount', 0)))
            for e in list_expenses(unit_id=br.business_unit_id, start_date=s_str, end_date=e_str, branch_id=br.id)
        )
        br_val, _ = compute_inventory_valuation(
            Product.objects.filter(business_unit_id=br.business_unit_id, branch_id=br.id, is_active=True)
        )
        all_branches_breakdown.append({
            'id': br.id,
            'name': br.name,
            'code': br.code,
            'business_unit': br.business_unit_id,
            'gross_revenue': str(br_rev),
            'refunds': str(br_refunds),
            'actual_revenue': str(br_rev - br_refunds),
            'expenses': str(br_expenses),
            'net_cash_after_liabilities': str(br_rev - br_refunds - br_expenses),
            'orders_count': br_count,
            'valuation': br_val,
        })

    # PDF Download Check
    if request.query_params.get('export') == 'pdf':
        pdf = ReportPDFBuilder(
            title="Consolidated Executive Enterprise Report",
            subtitle="The Fringe Enterprises (Bar & Lounge + Stationery)",
            date_range_label=range_label,
            business_unit="Consolidated (All Units)"
        )
        pdf.add_kpi_grid([
            {'label': 'Group Revenue', 'value': f"TSH {group_revenue:,.2f}", 'sub': f"{bar_orders_count + stat_orders_count} total orders", 'color': (0.85, 0.55, 0.05)},
            {'label': 'Group Gross Profit', 'value': f"TSH {group_profit:,.2f}", 'sub': f"Margin: {group_margin:.1f}%", 'color': (0.08, 0.6, 0.35)},
            {'label': 'Total Drawer Cash', 'value': f"TSH {group_drawer_cash:,.2f}", 'sub': 'Bar + Stationery registers', 'color': (0.1, 0.55, 0.8)},
            {'label': 'Net Working Capital', 'value': f"TSH {net_working_capital:,.2f}", 'sub': '(Cash+Debts+Stock)-Payables', 'color': (0.05, 0.5, 0.25)},
        ])

        pdf.add_section_header("Working Capital & Balance Sheet Snapshot")
        wc_rows = [
            ['Working Capital Tied in Inventory', f"TSH {group_inventory_cost:,.2f}", 'Bar + Stationery stock valued at cost price'],
            ['Accounts Receivable (Customer Credit)', f"TSH {total_receivables:,.2f}", 'Debts owed by Stationery customers'],
            ['Accounts Payable (Supplier Liabilities)', f"TSH {total_payables:,.2f}", 'Pending bills owed to stationery vendors'],
            ['Total Register Cash', f"TSH {group_drawer_cash:,.2f}", 'Physical & mobile money in drawers'],
            ['Net Working Capital Position', f"TSH {net_working_capital:,.2f}", '(Cash + Receivables + Stock) minus Payables'],
        ]
        pdf.add_table(
            headers=['Balance Sheet Line Item', 'Amount (TSH)', 'Notes & Details'],
            rows=wc_rows,
            col_widths=[190, 135, 190],
            alignments=['L', 'R', 'L']
        )

        pdf.add_section_header("Business Unit Performance Comparison")
        comp_rows = [
            ['Bar & Lounge', f"TSH {bar_revenue:,.2f}", f"{bar_share:.1f}%", str(bar_orders_count), f"TSH {bar_cogs:,.2f}", f"TSH {bar_profit:,.2f}", f"{bar_margin:.1f}%", f"TSH {bar_cash:,.2f}", f"TSH {bar_stock_cost:,.2f}"],
            ['Stationery & Retail', f"TSH {stat_revenue:,.2f}", f"{stat_share:.1f}%", str(stat_orders_count), f"TSH {stat_cogs:,.2f}", f"TSH {stat_profit:,.2f}", f"{stat_margin:.1f}%", f"TSH {stat_drawer_cash:,.2f}", f"TSH {stat_stock_cost:,.2f}"],
            ['Total Group', f"TSH {group_revenue:,.2f}", '100%', str(bar_orders_count + stat_orders_count), f"TSH {group_cogs:,.2f}", f"TSH {group_profit:,.2f}", f"{group_margin:.1f}%", f"TSH {group_drawer_cash:,.2f}", f"TSH {group_inventory_cost:,.2f}"],
        ]
        pdf.add_table(
            headers=['Business Unit', 'Revenue', 'Share', 'Orders', 'COGS', 'Profit', 'Margin', 'Drawer Cash', 'Stock (Cost)'],
            rows=comp_rows,
            col_widths=[105, 65, 35, 35, 55, 55, 40, 65, 60],
            alignments=['L', 'R', 'R', 'R', 'R', 'R', 'R', 'R', 'R']
        )

        pdf_bytes = pdf.build()
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f"fringe_consolidated_report_{range_type}_{timezone.now().strftime('%Y%m%d_%H%M')}.pdf"
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    if request.query_params.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="fringe_consolidated_report_{range_type}_{timezone.now().strftime("%Y%m%d_%H%M")}.csv"'
        writer = csv.writer(response)

        writer.writerow(['THE FRINGE ENTERPRISES - CONSOLIDATED EXECUTIVE REPORT'])
        writer.writerow(['Report Period', range_label, 'Currency', 'TSH'])
        writer.writerow(['Generated At', timezone.now().strftime('%Y-%m-%d %H:%M:%S')])
        writer.writerow([])

        writer.writerow(['GROUP EXECUTIVE SUMMARY'])
        writer.writerow(['Report Period', range_label])
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
        'date_range': {
            'range_type': range_type,
            'label': range_label,
            'start_date': s_str,
            'end_date': e_str,
        },
        'group_summary': {
            'group_revenue': str(group_revenue),
            'group_refunds': str(group_refunds_total),
            'group_actual_revenue': str(group_actual_revenue),
            'group_cogs': str(group_cogs),
            'group_profit': str(group_profit),
            'group_margin_percent': f"{group_margin:.1f}%",
            'group_drawer_cash': str(group_drawer_cash),
            'group_inventory_cost': str(group_inventory_cost),
            'group_inventory_retail': str(group_inventory_retail),
            'group_expenses': str(group_expenses_total),
            'group_liabilities': str(group_liabilities_total),
            'group_net_profit': str(group_net_profit),
            'group_net_margin_percent': f"{group_net_margin:.1f}%",
            'group_actual_net_cash': str(group_actual_net_cash),
            'total_receivables': str(total_receivables),
            'total_payables': str(total_payables),
            'net_working_capital': str(net_working_capital),
        },
        'inventory_valuation': group_valuation,
        'branches': all_branches_breakdown,
        'units': {
            'bar': {
                'name': 'Bar & Lounge',
                'revenue': str(bar_revenue),
                'refunds': str(bar_refunds_total),
                'actual_revenue': str(bar_actual_revenue),
                'share_percent': f"{bar_share:.1f}%",
                'orders_count': bar_orders_count,
                'cogs': str(bar_cogs),
                'gross_profit': str(bar_profit),
                'gross_margin_percent': f"{bar_margin:.1f}%",
                'expenses': str(bar_expenses_total),
                'net_profit': str(bar_profit - bar_expenses_total),
                'cash_in_drawer': str(bar_cash),
                'mobile_money': str(bar_momo),
                'inventory_cost': bar_valuation['valuation_at_cost'],
                'inventory_retail': bar_valuation['valuation_at_retail'],
                'valuation': bar_valuation,
            },
            'stationery': {
                'name': 'Stationery & Retail',
                'revenue': str(stat_revenue),
                'refunds': str(stat_refunds_total),
                'actual_revenue': str(stat_actual_revenue),
                'share_percent': f"{stat_share:.1f}%",
                'orders_count': stat_orders_count,
                'cogs': str(stat_cogs),
                'gross_profit': str(stat_profit),
                'gross_margin_percent': f"{stat_margin:.1f}%",
                'expenses': str(stat_expenses_total),
                'net_profit': str(stat_profit - stat_expenses_total),
                'cash_in_drawer': str(stat_drawer_cash),
                'cash_sales': str(stat_cash),
                'mobile_money': str(stat_momo),
                'credit_sales': str(stat_credit),
                'debt_recovered': str(stat_debt_rec),
                'receivables_owed': str(total_receivables),
                'payables_owed': str(total_payables),
                'inventory_cost': stat_valuation['valuation_at_cost'],
                'inventory_retail': stat_valuation['valuation_at_retail'],
                'valuation': stat_valuation,
            }
        }
    })


# ==========================================
# EXPENSES API (F9)
# ==========================================
@api_view(['GET', 'POST'])
@permission_classes([permissions.IsAuthenticated])
def expenses_view(request):
    """
    Operating Expenses Management:
    - Lists expenses with category, date range, and summary breakdown.
    - Records operating costs (Rent, Transport, Electricity, Salaries, Repairs, etc.).
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'stationery'
    branch_id = getattr(request, 'branch_id', None)
    if unit_id == 'all':
        branch_id = None
    elif not branch_id or branch_id == 'all':
        branch_id = None if request.user.role == User.ROLE_OWNER else request.user.branch_id

    if request.method == 'GET':
        start = request.query_params.get('start_date')
        end = request.query_params.get('end_date')
        cat = request.query_params.get('category')
        expenses = list_expenses(unit_id=unit_id, start_date=start, end_date=end, category=cat, branch_id=branch_id)
        total = sum(e.get('amount', 0) for e in expenses)
        
        category_totals = {}
        for c in EXPENSE_CATEGORIES:
            sub = sum(e.get('amount', 0) for e in expenses if e.get('category') == c)
            if sub > 0:
                category_totals[c] = sub
                
        return Response({
            'results': expenses,
            'count': len(expenses),
            'total_amount': total,
            'category_totals': category_totals,
            'categories': EXPENSE_CATEGORIES
        })

    # POST: Record expense
    if not (request.user.is_manager_or_owner or request.user.role == User.ROLE_STOCK_CLERK):
        return Response({
            'error': 'Manager, Owner, or Stock Clerk privileges required to record expenses.'
        }, status=status.HTTP_403_FORBIDDEN)

    record = add_expense(request.data, request.user, unit_id=unit_id, branch_id=branch_id)
    record_audit(
        request=request,
        action='EXPENSE_RECORDED',
        entity_type='Expense',
        entity_id=record['id'],
        metadata={'category': record['category'], 'amount': str(record['amount'])}
    )
    return Response({
        'message': f"Expense {record['id']} successfully recorded.",
        'expense': record
    }, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([permissions.IsAuthenticated])
def expense_detail_view(request, expense_id):
    """Delete an expense (Manager/Owner only)"""
    if not request.user.is_manager_or_owner:
        return Response({'error': 'Only Managers and Business Owners can delete expenses.'}, status=status.HTTP_403_FORBIDDEN)
        return Response({'error': 'Only Managers and Business Owners can delete expenses.'}, status=status.HTTP_403_FORBIDDEN)
    
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'stationery'
    ok = delete_expense(expense_id, unit_id=unit_id)
    if ok:
        record_audit(request=request, action='EXPENSE_DELETED', entity_type='Expense', entity_id=expense_id)
        return Response({'message': 'Expense deleted successfully.'})
    return Response({'error': 'Expense record not found.'}, status=status.HTTP_404_NOT_FOUND)


# ==========================================
# QUOTATIONS / PRICE OFFERS API (F5 & 4.4)
# ==========================================
@api_view(['GET', 'POST'])
@permission_classes([permissions.IsAuthenticated])
def quotations_view(request):
    """
    Quotations / Price Offers:
    - Lists price offers for schools, offices, and retail shops.
    - Creates formal quotation with validity period.
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'stationery'

    if request.method == 'GET':
        status_filter = request.query_params.get('status')
        customer_id = request.query_params.get('customer')
        quotes = list_quotations(unit_id=unit_id, status=status_filter, customer_id=customer_id)
        return Response({'results': quotes, 'count': len(quotes)})

    # POST: Create quotation
    data = request.data
    record = create_quotation(data, request.user, unit_id=unit_id)
    record_audit(
        request=request,
        action='QUOTATION_CREATED',
        entity_type='Quotation',
        entity_id=record['id'],
        metadata={'customer': record['customer_name'], 'final_amount': str(record['final_amount'])}
    )
    return Response({
        'message': f"Quotation {record['quote_number']} created successfully.",
        'quotation': record
    }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'POST'])
@permission_classes([permissions.IsAuthenticated])
def quotation_detail_view(request, quote_id):
    """View details or update status of a quotation"""
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'stationery'
    quote = get_quotation(quote_id, unit_id=unit_id)
    if not quote:
        return Response({'error': 'Quotation not found.'}, status=status.HTTP_404_NOT_FOUND)

    if request.method == 'GET':
        return Response(quote)

    new_status = request.data.get('status', 'ACCEPTED').upper()
    converted_order = request.data.get('converted_order_number')
    updated = update_quotation_status(quote_id, new_status, converted_order_number=converted_order, unit_id=unit_id)
    return Response({'message': f"Quotation status updated to {new_status}.", 'quotation': updated})


# ==========================================
# RETURNS & REFUNDS API (F8)
# ==========================================
@api_view(['GET', 'POST'])
@permission_classes([permissions.IsAuthenticated])
@transaction.atomic
def returns_view(request):
    """
    Customer Returns & Damaged Goods Disposition:
    - Return to stock or write-off as damaged.
    - Refund in cash or credit customer account.
    - Requires Manager PIN authorization.
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'stationery'

    if request.method == 'GET':
        returns_list = list_returns(unit_id=unit_id)
        return Response({'results': returns_list, 'count': len(returns_list)})

    # POST: Process return
    pin = request.data.get('manager_pin')
    approver = None
    if pin:
        managers = User.objects.filter(is_active=True).exclude(pin_hash__isnull=True).filter(
            role__in=[User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER]
        )
        for m in managers:
            if m.check_pin(pin):
                approver = m
                break

    if not approver and not request.user.is_stationery_manager_or_owner:
        return Response({
            'error': 'Manager authorization PIN required to approve customer returns and refunds.',
            'code': 'MANAGER_PIN_REQUIRED'
        }, status=status.HTTP_403_FORBIDDEN)

    items = request.data.get('items', [])
    if not items:
        return Response({'error': 'At least one return item is required.'}, status=status.HTTP_400_BAD_REQUEST)

    disposition = request.data.get('disposition', 'RETURN_TO_STOCK')
    refund_method = request.data.get('refund_method', 'CASH').upper()
    customer_id = request.data.get('customer_id')

    total_refund = Decimal('0.00')

    for itm in items:
        prod_id = itm.get('product_id')
        qty = Decimal(str(itm.get('quantity', 1)))
        price = Decimal(str(itm.get('unit_price', 0)))
        total_refund += price * qty

        if disposition == 'RETURN_TO_STOCK' and prod_id:
            try:
                prod = Product.objects.select_for_update().get(id=prod_id, business_unit_id=unit_id)
                is_pack = bool(itm.get('is_pack_sale', False))
                if prod.is_pack and not is_pack and prod.allow_split_sale:
                    stock_add = qty / Decimal(prod.pack_unit_ratio)
                else:
                    stock_add = qty
                prod.stock_quantity = F('stock_quantity') + stock_add
                prod.save(update_fields=['stock_quantity'])
            except Product.DoesNotExist:
                pass

    if refund_method == 'CREDIT' and customer_id:
        try:
            cust = Customer.objects.select_for_update().get(id=customer_id, business_unit_id=unit_id)
            balance_before = cust.current_balance
            cust.current_balance = max(Decimal('0.00'), F('current_balance') - total_refund)
            cust.save(update_fields=['current_balance'])
            cust.refresh_from_db()

            CustomerCreditTransaction.objects.create(
                business_unit_id=unit_id,
                customer=cust,
                transaction_type=CustomerCreditTransaction.TYPE_ADJUSTMENT,
                amount=total_refund,
                balance_before=balance_before,
                balance_after=cust.current_balance,
                payment_method='CREDIT_NOTE',
                recorded_by=request.user,
                approved_by=approver or request.user,
                notes=f"Customer return refund (Ref Order: {request.data.get('order_number', 'Sale')})"
            )
        except Customer.DoesNotExist:
            pass

    ret_record = create_return({
        **request.data,
        'approved_by_name': approver.username if approver else request.user.username
    }, request.user, unit_id=unit_id)

    record_audit(
        request=request,
        action='SALE_RETURN_PROCESSED',
        entity_type='Return',
        entity_id=ret_record['id'],
        metadata={'total_refund': str(total_refund), 'disposition': disposition, 'method': refund_method}
    )

    return Response({
        'message': f"Return {ret_record['return_number']} successfully processed.",
        'return': ret_record
    })


# ==========================================
# BULK PRODUCT IMPORT (F2)
# ==========================================
@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def product_bulk_import_view(request):
    """
    Import catalog products/drinks in bulk from CSV/Excel data.
    """
    if not (request.user.is_manager_or_owner or request.user.role == User.ROLE_STOCK_CLERK):
        return Response({
            'error': 'Manager, Owner, or Stock Clerk privileges required to import catalog items.'
        }, status=status.HTTP_403_FORBIDDEN)

    unit_id = getattr(request, 'business_unit', request.user.business_unit_id) or 'bar'
    if unit_id == 'all':
        unit_id = request.user.business_unit_id or 'bar'

    products_data = request.data.get('products', [])
    if not products_data:
        return Response({'error': 'No product records provided for import.'}, status=status.HTTP_400_BAD_REQUEST)

    created_count = 0
    updated_count = 0
    errors = []

    for idx, row in enumerate(products_data):
        name = row.get('name', '').strip()
        if not name:
            errors.append(f"Row {idx+1}: Missing product name.")
            continue

        cat_name = row.get('category', '').strip()
        cat = None
        if cat_name:
            cat, _ = Category.objects.get_or_create(
                business_unit_id=unit_id,
                name=cat_name,
                defaults={'slug': cat_name.lower().replace(' ', '-')}
            )

        sku = row.get('sku', '').strip()
        barcode = row.get('barcode', '').strip()
        desc = row.get('description', '').strip()
        cost_price = Decimal(str(row.get('cost_price', '0.00') or '0.00'))
        selling_price = Decimal(str(row.get('selling_price', '0.00') or '0.00'))
        stock_qty = Decimal(str(row.get('stock_quantity', '0.00') or '0.00'))
        threshold = Decimal(str(row.get('reorder_threshold', '5.00') or '5.00'))

        is_pack = bool(row.get('is_pack', False))
        ratio = int(row.get('pack_unit_ratio', 1) or 1)
        pack_name = row.get('pack_name', 'Crate' if unit_id == 'bar' else 'Pack') or ('Crate' if unit_id == 'bar' else 'Pack')
        unit_name = row.get('unit_name', 'Bottle' if unit_id == 'bar' else 'Piece') or ('Bottle' if unit_id == 'bar' else 'Piece')
        allow_split = bool(row.get('allow_split_sale', False))
        split_price = Decimal(str(row.get('split_unit_price', '0.00'))) if (allow_split and row.get('split_unit_price')) else None

        prod, created = Product.objects.update_or_create(
            business_unit_id=unit_id,
            name=name,
            defaults={
                'category': cat,
                'sku': sku,
                'barcode': barcode,
                'description': desc,
                'cost_price': cost_price,
                'selling_price': selling_price,
                'stock_quantity': stock_qty,
                'reorder_threshold': threshold,
                'is_pack': is_pack,
                'pack_unit_ratio': ratio,
                'pack_name': pack_name,
                'unit_name': unit_name,
                'allow_split_sale': allow_split,
                'split_unit_price': split_price,
                'is_active': True,
            }
        )
        if created:
            created_count += 1
        else:
            updated_count += 1

    return Response({
        'message': f"Import completed: {created_count} products added, {updated_count} updated.",
        'created_count': created_count,
        'updated_count': updated_count,
        'errors': errors
    })

