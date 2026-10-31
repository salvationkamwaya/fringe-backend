from rest_framework import status, viewsets, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.authtoken.models import Token
from django.contrib.auth import authenticate
from django.utils import timezone
from .models import BusinessUnit, User, AuditLog
from .serializers import BusinessUnitSerializer, UserSerializer, AuditLogSerializer
from .audit import record_audit
from .permissions import IsOwner, IsManagerOrOwner


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def login_view(request):
    """
    Authenticate user with username and password.
    Returns Auth Token and user session metadata.
    """
    username = request.data.get('username')
    password = request.data.get('password')

    if not username or not password:
        return Response(
            {'error': 'Username and password are required.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    user = authenticate(request, username=username, password=password)
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

    # Available business units
    if user.role == User.ROLE_OWNER:
        units = list(BusinessUnit.objects.filter(is_active=True).values('id', 'name', 'code'))
    else:
        units = list(BusinessUnit.objects.filter(id=user.business_unit_id).values('id', 'name', 'code'))

    return Response({
        'token': token.key,
        'user': UserSerializer(user).data,
        'active_unit': getattr(request, 'business_unit', user.business_unit_id or 'all'),
        'available_units': units,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def me_view(request):
    """
    Get current user profile and session unit context.
    """
    return Response({
        'user': UserSerializer(request.user).data,
        'active_unit': getattr(request, 'business_unit', request.user.business_unit_id or 'all'),
        'is_owner': request.user.is_owner,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def logout_view(request):
    """
    Invalidate the user's active session token and log event.
    """
    record_audit(
        request=request,
        action='USER_LOGOUT',
        entity_type='User',
        entity_id=request.user.id
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


class BusinessUnitViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List active business units.
    """
    queryset = BusinessUnit.objects.filter(is_active=True)
    serializer_class = BusinessUnitSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.role == User.ROLE_OWNER:
            return BusinessUnit.objects.filter(is_active=True)
        return BusinessUnit.objects.filter(id=user.business_unit_id, is_active=True)


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
