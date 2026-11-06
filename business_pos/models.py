import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from business_core.models import TenantModel, User


class Shift(TenantModel):
    """
    Tracks daily or shift-based register drawer status and reconciliation.
    """
    STATUS_OPEN = 'OPEN'
    STATUS_CLOSED = 'CLOSED'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_CLOSED, 'Closed'),
    ]

    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='shifts')
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    variance_reason = models.TextField(blank=True)
    
    total_sales_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_sales_mobile = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_sales_credit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_debt_recovered = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    order_count = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f"Shift #{self.id} [{self.business_unit_id}] - {self.cashier.username} ({self.status})"


class BarTab(TenantModel):
    """
    Open bar tabs for drinks served before final checkout settlement.
    """
    STATUS_OPEN = 'OPEN'
    STATUS_SETTLED = 'SETTLED'
    STATUS_CLOSED = 'CLOSED'
    STATUS_CHOICES = [
        (STATUS_OPEN, 'Open'),
        (STATUS_SETTLED, 'Settled'),
        (STATUS_CLOSED, 'Closed'),
    ]

    tab_name = models.CharField(max_length=100, help_text="e.g. Table 4, VIP Lounge - Alex")
    customer_name = models.CharField(max_length=100, blank=True)
    opened_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='opened_tabs')
    closed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='closed_tabs')
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-opened_at']

    @property
    def total_amount(self):
        return sum(order.final_amount for order in self.orders.exclude(status='VOIDED'))

    @property
    def total_paid(self):
        return sum(
            payment.amount
            for order in self.orders.exclude(status='VOIDED')
            for payment in order.payments.all()
        )

    @property
    def balance_due(self):
        return max(Decimal('0.00'), self.total_amount - self.total_paid)

    @property
    def items_count(self):
        return sum(
            item.quantity
            for order in self.orders.exclude(status='VOIDED')
            for item in order.items.all()
        )

    def __str__(self):
        return f"Tab '{self.tab_name}' [{self.status}]"


class Customer(TenantModel):
    """
    Registered customer directory for Stationery credit sales and account receivables.
    Strictly isolated per business unit (Stationery only).
    """
    name = models.CharField(max_length=200, db_index=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('2000.00'))
    current_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text="Current total amount owed by this customer."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    @property
    def available_credit(self):
        limit = Decimal(str(self.credit_limit or '0.00'))
        balance = Decimal(str(self.current_balance or '0.00'))
        return max(Decimal('0.00'), limit - balance)

    def __str__(self):
        return f"{self.name} (Balance: TSH {self.current_balance})"


class Order(TenantModel):
    """
    Completed or pending POS transaction.
    """
    STATUS_PENDING = 'PENDING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_VOIDED = 'VOIDED'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_VOIDED, 'Voided'),
    ]

    order_number = models.CharField(max_length=50, unique=True, db_index=True)
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders')
    tab = models.ForeignKey(BarTab, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders')
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders')
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='orders')
    
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_reason = models.CharField(max_length=200, blank=True)
    discount_approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_discounts'
    )
    final_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_COMPLETED, db_index=True)

    # Voids
    void_reason = models.TextField(blank=True)
    void_approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_voids'
    )
    voided_at = models.DateTimeField(null=True, blank=True)

    # Alcohol compliance check
    is_age_verified = models.BooleanField(
        default=False,
        help_text="Mandatory verification when alcoholic items are present in cart."
    )
    age_verified_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='verified_orders'
    )
    age_verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    @classmethod
    def generate_order_number(cls, unit_id: str) -> str:
        timestamp = timezone.now().strftime('%y%m%d%H%M')
        rand = uuid.uuid4().hex[:4].upper()
        prefix = 'BAR' if unit_id == 'bar' else 'STN'
        return f"{prefix}-{timestamp}-{rand}"

    def __str__(self):
        return f"Order #{self.order_number} ({self.final_amount} {self.business_unit.currency if hasattr(self, 'business_unit') else ''})"


class OrderItem(models.Model):
    """
    Line items within an order.
    Supports unit vs pack selling conversion for stationery.
    """
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('business_catalog.Product', on_delete=models.PROTECT, related_name='order_items')
    product_name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=1.00)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Flags for Bar & Stationery
    is_alcoholic = models.BooleanField(default=False)
    is_pack_sale = models.BooleanField(default=False)
    pack_unit_ratio = models.IntegerField(default=1)

    is_voided = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    void_approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_item_voids'
    )

    def __str__(self):
        return f"{self.quantity}x {self.product_name} in {self.order.order_number}"


class Payment(models.Model):
    """
    Payment settlements supporting split payments across cash, mobile money, and customer credit.
    """
    METHOD_CASH = 'CASH'
    METHOD_MOBILE_MONEY = 'MOBILE_MONEY'
    METHOD_CREDIT = 'CREDIT'  # Stationery only
    METHOD_CHOICES = [
        (METHOD_CASH, 'Cash'),
        (METHOD_MOBILE_MONEY, 'Mobile Money'),
        (METHOD_CREDIT, 'Customer Credit (Stationery Only)'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments')
    payment_method = models.CharField(max_length=30, choices=METHOD_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True, help_text="Mobile money or credit approval ref")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.payment_method}: {self.amount} for {self.order.order_number}"


class CustomerCreditTransaction(TenantModel):
    """
    Ledger of all customer credit sales, debt repayments, and adjustments.
    """
    TYPE_SALE = 'CREDIT_SALE'
    TYPE_PAYMENT = 'PAYMENT_RECEIVED'
    TYPE_ADJUSTMENT = 'ADJUSTMENT'
    TYPE_CHOICES = [
        (TYPE_SALE, 'Credit Sale'),
        (TYPE_PAYMENT, 'Debt Repayment'),
        (TYPE_ADJUSTMENT, 'Manual Adjustment'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='credit_transactions')
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL, related_name='credit_transactions')
    transaction_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=30, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='recorded_credit_txs')
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_credit_txs')
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.transaction_type}] {self.customer.name}: TSH {self.amount}"


class Supplier(TenantModel):
    """
    Suppliers for Stationery and goods replenishment.
    """
    name = models.CharField(max_length=200, db_index=True)
    contact_person = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    balance_payable = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} (Payable: TSH {self.balance_payable})"


class PurchaseOrder(TenantModel):
    """
    Purchase recording and goods receiving with payables tracking.
    """
    STATUS_UNPAID = 'UNPAID'
    STATUS_PARTIAL = 'PARTIALLY_PAID'
    STATUS_PAID = 'PAID'
    STATUS_CHOICES = [
        (STATUS_UNPAID, 'Unpaid'),
        (STATUS_PARTIAL, 'Partially Paid'),
        (STATUS_PAID, 'Fully Paid'),
    ]

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='purchases')
    order_number = models.CharField(max_length=50, unique=True, db_index=True)
    invoice_number = models.CharField(max_length=100, blank=True)
    total_cost = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_UNPAID)
    received_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='received_purchases')
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def balance_remaining(self):
        cost = Decimal(str(self.total_cost or '0.00'))
        paid = Decimal(str(self.amount_paid or '0.00'))
        return max(Decimal('0.00'), cost - paid)

    def __str__(self):
        return f"PO #{self.order_number} - {self.supplier.name} (TSH {self.total_cost})"


class SupplierPayment(TenantModel):
    """
    Payments made to suppliers against payables.
    """
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name='payments')
    purchase_order = models.ForeignKey(
        PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name='payments'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=30, choices=[('CASH', 'Cash'), ('MOBILE_MONEY', 'Mobile Money'), ('BANK_TRANSFER', 'Bank Transfer'), ('CHEQUE', 'Cheque')])
    reference = models.CharField(max_length=100, blank=True)
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='recorded_supplier_payments')
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Supplier Payment: TSH {self.amount} to {self.supplier.name}"
