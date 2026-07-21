"""Capa de servicio: resolución rápida por VIN + scraping por modelo en fondo.

Flujo de una búsqueda por VIN (`resolve_vin`):
  1. Si el VIN ya está resuelto y su dato de modelo está FRESCO -> se devuelve al
     instante (caché).
  2. Si no, se decodifica el VIN con NHTSA (marca/modelo/año/trim).
  3. Se busca el dato de mercado por MODELO en caché (`VehicleModel`).
     - Fresco -> se enlaza y se devuelve al instante.
     - Ausente o caducado -> se ENCOLA un trabajo de scraping y se responde
       "processing"; un worker lo procesará en segundo plano y avisará por
       webhook. Si había un dato caducado, se devuelve mientras tanto.

El scraping real por modelo (`scrape_model_data`) lo ejecuta el worker, probando
las fuentes activas por prioridad con fallback.
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
    ScrapedVehicle,
    ScraperError,
    VehicleNotFound,
)
from .vin_decoder import VinDecodeError, decode_vin

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_vin",
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

# Estados de resolución que devuelve resolve_vin.
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"


def _ttl() -> timedelta:
    return timedelta(hours=getattr(settings, "SCRAPER_CACHE_TTL_HOURS", 24))


def is_fresh(vehicle_model: VehicleModel | None) -> bool:
    """True si el dato de modelo existe y no ha superado el TTL de caché."""
    if not vehicle_model:
        return False
    return timezone.now() - vehicle_model.updated_at <= _ttl()


def _find_model(make: str, model: str, year: int | None) -> VehicleModel | None:
    """Busca el dato de mercado por modelo (granularidad marca/modelo/año)."""
    return (
        VehicleModel.objects.filter(
            make__iexact=make, model__iexact=model, year=year
        )
        .order_by("-updated_at")
        .first()
    )


def _link_model(vehicle: Vehicle, vm: VehicleModel) -> None:
    """Copia el precio de mercado del modelo al VIN y guarda."""
    vehicle.vehicle_model = vm
    vehicle.estimated_price = vm.estimated_price
    vehicle.currency = vm.currency or "USD"
    vehicle.source = vm.source
    vehicle.source_url = vm.source_url
    vehicle.save(update_fields=[
        "vehicle_model", "estimated_price", "currency", "source", "source_url", "updated_at"
    ])


def resolve_vin(vin: str, webhook_url: str = "") -> tuple[Vehicle, str]:
    """Resuelve un VIN: caché instantáneo o encola scraping en segundo plano.

    Returns:
        (vehicle, status) donde status es "ready" (dato fresco disponible) o
        "processing" (se encoló; el precio llegará por webhook / al consultar).

    Raises:
        VinDecodeError: si NHTSA no puede decodificar el VIN.
    """
    vehicle = (
        Vehicle.objects.select_related("vehicle_model").filter(vin=vin).first()
    )

    # 1) Caché por VIN con dato de modelo fresco.
    if vehicle and is_fresh(vehicle.vehicle_model):
        return vehicle, STATUS_READY

    # 2) Aseguramos los datos decodificados (reusar si ya los teníamos).
    if vehicle and vehicle.make and vehicle.model:
        make, model, year, trim = vehicle.make, vehicle.model, vehicle.year, vehicle.trim
    else:
        decoded = decode_vin(vin)  # puede lanzar VinDecodeError
        make, model, year, trim = decoded.make, decoded.model, decoded.year, decoded.trim
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

    # 3) Dato de mercado por modelo.
    vm = _find_model(make, model, year)
    if is_fresh(vm):
        _link_model(vehicle, vm)
        return vehicle, STATUS_READY

    # 4) Ausente o caducado -> encolar. Si hay uno caducado, lo mostramos ya.
    enqueue_scrape(make, model, year, trim="", vin=vin, webhook_url=webhook_url)
    if vm:
        _link_model(vehicle, vm)
    return vehicle, STATUS_PROCESSING


def enqueue_scrape(
    make: str,
    model: str,
    year: int | None = None,
    trim: str = "",
    vin: str = "",
    webhook_url: str = "",
) -> ScrapeJob:
    """Encola un trabajo de scraping por modelo, evitando duplicados.

    Si ya hay un trabajo pendiente o en proceso para el mismo modelo, se reutiliza
    (no se crea otro). Sirve tanto para búsquedas bajo demanda como para precargar
    una lista de VINs/modelos.
    """
    existente = (
        ScrapeJob.objects.filter(
            make__iexact=make, model__iexact=model, year=year
        )
        .filter(Q(status=ScrapeJob.Status.PENDING) | Q(status=ScrapeJob.Status.RUNNING))
        .first()
    )
    if existente:
        # Si el trabajo previo no tenía webhook/vin y ahora sí, los completamos.
        cambios = []
        if vin and not existente.vin:
            existente.vin = vin
            cambios.append("vin")
        if webhook_url and not existente.webhook_url:
            existente.webhook_url = webhook_url
            cambios.append("webhook_url")
        if cambios:
            existente.save(update_fields=cambios)
        return existente

    return ScrapeJob.objects.create(
        make=make, model=model, year=year, trim=trim, vin=vin, webhook_url=webhook_url
    )


def apply_model_to_vehicles(vm: VehicleModel) -> int:
    """Propaga el precio de un modelo recién scrapeado a todos sus VINs.

    Tras resolver un `VehicleModel`, actualiza los `Vehicle` ya conocidos de ese
    modelo/año para que sus consultas devuelvan el dato fresco. Devuelve cuántos
    vehículos se actualizaron.
    """
    vehiculos = Vehicle.objects.filter(
        make__iexact=vm.make, model__iexact=vm.model, year=vm.year
    )
    actualizados = 0
    for vehiculo in vehiculos:
        _link_model(vehiculo, vm)
        actualizados += 1
    return actualizados


def scrape_model_data(
    make: str, model: str, year: int | None = None, trim: str = ""
) -> VehicleModel:
    """Scrapea el dato de mercado de un modelo probando las fuentes por prioridad.

    Lo usa el worker. Guarda/actualiza el `VehicleModel` y lo devuelve.

    Raises:
        AllSourcesFailed: si ninguna fuente con scraping por modelo lo resuelve.
    """
    fuentes = list(
        ScraperSource.objects.filter(is_active=True)
        .exclude(model_path_template="")
        .order_by("priority")
    )
    if not fuentes:
        raise AllSourcesFailed(
            f"{make} {model}", {"config": "No hay fuentes con scraping por modelo."}
        )

    errores: dict[str, str] = {}
    for fuente in fuentes:
        provider = get_provider_class(fuente.provider_key)(fuente)
        try:
            resultado = provider.scrape_model(make, model, year, trim)
        except VehicleNotFound as exc:
            errores[fuente.name] = str(exc)
            continue
        except ScraperError as exc:
            logger.warning("Fuente '%s' falló para %s %s: %s", fuente.name, make, model, exc)
            errores[fuente.name] = str(exc)
            continue
        except Exception as exc:  # noqa: BLE001 — no dejar que un sitio roto tumbe el worker
            logger.exception("Error inesperado en '%s' para %s %s", fuente.name, make, model)
            errores[fuente.name] = f"Error inesperado: {exc}"
            continue

        if resultado.estimated_price is None:
            errores[fuente.name] = "No se pudo extraer precio del modelo."
            continue

        vm, _ = VehicleModel.objects.update_or_create(
            make=make,
            model=model,
            year=year,
            trim="",  # granularidad marca/modelo/año (la URL de Edmunds no usa trim)
            defaults={
                "estimated_price": resultado.estimated_price,
                "currency": resultado.currency or "USD",
                "source": fuente,
                "source_url": resultado.source_url,
                "raw_data": resultado.raw_data,
            },
        )
        logger.info("Modelo %s %s %s resuelto por '%s'.", year, make, model, fuente.name)
        return vm

    raise AllSourcesFailed(f"{make} {model} {year}", errores)
