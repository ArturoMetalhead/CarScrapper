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


def _set_activity(label: str, source: str, after_block: bool) -> None:
    """Publish what the worker is scraping right now (for the admin panel)."""
    try:
        from .worker import WORKER_STATE

        WORKER_STATE["activity"] = {
            "label": label, "source": source, "after_block": after_block,
        }
    except Exception:  # noqa: BLE001 — telemetry only, never break scraping
        pass


def is_fresh(vehicle_model: VehicleModel | None) -> bool:
    """True if the model data exists and has not exceeded the cache TTL."""
    if not vehicle_model:
        return False
    return timezone.now() - vehicle_model.updated_at <= _ttl()


def _find_model(
    make: str, model: str, year: int | None, trim: str = ""
) -> VehicleModel | None:
    """Find cached market data (make/model/year/trim granularity).

    trim="" is the model-level row (all trims, used by model searches); a specific
    trim from a VIN decode matches its own row, so a Sport and an LX don't share a
    price.
    """
    return (
        VehicleModel.objects.filter(
            make__iexact=make, model__iexact=model, year=year, trim__iexact=trim
        )
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


def resolve_vin(
    vin: str, webhook_url: str = "", force: bool = False
) -> tuple[Vehicle, str, "ScrapeJob | None"]:
    """Resolve a VIN: instant cache hit or enqueue background scraping.

    `force=True` skips the fresh-cache short-circuit and always enqueues a fresh
    scrape (the admin "re-scrape" button).

    Returns:
        (vehicle, status, job) where status is "ready" (fresh data available) or
        "processing" (enqueued); job is the enqueued ScrapeJob, or None if served
        from cache.

    Raises:
        VinDecodeError: if NHTSA cannot decode the VIN.
    """
    vehicle = Vehicle.objects.select_related("vehicle_model").filter(vin=vin).first()

    if not force and vehicle and is_fresh(vehicle.vehicle_model):
        return vehicle, STATUS_READY, None

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

    vm = _find_model(make, model, year, trim)
    if not force and is_fresh(vm):
        _link_model(vehicle, vm)
        return vehicle, STATUS_READY, None

    job = enqueue_scrape(
        make, model, year, trim=trim, vin=vin, webhook_url=webhook_url,
        series=series, origin="rescrape" if force else "lookup",
    )
    if vm:
        _link_model(vehicle, vm)
    return vehicle, STATUS_PROCESSING, job


def resolve_model(
    make: str, model: str, year: int | None = None, webhook_url: str = "", force: bool = False
) -> tuple[VehicleModel | None, str, "ScrapeJob | None"]:
    """Resolve market data by MODEL directly (no VIN needed).

    Useful for searching new cars by make/model/year. `force=True` re-scrapes even
    if fresh cached data exists (admin "re-scrape" button). Returns (vehicle_model,
    status, job): "ready" if fresh cached data exists, or "processing" if a scrape
    was enqueued; job is the enqueued ScrapeJob or None.
    """
    vm = _find_model(make, model, year)
    if not force and is_fresh(vm):
        return vm, STATUS_READY, None
    job = enqueue_scrape(
        make, model, year, webhook_url=webhook_url,
        origin="rescrape" if force else "model_lookup",
    )
    return vm, STATUS_PROCESSING, job


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
        ScrapeJob.objects.filter(
            make__iexact=make, model__iexact=model, year=year, trim__iexact=trim
        )
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
    """Propagate a freshly scraped model's price to its VINs.

    Matches the VehicleModel's trim exactly: a trim-specific row (from a VIN
    search, e.g. Sport) updates only that trim's VINs, and a model-level row
    (trim="") updates only trim-less VINs — so a model-level (crawler) scrape never
    clobbers a VIN that has trim-specific pricing. Returns how many were updated.
    """
    vehicles = Vehicle.objects.filter(
        make__iexact=vm.make, model__iexact=vm.model, year=vm.year, trim__iexact=vm.trim
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
    label = " ".join(str(x) for x in (year, make, model) if x)
    for source in sources:
        _set_activity(label, source.name, blocked_any)
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

        defaults = {
            "estimated_price": result.estimated_price,
            "price_low": result.price_low,
            "price_high": result.price_high,
            "price_kind": result.price_kind,
            "currency": result.currency or "USD",
            "source": source,
            "source_url": result.source_url,
            "raw_data": result.raw_data,
        }
        # Case-insensitive upsert. NHTSA returns UPPERCASE makes ("HONDA") while
        # model searches carry the user's casing ("honda"/"Honda"); a plain
        # update_or_create (exact match) would then create duplicate rows for the
        # same car. Reuse any existing row for this make/model/year (trim="" =
        # model-year granularity) regardless of case, so there is exactly one.
        vm = (
            VehicleModel.objects.filter(
                make__iexact=make, model__iexact=model, year=year, trim__iexact=trim
            )
            .order_by("id")
            .first()
        )
        if vm is not None:
            for field, value in defaults.items():
                setattr(vm, field, value)
            vm.save()
        else:
            vm = VehicleModel.objects.create(
                make=make, model=model, year=year, trim=trim, **defaults
            )
        logger.info("Model %s %s %s resolved by '%s'.", year, make, model, source.name)
        return vm

    # No source produced a price. If a source was blocked, signal the worker to
    # back off / rotate (recover); otherwise it's a genuine not-found.
    if blocked_any:
        raise BlockedError(f"All sources blocked or empty for {make} {model} {year}.")
    raise AllSourcesFailed(f"{make} {model} {year}", errors)
