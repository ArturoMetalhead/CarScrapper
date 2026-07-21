"""Provider para Edmunds (https://www.edmunds.com/).

Edmunds es un sitio con mucho JavaScript: la info del vehículo no suele estar
en el HTML plano, sino en bloques JSON embebidos (JSON-LD `application/ld+json`
o un estado precargado). Este provider intenta:

  1. Extraer datos estructurados de los bloques <script type="application/ld+json">.
  2. Si no encuentra, cae a los selectores CSS configurados en la fuente
     (comportamiento del GenericProvider).

NOTA: los caminos JSON y selectores concretos dependen de la estructura real
de Edmunds en el momento de usarlo; puede que haya que ajustarlos. La
arquitectura de fallback hace que, si Edmunds cambia y este provider deja de
extraer datos, el sistema pase a la siguiente fuente configurada.
"""
from __future__ import annotations

import json

from bs4 import BeautifulSoup

from .base import ScrapedVehicle, parse_price
from .generic import GenericProvider
from .playwright_fetch import PlaywrightFetchMixin
from .registry import register


@register("edmunds")
class EdmundsProvider(PlaywrightFetchMixin, GenericProvider):
    """Scraper específico para Edmunds.

    Usa Playwright (por el mixin) para renderizar el JavaScript de la página y
    luego extrae los datos del JSON-LD embebido; si no los encuentra, cae al
    parseo por selectores CSS del GenericProvider.
    """

    def parse(self, response, vin: str) -> ScrapedVehicle:
        soup = BeautifulSoup(response.text, "lxml")

        datos = self._extraer_json_ld(soup)
        if datos:
            resultado = self._desde_json_ld(datos, vin, response)
            if resultado.estimated_price or resultado.make:
                return resultado

        # Respaldo: usa el parseo genérico por selectores CSS de la fuente.
        return super().parse(response, vin)

    # --- Helpers ----------------------------------------------------------
    def _extraer_json_ld(self, soup: BeautifulSoup) -> dict | None:
        """Busca un bloque JSON-LD que describa un Vehicle/Car/Product."""
        for tag in soup.find_all("script", type="application/ld+json"):
            contenido = tag.string or tag.get_text()
            if not contenido:
                continue
            try:
                data = json.loads(contenido)
            except (json.JSONDecodeError, ValueError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    continue
                tipo = item.get("@type", "")
                tipos = tipo if isinstance(tipo, list) else [tipo]
                if any(t in ("Vehicle", "Car", "Product") for t in tipos):
                    return item
        return None

    def _desde_json_ld(self, data: dict, vin: str, response) -> ScrapedVehicle:
        oferta = data.get("offers") or {}
        if isinstance(oferta, list):
            oferta = oferta[0] if oferta else {}

        precio = oferta.get("price") or data.get("price")
        moneda = oferta.get("priceCurrency") or "USD"
        anio = data.get("modelDate") or data.get("productionDate") or data.get("vehicleModelDate")

        return ScrapedVehicle(
            vin=vin,
            make=self._nombre(data.get("brand")) or data.get("manufacturer", ""),
            model=data.get("model", "") if isinstance(data.get("model"), str) else "",
            year=int(str(anio)[:4]) if anio and str(anio)[:4].isdigit() else None,
            trim=data.get("vehicleConfiguration", ""),
            mileage=self._millas(data.get("mileageFromOdometer")),
            estimated_price=parse_price(str(precio)) if precio is not None else None,
            currency=moneda,
            source_url=response.url,
            raw_data={"json_ld": data},
        )

    @staticmethod
    def _nombre(valor) -> str:
        if isinstance(valor, dict):
            return valor.get("name", "")
        return valor or "" if isinstance(valor, str) else ""

    @staticmethod
    def _millas(valor) -> int | None:
        if isinstance(valor, dict):
            valor = valor.get("value")
        if valor is None:
            return None
        digitos = "".join(c for c in str(valor) if c.isdigit())
        return int(digitos) if digitos else None
