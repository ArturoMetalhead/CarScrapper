"""Serializers de la app scraper."""
import re

from rest_framework import serializers

from .models import ScrapeJob, ScraperSource, Vehicle

# El VIN son 17 caracteres alfanuméricos, sin las letras I, O y Q.
VIN_REGEX = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)


class ScraperSourceSerializer(serializers.ModelSerializer):
    """Representación de una fuente de scraping."""

    class Meta:
        model = ScraperSource
        fields = [
            "id",
            "name",
            "slug",
            "base_url",
            "vin_path_template",
            "provider_key",
            "selectors",
            "priority",
            "is_active",
            "timeout",
        ]


class VehicleSerializer(serializers.ModelSerializer):
    """Representación de un vehículo para las respuestas de la API."""

    source_name = serializers.CharField(source="source.name", read_only=True, default=None)
    market_updated_at = serializers.DateTimeField(
        source="vehicle_model.updated_at", read_only=True, default=None
    )

    class Meta:
        model = Vehicle
        fields = [
            "id",
            "vin",
            "make",
            "model",
            "year",
            "trim",
            "body_class",
            "mileage",
            "estimated_price",
            "currency",
            "source",
            "source_name",
            "source_url",
            "market_updated_at",
            "raw_data",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


def _validar_vin(value: str) -> str:
    value = value.strip().upper()
    if not VIN_REGEX.match(value):
        raise serializers.ValidationError(
            "VIN inválido. Debe tener 17 caracteres alfanuméricos "
            "(sin las letras I, O ni Q)."
        )
    return value


class ScrapeJobSerializer(serializers.ModelSerializer):
    """Estado de un trabajo de scraping en segundo plano."""

    class Meta:
        model = ScrapeJob
        fields = [
            "id", "make", "model", "year", "trim", "vin",
            "status", "attempts", "last_error",
            "created_at", "started_at", "finished_at",
        ]
        read_only_fields = fields


class VinLookupSerializer(serializers.Serializer):
    """Valida el VIN que entra en la petición de búsqueda.

    `webhook_url` es opcional: si se envía, el aviso de "dato listo" se hará a esa
    URL en vez de a la global (SCRAPER_WEBHOOK_URL).
    """

    vin = serializers.CharField(max_length=17, min_length=17)
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_vin(self, value: str) -> str:
        return _validar_vin(value)


class VinBatchSerializer(serializers.Serializer):
    """Valida una lista de VINs para precargar (pre-scraping proactivo)."""

    vins = serializers.ListField(
        child=serializers.CharField(max_length=17, min_length=17),
        allow_empty=False,
        max_length=500,
    )
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_vins(self, value: list[str]) -> list[str]:
        return [_validar_vin(v) for v in value]
