from .models import AuditLog


def get_client_ip(request):
    """Safely retrieve client IP from request."""
    if not request:
        return None
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record_audit(
    request=None,
    user=None,
    action="UNKNOWN_ACTION",
    business_unit_id=None,
    entity_type="",
    entity_id="",
    before_value=None,
    after_value=None,
    metadata=None,
):
    """
    Append an immutable audit entry into AuditLog.
    """
    effective_user = user or (request.user if request and getattr(request, 'user', None) and request.user.is_authenticated else None)
    username = effective_user.username if effective_user else "system"
    role = effective_user.role if effective_user and hasattr(effective_user, 'role') else "system"

    # Derive business unit if not explicitly supplied
    if not business_unit_id and request:
        req_unit = getattr(request, 'business_unit', None)
        if req_unit and req_unit != 'all':
            business_unit_id = getattr(req_unit, 'id', req_unit)
    if not business_unit_id and effective_user and hasattr(effective_user, 'business_unit_id'):
        business_unit_id = effective_user.business_unit_id

    ip = get_client_ip(request) if request else None

    return AuditLog.objects.create(
        business_unit_id=business_unit_id,
        user=effective_user,
        username=username,
        role=role,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id else "",
        before_value=before_value,
        after_value=after_value,
        ip_address=ip,
        metadata=metadata or {},
    )
