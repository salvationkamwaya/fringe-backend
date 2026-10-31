from rest_framework import serializers
from .models import Shift, BarTab, Order, OrderItem, Payment
from business_catalog.models import Product


class OrderItemSerializer(serializers.ModelSerializer):
    product_id = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), source='product', write_only=True
    )

    class Meta:
        model = OrderItem
        fields = [
            'id', 'product', 'product_id', 'product_name',
            'unit_price', 'quantity', 'subtotal',
            'is_alcoholic', 'is_voided', 'void_reason'
        ]
        read_only_fields = ['id', 'product', 'product_name', 'subtotal', 'is_alcoholic']


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'payment_method', 'amount', 'reference', 'created_at']
        read_only_fields = ['id', 'created_at']


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, required=False)
    payments = PaymentSerializer(many=True, required=False)
    cashier_name = serializers.ReadOnlyField(source='cashier.username')
    tab_name = serializers.ReadOnlyField(source='tab.tab_name')

    class Meta:
        model = Order
        fields = [
            'id', 'business_unit', 'order_number', 'shift', 'tab',
            'tab_name', 'cashier', 'cashier_name',
            'subtotal', 'discount_amount', 'discount_reason', 'discount_approved_by',
            'final_amount', 'status', 'void_reason', 'void_approved_by', 'voided_at',
            'is_age_verified', 'age_verified_by', 'age_verified_at',
            'items', 'payments', 'created_at'
        ]
        read_only_fields = [
            'id', 'order_number', 'business_unit', 'cashier', 'created_at'
        ]


class BarTabSerializer(serializers.ModelSerializer):
    opened_by_name = serializers.ReadOnlyField(source='opened_by.username')
    orders = OrderSerializer(many=True, read_only=True)
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = BarTab
        fields = [
            'id', 'business_unit', 'tab_name', 'customer_name',
            'status', 'opened_by', 'opened_by_name', 'closed_by',
            'opened_at', 'closed_at', 'notes', 'total_amount', 'orders'
        ]
        read_only_fields = ['id', 'business_unit', 'opened_by', 'opened_at']


class ShiftSerializer(serializers.ModelSerializer):
    cashier_name = serializers.ReadOnlyField(source='cashier.username')

    class Meta:
        model = Shift
        fields = [
            'id', 'business_unit', 'cashier', 'cashier_name',
            'started_at', 'ended_at', 'opening_float',
            'expected_cash', 'counted_cash', 'variance', 'variance_reason',
            'total_sales_cash', 'total_sales_mobile', 'order_count',
            'status', 'notes'
        ]
        read_only_fields = ['id', 'business_unit', 'cashier', 'started_at']
