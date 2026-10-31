from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.db.models import Sum, Count, F, Q
from django.http import Http404
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response

from .models import Shift, BarTab, Order, OrderItem, Payment
from .serializers import ShiftSerializer, BarTabSerializer, OrderSerializer
from business_catalog.models import Product
from business_core.audit import record_audit
from business_core.models import User


class ShiftViewSet(viewsets.ModelViewSet):
    """
    Shift and Cash Reconciliation management with Z-Report generation.
    """
    serializer_class = ShiftSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Shift.objects.for_request(self.request)

    @action(detail=False, methods=['get'])
    def current(self, request):
        """Get the cashier's active open shift in the current business unit."""
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
        """Open a new register shift with an opening float."""
        unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
        if unit_id == 'all':
            unit_id = request.user.business_unit_id or 'bar'

        # Check for already open shift
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
        """
        Close a shift, reconcile counted cash against expected cash, and calculate variance.
        """
        shift = self.get_object()
        if shift.status == Shift.STATUS_CLOSED:
            return Response({'error': 'Shift is already closed.'}, status=status.HTTP_400_BAD_REQUEST)

        counted_cash = Decimal(str(request.data.get('counted_cash', '0.00')))
        variance_reason = request.data.get('variance_reason', '')

        # Calculate expected cash: opening_float + cash_sales
        expected_cash = shift.opening_float + shift.total_sales_cash
        variance = counted_cash - expected_cash

        shift.counted_cash = counted_cash
        shift.expected_cash = expected_cash
        shift.variance = variance
        shift.variance_reason = variance_reason
        shift.ended_at = timezone.now()
        shift.status = Shift.STATUS_CLOSED
        shift.save()

        record_audit(
            request=request,
            action='SHIFT_CLOSED_Z_REPORT',
            entity_type='Shift',
            entity_id=shift.id,
            before_value={'expected_cash': str(expected_cash)},
            after_value={
                'counted_cash': str(counted_cash),
                'variance': str(variance),
                'total_sales_cash': str(shift.total_sales_cash),
                'total_sales_mobile': str(shift.total_sales_mobile)
            },
            metadata={'variance_reason': variance_reason}
        )

        return Response(ShiftSerializer(shift).data)

    @action(detail=True, methods=['get'])
    def z_report(self, request, pk=None):
        """Generate comprehensive Shift Z-Report reconciliation."""
        shift = self.get_object()
        orders = shift.orders.filter(status='COMPLETED')

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
            'total_sales_gross': str(shift.total_sales_cash + shift.total_sales_mobile),
            'expected_cash_in_drawer': str(shift.opening_float + shift.total_sales_cash),
            'counted_cash': str(shift.counted_cash or '0.00'),
            'variance': str(shift.variance),
            'variance_reason': shift.variance_reason,
            'order_count': shift.order_count,
            'generated_at': timezone.now().isoformat(),
        })


class BarTabViewSet(viewsets.ModelViewSet):
    """
    Open bar tabs management.
    """
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
        """
        Reopen a settled or closed tab.
        CRITICAL RULE: Requires Manager/Owner authorization.
        """
        tab = self.get_object()
        approver_id = request.data.get('approver_id')
        manager_pin = request.data.get('manager_pin')

        approver = None
        if manager_pin:
            # Verify PIN
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


class OrderViewSet(viewsets.ModelViewSet):
    """
    POS Order Processing:
    - Enforces non-bypassable alcohol age check.
    - Prevents credit sales in Bar unit.
    - Automatically deducts stock.
    - Reconciles active Shift cash/mobile totals.
    - Requires manager approval for voids.
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
        shift_id = request.data.get('shift_id')
        is_age_verified = bool(request.data.get('is_age_verified', False))
        discount_amount = Decimal(str(request.data.get('discount_amount', '0.00')))
        discount_reason = request.data.get('discount_reason', '')
        discount_approver_id = request.data.get('discount_approved_by')

        if not items_data:
            return Response({'error': 'Order must contain at least one item.'}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Inspect all items and check for alcoholic drinks
        has_alcoholic_item = False
        parsed_items = []
        subtotal = Decimal('0.00')

        for item_data in items_data:
            product_id = item_data.get('product_id')
            qty = Decimal(str(item_data.get('quantity', 1)))
            
            try:
                product = Product.objects.select_for_update().get(id=product_id, business_unit_id=unit_id)
            except Product.DoesNotExist:
                raise Http404(f"Product ID {product_id} not found in {unit_id}.")

            if product.is_alcoholic:
                has_alcoholic_item = True

            line_price = product.selling_price
            line_subtotal = line_price * qty
            subtotal += line_subtotal

            parsed_items.append({
                'product': product,
                'quantity': qty,
                'unit_price': line_price,
                'subtotal': line_subtotal,
                'is_alcoholic': product.is_alcoholic
            })

        # 2. NON-BYPASSABLE AGE CHECK ENFORCEMENT
        if has_alcoholic_item and not is_age_verified:
            # Mandatory alcohol age check failed
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

        # 3. PAYMENT METHOD VALIDATION
        # Bar: Cash and Mobile Money only. Strictly NO credit.
        total_paid = Decimal('0.00')
        parsed_payments = []

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

            total_paid += amt
            parsed_payments.append({'method': method, 'amount': amt, 'reference': ref})

        # Settle against open tab or direct payment
        tab = None
        if tab_id:
            try:
                tab = BarTab.objects.get(id=tab_id, business_unit_id=unit_id, status=BarTab.STATUS_OPEN)
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
            # Fallback to cashier's active shift
            shift = Shift.objects.filter(
                business_unit_id=unit_id, cashier=user, status=Shift.STATUS_OPEN
            ).first()

        # 5. CREATE ORDER RECORD
        order = Order.objects.create(
            business_unit_id=unit_id,
            order_number=Order.generate_order_number(unit_id),
            shift=shift,
            tab=tab,
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

        # 6. DEDUCT STOCK & CREATE ORDER ITEMS
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
                is_alcoholic=p_item['is_alcoholic']
            )

            # Automatic stock deduction
            product.stock_quantity = F('stock_quantity') - qty
            product.save(update_fields=['stock_quantity'])

        # 7. RECORD PAYMENTS & UPDATE SHIFT TOTALS
        for p in parsed_payments:
            Payment.objects.create(
                order=order,
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
            shift.order_count = F('order_count') + 1
            shift.save(update_fields=['total_sales_cash', 'total_sales_mobile', 'order_count'])

        # 8. AUDIT LOGGING
        record_audit(
            request=request,
            action='SALE_COMPLETED',
            entity_type='Order',
            entity_id=order.id,
            after_value={
                'order_number': order.order_number,
                'final_amount': str(order.final_amount),
                'items_count': len(parsed_items),
                'is_alcoholic': has_alcoholic_item
            },
            metadata={
                'payments': [
                    {'method': p['method'], 'amount': str(p['amount']), 'reference': p['reference']}
                    for p in parsed_payments
                ]
            }
        )

        if has_alcoholic_item:
            record_audit(
                request=request,
                action='AGE_VERIFIED',
                entity_type='Order',
                entity_id=order.id,
                metadata={'verified_by': user.username}
            )

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def void_order(self, request, pk=None):
        """
        Void a completed sale.
        RULE: Requires Manager or Owner approval PIN.
        Restores product inventory stock and updates shift totals.
        """
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

        # 1. Restore product inventory
        for item in order.items.all():
            product = item.product
            product.stock_quantity = F('stock_quantity') + item.quantity
            product.save(update_fields=['stock_quantity'])

        # 2. Adjust shift cash totals
        if order.shift:
            for payment in order.payments.all():
                if payment.payment_method == 'CASH':
                    order.shift.total_sales_cash = F('total_sales_cash') - payment.amount
                elif payment.payment_method == 'MOBILE_MONEY':
                    order.shift.total_sales_mobile = F('total_sales_mobile') - payment.amount
            order.shift.order_count = F('order_count') - 1
            order.shift.save(update_fields=['total_sales_cash', 'total_sales_mobile', 'order_count'])

        # 3. Mark as voided
        order.status = Order.STATUS_VOIDED
        order.void_reason = void_reason
        order.void_approved_by = approver or request.user
        order.voided_at = timezone.now()
        order.save()

        # 4. Audit
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
    """
    Bar financial reports:
    - Product Sales Breakdown
    - Stock-On-Hand Status
    - Profit Snapshot (Revenue vs COGS)
    """
    unit_id = getattr(request, 'business_unit', request.user.business_unit_id)
    if unit_id not in ('bar', 'all') and request.user.role != User.ROLE_OWNER:
        raise Http404("Report not found.")

    # 1. Product Sales
    product_sales = OrderItem.objects.filter(
        order__business_unit_id='bar',
        order__status='COMPLETED'
    ).values(
        'product__id', 'product__name', 'product__is_alcoholic'
    ).annotate(
        total_quantity=Sum('quantity'),
        total_revenue=Sum('subtotal')
    ).order_by('-total_revenue')

    # 2. Stock on Hand
    stock_on_hand = Product.objects.filter(
        business_unit_id='bar',
        is_active=True
    ).values(
        'id', 'name', 'stock_quantity', 'reorder_threshold', 'selling_price', 'cost_price', 'is_alcoholic'
    ).order_by('stock_quantity')

    # 3. Profit Snapshot
    completed_orders = Order.objects.filter(
        business_unit_id='bar',
        status='COMPLETED'
    )
    total_revenue = completed_orders.aggregate(total=Sum('final_amount'))['total'] or Decimal('0.00')

    # COGS
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
