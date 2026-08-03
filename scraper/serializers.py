"""Scraper app serializers."""
import ipaddress
import re
import socket
from urllib.parse import urlparse

from rest_framework import serializers

from .models import ScrapeJob, ScraperSource, Vehicle, VehicleModel

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


def _validate_webhook_url(value: str) -> str:
    """Reject webhook URLs that resolve to internal/private hosts (SSRF guard).

    The URL is client-provided and the server later POSTs to it, so an attacker
    could otherwise make it hit the internal network (metadata endpoints, DBs…).
    """
    if not value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise serializers.ValidationError("The webhook URL must be http(s).")
    host = parsed.hostname
    if not host:
        raise serializers.ValidationError("Invalid webhook URL.")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise serializers.ValidationError("Could not resolve the webhook host.") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise serializers.ValidationError(
                "The webhook URL may not point to an internal/private address."
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
    """Representation of a vehicle for API responses.

    price_low/price_high/price_kind come from the Vehicle's OWN fields, which hold
    the VinAudit per-VIN valuation when present, or the copied model data otherwise.
    """

    source_name = serializers.CharField(source="source.name", read_only=True, default=None)
    market_updated_at = serializers.SerializerMethodField()

    class Meta:
        model = Vehicle
        fields = [
            "id", "vin", "make", "model", "year", "trim", "body_class",
            "mileage", "estimated_price", "price_low", "price_high", "price_kind",
            "currency", "source", "source_name", "source_url", "market_updated_at",
            "vinaudit_priced_at", "raw_data", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_market_updated_at(self, obj):
        """When the shown price was set: VinAudit's timestamp if it priced this VIN,
        else the linked model data's, else the vehicle's own update time."""
        if obj.vinaudit_priced_at:
            return obj.vinaudit_priced_at
        if obj.vehicle_model_id and obj.vehicle_model:
            return obj.vehicle_model.updated_at
        return obj.updated_at


class VehicleModelSerializer(serializers.ModelSerializer):
    """Market data by model (for model-based lookups)."""

    source_name = serializers.CharField(source="source.name", read_only=True, default=None)

    class Meta:
        model = VehicleModel
        fields = [
            "id", "make", "model", "year", "trim", "estimated_price",
            "price_low", "price_high", "price_kind", "currency",
            "source", "source_name", "source_url", "raw_data", "updated_at",
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
    # force=True re-scrapes even if cached data is fresh (admin "re-scrape" button).
    force = serializers.BooleanField(required=False, default=False)
    # use_vinaudit=True opts INTO the paid, per-VIN VinAudit valuation (its own
    # button, with a cost warning). Default False -> free sources (Edmunds/CarGurus).
    use_vinaudit = serializers.BooleanField(required=False, default=False)

    def validate_vin(self, value: str) -> str:
        return _validate_vin(value)

    def validate_webhook_url(self, value: str) -> str:
        return _validate_webhook_url(value)


class VinBatchSerializer(serializers.Serializer):
    """Validates a list of VINs to prewarm (proactive scraping)."""

    vins = serializers.ListField(
        child=serializers.CharField(max_length=17, min_length=17),
        allow_empty=False,
        # Kept small: each VIN is decoded synchronously (NHTSA) inside the request,
        # so a big batch would exceed the gunicorn timeout and get the worker killed.
        max_length=25,
    )
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")

    def validate_vins(self, value: list[str]) -> list[str]:
        return [_validate_vin(v) for v in value]

    def validate_webhook_url(self, value: str) -> str:
        return _validate_webhook_url(value)


class ModelLookupSerializer(serializers.Serializer):
    """Validates a model lookup (search by make/model/year, no VIN needed)."""

    make = serializers.CharField(max_length=100)
    model = serializers.CharField(max_length=100)
    year = serializers.IntegerField(required=False, allow_null=True, min_value=1900, max_value=2100)
    webhook_url = serializers.URLField(required=False, allow_blank=True, default="")
    # force=True re-scrapes even if cached data is fresh (admin "re-scrape" button).
    force = serializers.BooleanField(required=False, default=False)

    def validate_webhook_url(self, value: str) -> str:
        return _validate_webhook_url(value)
