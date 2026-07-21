"""Clase base y estructuras compartidas de los providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.conf import settings


class ScraperError(Exception):
    """Error durante el scraping (red, HTTP, parseo, config, etc.)."""


class VehicleNotFound(ScraperError):
    """La fuente respondió pero no tiene datos para ese VIN."""


class AllSourcesFailed(ScraperError):
    """Ninguna de las fuentes configuradas pudo resolver el VIN."""

    def __init__(self, vin: str, errores: dict[str, str]):
        self.vin = vin
        self.errores = errores
        detalle = "; ".join(f"{fuente}: {msg}" for fuente, msg in errores.items())
        super().__init__(
            f"Ninguna fuente pudo resolver el VIN {vin}. Detalle -> {detalle}"
        )


@dataclass
class ScrapedVehicle:
    """Resultado del scraping, listo para persistir en el modelo Vehicle."""

    vin: str
    make: str = ""
    model: str = ""
    year: int | None = None
    trim: str = ""
    mileage: int | None = None
    estimated_price: Decimal | None = None
    currency: str = "USD"
    source_url: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)

    def as_model_kwargs(self) -> dict[str, Any]:
        """Convierte el dataclass en kwargs para crear/actualizar el modelo."""
        return {
            "make": self.make,
            "model": self.model,
            "year": self.year,
            "trim": self.trim,
            "mileage": self.mileage,
            "estimated_price": self.estimated_price,
            "currency": self.currency,
            "source_url": self.source_url,
            "raw_data": self.raw_data,
        }


def parse_price(text: str) -> Decimal | None:
    """Extrae un valor decimal de un texto tipo '$18,500' o '18.500,00'."""
    if not text:
        return None
    limpio = "".join(ch for ch in text if ch.isdigit() or ch in ".,")
    if not limpio:
        return None
    # Normaliza: quita separadores de miles y deja el punto decimal.
    if "," in limpio and "." in limpio:
        # El último separador que aparece es el decimal.
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".") if limpio.count(",") == 1 else limpio.replace(",", "")
    try:
        return Decimal(limpio)
    except InvalidOperation:
        return None


class BaseProvider:
    """Contrato base de un provider de scraping.

    Las subclases implementan `fetch` y `parse`. El método `scrape` es el
    template que orquesta ambos.
    """

    def __init__(self, source):
        # `source` es una instancia de scraper.models.ScraperSource.
        self.source = source

    # --- API pública -----------------------------------------------------
    def scrape(self, vin: str) -> ScrapedVehicle:
        response = self.fetch(vin)
        resultado = self.parse(response, vin)
        if not resultado.source_url:
            resultado.source_url = self.source.build_url(vin)
        return resultado

    def scrape_model(
        self, make: str, model: str, year: int | None = None, trim: str = ""
    ) -> ScrapedVehicle:
        """Scrapea los datos de mercado de un MODELO (para el worker de fondo).

        Devuelve un `ScrapedVehicle` con vin vacío: solo interesan
        make/model/year/trim y `estimated_price`.
        """
        response = self.fetch_model(make, model, year, trim)
        resultado = self.parse_model(response, make, model, year, trim)
        if not resultado.source_url:
            resultado.source_url = self.source.build_model_url(make, model, year, trim)
        return resultado

    # --- A implementar por subclases -------------------------------------
    def fetch(self, vin: str) -> requests.Response:
        raise NotImplementedError

    def parse(self, response: requests.Response, vin: str) -> ScrapedVehicle:
        raise NotImplementedError

    def fetch_model(
        self, make: str, model: str, year: int | None = None, trim: str = ""
    ) -> requests.Response:
        raise NotImplementedError

    def parse_model(
        self, response, make: str, model: str, year: int | None = None, trim: str = ""
    ) -> ScrapedVehicle:
        raise NotImplementedError

    # --- Utilidades compartidas ------------------------------------------
    def build_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": settings.SCRAPER_USER_AGENT})
        proxy = self.proxy_url
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        return session

    @property
    def timeout(self) -> int:
        return self.source.timeout or settings.SCRAPER_TIMEOUT

    @property
    def proxy_url(self) -> str:
        """URL de proxy a usar (vacía si no hay). Ver SCRAPER_PROXY."""
        return getattr(settings, "SCRAPER_PROXY", "") or ""
