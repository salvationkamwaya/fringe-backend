from decimal import Decimal
from django.http import Http404
from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import Q
from business_core.models import User
from .models import Category, Product
from .serializers import CategorySerializer, ProductSerializer
from business_core.audit import record_audit


class CategoryViewSet(viewsets.ModelViewSet):
    """
    CRUD for product categories scoped by active business unit.
    """
    serializer_class = CategorySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Category.objects.for_request(self.request)

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_manager_or_owner:
            self.permission_denied(self.request, message="Only managers or owner can create categories.")
        
        # Enforce unit from request or user
        unit_id = getattr(self.request, 'business_unit', user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = user.business_unit_id or 'bar'

        name = serializer.validated_data.get('name', '').strip()
        slug = serializer.validated_data.get('slug')
        if not slug:
            from django.utils.text import slugify
            slug = slugify(name)
            if not slug:
                slug = f"cat-{Category.objects.count() + 1}"

        # Prevent duplicate slug collisions in the same business unit
        existing = Category.objects.filter(business_unit_id=unit_id, slug=slug).first()
        if existing:
            from rest_framework.exceptions import ValidationError
            raise ValidationError({'name': [f"A category '{existing.name}' already exists in this business unit."]})
            
        instance = serializer.save(business_unit_id=unit_id, slug=slug)
        record_audit(
            request=self.request,
            action='CATEGORY_CREATED',
            entity_type='Category',
            entity_id=instance.id,
            after_value={'name': instance.name, 'slug': instance.slug}
        )


class ProductViewSet(viewsets.ModelViewSet):
    """
    Catalog products with strict row-level isolation.
    - Cashiers/Bartenders: Read-only access to their unit's products.
    - Managers/Owner: Can create/update products and update prices.
    - Attempting to access cross-unit items returns 404.
    """
    serializer_class = ProductSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Product.objects.for_request(self.request).filter(is_active=True)

        category_id = self.request.query_params.get('category')
        if category_id:
            qs = qs.filter(category_id=category_id)

        is_alcoholic = self.request.query_params.get('is_alcoholic')
        if is_alcoholic is not None:
            qs = qs.filter(is_alcoholic=is_alcoholic.lower() in ('true', '1', 't'))

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(name__icontains=search) |
                Q(sku__icontains=search) |
                Q(barcode__icontains=search)
            )

        return qs

    def get_object(self):
        obj = super().get_object()
        req_unit = getattr(self.request, 'business_unit', None)
        user = self.request.user

        if user.role != 'owner' and req_unit and obj.business_unit_id != req_unit:
            # Enforce 404 on cross-unit access
            raise Http404("Product not found.")
        return obj

    def perform_create(self, serializer):
        user = self.request.user
        if not user.is_manager_or_owner:
            self.permission_denied(self.request, message="Only managers or owner can add products.")

        unit_id = getattr(self.request, 'business_unit', user.business_unit_id)
        if unit_id == 'all' or not unit_id:
            unit_id = user.business_unit_id or 'bar'

        instance = serializer.save(business_unit_id=unit_id)
        record_audit(
            request=self.request,
            action='PRODUCT_CREATED',
            entity_type='Product',
            entity_id=instance.id,
            after_value={'name': instance.name, 'selling_price': str(instance.selling_price)}
        )

    def perform_update(self, serializer):
        user = self.request.user
        if not user.is_manager_or_owner:
            self.permission_denied(self.request, message="Only managers or owner can update products or change prices.")

        old_instance = self.get_object()
        old_price = str(old_instance.selling_price)
        old_stock = str(old_instance.stock_quantity)

        instance = serializer.save()

        # Audit price change if modified
        if str(instance.selling_price) != old_price:
            record_audit(
                request=self.request,
                action='PRICE_CHANGED',
                entity_type='Product',
                entity_id=instance.id,
                before_value={'selling_price': old_price},
                after_value={'selling_price': str(instance.selling_price)},
                metadata={'product_name': instance.name}
            )

        # Audit stock change if directly modified
        if str(instance.stock_quantity) != old_stock:
            record_audit(
                request=self.request,
                action='STOCK_MANUALLY_ADJUSTED',
                entity_type='Product',
                entity_id=instance.id,
                before_value={'stock_quantity': old_stock},
                after_value={'stock_quantity': str(instance.stock_quantity)},
                metadata={'product_name': instance.name}
            )

    def perform_destroy(self, instance):
        user = self.request.user
        if not user.is_manager_or_owner:
            self.permission_denied(self.request, message="Only managers or owner can deactivate products.")
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        record_audit(
            request=self.request,
            action='PRODUCT_DEACTIVATED',
            entity_type='Product',
            entity_id=instance.id,
            metadata={'name': instance.name, 'deactivated_by': user.username}
        )

    @action(detail=True, methods=['post'])
    def adjust_stock(self, request, pk=None):
        """
        Stock Clerk, Manager, or Owner adjusts physical inventory count or records incoming delivery.
        Modes:
        - 'ADD': adds received items to stock (e.g. delivery received)
        - 'SET': overrides stock with verified physical count (stocktake count)
        - 'SUBTRACT': deducts damaged, broken, or expired items
        """
        user = request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            return Response({
                'error': 'Permission Denied: Stock adjustments are reserved for Stock Clerks, Managers, and Owner.',
                'code': 'ROLE_STOCK_ADJUST_RESTRICTED'
            }, status=status.HTTP_403_FORBIDDEN)

        product = self.get_object()
        adjustment_type = request.data.get('adjustment_type', 'ADD')  # 'ADD', 'SET', 'SUBTRACT'
        try:
            qty = Decimal(str(request.data.get('quantity', '0.00')))
        except Exception:
            return Response({'error': 'Invalid quantity value.'}, status=status.HTTP_400_BAD_REQUEST)

        if qty <= 0 and adjustment_type != 'SET':
            return Response({'error': 'Quantity must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)
        if qty < 0 and adjustment_type == 'SET':
            return Response({'error': 'Physical stock count cannot be negative.'}, status=status.HTTP_400_BAD_REQUEST)

        reason = str(request.data.get('reason', '')).strip()
        if not reason:
            return Response({'error': 'A reason is required for stock adjustments.'}, status=status.HTTP_400_BAD_REQUEST)

        old_stock = product.stock_quantity
        if adjustment_type == 'ADD':
            new_stock = old_stock + qty
        elif adjustment_type == 'SUBTRACT':
            new_stock = max(Decimal('0.00'), old_stock - qty)
        elif adjustment_type == 'SET':
            new_stock = qty
        else:
            return Response({'error': f"Unknown adjustment type: {adjustment_type}"}, status=status.HTTP_400_BAD_REQUEST)

        product.stock_quantity = new_stock
        product.save(update_fields=['stock_quantity'])

        record_audit(
            request=request,
            action=f"STOCK_{adjustment_type}",
            entity_type='Product',
            entity_id=product.id,
            before_value={'stock_quantity': str(old_stock)},
            after_value={'stock_quantity': str(new_stock)},
            metadata={
                'product_name': product.name,
                'clerk': user.username,
                'clerk_role': user.get_role_display(),
                'adjustment_type': adjustment_type,
                'reason': reason,
                'delta': str(new_stock - old_stock)
            }
        )

        return Response({
            'message': f"Stock for {product.name} updated from {old_stock} to {new_stock} ({adjustment_type}).",
            'product': ProductSerializer(product).data
        })

    @action(detail=True, methods=['post'])
    def update_reorder_threshold(self, request, pk=None):
        """
        Stock Clerk, Manager, or Owner configures the low-stock alert threshold.
        """
        user = request.user
        if user.role not in (User.ROLE_OWNER, User.ROLE_BAR_MANAGER, User.ROLE_STATIONERY_MANAGER, User.ROLE_STOCK_CLERK):
            return Response({
                'error': 'Permission Denied: Only Stock Clerks, Managers, or Owner can update reorder thresholds.',
                'code': 'ROLE_THRESHOLD_RESTRICTED'
            }, status=status.HTTP_403_FORBIDDEN)

        product = self.get_object()
        try:
            threshold = Decimal(str(request.data.get('reorder_threshold', '5.00')))
            if threshold < 0:
                threshold = Decimal('0.00')
        except Exception:
            return Response({'error': 'Invalid threshold value.'}, status=status.HTTP_400_BAD_REQUEST)

        old_t = product.reorder_threshold
        product.reorder_threshold = threshold
        product.save(update_fields=['reorder_threshold'])

        record_audit(
            request=request,
            action='REORDER_THRESHOLD_UPDATED',
            entity_type='Product',
            entity_id=product.id,
            before_value={'reorder_threshold': str(old_t)},
            after_value={'reorder_threshold': str(threshold)},
            metadata={'product_name': product.name, 'updated_by': user.username}
        )

        return Response({
            'message': f"Reorder threshold updated to {threshold} for {product.name}.",
            'product': ProductSerializer(product).data
        })
