"""Servicio de scraping con fallback entre fuentes.

`scrape_vehicle(vin)` recorre las fuentes activas por prioridad. Si una falla
(error de red, HTTP, parseo, o no tiene el VIN), pasa automáticamente a la
siguiente sin romper el flujo. Devuelve el primer resultado exitoso junto con
la fuente que lo resolvió.
"""
from __future__ import annotations

import logging

from .models import ScraperSource
from .providers import get_provider_class
from .providers.base import (
    AllSourcesFailed,
    ScrapedVehicle,
    ScraperError,
    VehicleNotFound,
)

logger = logging.getLogger(__name__)

# Re-exportado para que las vistas puedan importarlo desde services.
__all__ = [
    "scrape_vehicle",
    "AllSourcesFailed",
    "ScraperError",
    "VehicleNotFound",
    "ScrapedVehicle",
]


def scrape_vehicle(vin: str) -> tuple[ScrapedVehicle, ScraperSource]:
    """Scrapea la info del vehículo probando las fuentes en orden de prioridad.

    Args:
        vin: VIN ya validado (17 caracteres).

    Returns:
        Tupla (resultado, fuente) del primer scraping exitoso.

    Raises:
        AllSourcesFailed: si no hay fuentes activas o todas fallan.
    """
    fuentes = list(ScraperSource.objects.filter(is_active=True).order_by("priority"))
    if not fuentes:
        raise AllSourcesFailed(
            vin, {"config": "No hay fuentes de scraping activas configuradas."}
        )

    errores: dict[str, str] = {}
    for fuente in fuentes:
        provider_cls = get_provider_class(fuente.provider_key)
        provider = provider_cls(fuente)
        try:
            resultado = provider.scrape(vin)
            logger.info("VIN %s resuelto por la fuente '%s'.", vin, fuente.name)
            return resultado, fuente
        except VehicleNotFound as exc:
            logger.info("Fuente '%s' sin datos para VIN %s: %s", fuente.name, vin, exc)
            errores[fuente.name] = str(exc)
        except ScraperError as exc:
            logger.warning("Fuente '%s' falló para VIN %s: %s", fuente.name, vin, exc)
            errores[fuente.name] = str(exc)
        except Exception as exc:  # noqa: BLE001 — no dejar que un sitio roto tumbe el flujo
            logger.exception("Error inesperado en fuente '%s' para VIN %s", fuente.name, vin)
            errores[fuente.name] = f"Error inesperado: {exc}"

    # Ninguna fuente lo resolvió.
    raise AllSourcesFailed(vin, errores)
