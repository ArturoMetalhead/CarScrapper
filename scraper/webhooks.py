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
        "price_low": str(vm.price_low) if vm and vm.price_low is not None else None,
        "price_high": str(vm.price_high) if vm and vm.price_high is not None else None,
        "price_kind": vm.price_kind if vm else "",
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
    """Notify EVERY subscriber of a job (each with its own VIN). Returns True if at
    least one webhook was delivered (2xx).

    Concurrent callers of a deduped job each register a ScrapeSubscriber, so all of
    them get a notification. Falls back to the job's own vin/webhook (or the global
    `SCRAPER_WEBHOOK_URL`) when no subscribers were recorded (e.g. crawler jobs).
    """
    from .models import ScrapeSubscriber

    payload = _error_payload(job) if error else _payload(job, vehicle_model)
    timeout = getattr(settings, "SCRAPER_WEBHOOK_TIMEOUT", 10)
    default_url = getattr(settings, "SCRAPER_WEBHOOK_URL", "") or ""

    all_subs = list(job.subscribers.all())
    pending = [s for s in all_subs if not s.notified]
    # Notify each not-yet-notified subscriber. Only when the job has NO subscribers
    # at all (e.g. a crawler/refresh job) fall back to its own vin/webhook.
    if all_subs:
        targets = [(s.vin, s.webhook_url, s.pk) for s in pending]
    else:
        targets = [(job.vin, job.webhook_url, None)]

    delivered = False
    for vin, webhook_url, pk in targets:
        url = webhook_url or default_url
        if not url:
            continue
        try:
            resp = requests.post(url, json={**payload, "vin": vin}, timeout=timeout)
            resp.raise_for_status()
            delivered = True
            logger.info("Webhook sent to %s for VIN %s (%s).", url, vin, job.status)
            if pk is not None:
                ScrapeSubscriber.objects.filter(pk=pk).update(notified=True)
        except requests.RequestException as exc:
            logger.warning("Failed to send webhook to %s: %s", url, exc)
    return delivered
