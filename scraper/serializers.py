"""Scraper app serializers."""
import re

from rest_framework import serializers

from .models import ScrapeJob, ScraperSource, Vehicle

# A VIN is 17 alphanumeric characters, excluding the letters I, O and Q.
VIN_REGEX = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)


def _validate_vin(value: str) -> str:
    value = value.strip().upper()
    if not VIN_REGEX.match(value):
        raise serializers.ValidationError(
            "Invalid VIN. It must be 17 alphanumeric characters "
            "(excluding the letters I, O and Q)."
        )
    return value


class ScraperSourceSerializer(serializers.ModelSerializer):
    """Representation of a scraper source."""

    class Meta:
        model = ScraperSource
        fields = [
            "id", "name", "slug", "base_url", "vin_path_template",
            "provider_key", "selectors", "priority", "is_active", "timeout",
        ]


class VehicleSerializer(serializers.ModelSerializer):
    """Representation of a vehicle for API responses."""

    source_name = serializers.CharField(source="source.name", read_only=True, default=None)
    market_updated_at = serializers.DateTimeField(
        source="vehicle_model.updated_at", read_only=True, default=None
    )

    class Meta:
        model = Vehicle
        fields = [
            "id", "vin", "make", "model", "year", "trim", "body_class",
            "mileage", "estimated_price", "currency", "source", "source_name",
            "source_url", "market_updated_at", "raw_data", "created_at", "updated_at",
        ]
        read_only_fields = fields


class ScrapeJobSerializer(serializers.ModelSerializer):
    """State of a background scrape job."""

    class Meta:
        model = ScrapeJob
        fields = [
            "id", "make", "model", "year", "trim", "vin",
            "status", "attempts", "last_error",
            "created_at", "started_at", "finished_at",
        ]
        read_only_fields = fields


class VinLookupSerializer(serializers.Serializer):
    """Validates the VIN of a lookup request.

    `webhook_url` is optional: if provided, the "data ready" notification goes to
    that URL instead of the global one (SCRAPER_WEBHOOK_URL).
    """

    vin = serializers.CharField(max_length=17, min_length=17)
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_vin(self, value: str) -> str:
        return _validate_vin(value)


class VinBatchSerializer(serializers.Serializer):
    """Validates a list of VINs to prewarm (proactive scraping)."""

    vins = serializers.ListField(
        child=serializers.CharField(max_length=17, min_length=17),
        allow_empty=False,
        max_length=500,
    )
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_vins(self, value: list[str]) -> list[str]:
        return [_validate_vin(v) for v in value]
