from django.db import models
from business_core.models import TenantModel


class Category(TenantModel):
    """
    Product categories scoped strictly by business unit.
    """
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100)
    icon = models.CharField(max_length=50, default='tag', blank=True)
    display_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['display_order', 'name']
        unique_together = ('business_unit', 'branch', 'slug')

    def __str__(self):
        return f"[{self.business_unit_id}] {self.name}"


class Product(TenantModel):
    """
    Unified product model with isolation per business unit.
    Includes Bar-specific fields (is_alcoholic for mandatory age verification)
    and Stationery-specific fields (is_pack, pack_unit_ratio).
    """
    category = models.ForeignKey(
        Category,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='products'
    )
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=50, blank=True, db_index=True)
    barcode = models.CharField(max_length=100, blank=True, db_index=True)
    description = models.TextField(blank=True)
    
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    selling_price = models.DecimalField(max_digits=12, decimal_places=2)
    stock_quantity = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    reorder_threshold = models.DecimalField(max_digits=12, decimal_places=2, default=5.00)
    is_active = models.BooleanField(default=True)
    image_url = models.CharField(max_length=500, blank=True)

    # Bar-specific fields
    is_alcoholic = models.BooleanField(
        default=False,
        db_index=True,
        help_text="CRITICAL: Non-bypassable age verification required before sale."
    )
    volume_ml = models.IntegerField(null=True, blank=True, help_text="Bottle/can/glass volume in ml.")

    # Stationery-specific fields
    is_pack = models.BooleanField(
        default=False,
        help_text="Indicates whether this product can be sold as a pack (e.g., box, carton, ream)."
    )
    pack_unit_ratio = models.IntegerField(
        default=1,
        help_text="Units per pack (e.g. 50 pens per box, 500 sheets per ream)."
    )
    pack_name = models.CharField(max_length=50, default='Pack', blank=True)
    unit_name = models.CharField(max_length=50, default='Piece', blank=True)
    allow_split_sale = models.BooleanField(
        default=False,
        help_text="Allow selling individual pieces from this pack with automatic stock fraction deduction."
    )
    split_unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Selling price per single unit when split from pack."
    )

    class Meta:
        ordering = ['name']

    @property
    def is_low_stock(self) -> bool:
        return self.stock_quantity <= self.reorder_threshold

    def __str__(self):
        return f"[{self.business_unit_id}] {self.name} (Stock: {self.stock_quantity})"
