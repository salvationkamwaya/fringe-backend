"""
URL configuration for Fringe Business System.
"""
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.db import connection
import django
import rest_framework

def health_check(request):
    """
    Health check endpoint to verify backend status and MySQL database connectivity.
    """
    db_status = "unhealthy"
    db_error = None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        db_status = "connected"
    except Exception as e:
        db_error = str(e)

    return JsonResponse({
        "status": "healthy" if db_status == "connected" else "degraded",
        "system": "Fringe Business System",
        "description": "Multi-tenant POS system for Bar and Stationery management",
        "database": {
            "engine": "MySQL",
            "name": connection.settings_dict.get("NAME"),
            "host": connection.settings_dict.get("HOST"),
            "port": str(connection.settings_dict.get("PORT")),
            "user": connection.settings_dict.get("USER"),
            "status": db_status,
            "error": db_error,
        },
        "backend": {
            "framework": f"Django {django.get_version()}",
            "rest_framework": f"DRF {rest_framework.__version__}",
            "cors_configured": True,
        }
    })

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/health/', health_check, name='health-check'),
    path('api/status/', health_check, name='status-check'),
    path('api/', include('business_core.urls')),
    path('api/catalog/', include('business_catalog.urls')),
    path('api/pos/', include('business_pos.urls')),
]
