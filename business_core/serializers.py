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


class UserManagementSerializer(serializers.ModelSerializer):
    """
    Dedicated serializer for Owner to create and manage staff accounts and assign roles.
    Strictly enforced with IsOwner permission.
    """
    password = serializers.CharField(write_only=True, required=False, min_length=6)
    pin = serializers.CharField(write_only=True, required=False, allow_blank=True, min_length=4, max_length=6)
    has_pin = serializers.SerializerMethodField()
    business_unit_name = serializers.ReadOnlyField(source='business_unit.name')
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'role', 'role_display', 'business_unit', 'business_unit_name',
            'password', 'pin', 'has_pin', 'is_active', 'date_joined'
        ]
        read_only_fields = ['id', 'has_pin', 'date_joined', 'role_display']

    def get_has_pin(self, obj) -> bool:
        return bool(obj.pin_hash)

    def validate_pin(self, value):
        if value:
            if not value.isdigit():
                raise serializers.ValidationError("PIN must contain only numeric digits (0-9).")
            if len(value) < 4 or len(value) > 6:
                raise serializers.ValidationError("PIN must be 4 to 6 digits in length.")
        return value

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        pin = validated_data.pop('pin', None)
        
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()

        if pin:
            user.set_pin(pin)

        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        pin = validated_data.pop('pin', None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if password:
            instance.set_password(password)
        if pin is not None:
            instance.set_pin(pin)

        instance.save()
        return instance

