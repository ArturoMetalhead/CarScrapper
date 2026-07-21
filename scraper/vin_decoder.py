"""Decodificación de VIN vía la API vPIC de NHTSA.

NHTSA (National Highway Traffic Safety Administration) expone una API pública,
gratuita y sin API key que decodifica un VIN a marca, modelo, año, versión,
carrocería, motor, etc. Es el estándar de la industria en EE.UU. y no aplica
bloqueos anti-bot.

Doc: https://vpic.nhtsa.dot.gov/api/

El resultado se normaliza a un dataclass `DecodedVin` con los campos que usa el
resto del sistema (marca/modelo/año/trim son la clave para buscar los datos de
mercado scrapeados por modelo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"


class VinDecodeError(Exception):
    """No se pudo decodificar el VIN (red, timeout, o VIN inválido para NHTSA)."""


@dataclass
class DecodedVin:
    """Datos decodificados de un VIN, normalizados."""

    vin: str
    make: str = ""
    model: str = ""
    year: int | None = None
    trim: str = ""
    body_class: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """True si tenemos lo mínimo para buscar por modelo (marca y modelo)."""
        return bool(self.make and self.model)


def _to_int(valor: Any) -> int | None:
    texto = str(valor or "").strip()
    return int(texto) if texto.isdigit() else None


def decode_vin(vin: str) -> DecodedVin:
    """Decodifica un VIN con NHTSA y devuelve un `DecodedVin`.

    Raises:
        VinDecodeError: si falla la red/HTTP o NHTSA reporta un VIN no válido.
    """
    url = VPIC_URL.format(vin=vin)
    timeout = getattr(settings, "SCRAPER_VIN_DECODE_TIMEOUT", 15)
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise VinDecodeError(f"Error decodificando VIN {vin} con NHTSA: {exc}") from exc

    resultados = data.get("Results") or []
    if not resultados:
        raise VinDecodeError(f"NHTSA no devolvió resultados para el VIN {vin}.")
    res = resultados[0]

    # ErrorCode "0" = decodificación limpia. Otros códigos pueden traer datos
    # parciales; los aceptamos si al menos hay marca y modelo.
    decoded = DecodedVin(
        vin=vin,
        make=(res.get("Make") or "").strip(),
        model=(res.get("Model") or "").strip(),
        year=_to_int(res.get("ModelYear")),
        trim=(res.get("Trim") or "").strip(),
        body_class=(res.get("BodyClass") or "").strip(),
        raw=res,
    )
    if not decoded.is_usable:
        error_text = res.get("ErrorText") or "sin marca/modelo"
        raise VinDecodeError(
            f"NHTSA no pudo decodificar marca/modelo del VIN {vin}: {error_text}"
        )
    return decoded
