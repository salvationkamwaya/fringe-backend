from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ShiftViewSet, BarTabViewSet, OrderViewSet, bar_reports_view

router = DefaultRouter()
router.register(r'shifts', ShiftViewSet, basename='shift')
router.register(r'tabs', BarTabViewSet, basename='tab')
router.register(r'orders', OrderViewSet, basename='order')

urlpatterns = [
    path('reports/bar/', bar_reports_view, name='bar-reports'),
    path('', include(router.urls)),
]
