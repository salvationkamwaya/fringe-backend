from django.http import Http404
from rest_framework.authentication import TokenAuthentication
from .audit import record_audit


class TenantMiddleware:
    """
    Middleware that enforces multi-tenant scoping and row-level isolation.
    - Resolves authenticated user (including DRF token auth).
    - Injects request.business_unit into request context.
    - Locks staff sessions strictly to their assigned business unit.
    - Returns 404 on any attempt to reach resources across business boundaries.
    - Audits Owner cross-unit views and business switches.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.token_auth = TokenAuthentication()

    def __call__(self, request):
        # 1. Resolve user if unauthenticated in Django session but Token or forced user provided
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            forced_user = getattr(request, '_force_auth_user', None)
            if forced_user and forced_user.is_authenticated:
                request.user = forced_user
            else:
                try:
                    auth_result = self.token_auth.authenticate(request)
                    if auth_result:
                        request.user, request.auth = auth_result
                except Exception:
                    pass  # Invalid token will be caught by DRF permission classes

        # 2. Extract requested business context from headers or user
        unit_header = request.headers.get('X-Business-Unit') or request.META.get('HTTP_X_BUSINESS_UNIT')
        branch_header = request.headers.get('X-Branch-Id') or request.META.get('HTTP_X_BRANCH_ID')
        user = getattr(request, 'user', None)

        if user and user.is_authenticated:
            if user.role == 'owner':
                # Owner can view 'bar', 'stationery', or 'all'
                if unit_header in ('bar', 'stationery'):
                    request.business_unit = unit_header
                else:
                    request.business_unit = 'all'

                # Owner can dynamically view any branch or 'all'
                if branch_header and branch_header != 'all':
                    request.branch_id = branch_header
                else:
                    request.branch_id = 'all'
                
                # Check for explicit business switcher event
                session_last_unit = request.session.get('owner_last_business_unit')
                if unit_header and unit_header != session_last_unit:
                    request.session['owner_last_business_unit'] = unit_header
                    record_audit(
                        request=request,
                        action='BUSINESS_SWITCHED',
                        entity_type='BusinessUnit',
                        entity_id=unit_header,
                        before_value={'unit': session_last_unit},
                        after_value={'unit': unit_header},
                        metadata={'scope': 'Owner Switcher'}
                    )
            elif user.role == 'stock_clerk':
                # Stock clerk can be assigned to one or both units
                if unit_header and user.can_access_unit(unit_header):
                    request.business_unit = unit_header
                else:
                    request.business_unit = user.business_unit_id
                request.branch_id = user.branch_id
            else:
                # All other roles are strictly locked to user.business_unit_id
                if unit_header and unit_header != user.business_unit_id:
                    # Non-owner attempting to access outside their unit returns 404
                    raise Http404("Resource not found")
                
                # Non-owner staff are permanently locked to their assigned branch
                if branch_header and user.branch_id and str(branch_header) != str(user.branch_id):
                    raise Http404("Resource not found")

                request.business_unit = user.business_unit_id
                request.branch_id = user.branch_id
        else:
            # Anonymous / pre-auth requests
            request.business_unit = unit_header if unit_header in ('bar', 'stationery') else None
            request.branch_id = branch_header if branch_header else None

        return self.get_response(request)
