import uuid
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
        return sum(order.final_amount for order in self.orders.filter(status='COMPLETED'))

    def __str__(self):
        return f"Tab '{self.tab_name}' [{self.status}]"


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
    """
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('business_catalog.Product', on_delete=models.PROTECT, related_name='order_items')
    product_name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(max_digits=12, decimal_places=2, default=1.00)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    is_alcoholic = models.BooleanField(default=False)

    is_voided = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=200, blank=True)
    void_approved_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name='approved_item_voids'
    )

    def __str__(self):
        return f"{self.quantity}x {self.product_name} in {self.order.order_number}"


class Payment(models.Model):
    """
    Payment settlements supporting split payments across cash and mobile money.
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
    reference = models.CharField(max_length=100, blank=True, help_text="Mobile money transaction ref")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.payment_method}: {self.amount} for {self.order.order_number}"
