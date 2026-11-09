from django.db import models
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.hashers import make_password, check_password
from django.core.exceptions import PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder


class BusinessUnit(models.Model):
    """
    Represents an isolated business unit (e.g. 'bar', 'stationery').
    """
    id = models.CharField(max_length=20, primary_key=True)  # 'bar', 'stationery'
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=10, unique=True)
    currency = models.CharField(max_length=10, default='TSH')
    settings = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.code})"


class Branch(models.Model):
    """
    Physical branch location created dynamically by the Business Owner.
    Scoped under a BusinessUnit (e.g. multiple Bar & Lounge branches).
    """
    business_unit = models.ForeignKey(
        BusinessUnit,
        on_delete=models.CASCADE,
        related_name='branches',
        db_index=True
    )
    name = models.CharField(max_length=150, help_text="e.g., Mikocheni Lounge")
    code = models.CharField(max_length=20, unique=True, help_text="e.g., MKC-01")
    address = models.CharField(max_length=255, blank=True, help_text="Physical location / street address")
    phone = models.CharField(max_length=50, blank=True)
    manager = models.ForeignKey(
        'User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='managed_branches'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Branches'

    def __str__(self):
        return f"{self.name} ({self.code})"


class User(AbstractUser):
    """
    Custom user model supporting the 6 system roles, business unit context,
    and fast manager approval PIN hashes.
    """
    ROLE_OWNER = 'owner'
    ROLE_BAR_MANAGER = 'bar_manager'
    ROLE_BARTENDER = 'bartender'
    ROLE_STATIONERY_MANAGER = 'stationery_manager'
    ROLE_STATIONERY_CASHIER = 'stationery_cashier'
    ROLE_STOCK_CLERK = 'stock_clerk'

    ROLE_CHOICES = [
        (ROLE_OWNER, 'Business Owner'),
        (ROLE_BAR_MANAGER, 'Bar Manager'),
        (ROLE_BARTENDER, 'Bartender / Cashier'),
        (ROLE_STATIONERY_MANAGER, 'Stationery Manager'),
        (ROLE_STATIONERY_CASHIER, 'Stationery Cashier'),
        (ROLE_STOCK_CLERK, 'Stock Clerk'),
    ]

    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default=ROLE_BARTENDER, db_index=True)
    business_unit = models.ForeignKey(
        BusinessUnit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='staff',
        help_text="Primary business unit. Null for Owner."
    )
    branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='staff_members',
        help_text="Single assigned branch. Frontline staff can only ring up sales and view tabs at this branch."
    )
    assigned_units = models.ManyToManyField(
        BusinessUnit,
        blank=True,
        related_name='assigned_clerks',
        help_text="Units assigned to a Stock Clerk."
    )
    pin_hash = models.CharField(
        max_length=128,
        blank=True,
        null=True,
        help_text="Hashed 4-6 digit numeric PIN for in-person manager overrides."
    )

    def set_pin(self, raw_pin: str):
        if raw_pin:
            self.pin_hash = make_password(str(raw_pin))
        else:
            self.pin_hash = None

    def check_pin(self, raw_pin: str) -> bool:
        if not self.pin_hash or not raw_pin:
            return False
        return check_password(str(raw_pin), self.pin_hash)

    def can_access_unit(self, unit_id: str) -> bool:
        if self.role == self.ROLE_OWNER:
            return True
        if self.role == self.ROLE_STOCK_CLERK:
            if self.business_unit_id == unit_id:
                return True
            return self.assigned_units.filter(id=unit_id).exists()
        return self.business_unit_id == unit_id

    @property
    def is_owner(self) -> bool:
        return self.role == self.ROLE_OWNER

    @property
    def is_bar_staff(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_BAR_MANAGER, self.ROLE_BARTENDER)

    @property
    def is_stationery_staff(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_STATIONERY_MANAGER, self.ROLE_STATIONERY_CASHIER)

    @property
    def is_bar_manager_or_owner(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_BAR_MANAGER)

    @property
    def is_stationery_manager_or_owner(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_STATIONERY_MANAGER)

    @property
    def can_record_sales(self) -> bool:
        """
        Only frontline cashiers/bartenders may ring up sales.
        Managers and Business Owners perform oversight, inventory, reporting, and approvals.
        """
        return self.role in (self.ROLE_BARTENDER, self.ROLE_STATIONERY_CASHIER)

    @property
    def is_frontline_cashier(self) -> bool:
        return self.role in (self.ROLE_BARTENDER, self.ROLE_STATIONERY_CASHIER)

    @property
    def is_management(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_BAR_MANAGER, self.ROLE_STATIONERY_MANAGER)

    @property
    def is_manager_or_owner(self) -> bool:
        return self.role in (self.ROLE_OWNER, self.ROLE_BAR_MANAGER, self.ROLE_STATIONERY_MANAGER)

    def __str__(self):
        unit = self.business_unit_id or 'All Units'
        return f"{self.username} [{self.get_role_display()} - {unit}]"


class TenantQuerySet(models.QuerySet):
    """
    QuerySet that scopes rows strictly by business unit and branch.
    """
    def for_unit(self, unit_id: str):
        if not unit_id or unit_id == 'all':
            return self
        return self.filter(business_unit_id=unit_id)

    def for_branch(self, branch_id):
        if not branch_id or branch_id == 'all':
            return self
        return self.filter(branch_id=branch_id)

    def for_request(self, request):
        qs = self
        user = getattr(request, 'user', None)
        is_owner = getattr(user, 'role', None) == User.ROLE_OWNER

        # 1. Filter by business unit
        unit_id = getattr(request, 'business_unit', None)
        if hasattr(unit_id, 'id'):
            unit_id = unit_id.id
        if not unit_id or unit_id == 'all':
            if not is_owner and user and user.is_authenticated:
                unit_id = user.business_unit_id

        if not (is_owner and (unit_id is None or unit_id == 'all')):
            if unit_id:
                qs = qs.filter(business_unit_id=unit_id)

        # 2. Filter by branch
        if not is_owner and user and user.is_authenticated:
            # Frontline staff and local branch managers can ONLY see their assigned branch
            if user.branch_id:
                qs = qs.filter(branch_id=user.branch_id)
        else:
            # Business Owner can filter by a specific branch if selected, or 'all'
            req_branch = getattr(request, 'branch_id', None)
            if req_branch and req_branch != 'all':
                qs = qs.filter(branch_id=req_branch)

        return qs


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    pass


class TenantModel(models.Model):
    """
    Abstract base model for all business data tables requiring strict row-level isolation.
    """
    business_unit = models.ForeignKey(
        BusinessUnit,
        on_delete=models.CASCADE,
        related_name='%(class)s_items',
        db_index=True
    )
    branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='%(class)s_items',
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TenantManager()

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """
    Immutable, append-only audit trail.
    No UPDATE or DELETE is allowed under any circumstances.
    """
    business_unit = models.ForeignKey(
        BusinessUnit,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_logs',
        db_index=True
    )
    branch = models.ForeignKey(
        Branch,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_entries',
        db_index=True
    )
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='audit_entries'
    )
    username = models.CharField(max_length=150)
    role = models.CharField(max_length=50)
    action = models.CharField(max_length=100, db_index=True)
    entity_type = models.CharField(max_length=100, blank=True)
    entity_id = models.CharField(max_length=100, blank=True)
    before_value = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    after_value = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ['-timestamp']

    def save(self, *args, **kwargs):
        # Prevent updates to existing audit entries
        if self.pk and AuditLog.objects.filter(pk=self.pk).exists():
            raise PermissionDenied("Audit records are immutable and cannot be updated.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionDenied("Audit records are immutable and cannot be deleted.")

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M:%S}] {self.role}:{self.username} -> {self.action} ({self.business_unit_id or 'Global'})"
