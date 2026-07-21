"""Outbound webhook notification when a background scrape finishes.

When the worker resolves a `ScrapeJob`, it POSTs to the frontend callback URL
(the job's own, or `SCRAPER_WEBHOOK_URL` by default) with the VIN and the ready
market data. Best-effort: a webhook failure does not break scraping (the data is
cached and available on lookup anyway).
"""
from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _payload(job, vehicle_model) -> dict:
    vm = vehicle_model
    return {
        "event": "scrape.completed",
        "vin": job.vin,
        "make": vm.make if vm else job.make,
        "model": vm.model if vm else job.model,
        "year": vm.year if vm else job.year,
        "trim": job.trim,
        "estimated_price": str(vm.estimated_price) if vm and vm.estimated_price is not None else None,
        "currency": vm.currency if vm else "USD",
        "source": vm.source.name if vm and vm.source else None,
        "source_url": vm.source_url if vm else "",
        "status": job.status,
    }


def _error_payload(job) -> dict:
    return {
        "event": "scrape.failed",
        "vin": job.vin,
        "make": job.make,
        "model": job.model,
        "year": job.year,
        "trim": job.trim,
        "status": job.status,
        "error": job.last_error,
    }


def notify(job, vehicle_model=None, *, error: bool = False) -> bool:
    """Send a job's webhook. Returns True if delivered (2xx).

    Uses `job.webhook_url` if set, else `settings.SCRAPER_WEBHOOK_URL`. If none is
    configured, it does nothing (returns False without error).
    """
    url = job.webhook_url or getattr(settings, "SCRAPER_WEBHOOK_URL", "") or ""
    if not url:
        return False

    payload = _error_payload(job) if error else _payload(job, vehicle_model)
    timeout = getattr(settings, "SCRAPER_WEBHOOK_TIMEOUT", 10)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        logger.info("Webhook sent to %s for VIN %s (%s).", url, job.vin, job.status)
        return True
    except requests.RequestException as exc:
        logger.warning("Failed to send webhook to %s: %s", url, exc)
        return False
