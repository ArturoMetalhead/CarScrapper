"""Scraper API routes."""
from django.urls import path

from .views import (
    CrawlerControlView,
    HealthView,
    JobStatusView,
    ModelLookupView,
    SourceListView,
    VehicleDetailView,
    VehicleListView,
    VehicleLookupView,
    VehiclePrewarmView,
    VehicleStatusView,
    WorkerControlView,
)

app_name = "scraper"

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("sources/", SourceListView.as_view(), name="source-list"),
    path("worker/", WorkerControlView.as_view(), name="worker-status"),
    path("worker/<str:action>/", WorkerControlView.as_view(), name="worker-control"),
    path("crawler/", CrawlerControlView.as_view(), name="crawler-status"),
    path("crawler/<str:action>/", CrawlerControlView.as_view(), name="crawler-control"),
    path("models/lookup/", ModelLookupView.as_view(), name="model-lookup"),
    path("jobs/<int:job_id>/", JobStatusView.as_view(), name="job-status"),
    path("vehicles/", VehicleListView.as_view(), name="vehicle-list"),
    path("vehicles/lookup/", VehicleLookupView.as_view(), name="vehicle-lookup"),
    path("vehicles/prewarm/", VehiclePrewarmView.as_view(), name="vehicle-prewarm"),
    path("vehicles/<str:vin>/status/", VehicleStatusView.as_view(), name="vehicle-status"),
    path("vehicles/<str:vin>/", VehicleDetailView.as_view(), name="vehicle-detail"),
]
