from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    login_view, me_view, logout_view, verify_manager_pin_view,
    profile_view, change_password_view, change_pin_view,
    forgot_password_request_view, forgot_password_confirm_view,
    reset_password_with_token_view,
    BusinessUnitViewSet, AuditLogViewSet, UserManagementViewSet, BranchViewSet
)

router = DefaultRouter()
router.register(r'business-units', BusinessUnitViewSet, basename='business-unit')
router.register(r'branches', BranchViewSet, basename='branch')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')
router.register(r'users', UserManagementViewSet, basename='user')

urlpatterns = [
    path('auth/login/', login_view, name='auth-login'),
    path('auth/me/', me_view, name='auth-me'),
    path('auth/logout/', logout_view, name='auth-logout'),
    path('auth/verify-manager-pin/', verify_manager_pin_view, name='auth-verify-pin'),
    path('auth/profile/', profile_view, name='auth-profile'),
    path('auth/change-password/', change_password_view, name='auth-change-password'),
    path('auth/change-pin/', change_pin_view, name='auth-change-pin'),
    path('auth/forgot-password/request/', forgot_password_request_view, name='auth-forgot-password-request'),
    path('auth/forgot-password/confirm/', forgot_password_confirm_view, name='auth-forgot-password-confirm'),
    path('auth/reset-password-with-token/', reset_password_with_token_view, name='auth-reset-password-with-token'),
    path('', include(router.urls)),
]
