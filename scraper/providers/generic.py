"""Provider genérico configurable por selectores CSS.

Lee la configuración de la fuente (`ScraperSource.selectors`) y extrae los
campos del HTML. Sirve para la mayoría de sitios sin escribir código nuevo:
basta con crear/editar la fuente en el admin y ajustar los selectores.
"""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from .base import BaseProvider, ScrapedVehicle, ScraperError, VehicleNotFound, parse_price
from .playwright_fetch import PlaywrightFetchMixin
from .registry import register


@register("generic")
class GenericProvider(BaseProvider):
    """Extrae datos usando el mapa de selectores CSS de la fuente."""

    def fetch(self, vin: str) -> requests.Response:
        url = self.source.build_url(vin)
        session = self.build_session()
        try:
            response = session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ScraperError(f"Error de red en {self.source.name}: {exc}") from exc

        if response.status_code == 404:
            raise VehicleNotFound(
                f"{self.source.name} no tiene datos para el VIN {vin}."
            )
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} respondió estado {response.status_code}."
            )
        return response

    def parse(self, response: requests.Response, vin: str) -> ScrapedVehicle:
        selectores = self.source.selectors or {}
        soup = BeautifulSoup(response.text, "lxml")

        def texto(campo: str) -> str:
            selector = selectores.get(campo)
            if not selector:
                return ""
            el = soup.select_one(selector)
            return el.get_text(strip=True) if el else ""

        # Si el sitio marca "no encontrado" con algún elemento, respétalo.
        indicador = selectores.get("not_found")
        if indicador and soup.select_one(indicador):
            raise VehicleNotFound(
                f"{self.source.name} indica que no hay datos para el VIN {vin}."
            )

        anio_txt = texto("year")
        km_txt = texto("mileage")
        return ScrapedVehicle(
            vin=vin,
            make=texto("make"),
            model=texto("model"),
            year=int(anio_txt) if anio_txt.isdigit() else None,
            trim=texto("trim"),
            mileage=int("".join(c for c in km_txt if c.isdigit())) if any(c.isdigit() for c in km_txt) else None,
            estimated_price=parse_price(texto("estimated_price")),
            currency=texto("currency") or "USD",
            source_url=response.url,
            raw_data={"http_status": response.status_code},
        )


@register("playwright")
class PlaywrightGenericProvider(PlaywrightFetchMixin, GenericProvider):
    """Igual que GenericProvider pero renderiza JS con Chromium headless.

    Útil para sitios de fallback que también cargan datos por JavaScript. Usa
    los mismos selectores CSS de la fuente; solo cambia cómo obtiene el HTML.
    """
