"""Notificación por webhook saliente cuando un scrape en segundo plano termina.

Cuando el worker resuelve un `ScrapeJob`, hace un POST a la URL de callback del
frontend (la del propio job, o `SCRAPER_WEBHOOK_URL` por defecto) con el VIN y
los datos de mercado ya listos. Es best-effort: un fallo de webhook no rompe el
scraping (el dato queda igualmente en caché y disponible al consultar el VIN).
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


def _payload_error(job) -> dict:
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
    """Envía el webhook de un job. Devuelve True si se entregó (2xx).

    Usa `job.webhook_url` si está, o `settings.SCRAPER_WEBHOOK_URL`. Si no hay
    ninguna configurada, no hace nada (devuelve False sin error).
    """
    url = job.webhook_url or getattr(settings, "SCRAPER_WEBHOOK_URL", "") or ""
    if not url:
        return False

    payload = _payload_error(job) if error else _payload(job, vehicle_model)
    timeout = getattr(settings, "SCRAPER_WEBHOOK_TIMEOUT", 10)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        logger.info("Webhook enviado a %s para VIN %s (%s).", url, job.vin, job.status)
        return True
    except requests.RequestException as exc:
        logger.warning("Fallo al enviar webhook a %s: %s", url, exc)
        return False
