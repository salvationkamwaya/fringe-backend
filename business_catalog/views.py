from django.http import Http404
from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from django.db.models import Q
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
            
        instance = serializer.save(business_unit_id=unit_id)
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
