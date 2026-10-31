from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ShiftViewSet, BarTabViewSet, OrderViewSet,
    CustomerViewSet, SupplierViewSet, PurchaseOrderViewSet,
    bar_reports_view, stationery_reports_view, debtors_aging_report_view
)

router = DefaultRouter()
router.register(r'shifts', ShiftViewSet, basename='shift')
router.register(r'tabs', BarTabViewSet, basename='tab')
router.register(r'orders', OrderViewSet, basename='order')
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'suppliers', SupplierViewSet, basename='supplier')
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='purchase-order')

urlpatterns = [
    path('reports/bar/', bar_reports_view, name='bar-reports'),
    path('reports/stationery/', stationery_reports_view, name='stationery-reports'),
    path('debtors-aging/', debtors_aging_report_view, name='debtors-aging'),
    path('', include(router.urls)),
]

