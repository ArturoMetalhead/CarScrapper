"""Serializers de la app scraper."""
import re

from rest_framework import serializers

from .models import ScraperSource, Vehicle

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

    class Meta:
        model = Vehicle
        fields = [
            "id",
            "vin",
            "make",
            "model",
            "year",
            "trim",
            "mileage",
            "estimated_price",
            "currency",
            "source",
            "source_name",
            "source_url",
            "raw_data",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class VinLookupSerializer(serializers.Serializer):
    """Valida el VIN que entra en la petición de scraping."""

    vin = serializers.CharField(max_length=17, min_length=17)

    def validate_vin(self, value: str) -> str:
        value = value.strip().upper()
        if not VIN_REGEX.match(value):
            raise serializers.ValidationError(
                "VIN inválido. Debe tener 17 caracteres alfanuméricos "
                "(sin las letras I, O ni Q)."
            )
        return value
