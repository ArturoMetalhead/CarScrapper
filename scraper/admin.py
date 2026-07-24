"""Admin de Django para el scraper.

Objetivo: que las tablas sean legibles de un vistazo (precios formateados como
$15,500, estados con color, JSON en bloque, enlaces entre objetos y campos
agrupados en secciones) en lugar de mostrar decimales crudos y objetos sin
identificar. Todo el texto visible está en español.
"""
from __future__ import annotations

import json

from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html
from django.utils.timesince import timesince

from .models import ScrapeJob, ScraperSource, ScrapeSubscriber, Vehicle, VehicleModel

# --- Branding del sitio admin -------------------------------------------------
admin.site.site_header = "CarScrapper — Datos de scraping"
admin.site.site_title = "CarScrapper admin"
admin.site.index_title = "Panel de administración"
# Un solo símbolo de "vacío" en todo el admin (Django usa "-" por defecto).
admin.site.empty_value_display = "—"


# --- Helpers de presentación --------------------------------------------------
def _money(value) -> str:
    """Formatea un precio como '$15,500' (o '—' si no hay valor)."""
    if value is None:
        return "—"
    try:
        return f"${value:,.0f}"
    except (TypeError, ValueError):
        return str(value)


# Etiquetas legibles para price_kind (de dónde salió el precio).
_PRICE_KIND_LABELS = {
    "edmunds_suggested": "Edmunds — sugerido",
    "edmunds_market": "Edmunds — mercado",
    "msrp_range_mid": "MSRP (punto medio)",
    "used_listings_median": "Mediana de anuncios",
    "cargurus_listings_median": "CarGurus — mediana",
    "cargurus_alltrims_median": "CarGurus — todas las versiones",
}

# Etiquetas legibles para el origen de un ScrapeJob.
_ORIGIN_LABELS = {
    "lookup": "Búsqueda por VIN",
    "model_lookup": "Búsqueda por modelo",
    "prewarm": "Precalentado",
    "crawl": "Rastreo automático",
    "refresh": "Refresco",
    "rescrape": "Reescrapeo",
}

# (texto, fondo, etiqueta) por estado de un ScrapeJob.
_STATUS_STYLE = {
    "pending": ("#8a6d00", "#fff3cd", "Pendiente"),
    "running": ("#0b5394", "#cfe2ff", "En curso"),
    "done": ("#1b5e20", "#d4edda", "Listo"),
    "failed": ("#8b0000", "#f8d7da", "Falló"),
}


def _pretty_json(data) -> str:
    """Muestra un dict/JSON en un bloque legible (o '—' si está vacío)."""
    if not data:
        return "—"
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str, sort_keys=True)
    return format_html(
        '<pre style="max-height:420px;overflow:auto;background:#f6f8fa;'
        'padding:10px;border-radius:6px;font-size:12px;line-height:1.45;'
        'white-space:pre-wrap">{}</pre>',
        text,
    )


def _admin_link(obj, label=None) -> str:
    """Enlace a la página de edición de otro objeto del admin."""
    if obj is None:
        return "—"
    meta = obj._meta
    url = reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])
    return format_html('<a href="{}">{}</a>', url, label or str(obj))


# --- Filtros con etiquetas legibles -------------------------------------------
class _LabeledFilter(admin.SimpleListFilter):
    """Filtro de barra lateral que muestra etiquetas en español para un
    CharField sin `choices` (que si no listaría los códigos crudos)."""

    field = ""      # nombre del campo en el modelo (subclases)
    labels: dict = {}   # {valor_crudo: etiqueta legible}

    def lookups(self, request, model_admin):
        present = (
            model_admin.get_queryset(request)
            .exclude(**{self.field: ""})
            .values_list(self.field, flat=True)
            .distinct()
        )
        return [(v, self.labels.get(v, v)) for v in sorted(present)]

    def queryset(self, request, queryset):
        value = self.value()
        return queryset.filter(**{self.field: value}) if value else queryset


class OriginFilter(_LabeledFilter):
    title = "origen"
    parameter_name = "origin"
    field = "origin"
    labels = _ORIGIN_LABELS


class PriceKindFilter(_LabeledFilter):
    title = "tipo de precio"
    parameter_name = "price_kind"
    field = "price_kind"
    labels = _PRICE_KIND_LABELS


# --- Fuentes de scraping ------------------------------------------------------
@admin.register(ScraperSource)
class ScraperSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "priority", "is_active", "provider_key", "base_url", "models_count")
    list_editable = ("priority", "is_active")
    list_filter = ("is_active", "provider_key")
    search_fields = ("name", "base_url")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("priority", "name")
    readonly_fields = ("created_at", "updated_at", "selectors_pretty")
    fieldsets = (
        ("Identificación", {"fields": ("name", "slug", "provider_key", "is_active", "priority")}),
        ("URLs", {"fields": ("base_url", "vin_path_template", "model_path_template", "timeout")}),
        ("Selectores CSS (proveedor 'generic')", {
            "classes": ("collapse",),
            "fields": ("selectors", "selectors_pretty"),
        }),
        ("Fechas", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_models=Count("vehicle_models"))

    @admin.display(description="Modelos cacheados", ordering="_models")
    def models_count(self, obj):
        return obj._models

    @admin.display(description="Selectores (vista)")
    def selectors_pretty(self, obj):
        return _pretty_json(obj.selectors)


# --- Datos de modelo (el caché de precios) ------------------------------------
@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = (
        "vehicle", "price_range", "price_kind_label", "source",
        "listings", "freshness",
    )
    search_fields = ("make", "model", "trim")
    list_filter = ("make", "year", PriceKindFilter, "source", "updated_at")
    date_hierarchy = "updated_at"
    ordering = ("-updated_at",)
    list_select_related = ("source",)
    readonly_fields = ("created_at", "updated_at", "price_summary", "raw_data_pretty")
    fieldsets = (
        ("Vehículo", {"fields": ("make", "model", "year", "trim")}),
        ("Precios", {
            "fields": ("price_summary", "estimated_price", "price_low", "price_high",
                       "price_kind", "currency"),
        }),
        ("Origen de los datos", {"fields": ("source", "source_url")}),
        ("Datos crudos (JSON)", {"classes": ("collapse",), "fields": ("raw_data_pretty",)}),
        ("Fechas", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Vehículo", ordering="make")
    def vehicle(self, obj):
        return " ".join(str(x) for x in (obj.year, obj.make, obj.model, obj.trim) if x)

    @admin.display(description="Mín · Sugerido · Máx")
    def price_range(self, obj):
        return format_html(
            '<span style="white-space:nowrap;font-variant-numeric:tabular-nums">'
            '<span style="color:#888">{}</span>&nbsp;·&nbsp;'
            '<b style="color:#1b5e20">{}</b>&nbsp;·&nbsp;'
            '<span style="color:#888">{}</span></span>',
            _money(obj.price_low), _money(obj.estimated_price), _money(obj.price_high),
        )

    @admin.display(description="Precio sugerido")
    def price_summary(self, obj):
        return format_html(
            '<div style="font-size:15px">Rango: <b>{}</b> – <b>{}</b><br>'
            'Sugerido: <b style="color:#1b5e20;font-size:18px">{}</b> {}</div>',
            _money(obj.price_low), _money(obj.price_high),
            _money(obj.estimated_price), obj.currency or "USD",
        )

    @admin.display(description="Método", ordering="price_kind")
    def price_kind_label(self, obj):
        return _PRICE_KIND_LABELS.get(obj.price_kind, obj.price_kind or "—")

    @admin.display(description="Anuncios")
    def listings(self, obj):
        # Los proveedores guardan el nº de anuncios en 'listing_samples'
        # (Edmunds sugerido / CarGurus) o 'market_listings' (Edmunds mercado).
        raw = obj.raw_data if isinstance(obj.raw_data, dict) else {}
        n = raw.get("listing_samples")
        if n is None:
            n = raw.get("market_listings")
        return n if n is not None else "—"

    @admin.display(description="Actualizado", ordering="updated_at")
    def freshness(self, obj):
        if not obj.updated_at:
            return "—"
        return format_html(
            '{}<br><span style="color:#888">hace {}</span>',
            obj.updated_at.strftime("%Y-%m-%d %H:%M"), timesince(obj.updated_at),
        )

    @admin.display(description="Datos crudos")
    def raw_data_pretty(self, obj):
        return _pretty_json(obj.raw_data)


# --- Vehículos (VIN ya resuelto) ----------------------------------------------
@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = (
        "vin", "vehicle", "body_class", "price", "model_link", "source", "freshness",
    )
    search_fields = ("vin", "make", "model", "trim")
    list_filter = ("make", "year", "source", "updated_at")
    date_hierarchy = "updated_at"
    ordering = ("-updated_at",)
    list_select_related = ("source", "vehicle_model")
    readonly_fields = ("created_at", "updated_at", "model_link", "raw_data_pretty")
    fieldsets = (
        ("VIN", {"fields": ("vin",)}),
        ("Decodificado (NHTSA)", {
            "fields": ("make", "model", "year", "trim", "body_class", "mileage"),
        }),
        ("Precio de mercado", {"fields": ("estimated_price", "currency", "model_link")}),
        ("Origen", {"fields": ("source", "source_url")}),
        ("Datos crudos (JSON)", {"classes": ("collapse",), "fields": ("raw_data_pretty",)}),
        ("Fechas", {"classes": ("collapse",), "fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Vehículo", ordering="make")
    def vehicle(self, obj):
        return " ".join(str(x) for x in (obj.year, obj.make, obj.model, obj.trim) if x)

    @admin.display(description="Precio", ordering="estimated_price")
    def price(self, obj):
        return format_html(
            '<b style="color:#1b5e20">{}</b> {}',
            _money(obj.estimated_price), obj.currency or "",
        )

    @admin.display(description="Datos de modelo")
    def model_link(self, obj):
        return _admin_link(obj.vehicle_model)

    @admin.display(description="Actualizado", ordering="updated_at")
    def freshness(self, obj):
        if not obj.updated_at:
            return "—"
        return format_html(
            '{}<br><span style="color:#888">hace {}</span>',
            obj.updated_at.strftime("%Y-%m-%d %H:%M"), timesince(obj.updated_at),
        )

    @admin.display(description="Datos crudos")
    def raw_data_pretty(self, obj):
        return _pretty_json(obj.raw_data)


# --- Suscriptores de un job (para el webhook) ---------------------------------
class ScrapeSubscriberInline(admin.TabularInline):
    model = ScrapeSubscriber
    extra = 0
    can_delete = False
    fields = ("vin", "webhook_url", "notified", "created_at")
    readonly_fields = ("created_at",)
    verbose_name = "Suscriptor"
    verbose_name_plural = "Suscriptores (esperan el resultado)"


# --- Cola de scraping ---------------------------------------------------------
@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = (
        "target", "status_badge", "error_short", "origin_label", "priority",
        "vin", "attempts", "subscribers_count", "created_at", "duration",
    )
    search_fields = ("vin", "make", "model", "trim")
    list_filter = ("status", OriginFilter, "priority", "make", "created_at")
    date_hierarchy = "created_at"
    ordering = ("priority", "created_at")
    inlines = (ScrapeSubscriberInline,)
    readonly_fields = (
        "created_at", "started_at", "finished_at", "duration", "result_link",
    )
    fieldsets = (
        ("Objetivo", {"fields": ("make", "model", "year", "trim", "series", "vin")}),
        ("Estado", {
            "fields": ("status", "origin", "priority", "attempts", "duration", "last_error"),
        }),
        ("Resultado", {"fields": ("result", "result_link")}),
        ("Webhook", {"fields": ("webhook_url",)}),
        ("Fechas", {"fields": ("created_at", "started_at", "finished_at")}),
    )

    def get_queryset(self, request):
        # Anota el nº de suscriptores para evitar un COUNT por fila (N+1).
        return super().get_queryset(request).annotate(_subs=Count("subscribers"))

    @admin.display(description="Objetivo", ordering="make")
    def target(self, obj):
        return " ".join(str(x) for x in (obj.year, obj.make, obj.model, obj.trim) if x) or (obj.vin or "—")

    @admin.display(description="Estado", ordering="status")
    def status_badge(self, obj):
        fg, bg, label = _STATUS_STYLE.get(obj.status, ("#333", "#eee", obj.status))
        return format_html(
            '<span style="background:{};color:{};padding:2px 9px;border-radius:10px;'
            'font-weight:600;font-size:12px;white-space:nowrap">{}</span>', bg, fg, label,
        )

    @admin.display(description="Error")
    def error_short(self, obj):
        if obj.status != ScrapeJob.Status.FAILED or not obj.last_error:
            return "—"
        text = obj.last_error
        short = text if len(text) <= 60 else text[:57] + "…"
        return format_html('<span style="color:#8b0000" title="{}">{}</span>', text, short)

    @admin.display(description="Origen", ordering="origin")
    def origin_label(self, obj):
        return _ORIGIN_LABELS.get(obj.origin, obj.origin or "—")

    @admin.display(description="Suscriptores", ordering="_subs")
    def subscribers_count(self, obj):
        return obj._subs

    @admin.display(description="Duración")
    def duration(self, obj):
        if obj.started_at and obj.finished_at:
            secs = (obj.finished_at - obj.started_at).total_seconds()
            if secs < 90:
                return f"{secs:.0f} s"
            return f"{secs / 60:.1f} min"
        return "—"

    @admin.display(description="Resultado")
    def result_link(self, obj):
        return _admin_link(obj.result)


# --- Suscriptores (tabla propia) ----------------------------------------------
@admin.register(ScrapeSubscriber)
class ScrapeSubscriberAdmin(admin.ModelAdmin):
    list_display = ("id", "job_link", "vin", "webhook_short", "notified", "created_at")
    list_filter = ("notified", "created_at")
    search_fields = ("vin", "webhook_url", "job__make", "job__model")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_select_related = ("job",)
    readonly_fields = ("created_at",)

    @admin.display(description="Job")
    def job_link(self, obj):
        return _admin_link(obj.job)

    @admin.display(description="Webhook")
    def webhook_short(self, obj):
        url = obj.webhook_url or "(webhook por defecto)"
        return url if len(url) <= 50 else url[:47] + "…"
