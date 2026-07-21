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
import re
from decimal import Decimal, InvalidOperation
from statistics import median

from bs4 import BeautifulSoup

from .base import ScrapedVehicle, parse_price
from .generic import GenericProvider
from .nodriver_fetch import NodriverFetchMixin
from .registry import register

# Rango plausible de precio de coche (USD) para filtrar ruido al agregar.
_PRECIO_MIN = 1000
_PRECIO_MAX = 200000
_RE_PRECIO = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)")


@register("edmunds")
class EdmundsProvider(NodriverFetchMixin, GenericProvider):
    """Scraper específico para Edmunds.

    Edmunds está protegido por DataDome, que bloquea con 403 a los navegadores
    automatizados por Playwright/Selenium (se verificó en este proyecto). Por
    eso usa `NodriverFetchMixin`: un Chrome real vía nodriver, con perfil
    persistente y reintento, que atraviesa el bloqueo. Tras renderizar, extrae
    los datos del JSON-LD embebido; si no los encuentra, cae al parseo por
    selectores CSS del GenericProvider.
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

    def parse_model(
        self, response, make: str, model: str, year=None, trim: str = ""
    ) -> ScrapedVehicle:
        """Estima el precio de mercado del modelo/año agregando los listados.

        La página de modelo de Edmunds no expone un precio único ni JSON-LD con
        precio; muestra muchos listados (`.heading-3` por defecto, configurable
        vía selectors['model_price_nodes']). Tomamos la MEDIANA de esos precios,
        robusta frente a outliers (p. ej. un modelo de otro año colado en la
        página).
        """
        soup = BeautifulSoup(response.text, "lxml")
        selectores = self.source.selectors or {}

        # 1) Precios desde nodos de listado (más fiable que el texto completo).
        selector = selectores.get("model_price_nodes", ".heading-3")
        precios: list[Decimal] = []
        for nodo in soup.select(selector):
            precio = self._precio_valido(nodo.get_text(" ", strip=True))
            if precio is not None:
                precios.append(precio)

        # 2) Respaldo: regex sobre todo el texto si los nodos no dieron nada.
        if not precios:
            for m in _RE_PRECIO.finditer(soup.get_text(" ", strip=True)):
                precio = self._precio_valido(m.group(0))
                if precio is not None:
                    precios.append(precio)

        estimado = Decimal(round(median(precios))) if precios else None

        return ScrapedVehicle(
            vin="",
            make=make,
            model=model,
            year=year,
            trim=trim or "",
            estimated_price=estimado,
            currency="USD",
            source_url=response.url,
            raw_data={
                "metodo": "mediana_listados",
                "muestras": len(precios),
                "min": float(min(precios)) if precios else None,
                "max": float(max(precios)) if precios else None,
                "mediana": float(estimado) if estimado is not None else None,
            },
        )

    @staticmethod
    def _precio_valido(texto: str) -> Decimal | None:
        """Extrae el primer precio del texto y lo valida contra el rango plausible.

        Los precios de Edmunds usan la coma como separador de MILLARES (formato
        US: "$15,779"). Por eso quitamos las comas y parseamos como entero, en
        vez de usar parse_price (que ante una sola coma asumiría decimal).
        """
        m = _RE_PRECIO.search(texto)
        if not m:
            return None
        try:
            valor = Decimal(m.group(1).replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        if not (_PRECIO_MIN <= valor <= _PRECIO_MAX):
            return None
        return valor

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
