from django.contrib import admin

from .models import ScrapeJob, ScraperSource, Vehicle, VehicleModel


@admin.register(ScraperSource)
class ScraperSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "priority", "is_active", "provider_key", "base_url")
    list_editable = ("priority", "is_active")
    list_filter = ("is_active", "provider_key")
    search_fields = ("name", "base_url")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("priority", "name")


@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = (
        "make", "model", "year", "estimated_price", "price_low", "price_high",
        "price_kind", "source", "updated_at",
    )
    search_fields = ("make", "model")
    list_filter = ("make", "year", "price_kind", "source")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = (
        "__str__", "status", "origin", "priority", "vin", "attempts",
        "created_at", "finished_at",
    )
    search_fields = ("vin", "make", "model")
    list_filter = ("status", "origin", "make")
    readonly_fields = ("created_at", "started_at", "finished_at")


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("vin", "make", "model", "year", "estimated_price", "source", "updated_at")
    search_fields = ("vin", "make", "model")
    list_filter = ("make", "year", "source")
    readonly_fields = ("created_at", "updated_at")
