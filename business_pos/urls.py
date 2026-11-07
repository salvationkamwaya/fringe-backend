from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ShiftViewSet, BarTabViewSet, OrderViewSet,
    CustomerViewSet, SupplierViewSet, PurchaseOrderViewSet,
    bar_reports_view, stationery_reports_view, consolidated_reports_view, debtors_aging_report_view,
    expenses_view, expense_detail_view, quotations_view, quotation_detail_view,
    returns_view, product_bulk_import_view
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
    path('reports/consolidated/', consolidated_reports_view, name='consolidated-reports'),
    path('debtors-aging/', debtors_aging_report_view, name='debtors-aging'),
    path('expenses/', expenses_view, name='pos-expenses'),
    path('expenses/<str:expense_id>/', expense_detail_view, name='pos-expense-detail'),
    path('quotations/', quotations_view, name='pos-quotations'),
    path('quotations/<str:quote_id>/', quotation_detail_view, name='pos-quotation-detail'),
    path('returns/', returns_view, name='pos-returns'),
    path('bulk-import/', product_bulk_import_view, name='pos-bulk-import'),
    path('', include(router.urls)),
]

