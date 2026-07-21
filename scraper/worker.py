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
import threading
from typing import Callable

from django.conf import settings
from django.utils import timezone

from . import webhooks
from .models import ScrapeJob
from .services import AllSourcesFailed, apply_model_to_vehicles, scrape_model_data

logger = logging.getLogger(__name__)

# A simple logger (print, self.stdout.write, or logger.info).
LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


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
    job.attempts += 1
    # Crawl jobs are best-effort discovery (a 404 model is not worth retrying);
    # on-demand and refresh jobs get the full retry budget.
    max_attempts = 1 if job.origin == "crawl" else getattr(settings, "SCRAPER_JOB_MAX_ATTEMPTS", 3)

    try:
        vm = scrape_model_data(job.make, job.model, job.year, job.trim)
    except AllSourcesFailed as exc:
        job.last_error = str(exc)
        if job.attempts >= max_attempts:
            job.status = ScrapeJob.Status.FAILED
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
            log(f"   FAILED after {job.attempts} attempts: {exc}")
            webhooks.notify(job, error=True)
        else:
            job.status = ScrapeJob.Status.PENDING  # requeue for retry
            job.started_at = None
            job.save(update_fields=["status", "attempts", "last_error", "started_at"])
            log(f"   Retry {job.attempts}/{max_attempts}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — an unexpected failure must not kill the worker
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
    job.finished_at = timezone.now()
    job.save(update_fields=["result", "status", "attempts", "finished_at"])
    n = apply_model_to_vehicles(vm)
    log(
        f"   OK: {label} -> {vm.estimated_price} {vm.currency} "
        f"({vm.raw_data.get('samples', '?')} listings; {n} VIN(s) updated)."
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
    while not stop_event.is_set():
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
        process_job(job, log=log)


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
        return {
            "running": self.is_running(),
            "started_at": self._started_at,
            "queue": {
                "pending": counts.get(ScrapeJob.Status.PENDING, 0),
                "running": counts.get(ScrapeJob.Status.RUNNING, 0),
                "done": counts.get(ScrapeJob.Status.DONE, 0),
                "failed": counts.get(ScrapeJob.Status.FAILED, 0),
            },
        }


# Singleton used by the autostart, the API and the command.
controller = WorkerController()
