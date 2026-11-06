import secrets
from datetime import timedelta
from django.utils import timezone
from .models import AuditLog, User

# In-memory fast token cache: { user_id: { 'token': '...', 'expires_at': datetime, 'type': 'owner'|'self' } }
_ACTIVE_RESET_TOKENS = {}


def generate_owner_reset_token(user, generated_by_user=None):
    """
    Generates an Owner-assisted temporary reset token for an employee (e.g. RST-7K2X9P).
    Valid for 24 hours.
    """
    raw_code = secrets.token_hex(3).upper()
    token = f"RST-{raw_code}"
    expires_at = timezone.now() + timedelta(hours=24)
    
    _ACTIVE_RESET_TOKENS[user.id] = {
        'token': token,
        'expires_at': expires_at,
        'type': 'owner'
    }
    
    AuditLog.objects.create(
        user=generated_by_user,
        username=generated_by_user.username if generated_by_user else 'owner',
        role=generated_by_user.role if generated_by_user else 'owner',
        action='STAFF_RESET_TOKEN_GENERATED',
        entity_type='User',
        entity_id=str(user.id),
        metadata={
            'target_username': user.username,
            'token': token,
            'expires_at': expires_at.isoformat(),
            'type': 'owner_assisted'
        }
    )
    return token, expires_at


def generate_self_service_code(user):
    """
    Generates a 6-digit verification code for direct self-service password reset.
    Valid for 15 minutes.
    """
    code = f"{secrets.randbelow(900000) + 100000}"
    expires_at = timezone.now() + timedelta(minutes=15)
    
    _ACTIVE_RESET_TOKENS[user.id] = {
        'token': code,
        'expires_at': expires_at,
        'type': 'self'
    }
    
    AuditLog.objects.create(
        user=user,
        username=user.username,
        role=user.role,
        action='SELF_SERVICE_RESET_CODE_GENERATED',
        entity_type='User',
        entity_id=str(user.id),
        metadata={
            'code': code,
            'expires_at': expires_at.isoformat(),
            'type': 'self_service'
        }
    )
    return code, expires_at


def verify_and_consume_token(user, token, expected_type=None):
    """
    Verifies the reset token or code, checking expiry and type.
    Consumes the token upon successful verification so it cannot be reused.
    """
    if not user or not token:
        return False, "User and token are required."

    now = timezone.now()
    clean_token = str(token).strip().upper()
    
    # 1. Check in-memory store
    entry = _ACTIVE_RESET_TOKENS.get(user.id)
    if entry:
        if expected_type and entry.get('type') != expected_type:
            return False, "Token type mismatch."
        if entry['token'].strip().upper() == clean_token:
            if now <= entry['expires_at']:
                del _ACTIVE_RESET_TOKENS[user.id]
                return True, "Token verified successfully."
            else:
                del _ACTIVE_RESET_TOKENS[user.id]
                return False, "Token has expired. Please request a new reset token."

    # 2. Fallback check from AuditLog in case of process reload
    action_filter = 'STAFF_RESET_TOKEN_GENERATED' if expected_type == 'owner' else 'SELF_SERVICE_RESET_CODE_GENERATED'
    log_entry = AuditLog.objects.filter(
        action=action_filter,
        entity_id=str(user.id)
    ).order_by('-timestamp').first()

    if log_entry and log_entry.metadata:
        stored_token = log_entry.metadata.get('token') or log_entry.metadata.get('code')
        expires_str = log_entry.metadata.get('expires_at')
        if stored_token and expires_str:
            from dateutil.parser import parse as parse_date
            try:
                exp_dt = parse_date(expires_str)
                if stored_token.strip().upper() == clean_token and now <= exp_dt:
                    return True, "Token verified successfully."
            except Exception:
                pass

    return False, "Invalid or expired reset token/code. Please check your token or contact the Business Owner."


def validate_password_strength(password: str):
    """
    Validates strong password rules:
    - Minimum 8 characters
    - At least one uppercase letter (A-Z)
    - At least one lowercase letter (a-z)
    - At least one numeric digit (0-9)
    - At least one special character (!@#$%^&* etc.)
    """
    import re
    if not password:
        return False, "Password cannot be empty."
    missing = []
    if len(password) < 8:
        missing.append("at least 8 characters")
    if not re.search(r'[A-Z]', password):
        missing.append("one uppercase letter (A-Z)")
    if not re.search(r'[a-z]', password):
        missing.append("one lowercase letter (a-z)")
    if not re.search(r'[0-9]', password):
        missing.append("one number (0-9)")
    if not re.search(r'[!@#$%^&*()_+\-=\[\]{};\':"\\|,.<>\/?]', password):
        missing.append("one special character (!@#$%^&*)")
    
    if missing:
        return False, f"Password does not meet security standards. Missing: {', '.join(missing)}."
    return True, "Password is valid."

