from rest_framework import serializers
from .models import (
    Shift, BarTab, Customer, Order, OrderItem, Payment,
    CustomerCreditTransaction, Supplier, PurchaseOrder, SupplierPayment
)
from business_catalog.models import Product


class CustomerSerializer(serializers.ModelSerializer):
    available_credit = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = Customer
        fields = [
            'id', 'business_unit', 'name', 'contact_person', 'phone', 'email', 'address',
            'credit_limit', 'current_balance', 'available_credit', 'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'current_balance', 'created_at']


class CustomerCreditTransactionSerializer(serializers.ModelSerializer):
    customer_name = serializers.ReadOnlyField(source='customer.name')
    recorded_by_name = serializers.ReadOnlyField(source='recorded_by.username')
    approved_by_name = serializers.ReadOnlyField(source='approved_by.username')

    class Meta:
        model = CustomerCreditTransaction
        fields = [
            'id', 'business_unit', 'customer', 'customer_name', 'order',
            'transaction_type', 'amount', 'balance_before', 'balance_after',
            'payment_method', 'reference', 'recorded_by', 'recorded_by_name',
            'approved_by', 'approved_by_name', 'notes', 'due_date', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'balance_before', 'balance_after', 'recorded_by', 'created_at']


class OrderItemSerializer(serializers.ModelSerializer):
    product_id = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), source='product', write_only=True
    )

    class Meta:
        model = OrderItem
        fields = [
            'id', 'product', 'product_id', 'product_name',
            'unit_price', 'quantity', 'subtotal',
            'is_alcoholic', 'is_pack_sale', 'pack_unit_ratio',
            'is_voided', 'void_reason'
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
    customer_name = serializers.ReadOnlyField(source='customer.name')

    class Meta:
        model = Order
        fields = [
            'id', 'business_unit', 'order_number', 'shift', 'tab',
            'tab_name', 'customer', 'customer_name', 'cashier', 'cashier_name',
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
            'total_sales_cash', 'total_sales_mobile', 'total_sales_credit',
            'total_debt_recovered', 'order_count', 'status', 'notes'
        ]
        read_only_fields = ['id', 'business_unit', 'cashier', 'started_at']


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = [
            'id', 'business_unit', 'name', 'contact_person', 'phone',
            'email', 'address', 'balance_payable', 'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'created_at']


class PurchaseOrderSerializer(serializers.ModelSerializer):
    supplier_name = serializers.ReadOnlyField(source='supplier.name')
    received_by_name = serializers.ReadOnlyField(source='received_by.username')
    balance_remaining = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = PurchaseOrder
        fields = [
            'id', 'business_unit', 'supplier', 'supplier_name', 'order_number',
            'invoice_number', 'total_cost', 'amount_paid', 'balance_remaining',
            'status', 'received_by', 'received_by_name', 'notes', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'order_number', 'received_by', 'created_at']


class SupplierPaymentSerializer(serializers.ModelSerializer):
    supplier_name = serializers.ReadOnlyField(source='supplier.name')
    recorded_by_name = serializers.ReadOnlyField(source='recorded_by.username')

    class Meta:
        model = SupplierPayment
        fields = [
            'id', 'business_unit', 'supplier', 'supplier_name', 'purchase_order',
            'amount', 'payment_method', 'reference', 'recorded_by', 'recorded_by_name',
            'notes', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'recorded_by', 'created_at']
