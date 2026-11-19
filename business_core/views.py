from rest_framework import status, viewsets, permissions
from rest_framework.decorators import api_view, permission_classes, authentication_classes, action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.db import transaction
from django.utils import timezone
from django.db.models import Q
from .models import BusinessUnit, Branch, User, AuditLog
from .serializers import (
    BusinessUnitSerializer, BranchSerializer, UserSerializer,
    AuditLogSerializer, UserManagementSerializer
)
from .audit import record_audit
from .permissions import IsOwner, IsManagerOrOwner
from .tokens import (
    generate_owner_reset_token,
    generate_self_service_code,
    verify_and_consume_token,
    validate_password_strength
)

REQUIRED_UNIT_IDS = ('bar', 'stationery')


def _get_owner_setup_status(user):
    """
    Returns (requires_business_setup, requires_branch_setup, active_units_list) for the given user.
    Owners must have both 'bar' and 'stationery' active BusinessUnits initialized,
    and at least one active Branch under each of those two BusinessUnits.
    """
    if user.role == User.ROLE_OWNER:
        units = list(
            BusinessUnit.objects.filter(is_active=True).values(
                'id', 'name', 'code', 'currency', 'settings'
            )
        )
        existing_ids = {u['id'] for u in units}
        requires_business_setup = not all(uid in existing_ids for uid in REQUIRED_UNIT_IDS)

        branch_unit_ids = set(
            Branch.objects.filter(is_active=True, business_unit_id__in=REQUIRED_UNIT_IDS)
            .values_list('business_unit_id', flat=True)
            .distinct()
        )
        requires_branch_setup = not all(uid in branch_unit_ids for uid in REQUIRED_UNIT_IDS)
        return requires_business_setup, requires_branch_setup, units

    units = list(
        BusinessUnit.objects.filter(id=user.business_unit_id, is_active=True).values(
            'id', 'name', 'code', 'currency', 'settings'
        )
    )
    return False, False, units


@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def login_view(request):
    """
    Authenticate user with username or email and password.
    Returns Auth Token and user session metadata.
    """
    identifier = request.data.get('username') or request.data.get('email') or request.data.get('login')
    password = request.data.get('password')

    if not identifier or not password:
        return Response(
            {'error': 'Username or email and password are required.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Allow login with either username or email (case-insensitive lookup)
    identifier_str = str(identifier).strip()
    user_candidate = User.objects.filter(
        Q(username__iexact=identifier_str) | Q(email__iexact=identifier_str)
    ).first()

    if user_candidate:
        user = authenticate(request, username=user_candidate.username, password=password)
    else:
        user = None

    if not user:
        return Response(
            {'error': 'Invalid username or password.'},
            status=status.HTTP_401_UNAUTHORIZED
        )

    if not user.is_active:
        return Response(
            {'error': 'This account has been deactivated.'},
            status=status.HTTP_403_FORBIDDEN
        )

    token, _ = Token.objects.get_or_create(user=user)

    # Record login in audit log
    record_audit(
        request=request,
        user=user,
        action='USER_LOGIN',
        entity_type='User',
        entity_id=user.id,
        metadata={'client': 'POS_WEB'}
    )

    # Available business units and branches
    requires_business_setup, requires_branch_setup, units = _get_owner_setup_status(user)
    if user.role == User.ROLE_OWNER:
        branches = list(Branch.objects.filter(is_active=True).values('id', 'business_unit_id', 'name', 'code', 'address', 'phone'))
    else:
        if user.branch_id:
            branches = list(Branch.objects.filter(id=user.branch_id, is_active=True).values('id', 'business_unit_id', 'name', 'code', 'address', 'phone'))
        else:
            branches = []

    active_unit = user.business_unit_id if user.role != User.ROLE_OWNER else (request.headers.get('X-Business-Unit') or 'all')
    active_branch = user.branch_id if user.role != User.ROLE_OWNER else (request.headers.get('X-Branch-Id') or 'all')

    return Response({
        'token': token.key,
        'user': UserSerializer(user).data,
        'active_unit': active_unit,
        'available_units': units,
        'requires_business_setup': requires_business_setup,
        'requires_branch_setup': requires_branch_setup,
        'active_branch': active_branch,
        'branches': branches,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def me_view(request):
    """
    Get current user profile and session unit context.
    """
    requires_business_setup, requires_branch_setup, units = _get_owner_setup_status(request.user)
    branches = list(Branch.objects.filter(is_active=True).values('id', 'business_unit_id', 'name', 'code', 'address', 'phone'))
    return Response({
        'user': UserSerializer(request.user).data,
        'active_unit': getattr(request, 'business_unit', request.user.business_unit_id or 'all'),
        'available_units': units,
        'requires_business_setup': requires_business_setup,
        'requires_branch_setup': requires_branch_setup,
        'active_branch': getattr(request, 'branch_id', getattr(request.user, 'branch_id', None)),
        'branches': branches,
        'is_owner': request.user.is_owner,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def logout_view(request):
    """
    Invalidate the user's active session token and log event.
    Supports audit tracking for manual logouts vs. automated idle timeout locks.
    """
    reason = request.data.get('reason', 'manual')
    action = 'USER_IDLE_LOGOUT' if reason == 'idle_timeout' else 'USER_LOGOUT'
    metadata = {
        'reason': 'Automated idle session timeout' if reason == 'idle_timeout' else 'Manual session sign-out'
    }

    record_audit(
        request=request,
        action=action,
        entity_type='User',
        entity_id=request.user.id,
        metadata=metadata
    )
    Token.objects.filter(user=request.user).delete()
    return Response({'message': 'Logged out successfully.'})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def verify_manager_pin_view(request):
    """
    Fast in-person Manager/Owner dual-control authorization.
    Verifies a 4-6 digit numeric PIN to approve sensitive operations:
    - Voiding items / orders
    - Price overrides / Custom discounts
    - Reopening settled tabs
    - Stock adjustments
    - Customer credit approvals
    """
    raw_pin = request.data.get('pin')
    action_type = request.data.get('action', 'MANAGER_OVERRIDE')
    context_data = request.data.get('context', {})

    if not raw_pin:
        return Response({'approved': False, 'error': 'Manager PIN is required.'}, status=status.HTTP_400_BAD_REQUEST)

    # Determine eligible managers:
    # 1. Check if the current user is a manager/owner and entered their own PIN
    # 2. Check if another active manager/owner for this business unit entered their PIN
    current_unit = getattr(request, 'business_unit', request.user.business_unit_id)

    # Build candidate managers
    candidates = User.objects.filter(is_active=True).exclude(pin_hash__isnull=True).exclude(pin_hash='')
    if current_unit == 'bar':
        candidates = candidates.filter(role__in=[User.ROLE_OWNER, User.ROLE_BAR_MANAGER])
    elif current_unit == 'stationery':
        candidates = candidates.filter(role__in=[User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER])
    else:
        candidates = candidates.filter(role=User.ROLE_OWNER)

    approver = None
    for manager in candidates:
        if manager.check_pin(raw_pin):
            approver = manager
            break

    if not approver:
        # Log failed approval attempt
        record_audit(
            request=request,
            action='PIN_APPROVAL_FAILED',
            entity_type='Approval',
            metadata={'action_requested': action_type, 'context': context_data}
        )
        return Response({
            'approved': False,
            'error': 'Invalid manager PIN or insufficient permissions for this business unit.'
        }, status=status.HTTP_403_FORBIDDEN)

    # Record successful approval in audit log
    record_audit(
        request=request,
        user=approver,
        action=f"APPROVAL_{action_type}",
        entity_type='Approval',
        entity_id=approver.id,
        metadata={
            'approved_for_user': request.user.username,
            'action_type': action_type,
            'context': context_data
        }
    )

    return Response({
        'approved': True,
        'approver_id': approver.id,
        'approver_name': approver.get_full_name() or approver.username,
        'approver_role': approver.get_role_display(),
        'timestamp': timezone.now().isoformat(),
    })


class BusinessUnitViewSet(viewsets.ModelViewSet):
    """
    List, create, or initialize active business units ('bar' and 'stationery').
    Only the Business Owner may create, update, or initialize business units.
    """
    queryset = BusinessUnit.objects.filter(is_active=True)
    serializer_class = BusinessUnitSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.role == User.ROLE_OWNER:
            return BusinessUnit.objects.filter(is_active=True)
        return BusinessUnit.objects.filter(id=user.business_unit_id, is_active=True)

    def perform_create(self, serializer):
        if self.request.user.role != User.ROLE_OWNER:
            self.permission_denied(self.request, message="Only the Business Owner can create business units.")
        unit = serializer.save()
        record_audit(
            request=self.request,
            action='BUSINESS_UNIT_CREATED',
            entity_type='BusinessUnit',
            entity_id=unit.id,
            after_value={'id': unit.id, 'name': unit.name, 'code': unit.code, 'currency': unit.currency},
        )

    def perform_update(self, serializer):
        if self.request.user.role != User.ROLE_OWNER:
            self.permission_denied(self.request, message="Only the Business Owner can update business units.")
        unit = serializer.save()
        record_audit(
            request=self.request,
            action='BUSINESS_UNIT_UPDATED',
            entity_type='BusinessUnit',
            entity_id=unit.id,
            after_value={'id': unit.id, 'name': unit.name, 'code': unit.code, 'currency': unit.currency},
        )

    def perform_destroy(self, instance):
        self.permission_denied(self.request, message="Core business units cannot be deleted.")

    @action(detail=False, methods=['post'], url_path='setup-initial')
    def setup_initial(self, request):
        """
        Atomically initialize both required BusinessUnits ('bar' and 'stationery')
        for the Business Owner on first login.
        """
        if request.user.role != User.ROLE_OWNER:
            return Response(
                {'error': 'Only the Business Owner can initialize business units.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        payload = {}
        for unit_id in REQUIRED_UNIT_IDS:
            raw = request.data.get(unit_id)
            if not isinstance(raw, dict):
                return Response(
                    {'error': f"Configuration for '{unit_id}' business unit is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            name = (raw.get('name') or '').strip()
            code = (raw.get('code') or '').strip().upper()
            currency = (raw.get('currency') or 'TSH').strip().upper()[:10] or 'TSH'
            settings_data = raw.get('settings') if isinstance(raw.get('settings'), dict) else {}

            if not name:
                return Response(
                    {'error': f"Business name for '{unit_id}' is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not code or len(code) > 10:
                return Response(
                    {'error': f"Business code for '{unit_id}' must be 1 to 10 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            payload[unit_id] = {
                'name': name[:100],
                'code': code,
                'currency': currency,
                'settings': settings_data,
            }

        if payload['bar']['code'] == payload['stationery']['code']:
            return Response(
                {'error': 'Bar and Stationery business codes must be unique.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            created_or_updated = []
            for unit_id in REQUIRED_UNIT_IDS:
                data = payload[unit_id]
                unit, _ = BusinessUnit.objects.update_or_create(
                    id=unit_id,
                    defaults={
                        'name': data['name'],
                        'code': data['code'],
                        'currency': data['currency'],
                        'settings': data['settings'],
                        'is_active': True,
                    },
                )
                created_or_updated.append(unit)

            record_audit(
                request=request,
                action='BUSINESS_UNITS_INITIALIZED',
                entity_type='BusinessUnit',
                entity_id='bar,stationery',
                after_value={
                    uid: {
                        'name': payload[uid]['name'],
                        'code': payload[uid]['code'],
                        'currency': payload[uid]['currency'],
                    }
                    for uid in REQUIRED_UNIT_IDS
                },
            )

        return Response(
            {
                'business_units': BusinessUnitSerializer(created_or_updated, many=True).data,
                'requires_business_setup': False,
            },
            status=status.HTTP_200_OK,
        )


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Immutable audit log viewer.
    - Owner can see all or filter by unit.
    - Managers see only their respective unit's logs.
    - Cashiers/Bartenders have zero access.
    """
    serializer_class = AuditLogSerializer
    permission_classes = [IsManagerOrOwner]

    def get_queryset(self):
        user = self.request.user
        qs = AuditLog.objects.all()

        if user.role == User.ROLE_OWNER:
            unit = getattr(self.request, 'business_unit', 'all')
            if unit and unit != 'all':
                qs = qs.filter(business_unit_id=unit)
        elif user.role == User.ROLE_BAR_MANAGER:
            qs = qs.filter(business_unit_id='bar')
        elif user.role == User.ROLE_STATIONERY_MANAGER:
            qs = qs.filter(business_unit_id='stationery')
        else:
            qs = qs.none()

        action = self.request.query_params.get('action')
        if action:
            qs = qs.filter(action__icontains=action)

        return qs[:200]


class UserManagementViewSet(viewsets.ModelViewSet):
    """
    Staff and Role management.
    NON-NEGOTIABLE ARCHITECTURAL RULE:
    Strictly restricted to Business Owner.
    Only the Owner is authorized to create staff accounts, assign/change roles,
    configure approval PINs, or activate/suspend employee accounts.
    """
    serializer_class = UserManagementSerializer
    permission_classes = [IsOwner]

    def get_queryset(self):
        return User.objects.all().order_by('role', 'username')

    def perform_create(self, serializer):
        user = serializer.save()
        record_audit(
            request=self.request,
            action='STAFF_CREATED',
            entity_type='User',
            entity_id=user.id,
            after_value={
                'username': user.username,
                'role': user.role,
                'unit': user.business_unit_id,
                'branch': user.branch_id,
            },
            metadata={'created_by': 'Owner'}
        )

    def perform_update(self, serializer):
        before = self.get_object()
        before_role = before.role
        before_unit = before.business_unit_id
        before_branch = before.branch_id
        user = serializer.save()
        action_name = 'STAFF_BRANCH_REASSIGNED' if before_branch != user.branch_id else 'STAFF_ROLE_ASSIGNED'
        record_audit(
            request=self.request,
            action=action_name,
            entity_type='User',
            entity_id=user.id,
            before_value={'role': before_role, 'unit': before_unit, 'branch': before_branch},
            after_value={'role': user.role, 'unit': user.business_unit_id, 'branch': user.branch_id},
            metadata={'updated_by': 'Owner'}
        )

    @action(detail=True, methods=['post'])
    def set_pin(self, request, pk=None):
        """Owner sets or resets an employee's authorization PIN."""
        user = self.get_object()
        raw_pin = request.data.get('pin')
        if not raw_pin or not str(raw_pin).isdigit() or len(str(raw_pin)) < 4 or len(str(raw_pin)) > 6:
            return Response({'error': 'PIN must be a 4 to 6 digit numeric code.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_pin(str(raw_pin))
        user.save(update_fields=['pin_hash'])

        record_audit(
            request=request,
            action='STAFF_PIN_SET',
            entity_type='User',
            entity_id=user.id,
            metadata={'target_user': user.username, 'set_by': 'Owner'}
        )
        return Response({'message': f"PIN successfully configured for {user.username}."})

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Owner toggles active/suspended status of an employee."""
        user = self.get_object()
        if user == request.user:
            return Response({'error': 'Owner account cannot be deactivated.'}, status=status.HTTP_400_BAD_REQUEST)

        user.is_active = not user.is_active
        user.save(update_fields=['is_active'])

        action_name = 'STAFF_ACTIVATED' if user.is_active else 'STAFF_DEACTIVATED'
        record_audit(
            request=request,
            action=action_name,
            entity_type='User',
            entity_id=user.id,
            after_value={'is_active': user.is_active},
            metadata={'target_user': user.username, 'toggled_by': 'Owner'}
        )
        return Response({
            'message': f"Account {user.username} is now {'Active' if user.is_active else 'Suspended'}.",
            'is_active': user.is_active
        })

    @action(detail=True, methods=['post'])
    def generate_reset_token(self, request, pk=None):
        """
        Owner generates a temporary password reset token for an employee.
        The employee uses this token to set a new password.
        """
        user = self.get_object()
        token, expires_at = generate_owner_reset_token(user, generated_by_user=request.user)
        return Response({
            'message': f"Temporary reset token generated for @{user.username}.",
            'username': user.username,
            'token': token,
            'expires_at': expires_at.isoformat()
        })


@api_view(['GET', 'PATCH'])
@permission_classes([permissions.IsAuthenticated])
def profile_view(request):
    """
    Get or update the current user's profile information.
    """
    user = request.user
    if request.method == 'GET':
        serializer = UserSerializer(user)
        return Response(serializer.data)

    elif request.method == 'PATCH':
        first_name = request.data.get('first_name', user.first_name)
        last_name = request.data.get('last_name', user.last_name)
        email = request.data.get('email', user.email)

        user.first_name = first_name
        user.last_name = last_name
        user.email = email
        user.save(update_fields=['first_name', 'last_name', 'email'])

        record_audit(
            request=request,
            action='USER_PROFILE_UPDATED',
            entity_type='User',
            entity_id=user.id,
            after_value={'first_name': user.first_name, 'last_name': user.last_name, 'email': user.email}
        )
        return Response({
            'message': 'Profile updated successfully.',
            'user': UserSerializer(user).data
        })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def change_password_view(request):
    """
    Update password for the logged-in user.
    Requires current password verification and enforces strong password standards.
    """
    user = request.user
    current_password = request.data.get('current_password')
    new_password = request.data.get('new_password')
    confirm_password = request.data.get('confirm_password')

    if not current_password or not new_password or not confirm_password:
        return Response({'error': 'Current password, new password, and confirmation are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.check_password(current_password):
        return Response({'error': 'Current password does not match.'}, status=status.HTTP_400_BAD_REQUEST)

    if new_password != confirm_password:
        return Response({'error': 'New password and confirmation do not match.'}, status=status.HTTP_400_BAD_REQUEST)

    if current_password == new_password:
        return Response({'error': 'New password must be different from current password.'}, status=status.HTTP_400_BAD_REQUEST)

    is_valid, msg = validate_password_strength(new_password)
    if not is_valid:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()

    # Refresh auth token
    Token.objects.filter(user=user).delete()
    new_token = Token.objects.create(user=user)

    record_audit(
        request=request,
        action='USER_PASSWORD_CHANGED',
        entity_type='User',
        entity_id=user.id
    )

    return Response({
        'message': 'Password successfully changed and secured!',
        'token': new_token.key
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def change_pin_view(request):
    """
    Update authorization PIN for the logged-in manager or owner.
    Verifies current password for authentication.
    """
    user = request.user
    current_password = request.data.get('current_password')
    pin = request.data.get('pin')
    confirm_pin = request.data.get('confirm_pin')

    if not current_password or not pin:
        return Response({'error': 'Current password and new PIN are required.'}, status=status.HTTP_400_BAD_REQUEST)

    if not user.check_password(current_password):
        return Response({'error': 'Current password verification failed.'}, status=status.HTTP_400_BAD_REQUEST)

    if not str(pin).isdigit() or len(str(pin)) < 4 or len(str(pin)) > 6:
        return Response({'error': 'PIN must be a 4 to 6 digit numeric code.'}, status=status.HTTP_400_BAD_REQUEST)

    if confirm_pin and str(pin) != str(confirm_pin):
        return Response({'error': 'PIN confirmation does not match.'}, status=status.HTTP_400_BAD_REQUEST)

    user.set_pin(str(pin))
    user.save(update_fields=['pin_hash'])

    record_audit(
        request=request,
        action='USER_PIN_CHANGED',
        entity_type='User',
        entity_id=user.id
    )

    return Response({'message': 'Authorization PIN successfully updated!'})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def forgot_password_request_view(request):
    """
    Self-service step 1: User requests password reset by providing username or email.
    Generates a secure 6-digit verification code.
    """
    identifier = request.data.get('identifier') or request.data.get('username') or request.data.get('email')
    if not identifier:
        return Response({'error': 'Please enter your username or registered email address.'}, status=status.HTTP_400_BAD_REQUEST)

    identifier_str = str(identifier).strip()
    user = User.objects.filter(
        Q(username__iexact=identifier_str) | Q(email__iexact=identifier_str)
    ).first()

    if not user:
        return Response({'error': f"No account found matching '{identifier_str}'."}, status=status.HTTP_404_NOT_FOUND)

    if not user.is_active:
        return Response({'error': 'This account has been deactivated. Please contact the Business Owner.'}, status=status.HTTP_403_FORBIDDEN)

    code, expires_at = generate_self_service_code(user)

    # In production with SMTP, send via email. Here, we also return it for immediate verification.
    return Response({
        'message': f"Verification code generated for @{user.username}.",
        'username': user.username,
        'code': code,
        'expires_at': expires_at.isoformat()
    })


@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def forgot_password_confirm_view(request):
    """
    Self-service step 2: User provides 6-digit verification code and sets new password.
    """
    identifier = request.data.get('identifier') or request.data.get('username')
    code = request.data.get('code')
    new_password = request.data.get('new_password')
    confirm_password = request.data.get('confirm_password')

    if not identifier or not code or not new_password:
        return Response({'error': 'Username, verification code, and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    identifier_str = str(identifier).strip()
    user = User.objects.filter(
        Q(username__iexact=identifier_str) | Q(email__iexact=identifier_str)
    ).first()

    if not user:
        return Response({'error': 'Account not found.'}, status=status.HTTP_404_NOT_FOUND)

    is_verified, msg = verify_and_consume_token(user, code, expected_type='self')
    if not is_verified:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    if new_password != confirm_password:
        return Response({'error': 'New password and confirmation do not match.'}, status=status.HTTP_400_BAD_REQUEST)

    is_valid, pwd_msg = validate_password_strength(new_password)
    if not is_valid:
        return Response({'error': pwd_msg}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()

    # Invalidate old tokens
    Token.objects.filter(user=user).delete()

    record_audit(
        request=request,
        user=user,
        action='PASSWORD_RESET_SELF_SERVICE',
        entity_type='User',
        entity_id=user.id
    )

    return Response({'message': f"Password for @{user.username} has been successfully reset! You can now sign in."})


@api_view(['POST'])
@authentication_classes([])
@permission_classes([permissions.AllowAny])
def reset_password_with_token_view(request):
    """
    Owner-assisted reset: Employee enters username, the temporary token given by Business Owner,
    and sets their new password.
    """
    username = request.data.get('username')
    token = request.data.get('token')
    new_password = request.data.get('new_password')
    confirm_password = request.data.get('confirm_password')

    if not username or not token or not new_password:
        return Response({'error': 'Username, Owner Reset Token, and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)

    username_str = str(username).strip()
    user = User.objects.filter(
        Q(username__iexact=username_str) | Q(email__iexact=username_str)
    ).first()

    if not user:
        return Response({'error': f"User '@{username_str}' not found."}, status=status.HTTP_404_NOT_FOUND)

    is_verified, msg = verify_and_consume_token(user, token, expected_type='owner')
    if not is_verified:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    if new_password != confirm_password:
        return Response({'error': 'New password and confirmation do not match.'}, status=status.HTTP_400_BAD_REQUEST)

    is_valid, pwd_msg = validate_password_strength(new_password)
    if not is_valid:
        return Response({'error': pwd_msg}, status=status.HTTP_400_BAD_REQUEST)

    user.set_password(new_password)
    user.save()

    # Invalidate old auth tokens
    Token.objects.filter(user=user).delete()

    record_audit(
        request=request,
        user=user,
        action='PASSWORD_RESET_VIA_OWNER_TOKEN',
        entity_type='User',
        entity_id=user.id,
        metadata={'reset_by': 'Owner-Assisted Token'}
    )

    return Response({'message': f"Password for @{user.username} has been successfully updated using Owner Token! You can now sign in."})


class BranchViewSet(viewsets.ModelViewSet):
    """
    CRUD for physical business branches dynamically managed by the Business Owner.
    - Owner can create, edit, list, and deactivate branches.
    - Frontline staff and managers can list active branches.
    """
    serializer_class = BranchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Branch.objects.all()
        unit_id = getattr(self.request, 'business_unit', None)
        if unit_id and unit_id != 'all':
            qs = qs.filter(business_unit_id=unit_id)
        if self.request.user.role != User.ROLE_OWNER:
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        if user.role != User.ROLE_OWNER:
            self.permission_denied(self.request, message="Only the Business Owner can register new branches.")

        unit_id = self.request.data.get('business_unit') or getattr(self.request, 'business_unit', 'bar')
        if unit_id == 'all' or not unit_id:
            unit_id = 'bar'

        if not BusinessUnit.objects.filter(id=unit_id, is_active=True).exists():
            raise ValidationError({
                'business_unit': f"Business unit '{unit_id}' has not been initialized yet. Please complete business setup first."
            })

        branch = serializer.save(business_unit_id=unit_id)
        record_audit(
            request=self.request,
            action='BRANCH_CREATED',
            entity_type='Branch',
            entity_id=branch.id,
            after_value={'name': branch.name, 'code': branch.code, 'address': branch.address, 'business_unit': unit_id}
        )

    def perform_update(self, serializer):
        user = self.request.user
        if user.role != User.ROLE_OWNER:
            self.permission_denied(self.request, message="Only the Business Owner can update branch settings.")

        old_instance = self.get_object()
        before_val = {'name': old_instance.name, 'code': old_instance.code, 'is_active': old_instance.is_active}
        branch = serializer.save()
        record_audit(
            request=self.request,
            action='BRANCH_UPDATED',
            entity_type='Branch',
            entity_id=branch.id,
            before_value=before_val,
            after_value={'name': branch.name, 'code': branch.code, 'is_active': branch.is_active}
        )

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        """Toggle active / deactivated status of a branch (Owner only)."""
        if request.user.role != User.ROLE_OWNER:
            return Response({'error': 'Only the Business Owner can deactivate branches.'}, status=status.HTTP_403_FORBIDDEN)
        branch = self.get_object()
        branch.is_active = not branch.is_active
        branch.save(update_fields=['is_active', 'updated_at'])
        action_name = 'BRANCH_ACTIVATED' if branch.is_active else 'BRANCH_DEACTIVATED'
        record_audit(request=request, action=action_name, entity_type='Branch', entity_id=branch.id)
        return Response({
            'message': f"Branch '{branch.name}' is now {'Active' if branch.is_active else 'Deactivated'}.",
            'is_active': branch.is_active
        })

    @action(detail=False, methods=['post'], url_path='setup-initial')
    def setup_initial(self, request):
        """
        Atomically create initial branches for 'bar' and 'stationery' business units
        so the Business Owner has at least one branch per business before adding products.
        """
        if request.user.role != User.ROLE_OWNER:
            return Response(
                {'error': 'Only the Business Owner can initialize branches.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        existing_unit_ids = set(
            BusinessUnit.objects.filter(id__in=REQUIRED_UNIT_IDS, is_active=True).values_list('id', flat=True)
        )
        if not all(uid in existing_unit_ids for uid in REQUIRED_UNIT_IDS):
            return Response(
                {'error': 'Both Bar and Stationery business units must be created before adding branches.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        already_branched = set(
            Branch.objects.filter(is_active=True, business_unit_id__in=REQUIRED_UNIT_IDS)
            .values_list('business_unit_id', flat=True)
            .distinct()
        )

        to_create = {}
        seen_codes = set()
        for unit_id in REQUIRED_UNIT_IDS:
            raw = request.data.get(unit_id)
            if not raw and unit_id in already_branched:
                continue
            if not isinstance(raw, dict):
                return Response(
                    {'error': f"A branch configuration for '{unit_id}' is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            name = (raw.get('name') or '').strip()
            code = (raw.get('code') or '').strip().upper()
            address = (raw.get('address') or '').strip()[:255]
            phone = (raw.get('phone') or '').strip()[:50]

            if not name:
                return Response(
                    {'error': f"Branch name for '{unit_id}' is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not code or len(code) > 20:
                return Response(
                    {'error': f"Branch code for '{unit_id}' must be 1 to 20 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if code in seen_codes:
                return Response(
                    {'error': 'Bar and Stationery branch codes must be unique.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if Branch.objects.filter(code__iexact=code).exists():
                return Response(
                    {'error': f"Branch code '{code}' is already in use."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            seen_codes.add(code)
            to_create[unit_id] = {
                'name': name[:150],
                'code': code,
                'address': address,
                'phone': phone,
            }

        with transaction.atomic():
            for unit_id, data in to_create.items():
                branch = Branch.objects.create(
                    business_unit_id=unit_id,
                    name=data['name'],
                    code=data['code'],
                    address=data['address'],
                    phone=data['phone'],
                    is_active=True,
                )
                record_audit(
                    request=request,
                    action='BRANCH_CREATED',
                    entity_type='Branch',
                    entity_id=branch.id,
                    after_value={
                        'name': branch.name,
                        'code': branch.code,
                        'address': branch.address,
                        'business_unit': unit_id,
                    },
                )

        all_branches = list(
            Branch.objects.filter(is_active=True).values(
                'id', 'business_unit_id', 'name', 'code', 'address', 'phone'
            )
        )
        return Response(
            {
                'branches': all_branches,
                'requires_branch_setup': False,
            },
            status=status.HTTP_201_CREATED,
        )

