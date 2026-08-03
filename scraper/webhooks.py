"""Outbound webhook notification when a background scrape finishes.

When the worker resolves a `ScrapeJob`, it POSTs to the frontend callback URL
(the job's own, or `SCRAPER_WEBHOOK_URL` by default) with the VIN and the ready
market data. Best-effort: a webhook failure does not break scraping (the data is
cached and available on lookup anyway).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def _host_is_safe(url: str) -> bool:
    """Re-resolve the URL host and reject private/reserved IPs — SSRF guard at SEND
    time (the submit-time serializer check is TOCTOU: DNS can rebind before delivery).
    Combined with allow_redirects=False on the POST, this blocks the redirect and
    rebinding bypasses to cloud-metadata / internal hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def _payload(job, vehicle_model, vehicle=None) -> dict:
    # Prefer the VIN's OWN stored price (the VinAudit per-VIN valuation when present),
    # so the webhook matches GET /api/vehicles/<vin>/. Fall back to the scraped model,
    # then to the job. Vehicle and VehicleModel share these field names.
    obj = vehicle if (vehicle is not None and vehicle.estimated_price is not None) else vehicle_model
    return {
        "event": "scrape.completed",
        "vin": job.vin,
        "make": obj.make if obj else job.make,
        "model": obj.model if obj else job.model,
        "year": obj.year if obj else job.year,
        "trim": obj.trim if obj else job.trim,
        "estimated_price": str(obj.estimated_price) if obj and obj.estimated_price is not None else None,
        "price_low": str(obj.price_low) if obj and obj.price_low is not None else None,
        "price_high": str(obj.price_high) if obj and obj.price_high is not None else None,
        "price_kind": obj.price_kind if obj else "",
        "currency": obj.currency if obj else "USD",
        "source": obj.source.name if obj and obj.source else None,
        "source_url": obj.source_url if obj else "",
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
    from .models import ScrapeSubscriber, Vehicle

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
        if error:
            payload = _error_payload(job)
        else:
            # Use THIS VIN's own row (its VinAudit price when set), not the shared model.
            veh = Vehicle.objects.filter(vin=vin).select_related("source").first() if vin else None
            payload = _payload(job, vehicle_model, veh)
        # SSRF: re-validate the host at send time AND refuse redirects (a validated
        # public host must not 302 us to internal, and DNS must not have rebound).
        if not _host_is_safe(url):
            logger.warning("Webhook %s failed the SSRF re-check; not delivering (VIN %s).", url, vin)
            continue
        try:
            resp = requests.post(
                url, json={**payload, "vin": vin}, timeout=timeout, allow_redirects=False
            )
            resp.raise_for_status()
            delivered = True
            logger.info("Webhook sent to %s for VIN %s (%s).", url, vin, job.status)
            if pk is not None:
                ScrapeSubscriber.objects.filter(pk=pk).update(notified=True)
        except requests.RequestException as exc:
            logger.warning("Failed to send webhook to %s: %s", url, type(exc).__name__)
    return delivered
