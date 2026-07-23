"""Root URL routes for the CarScrapper project."""
from django.contrib import admin
from django.urls import include, path
from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    # Customer-facing search page + internal ops panel (both call the API).
    # never_cache so the browser always loads the latest template (no stale UI).
    path("", never_cache(TemplateView.as_view(template_name="dashboard.html")), name="dashboard"),
    path("panel/", never_cache(TemplateView.as_view(template_name="panel.html")), name="panel"),
    path("admin/", admin.site.urls),
    path("api/", include("scraper.urls")),
    # OpenAPI schema + interactive docs.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
