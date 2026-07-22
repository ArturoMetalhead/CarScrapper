"""Service layer: fast VIN resolution + background model scraping.

VIN lookup flow (`resolve_vin`):
  1. If the VIN is already resolved and its model data is FRESH -> return it
     instantly (cache).
  2. Otherwise decode the VIN with NHTSA (make/model/year/trim).
  3. Look up the market data by MODEL in the cache (`VehicleModel`).
     - Fresh -> link it and return instantly.
     - Missing or stale -> ENQUEUE a scrape job and return "processing"; a worker
       processes it in the background and notifies via webhook. If stale data
       exists, it is returned in the meantime.

The actual per-model scraping (`scrape_model_data`) is run by the worker, trying
the active sources by priority with fallback.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import ScrapeJob, ScraperSource, Vehicle, VehicleModel
from .providers import get_provider_class
from .providers.base import (
    AllSourcesFailed,
    BlockedError,
    ScrapedVehicle,
    ScraperError,
    VehicleNotFound,
)
from .vin_decoder import VinDecodeError, decode_vin

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_vin",
    "resolve_model",
    "scrape_model_data",
    "enqueue_scrape",
    "apply_model_to_vehicles",
    "is_fresh",
    "AllSourcesFailed",
    "ScraperError",
    "VehicleNotFound",
    "VinDecodeError",
    "ScrapedVehicle",
]

# Resolution states returned by resolve_vin.
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"


def _ttl() -> timedelta:
    return timedelta(hours=getattr(settings, "SCRAPER_CACHE_TTL_HOURS", 24))


def is_fresh(vehicle_model: VehicleModel | None) -> bool:
    """True if the model data exists and has not exceeded the cache TTL."""
    if not vehicle_model:
        return False
    return timezone.now() - vehicle_model.updated_at <= _ttl()


def _find_model(make: str, model: str, year: int | None) -> VehicleModel | None:
    """Find market data by model (make/model/year granularity)."""
    return (
        VehicleModel.objects.filter(make__iexact=make, model__iexact=model, year=year)
        .order_by("-updated_at")
        .first()
    )


def _link_model(vehicle: Vehicle, vm: VehicleModel) -> None:
    """Copy the model's market price onto the VIN and save."""
    vehicle.vehicle_model = vm
    vehicle.estimated_price = vm.estimated_price
    vehicle.currency = vm.currency or "USD"
    vehicle.source = vm.source
    vehicle.source_url = vm.source_url
    vehicle.save(update_fields=[
        "vehicle_model", "estimated_price", "currency", "source", "source_url", "updated_at"
    ])


def resolve_vin(vin: str, webhook_url: str = "") -> tuple[Vehicle, str]:
    """Resolve a VIN: instant cache hit or enqueue background scraping.

    Returns:
        (vehicle, status) where status is "ready" (fresh data available) or
        "processing" (enqueued; the price will arrive via webhook / on lookup).

    Raises:
        VinDecodeError: if NHTSA cannot decode the VIN.
    """
    vehicle = Vehicle.objects.select_related("vehicle_model").filter(vin=vin).first()

    if vehicle and is_fresh(vehicle.vehicle_model):
        return vehicle, STATUS_READY

    if vehicle and vehicle.make and vehicle.model:
        make, model, year, trim = vehicle.make, vehicle.model, vehicle.year, vehicle.trim
        raw = vehicle.raw_data if isinstance(vehicle.raw_data, dict) else {}
        series = (raw.get("nhtsa") or {}).get("Series", "")
    else:
        decoded = decode_vin(vin)  # may raise VinDecodeError
        make, model, year, trim = decoded.make, decoded.model, decoded.year, decoded.trim
        series = decoded.series
        vehicle, _ = Vehicle.objects.update_or_create(
            vin=vin,
            defaults={
                "make": make,
                "model": model,
                "year": year,
                "trim": trim,
                "body_class": decoded.body_class,
                "raw_data": {"nhtsa": decoded.raw},
            },
        )

    vm = _find_model(make, model, year)
    if is_fresh(vm):
        _link_model(vehicle, vm)
        return vehicle, STATUS_READY

    enqueue_scrape(make, model, year, trim="", vin=vin, webhook_url=webhook_url, series=series)
    if vm:
        _link_model(vehicle, vm)
    return vehicle, STATUS_PROCESSING


def resolve_model(
    make: str, model: str, year: int | None = None, webhook_url: str = ""
) -> tuple[VehicleModel | None, str]:
    """Resolve market data by MODEL directly (no VIN needed).

    Useful for searching new cars by make/model/year. Returns (vehicle_model,
    status): "ready" if fresh cached data exists, or "processing" if a scrape was
    enqueued (a stale VehicleModel may be returned meanwhile, or None).
    """
    vm = _find_model(make, model, year)
    if is_fresh(vm):
        return vm, STATUS_READY
    enqueue_scrape(make, model, year, webhook_url=webhook_url, origin="model_lookup")
    return vm, STATUS_PROCESSING


# Job priorities (lower = processed first).
PRIORITY_ONDEMAND = 10
PRIORITY_REFRESH = 50
PRIORITY_CRAWL = 100


def enqueue_scrape(
    make: str,
    model: str,
    year: int | None = None,
    trim: str = "",
    vin: str = "",
    webhook_url: str = "",
    priority: int = PRIORITY_ONDEMAND,
    origin: str = "lookup",
    series: str = "",
) -> ScrapeJob:
    """Enqueue a per-model scrape job, avoiding duplicates.

    If a pending or running job already exists for the same model, it is reused
    (no new one is created). Works for on-demand lookups, prewarming, and the
    background crawler. If the new request is higher priority (lower number) than
    the existing job, the existing job is bumped up so it jumps ahead.
    """
    existing = (
        ScrapeJob.objects.filter(make__iexact=make, model__iexact=model, year=year)
        .filter(Q(status=ScrapeJob.Status.PENDING) | Q(status=ScrapeJob.Status.RUNNING))
        .first()
    )
    if existing:
        changed = []
        if vin and not existing.vin:
            existing.vin = vin
            changed.append("vin")
        if webhook_url and not existing.webhook_url:
            existing.webhook_url = webhook_url
            changed.append("webhook_url")
        if priority < existing.priority:
            existing.priority = priority
            existing.origin = origin
            changed += ["priority", "origin"]
        if changed:
            existing.save(update_fields=changed)
        return existing

    return ScrapeJob.objects.create(
        make=make, model=model, year=year, trim=trim, vin=vin,
        webhook_url=webhook_url, priority=priority, origin=origin, series=series,
    )


def apply_model_to_vehicles(vm: VehicleModel) -> int:
    """Propagate a freshly scraped model's price to all its VINs.

    Updates the known `Vehicle`s of that model/year so their lookups return the
    fresh data. Returns how many vehicles were updated.
    """
    vehicles = Vehicle.objects.filter(
        make__iexact=vm.make, model__iexact=vm.model, year=vm.year
    )
    updated = 0
    for vehicle in vehicles:
        _link_model(vehicle, vm)
        updated += 1
    return updated


def scrape_model_data(
    make: str, model: str, year: int | None = None, trim: str = "", series: str = ""
) -> VehicleModel:
    """Scrape a model's market data trying the sources by priority.

    Used by the worker. Saves/updates the `VehicleModel` and returns it.

    Raises:
        AllSourcesFailed: if no source with model scraping can resolve it.
    """
    sources = list(
        ScraperSource.objects.filter(is_active=True)
        .exclude(model_path_template="")
        .order_by("priority")
    )
    if not sources:
        raise AllSourcesFailed(
            f"{make} {model}", {"config": "No sources with model scraping."}
        )

    errors: dict[str, str] = {}
    blocked_any = False
    for source in sources:
        provider = get_provider_class(source.provider_key)(source)
        try:
            result = provider.scrape_model(make, model, year, trim, series=series)
        except BlockedError as exc:
            # This source is blocked — record and FALL THROUGH to the next
            # configured source (e.g. Edmunds blocked -> try CarGurus).
            blocked_any = True
            errors[source.name] = f"blocked (403): {exc}"
            logger.warning("Source '%s' blocked; trying next source.", source.name)
            continue
        except VehicleNotFound as exc:
            errors[source.name] = str(exc)
            continue
        except ScraperError as exc:
            logger.warning("Source '%s' failed for %s %s: %s", source.name, make, model, exc)
            errors[source.name] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 — a broken site must not take down the worker
            logger.exception("Unexpected error in '%s' for %s %s", source.name, make, model)
            errors[source.name] = f"Unexpected error: {exc}"
            continue

        if result.estimated_price is None:
            errors[source.name] = "Could not extract the model price."
            continue

        vm, _ = VehicleModel.objects.update_or_create(
            make=make,
            model=model,
            year=year,
            trim="",  # make/model/year granularity (the Edmunds URL ignores trim)
            defaults={
                "estimated_price": result.estimated_price,
                "price_low": result.price_low,
                "price_high": result.price_high,
                "price_kind": result.price_kind,
                "currency": result.currency or "USD",
                "source": source,
                "source_url": result.source_url,
                "raw_data": result.raw_data,
            },
        )
        logger.info("Model %s %s %s resolved by '%s'.", year, make, model, source.name)
        return vm

    # No source produced a price. If a source was blocked, signal the worker to
    # back off / rotate (recover); otherwise it's a genuine not-found.
    if blocked_any:
        raise BlockedError(f"All sources blocked or empty for {make} {model} {year}.")
    raise AllSourcesFailed(f"{make} {model} {year}", errors)
