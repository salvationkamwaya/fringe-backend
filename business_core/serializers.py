from rest_framework import serializers
from .models import BusinessUnit, User, AuditLog


class BusinessUnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessUnit
        fields = ['id', 'name', 'code', 'currency', 'settings', 'is_active', 'created_at']


class UserSerializer(serializers.ModelSerializer):
    has_pin = serializers.SerializerMethodField()
    business_unit_name = serializers.ReadOnlyField(source='business_unit.name')

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'role', 'business_unit', 'business_unit_name', 'is_active',
            'has_pin', 'date_joined'
        ]
        read_only_fields = ['id', 'has_pin', 'date_joined']

    def get_has_pin(self, obj) -> bool:
        return bool(obj.pin_hash)


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            'id', 'business_unit', 'user', 'username', 'role',
            'action', 'entity_type', 'entity_id',
            'before_value', 'after_value', 'ip_address',
            'timestamp', 'metadata'
        ]
