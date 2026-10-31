from rest_framework.permissions import BasePermission
from .models import User


class IsOwner(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == User.ROLE_OWNER)


class IsBarStaff(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_BARTENDER)


class IsBarManagerOrOwner(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER)


class IsStationeryStaff(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STATIONERY_CASHIER)


class IsStationeryManagerOrOwner(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (User.ROLE_OWNER, User.ROLE_STATIONERY_MANAGER)


class IsManagerOrOwner(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER)


class IsStockClerkOrManager(BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.user.role in (
            User.ROLE_OWNER,
            User.ROLE_BAR_MANAGER,
            User.ROLE_STATIONERY_MANAGER,
            User.ROLE_STOCK_CLERK,
        )
