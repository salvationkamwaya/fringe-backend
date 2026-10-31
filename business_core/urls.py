from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    login_view, me_view, logout_view, verify_manager_pin_view,
    BusinessUnitViewSet, AuditLogViewSet
)

router = DefaultRouter()
router.register(r'business-units', BusinessUnitViewSet, basename='business-unit')
router.register(r'audit-logs', AuditLogViewSet, basename='audit-log')

urlpatterns = [
    path('auth/login/', login_view, name='auth-login'),
    path('auth/me/', me_view, name='auth-me'),
    path('auth/logout/', logout_view, name='auth-logout'),
    path('auth/verify-manager-pin/', verify_manager_pin_view, name='auth-verify-pin'),
    path('', include(router.urls)),
]
