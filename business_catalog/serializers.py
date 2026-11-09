from rest_framework import serializers
from .models import Category, Product


class CategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(source='products.count', read_only=True)
    slug = serializers.SlugField(required=False, allow_blank=True)
    branch_name = serializers.ReadOnlyField(source='branch.name')

    class Meta:
        model = Category
        fields = [
            'id', 'business_unit', 'branch', 'branch_name', 'name', 'slug', 'icon',
            'display_order', 'product_count', 'created_at'
        ]
        read_only_fields = ['id', 'business_unit', 'created_at']


class ProductSerializer(serializers.ModelSerializer):
    category_name = serializers.ReadOnlyField(source='category.name')
    branch_name = serializers.ReadOnlyField(source='branch.name')
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'business_unit', 'branch', 'branch_name', 'category', 'category_name',
            'name', 'sku', 'barcode', 'description',
            'cost_price', 'selling_price', 'stock_quantity', 'reorder_threshold',
            'is_active', 'is_low_stock', 'image_url',
            # Bar-specific
            'is_alcoholic', 'volume_ml',
            # Stationery-specific
            'is_pack', 'pack_unit_ratio', 'pack_name', 'unit_name',
            'allow_split_sale', 'split_unit_price',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business_unit', 'created_at', 'updated_at']
