"""VIN decoding via NHTSA's vPIC API.

NHTSA (National Highway Traffic Safety Administration) exposes a public, free,
no-API-key API that decodes a VIN into make, model, year, trim, body class,
engine, etc. It is the US industry standard and applies no anti-bot blocking.

Docs: https://vpic.nhtsa.dot.gov/api/

The result is normalized into a `DecodedVin` dataclass with the fields the rest
of the system uses (make/model/year/trim are the key to look up the market data
scraped per model).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests
from django.conf import settings

VPIC_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}?format=json"


class VinDecodeError(Exception):
    """Could not decode the VIN (network, timeout, or invalid VIN for NHTSA)."""


@dataclass
class DecodedVin:
    """Normalized decoded VIN data."""

    vin: str
    make: str = ""
    model: str = ""
    year: int | None = None
    trim: str = ""
    series: str = ""
    body_class: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """True if we have the minimum to look up by model (make and model)."""
        return bool(self.make and self.model)


def _to_int(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def decode_vin(vin: str) -> DecodedVin:
    """Decode a VIN with NHTSA and return a `DecodedVin`.

    Raises:
        VinDecodeError: on network/HTTP failure or if NHTSA reports an invalid VIN.
    """
    url = VPIC_URL.format(vin=vin)
    timeout = getattr(settings, "SCRAPER_VIN_DECODE_TIMEOUT", 15)
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise VinDecodeError(f"Error decoding VIN {vin} with NHTSA: {exc}") from exc

    results = data.get("Results") or []
    if not results:
        raise VinDecodeError(f"NHTSA returned no results for VIN {vin}.")
    res = results[0]

    # ErrorCode "0" means a clean decode; other codes may still carry partial
    # data, which we accept as long as make and model are present.
    decoded = DecodedVin(
        vin=vin,
        make=(res.get("Make") or "").strip(),
        model=(res.get("Model") or "").strip(),
        year=_to_int(res.get("ModelYear")),
        trim=(res.get("Trim") or "").strip(),
        series=(res.get("Series") or "").strip(),
        body_class=(res.get("BodyClass") or "").strip(),
        raw=res,
    )
    if not decoded.is_usable:
        error_text = res.get("ErrorText") or "no make/model"
        raise VinDecodeError(f"NHTSA could not decode make/model for VIN {vin}: {error_text}")
    return decoded
