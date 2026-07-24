"""Background scraping worker and its controller.

Contains:
  * The logic to process the `ScrapeJob` queue (atomic claim + scrape + cache +
    webhook), shared by the `run_scrape_worker` command (foreground) and by the
    thread that starts alongside the API.
  * `WorkerController`: manages that loop in a daemon thread, with hot start/stop.
    A singleton `controller` is used by the autostart (AppConfig.ready), the API
    endpoints and the command.

The worker processes one job at a time because nodriver allows a single browser.
It must run on the machine with the residential IP + Chrome.
"""
from __future__ import annotations

import logging
import random
import threading
from datetime import timedelta
from typing import Callable

from django.conf import settings
from django.utils import timezone

from . import webhooks
from .models import ScrapeJob
from .providers.base import BlockedError
from .providers.nodriver_fetch import reset_profile
from .services import (
    PRIORITY_ONDEMAND,
    AllSourcesFailed,
    apply_model_to_vehicles,
    scrape_model_data,
)

# Live worker state (block/cool-down/activity), surfaced in the status endpoint.
# block_phase: "rotating" (fresh-session retries) | "ip_cooldown" | None.
# activity: {label, source, after_block} while scraping, else None.
WORKER_STATE = {
    "cooling_until": None, "consecutive_blocks": 0, "block_phase": None, "activity": None,
}

logger = logging.getLogger(__name__)

# A simple logger (print, self.stdout.write, or logger.info).
LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def reclaim_running() -> int:
    """Reset jobs stuck in RUNNING back to PENDING.

    If the process is killed mid-scrape, that job stays RUNNING forever. Called
    once at worker start (single worker) to requeue those orphans.
    """
    return ScrapeJob.objects.filter(status=ScrapeJob.Status.RUNNING).update(
        status=ScrapeJob.Status.PENDING, started_at=None
    )


def _has_pending_ondemand() -> bool:
    """True if a user (on-demand) search is waiting — it must not be delayed."""
    return ScrapeJob.objects.filter(
        status=ScrapeJob.Status.PENDING, priority__lte=PRIORITY_ONDEMAND
    ).exists()


def claim_next_job() -> ScrapeJob | None:
    """Take the next pending job and mark it 'running' atomically.

    The update conditioned on status=PENDING prevents two runners from taking the
    same job.
    """
    job = (
        ScrapeJob.objects.filter(status=ScrapeJob.Status.PENDING)
        .order_by("priority", "created_at")
        .first()
    )
    if job is None:
        return None
    taken = ScrapeJob.objects.filter(
        pk=job.pk, status=ScrapeJob.Status.PENDING
    ).update(status=ScrapeJob.Status.RUNNING, started_at=timezone.now())
    if not taken:
        return None
    job.refresh_from_db()
    return job


def process_job(job: ScrapeJob, log: LogFn = _noop) -> None:
    """Scrape a job, cache the result, propagate to the VINs and notify."""
    label = " ".join(str(x) for x in (job.year, job.make, job.model) if x)
    log(f"-> Scraping {label} (origin: {job.origin}, VIN: {job.vin or 'n/a'})...")
    # Crawl jobs are best-effort discovery (a 404 model is not worth retrying);
    # on-demand and refresh jobs get the full retry budget.
    max_attempts = 1 if job.origin == "crawl" else getattr(settings, "SCRAPER_JOB_MAX_ATTEMPTS", 3)

    try:
        vm = scrape_model_data(job.make, job.model, job.year, job.trim, series=job.series)
    except BlockedError:
        # Anti-bot block. A user (on-demand) search fails fast so the person is
        # not stalled; background jobs requeue to retry after the cool-down.
        if job.priority <= PRIORITY_ONDEMAND:
            job.status = ScrapeJob.Status.FAILED
            job.last_error = "Blocked (403) — source temporarily unavailable."
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "last_error", "finished_at"])
            log("   BLOCKED (403) — on-demand failed fast.")
            webhooks.notify(job, error=True)
        else:
            job.status = ScrapeJob.Status.PENDING
            job.started_at = None
            job.save(update_fields=["status", "started_at"])
            log("   BLOCKED (403) — requeued; backing off.")
        raise
    except AllSourcesFailed as exc:
        job.attempts += 1
        job.last_error = str(exc)
        if job.attempts >= max_attempts:
            job.status = ScrapeJob.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
            log(f"   FAILED after {job.attempts} attempts: {exc}")
            webhooks.notify(job, error=True)
        else:
            job.status = ScrapeJob.Status.PENDING
            job.started_at = None
            job.save(update_fields=["status", "attempts", "last_error", "started_at"])
            log(f"   Retry {job.attempts}/{max_attempts}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — an unexpected failure must not kill the worker
        job.attempts += 1
        job.status = ScrapeJob.Status.FAILED
        job.last_error = f"Unexpected error: {exc}"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
        logger.exception("Unexpected error processing job %s", job.pk)
        log(f"   Unexpected ERROR: {exc}")
        webhooks.notify(job, error=True)
        return

    job.result = vm
    job.status = ScrapeJob.Status.DONE
    job.attempts += 1  # count the successful try too (not only failures)
    job.finished_at = timezone.now()
    job.save(update_fields=["result", "status", "attempts", "finished_at"])
    n = apply_model_to_vehicles(vm)
    _rd = vm.raw_data if isinstance(vm.raw_data, dict) else {}
    _samples = _rd.get("listing_samples", _rd.get("market_listings", "?"))
    log(
        f"   OK: {label} -> {vm.estimated_price} {vm.currency} "
        f"({_samples} listings; {n} VIN(s) updated)."
    )
    webhooks.notify(job, vm)


def run_loop(
    stop_event: threading.Event,
    poll: int | None = None,
    once: bool = False,
    log: LogFn = _noop,
) -> None:
    """Main loop: process jobs until stop is requested (or until the queue is
    empty if `once`). Checks `stop_event` frequently."""
    poll = poll or getattr(settings, "SCRAPER_WORKER_POLL_SECONDS", 5)
    base_cd = getattr(settings, "SCRAPER_BLOCK_COOLDOWN", 300)
    max_cd = getattr(settings, "SCRAPER_BLOCK_COOLDOWN_MAX", 3600)
    # Let app initialization finish before the first DB query (avoids Django 6's
    # app-init DB-access warning and startup lock contention with the crawler).
    if stop_event.wait(getattr(settings, "SCRAPER_STARTUP_DELAY", 2)):
        return
    reclaimed = reclaim_running()
    if reclaimed:
        log(f"Reclaimed {reclaimed} orphan job(s) stuck in 'running'.")
    while not stop_event.is_set():
        WORKER_STATE["activity"] = None  # cleared between jobs; set while scraping
        try:
            job = claim_next_job()
        except Exception:  # noqa: BLE001 — transient DB issues must not kill the loop
            logger.exception("Error claiming the next job")
            job = None

        if job is None:
            if once:
                break
            # Interruptible wait: exits promptly if stop_event is set.
            stop_event.wait(poll)
            continue

        is_ondemand = job.priority <= PRIORITY_ONDEMAND
        try:
            process_job(job, log=log)
        except BlockedError:
            WORKER_STATE["consecutive_blocks"] += 1
            n = WORKER_STATE["consecutive_blocks"]
            rotations = getattr(settings, "SCRAPER_BLOCK_ROTATIONS", 3)
            if n <= rotations:
                # The ban is usually on our session/cookie (a manual browser on
                # the same IP still works). Rotate to a FRESH profile and retry
                # soon — this recovers fast when it's not the IP.
                ok = reset_profile()
                wait = getattr(settings, "SCRAPER_BLOCK_ROTATE_WAIT", 15) * n
                WORKER_STATE["block_phase"] = "rotating"
                log(f"Blocked (403) x{n}. {'Fresh profile' if ok else 'Profile reset FAILED'}"
                    f" — retrying in {wait}s (user searches still run now)...")
            else:
                # Fresh profiles kept getting blocked -> likely IP-level. Back off
                # exponentially and auto-resume when access returns.
                wait = min(max_cd, base_cd * (2 ** (n - rotations - 1)))
                WORKER_STATE["block_phase"] = "ip_cooldown"
                log(f"Blocked (403) x{n}. Looks IP-level; cooling down {wait}s...")
            WORKER_STATE["cooling_until"] = timezone.now() + timedelta(seconds=wait)
            # Interruptible wait: a user (on-demand) search breaks it to run now.
            remaining = wait
            while remaining > 0 and not stop_event.is_set():
                if _has_pending_ondemand():
                    break
                step = min(3, remaining)
                stop_event.wait(step)
                remaining -= step
            WORKER_STATE["cooling_until"] = None
            continue

        # A scrape that didn't block (success or plain not-found) means access
        # works again; clear the block state.
        if WORKER_STATE["consecutive_blocks"]:
            log("Access restored — resuming normal scraping.")
        WORKER_STATE["consecutive_blocks"] = 0
        WORKER_STATE["block_phase"] = None

        # Throttle only for background politeness — NEVER delay a user search:
        # skip the wait if this job was on-demand or another one is waiting.
        if not is_ondemand and not _has_pending_ondemand():
            delay = getattr(settings, "SCRAPER_WORKER_DELAY", 0)
            if delay and not stop_event.is_set():
                jitter = delay * 0.3
                stop_event.wait(max(1.0, delay + random.uniform(-jitter, jitter)))


class WorkerController:
    """Manages the worker loop in a daemon thread, with hot start/stop."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at = None

    def start(self, log: LogFn = _noop) -> bool:
        """Start the worker thread. Returns True if started, False if already running."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=run_loop,
                args=(self._stop,),
                kwargs={"log": log or _noop},
                name="scrape-worker",
                daemon=True,
            )
            self._thread.start()
            self._started_at = timezone.now()
            logger.info("Scraping worker started (background thread).")
            return True

    def stop(self, timeout: float = 65.0) -> bool:
        """Request stop and wait for the in-flight job to finish.

        Returns True if it was running and stopped, False if it was not running.
        """
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return False
            self._stop.set()
            thread = self._thread
        thread.join(timeout=timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._started_at = None
            logger.info("Scraping worker stopped.")
        return stopped

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        """Worker state + queue summary."""
        from django.db.models import Count

        counts = {
            row["status"]: row["n"]
            for row in ScrapeJob.objects.values("status").annotate(n=Count("id"))
        }
        cooling_until = WORKER_STATE["cooling_until"]
        return {
            "running": self.is_running(),
            "started_at": self._started_at,
            "cooling_down": cooling_until is not None,
            "cooling_until": cooling_until,
            "block_phase": WORKER_STATE["block_phase"],
            "consecutive_blocks": WORKER_STATE["consecutive_blocks"],
            "activity": WORKER_STATE["activity"],
            "queue": {
                "pending": counts.get(ScrapeJob.Status.PENDING, 0),
                "running": counts.get(ScrapeJob.Status.RUNNING, 0),
                "done": counts.get(ScrapeJob.Status.DONE, 0),
                "failed": counts.get(ScrapeJob.Status.FAILED, 0),
            },
        }


# Singleton used by the autostart, the API and the command.
controller = WorkerController()
