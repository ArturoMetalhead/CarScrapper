"""Background crawler: discover models, seed the queue and refresh stale data.

The crawler does NOT scrape Edmunds itself — it only decides *what* to scrape and
enqueues low-priority `ScrapeJob`s that the single worker processes (so on-demand
user lookups always jump ahead). Discovery uses NHTSA (free, unblocked) to
enumerate make -> models per year; the worker then scrapes Edmunds for each.

Three responsibilities:
  * discover_frontier: build the (make, model, year) universe from NHTSA.
  * seed_crawl: enqueue models that have never been scraped (priority "crawl").
  * refresh_stale: re-enqueue VehicleModels past the cache TTL (priority "refresh").

`CrawlPlanner` runs these periodically in a daemon thread, kept alive/paused via
its `controller`-style API, and started alongside the API when SCRAPER_CRAWL_ENABLED.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from urllib.parse import quote

import requests
from django.conf import settings
from django.utils import timezone

from .models import ScrapeJob, VehicleModel
from .services import PRIORITY_CRAWL, PRIORITY_REFRESH, enqueue_scrape

logger = logging.getLogger(__name__)

# NHTSA endpoint to list models of a make/year for a given vehicle type.
_NHTSA_MODELS = (
    "https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/"
    "make/{make}/modelyear/{year}/vehicletype/{vtype}?format=json"
)
# Consumer vehicle types on Edmunds (excludes motorcycles, buses, trailers...).
_VEHICLE_TYPES = ("car", "truck", "mpv")

# Mainstream makes crawled by default (override with SCRAPER_CRAWL_MAKES).
MAINSTREAM_MAKES = [
    "Toyota", "Honda", "Ford", "Chevrolet", "Nissan", "Jeep", "Hyundai", "Kia",
    "Subaru", "GMC", "Ram", "Dodge", "Mazda", "Volkswagen", "BMW",
    "Mercedes-Benz", "Audi", "Lexus", "Tesla", "Chrysler", "Buick", "Cadillac",
    "Acura", "Infiniti", "Volvo", "Mitsubishi", "Lincoln", "Porsche",
    "Land Rover", "Mini",
]


def _makes() -> list[str]:
    return list(getattr(settings, "SCRAPER_CRAWL_MAKES", None) or MAINSTREAM_MAKES)


def _years() -> list[int]:
    back = getattr(settings, "SCRAPER_CRAWL_YEARS_BACK", 8)
    current = timezone.now().year
    # +2 so the NEXT model year (e.g. 2027 cars on sale mid-2026) is discovered too.
    return list(range(current - back + 1, current + 2))


def _models_for(make: str, year: int, timeout: int) -> set[str]:
    """Model names for a make/year across the consumer vehicle types (NHTSA)."""
    found: set[str] = set()
    for vtype in _VEHICLE_TYPES:
        url = _NHTSA_MODELS.format(make=quote(make), year=year, vtype=vtype)
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            for row in resp.json().get("Results", []):
                name = (row.get("Model_Name") or "").strip()
                if name:
                    found.add(name)
        except (requests.RequestException, ValueError) as exc:
            logger.warning("NHTSA discovery failed for %s %s (%s): %s", make, year, vtype, exc)
    return found


def discover_frontier(
    makes: list[str] | None = None,
    years: list[int] | None = None,
    stop_event: threading.Event | None = None,
) -> list[tuple[str, str, int]]:
    """Build the (make, model, year) universe from NHTSA. Interruptible."""
    makes = makes or _makes()
    years = years or _years()
    timeout = getattr(settings, "SCRAPER_VIN_DECODE_TIMEOUT", 15)
    frontier: list[tuple[str, str, int]] = []
    for make in makes:
        for year in years:
            if stop_event is not None and stop_event.is_set():
                return frontier
            for model in _models_for(make, year, timeout):
                frontier.append((make, model, year))
    logger.info("Crawl frontier discovered: %d model-years.", len(frontier))
    return frontier


def seed_crawl(frontier: list[tuple[str, str, int]], limit: int) -> int:
    """Enqueue up to `limit` never-scraped models as crawl jobs."""
    existing = {
        (m.lower(), mo.lower(), yr)
        for m, mo, yr in VehicleModel.objects.values_list("make", "model", "year")
    }
    queued = {
        (m.lower(), mo.lower(), yr)
        for m, mo, yr in ScrapeJob.objects.filter(
            status__in=(ScrapeJob.Status.PENDING, ScrapeJob.Status.RUNNING)
        ).values_list("make", "model", "year")
    }
    seeded = 0
    for make, model, year in frontier:
        key = (make.lower(), model.lower(), year)
        if key in existing or key in queued:
            continue
        enqueue_scrape(make, model, year, priority=PRIORITY_CRAWL, origin="crawl")
        queued.add(key)
        seeded += 1
        if seeded >= limit:
            break
    return seeded


def refresh_stale(limit: int) -> int:
    """Re-enqueue VehicleModels past the cache TTL as refresh jobs."""
    ttl_hours = getattr(settings, "SCRAPER_CACHE_TTL_HOURS", 24)
    cutoff = timezone.now() - timedelta(hours=ttl_hours)
    stale = (
        VehicleModel.objects.filter(updated_at__lt=cutoff)
        .order_by("updated_at")[:limit]
    )
    refreshed = 0
    for vm in stale:
        # Keep the trim so trim-specific rows (from VIN searches) actually refresh
        # instead of forever re-scraping the trim="" variant every cycle.
        enqueue_scrape(
            vm.make, vm.model, vm.year, trim=vm.trim,
            priority=PRIORITY_REFRESH, origin="refresh",
        )
        refreshed += 1
    return refreshed


def _pending_crawl() -> int:
    return ScrapeJob.objects.filter(
        status=ScrapeJob.Status.PENDING, origin="crawl"
    ).count()


def run_planner(stop_event: threading.Event, planner: "CrawlPlanner") -> None:
    """Periodically discover, top up the crawl queue and refresh stale data.

    Discovery is INCREMENTAL: models are enqueued make-by-make as they are
    discovered, so jobs appear within seconds instead of after the whole (slow)
    NHTSA sweep. The queue is kept topped up to `queue_min`.
    """
    interval = getattr(settings, "SCRAPER_CRAWL_PLAN_INTERVAL", 900)
    queue_min = getattr(settings, "SCRAPER_CRAWL_QUEUE_MIN", 20)
    batch = getattr(settings, "SCRAPER_CRAWL_BATCH", 50)
    discovery_ttl = timedelta(hours=getattr(settings, "SCRAPER_CRAWL_DISCOVERY_TTL_HOURS", 24))
    timeout = getattr(settings, "SCRAPER_VIN_DECODE_TIMEOUT", 15)

    while not stop_event.is_set():
        try:
            planner.last_refreshed = refresh_stale(batch)

            stale_frontier = planner.discovered_at is None or (
                timezone.now() - planner.discovered_at > discovery_ttl
            )
            if not planner.frontier or stale_frontier:
                # Build the frontier make-by-make, seeding as we go.
                planner.phase = "discovering"
                makes, years = _makes(), _years()
                frontier, seeded = [], 0
                for make in makes:
                    if stop_event.is_set():
                        break
                    for year in years:
                        if stop_event.is_set():
                            break
                        for model in _models_for(make, year, timeout):
                            frontier.append((make, model, year))
                    planner.frontier = list(frontier)  # progress (visible in status)
                    if _pending_crawl() < queue_min:
                        seeded += seed_crawl(frontier, batch)
                planner.frontier = frontier
                planner.discovered_at = timezone.now()
                planner.last_seeded = seeded
            else:
                planner.phase = "seeding"
                planner.last_seeded = (
                    seed_crawl(planner.frontier, batch) if _pending_crawl() < queue_min else 0
                )
            planner.phase = "idle"
            logger.info(
                "Crawl plan cycle: seeded=%s refreshed=%s (frontier=%d).",
                planner.last_seeded, planner.last_refreshed, len(planner.frontier),
            )
        except Exception:  # noqa: BLE001 — a bad cycle must not kill the planner
            logger.exception("Crawl planner cycle failed")
            planner.phase = "idle"
        stop_event.wait(interval)


class CrawlPlanner:
    """Runs the crawl planner in a daemon thread, with hot start/stop."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at = None
        self.phase = "idle"
        self.frontier: list[tuple[str, str, int]] = []
        self.discovered_at = None
        self.last_seeded = 0
        self.last_refreshed = 0

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=run_planner, args=(self._stop, self), name="crawl-planner", daemon=True
            )
            self._thread.start()
            self._started_at = timezone.now()
            logger.info("Crawl planner started.")
            return True

    def stop(self, timeout: float = 10.0) -> bool:
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return False
            self._stop.set()
            thread = self._thread
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._started_at = None
            logger.info("Crawl planner stopped.")
        return stopped

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        from django.db.models import Count

        by_origin = {
            row["origin"]: row["n"]
            for row in ScrapeJob.objects.filter(status=ScrapeJob.Status.PENDING)
            .values("origin").annotate(n=Count("id"))
        }
        return {
            "running": self.is_running(),
            "phase": self.phase if self.is_running() else "stopped",
            "started_at": self._started_at,
            "frontier_size": len(self.frontier),
            "discovered_at": self.discovered_at,
            "last_seeded": self.last_seeded,
            "last_refreshed": self.last_refreshed,
            "pending_by_origin": by_origin,
        }


# Singleton used by the autostart, the API and the commands.
planner = CrawlPlanner()
