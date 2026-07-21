"""Rutas de la API del scraper."""
from django.urls import path

from .views import (
    HealthView,
    SourceListView,
    VehicleDetailView,
    VehicleListView,
    VehicleLookupView,
    VehiclePrewarmView,
    VehicleStatusView,
)

app_name = "scraper"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("sources/", SourceListView.as_view(), name="source-list"),
    path("vehicles/", VehicleListView.as_view(), name="vehicle-list"),
    path("vehicles/lookup/", VehicleLookupView.as_view(), name="vehicle-lookup"),
    path("vehicles/prewarm/", VehiclePrewarmView.as_view(), name="vehicle-prewarm"),
    path("vehicles/<str:vin>/status/", VehicleStatusView.as_view(), name="vehicle-status"),
    path("vehicles/<str:vin>/", VehicleDetailView.as_view(), name="vehicle-detail"),
]
