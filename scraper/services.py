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
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import ScrapeJob, ScraperSource, ScrapeSubscriber, Vehicle, VehicleModel
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
# Dedicated namespace for the on-demand VinAudit path (shared with the provider),
# so all VinAudit activity can be filtered/leveled together as "scraper.vinaudit".
va_logger = logging.getLogger("scraper.vinaudit")

__all__ = [
    "resolve_vin",
    "resolve_model",
    "scrape_model_data",
    "enqueue_scrape",
    "apply_model_to_vehicles",
    "mark_model_failure",
    "is_fresh",
    "vinaudit_enabled",
    "AllSourcesFailed",
    "ScraperError",
    "VehicleNotFound",
    "VinDecodeError",
    "VinAuditLookupError",
    "ScrapedVehicle",
]


class VinAuditLookupError(Exception):
    """A VinAudit-ONLY lookup (the paid button) could not return a value and must
    NOT fall back to the free sources. `reason` is 'no_data' (VinAudit has no value
    for this VIN) or 'unavailable' (rate limit / bad key / network / disabled)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# Resolution states returned by resolve_vin.
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"

# VinAudit is the authoritative price source, but it is a PAID, rate-limited API.
# So it is queried ONLY on-demand (in resolve_vin, during the request), per VIN,
# at most once per VINAUDIT_TTL_DAYS — never by the background worker/crawler. A
# VIN priced by VinAudit is protected: no scraper may overwrite it.
VINAUDIT_PROVIDER_KEY = "vinaudit"


def _ttl() -> timedelta:
    return timedelta(hours=getattr(settings, "SCRAPER_CACHE_TTL_HOURS", 24))


def _vinaudit_ttl() -> timedelta:
    """How long a per-VIN VinAudit result (or negative check) stays valid."""
    return timedelta(days=getattr(settings, "VINAUDIT_TTL_DAYS", 30))


def _vinaudit_source() -> "ScraperSource | None":
    """The active VinAudit source (config for the on-demand provider), or None."""
    return (
        ScraperSource.objects.filter(
            provider_key=VINAUDIT_PROVIDER_KEY, is_active=True
        )
        .order_by("priority")
        .first()
    )


def vinaudit_enabled() -> bool:
    """True if the on-demand VinAudit lookup is actually usable — an API key is set
    AND an active `vinaudit` source exists (same gate as _resolve_via_vinaudit). Used
    to show/hide the paid VinAudit button.

    The context processor wraps this in a SimpleLazyObject, so the ScraperSource query
    only runs if a template actually reads the flag (i.e. the dashboard) — not on every
    admin/DRF/404 render. No key -> no query at all.
    """
    if not str(getattr(settings, "VINAUDIT_API_KEY", "") or "").strip():
        return False
    return _vinaudit_source() is not None


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


def _link_model(vehicle: Vehicle, vm: VehicleModel) -> bool:
    """Copy the model's market price (and range) onto the VIN. Returns True if it wrote.

    - A VIN already priced by VinAudit is authoritative and is NEVER overwritten — and
      the guard is enforced ATOMICALLY via a conditional UPDATE (WHERE vinaudit_priced_at
      IS NULL), so a VinAudit valuation committing concurrently can't be clobbered by a
      stale in-memory read.
    - Skips the write entirely when the VIN is already linked to this exact model and
      price, so a plain cache-hit read doesn't bump updated_at / reorder the recent list.
    """
    if vehicle.vinaudit_priced_at is not None or (
        vehicle.vehicle_model_id == vm.id
        and vehicle.estimated_price == vm.estimated_price
        and vehicle.price_low == vm.price_low
        and vehicle.price_high == vm.price_high
        and vehicle.price_kind == vm.price_kind
    ):
        return False

    now = timezone.now()
    updated = Vehicle.objects.filter(pk=vehicle.pk, vinaudit_priced_at__isnull=True).update(
        vehicle_model=vm,
        estimated_price=vm.estimated_price,
        price_low=vm.price_low,
        price_high=vm.price_high,
        price_kind=vm.price_kind,
        currency=vm.currency or "USD",
        source=vm.source,
        source_url=vm.source_url,
        updated_at=now,
    )
    if not updated:  # a concurrent VinAudit valuation won the row — leave it alone
        return False
    # Keep the in-memory instance consistent for callers that read it afterward.
    vehicle.vehicle_model = vm
    vehicle.estimated_price = vm.estimated_price
    vehicle.price_low = vm.price_low
    vehicle.price_high = vm.price_high
    vehicle.price_kind = vm.price_kind
    vehicle.currency = vm.currency or "USD"
    vehicle.source = vm.source
    vehicle.source_url = vm.source_url
    vehicle.updated_at = now
    return True


def resolve_vin(
    vin: str, webhook_url: str = "", force: bool = False,
    allow_vinaudit: bool = True, vinaudit_only: bool = False,
) -> tuple[Vehicle, str, "ScrapeJob | None"]:
    """Resolve a VIN's market price.

    Order:
      1. VinAudit (authoritative, per-VIN) — queried HOT, in this request, but only
         on-demand and at most once per VINAUDIT_TTL_DAYS (see _resolve_via_vinaudit).
         If it yields/holds a price, return it instantly ("ready").
      2. Otherwise the model-cache flow: fresh cache -> "ready"; else enqueue the
         background scrapers (Edmunds/CarGurus) and return "processing".

    `force=True` re-queries VinAudit and re-scrapes even if cached (admin button).
    `allow_vinaudit=False` skips the VinAudit call entirely — used by the proactive
    prewarm so background/bulk work never spends VinAudit quota.
    `vinaudit_only=True` (the paid VinAudit button) means VinAudit OR error: if
    VinAudit can't return a value, raise VinAuditLookupError instead of falling back
    to the free sources.

    Returns (vehicle, status, job); job is None when served without enqueuing.

    Raises:
        VinDecodeError: if NHTSA cannot decode the VIN.
        VinAuditLookupError: if vinaudit_only and VinAudit couldn't resolve the VIN.
    """
    vehicle = Vehicle.objects.select_related("vehicle_model", "source").filter(vin=vin).first()

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

    # 1. Hot, on-demand VinAudit (per-VIN, ~monthly TTL). "served" -> the vehicle now
    # carries a VinAudit price; "no_data"/"unavailable" -> VinAudit couldn't value it.
    if allow_vinaudit:
        outcome = _resolve_via_vinaudit(vehicle, force)
        if outcome == "served":
            return vehicle, STATUS_READY, None
        if vinaudit_only:
            # Paid VinAudit button: no free fallback — surface the failure.
            raise VinAuditLookupError(outcome)  # 'no_data' | 'unavailable'

    # 2. Model-cache flow (Edmunds/CarGurus via the background worker).
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


def _resolve_via_vinaudit(vehicle: Vehicle, force: bool) -> str:
    """HOT, on-demand VinAudit valuation for a single VIN.

    Minimizes API calls: VinAudit is queried only when forced, never queried before,
    or last queried more than VINAUDIT_TTL_DAYS ago (~1 month). A successful price
    and a definitive "not found" are both cached for that window, so the same VIN is
    not re-queried again and again.

    Returns one of:
        "served"      -> the vehicle now holds a VinAudit price; caller returns it.
        "no_data"     -> VinAudit definitively has no value for this VIN.
        "unavailable" -> VinAudit failed (rate limit / bad key / network) or is off.
    The caller decides what to do (free fallback, or error for the paid button).
    """
    # Cheap gate first (no DB): if there is no API key, VinAudit is off — skip the
    # ScraperSource query entirely.
    if not str(getattr(settings, "VINAUDIT_API_KEY", "") or "").strip():
        va_logger.debug("VinAudit disabled (no API key) for %s.", vehicle.vin)
        return "unavailable"
    source = _vinaudit_source()
    if source is None:
        va_logger.debug("VinAudit has no active source for %s.", vehicle.vin)
        return "unavailable"  # VinAudit disabled/unconfigured

    now = timezone.now()
    owns_price = vehicle.vinaudit_priced_at is not None  # already priced by VinAudit
    checked = vehicle.vinaudit_checked_at
    should_query = force or checked is None or (now - checked) > _vinaudit_ttl()

    if not should_query:
        # Queried recently: serve the cached VinAudit price if we have one, else the
        # cached negative is still valid (no value) — no re-query.
        age_days = (now - checked).days if checked else None
        va_logger.debug(
            "VinAudit cache hit for %s (checked %s day(s) ago, within TTL); no API call.",
            vehicle.vin, age_days,
        )
        return "served" if owns_price else "no_data"

    # Atomic claim (compare-and-swap on the checked_at we read): only ONE concurrent
    # request for this VIN wins and reaches the BILLED call — this prevents double
    # charging from concurrent requests or a double-clicked button.
    claimed = Vehicle.objects.filter(
        pk=vehicle.pk, vinaudit_checked_at=checked
    ).update(vinaudit_checked_at=now)
    if claimed != 1:
        # Lost the race — another request is querying (or just did). Serve what's cached.
        vehicle.refresh_from_db()
        va_logger.info(
            "VinAudit lookup for %s deduped (concurrent claim); serving cached.", vehicle.vin
        )
        return "served" if vehicle.vinaudit_priced_at is not None else "no_data"
    vehicle.vinaudit_checked_at = now  # reflect the claim in memory

    reason = "forced" if force else ("never-queried" if checked is None else "older-than-TTL")
    va_logger.info("VinAudit query for %s (reason=%s).", vehicle.vin, reason)
    provider = get_provider_class(VINAUDIT_PROVIDER_KEY)(source)
    try:
        sv = provider.scrape(vehicle.vin)
    except VehicleNotFound:
        # No value for this VIN. The claim already persisted vinaudit_checked_at=now, so
        # the negative is cached for the TTL. Keep any prior VinAudit price.
        va_logger.info(
            "VinAudit has no value for %s; cached negative for ~%s day(s)%s.",
            vehicle.vin, _vinaudit_ttl().days,
            " (keeping prior VinAudit price)" if owns_price else "",
        )
        return "served" if owns_price else "no_data"
    except (BlockedError, ScraperError) as exc:
        # Rate-limited / transient / misconfigured: REVERT the claim so a later request
        # can retry (don't lock the VIN out for the whole TTL). Keep any prior price.
        Vehicle.objects.filter(pk=vehicle.pk).update(vinaudit_checked_at=checked)
        vehicle.vinaudit_checked_at = checked
        va_logger.warning(
            "VinAudit unavailable for %s (%s)%s.",
            vehicle.vin, exc,
            "; serving the CACHED VinAudit price (stale)" if owns_price else "",
        )
        return "served" if owns_price else "unavailable"

    _apply_vinaudit(vehicle, sv, source, now)
    return "served"


def _apply_vinaudit(vehicle: Vehicle, sv: ScrapedVehicle, source, now) -> None:
    """Store a successful VinAudit valuation on the VIN (authoritative price)."""
    vehicle.estimated_price = sv.estimated_price
    vehicle.price_low = sv.price_low
    vehicle.price_high = sv.price_high
    vehicle.price_kind = sv.price_kind or "vinaudit_market"
    vehicle.currency = sv.currency or "USD"
    vehicle.source = source
    vehicle.source_url = sv.source_url
    vehicle.vinaudit_priced_at = now
    vehicle.vinaudit_checked_at = now
    raw = vehicle.raw_data if isinstance(vehicle.raw_data, dict) else {}
    raw["vinaudit"] = sv.raw_data
    vehicle.raw_data = raw
    vehicle.save(update_fields=[
        "estimated_price", "price_low", "price_high", "price_kind", "currency",
        "source", "source_url", "vinaudit_priced_at", "vinaudit_checked_at",
        "raw_data", "updated_at",
    ])


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

    Concurrency-safe: the read + create run in a transaction, the existing job is
    locked with select_for_update, and a partial unique constraint (plus the
    IntegrityError fallback) guarantees two concurrent callers can't create
    duplicate active jobs.
    """
    active = Q(status=ScrapeJob.Status.PENDING) | Q(status=ScrapeJob.Status.RUNNING)
    lookup = {"make__iexact": make, "model__iexact": model, "year": year, "trim__iexact": trim}
    with transaction.atomic():
        existing = ScrapeJob.objects.select_for_update().filter(**lookup).filter(active).first()
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
            job = existing
        else:
            try:
                with transaction.atomic():  # savepoint: a lost race won't abort the outer txn
                    job = ScrapeJob.objects.create(
                        make=make, model=model, year=year, trim=trim, vin=vin,
                        webhook_url=webhook_url, priority=priority, origin=origin, series=series,
                    )
            except IntegrityError:
                job = None  # a concurrent caller inserted it first — reuse it below
    if job is None:
        job = ScrapeJob.objects.filter(**lookup).filter(active).order_by("created_at").first()
    # Register this caller as a subscriber so each distinct requester of a deduped
    # job is notified with its OWN vin/webhook (not only whoever created the job).
    if job is not None and (vin or webhook_url):
        ScrapeSubscriber.objects.get_or_create(job=job, vin=vin, webhook_url=webhook_url)
    return job


def apply_model_to_vehicles(vm: VehicleModel) -> int:
    """Propagate a freshly scraped model's price to its VINs.

    Matches the VehicleModel's trim exactly: a trim-specific row (from a VIN
    search, e.g. Sport) updates only that trim's VINs, and a model-level row
    (trim="") updates only trim-less VINs — so a model-level (crawler) scrape never
    clobbers a VIN that has trim-specific pricing. Returns how many were updated.
    """
    # Exclude VinAudit-priced VINs in the query (don't even load them): their per-VIN
    # valuation is authoritative and only VinAudit (on-demand) may update it. This
    # also keeps `updated` equal to the number of rows actually re-linked.
    vehicles = Vehicle.objects.filter(
        make__iexact=vm.make, model__iexact=vm.model, year=vm.year, trim__iexact=vm.trim,
        vinaudit_priced_at__isnull=True,
    )
    updated = 0
    for vehicle in vehicles:
        if _link_model(vehicle, vm):
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

    def _persist(source, result):
        """Upsert (case-insensitive) the VehicleModel for a winning scrape result."""
        # Keep the headline (estimated) price WITHIN the shown range. Some sources
        # (e.g. Edmunds' new-car MSRP midpoint vs a thin used-listing range) can put
        # the suggested price outside [low, high]; widen the range to include it
        # instead of persisting an inverted min/max.
        if result.price_low is not None and result.price_high is not None:
            result.price_low = min(result.price_low, result.estimated_price)
            result.price_high = max(result.price_high, result.estimated_price)

        defaults = {
            "estimated_price": result.estimated_price,
            "price_low": result.price_low,
            "price_high": result.price_high,
            "price_kind": result.price_kind,
            "currency": result.currency or "USD",
            "source": source,
            "source_url": result.source_url,
            "raw_data": result.raw_data,
            "scrape_failures": 0,  # success clears the dead-letter counter
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
        if vm is None:
            try:
                vm = VehicleModel.objects.create(
                    make=make, model=model, year=year, trim=trim, **defaults
                )
                logger.info(
                    "Model %s %s %s resolved by '%s'.", year, make, model, source.name
                )
                return vm
            except IntegrityError:
                # Otra petición concurrente insertó esta fila primero (posiblemente
                # con otra capitalización) y el índice único case-insensitive la
                # rechazó. Reutiliza la existente en vez de propagar el error como
                # un job FAILED espurio. Relevante bajo multi-worker.
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
        logger.info("Model %s %s %s resolved by '%s'.", year, make, model, source.name)
        return vm

    errors: dict[str, str] = {}
    blocked_any = False
    fallback = None  # (source, result): a suggested price WITHOUT a min/max range
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

        # A source that returns a suggested price but NO min/max range (e.g. Edmunds'
        # new-car "suggests you pay $X") is a WEAK result — the UI would show only the
        # suggested value. Remember it, but keep trying another source (e.g. CarGurus)
        # that also yields a real range, so the cached row isn't "suggested only".
        if result.price_low is None or result.price_high is None:
            errors.setdefault(source.name, "Only a suggested price (no min/max range).")
            if fallback is None:
                fallback = (source, result)
            continue

        return _persist(source, result)

    # No source gave a full range; use the best suggested-only result if we got one.
    if fallback is not None:
        return _persist(*fallback)

    # Nothing at all. If a source was blocked, signal the worker to back off /
    # rotate (recover); otherwise it's a genuine not-found.
    if blocked_any:
        raise BlockedError(f"All sources blocked or empty for {make} {model} {year}.")
    raise AllSourcesFailed(f"{make} {model} {year}", errors)


def mark_model_failure(make: str, model: str, year, trim: str = "") -> None:
    """Bump a cached model's consecutive-failure counter (case-insensitive).

    When it passes SCRAPER_MODEL_MAX_FAILURES the crawler stops auto-refreshing it
    (a retired model would otherwise be re-scraped every cycle forever, burning IP
    quota). A later successful scrape resets it to 0. No-op if not cached yet.
    Uses .update() so it does NOT bump updated_at.
    """
    VehicleModel.objects.filter(
        make__iexact=make, model__iexact=model, year=year, trim__iexact=trim
    ).update(scrape_failures=F("scrape_failures") + 1)
