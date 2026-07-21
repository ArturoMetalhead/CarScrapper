from django.contrib import admin

from .models import ScraperSource, Vehicle


@admin.register(ScraperSource)
class ScraperSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "priority", "is_active", "provider_key", "base_url")
    list_editable = ("priority", "is_active")
    list_filter = ("is_active", "provider_key")
    search_fields = ("name", "base_url")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("priority", "name")


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("vin", "make", "model", "year", "estimated_price", "source", "updated_at")
    search_fields = ("vin", "make", "model")
    list_filter = ("make", "year", "source")
    readonly_fields = ("created_at", "updated_at")
