"""VinAudit Market Value provider (https://www.vinaudit.com/).

VinAudit is a paid HTTP JSON API (no browser, no anti-bot). Here it is used ONLY
on-demand, by VIN, from services.resolve_vin — never by the background worker.
It's the same data behind https://www.vinaudit.com/car-value-calculator.

Endpoint (v2):
    GET https://marketvalue.vinaudit.com/v2/marketvalue?key=YOUR_KEY&vin=<VIN>

Query params we send (docs: vinaudit.com/vehicle-market-value-api-doc):
  * key      — the API key (required)
  * vin      — the 17-char VIN (required for a VIN query)
  * format   — json
  * period   — days of sales history to analyze (max 365)
  * country  — usa | canada
  * mileage  — a NUMBER; OMITTED to value at market-average mileage (the API uses
               average when the param is absent — "average" is NOT a valid value).

Successful response (relevant fields):
    {"success": true, "vin": "...", "id": "...", "vehicle": "2005 Toyota Corolla LE",
     "mileage": 75248, "count": 120, "mean": 7044, "stdev": 1276, "certainty": 99,
     "period": ["2015-06-27","2015-07-16"], "type": "retail",
     "prices": {"average": 7044, "below": 5768, "above": 8320}, "adjustments": {...}}

Documented error codes (in the `error` field when success=false) are mapped onto
the shared provider exceptions by how the caller should react:
  * no_data | invalid_vehicle           -> VehicleNotFound (vehicle unvaluable; the
                                           caller may cache this negative for a while)
  * rate_limit_exceeded | internal_error -> BlockedError (transient; retry later)
  * invalid_key | invalid_request        -> ScraperError (our key/request is wrong)

The API key is NEVER stored on the result: ``source_url`` is a keyless reference.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from .base import BaseProvider, BlockedError, ScrapedVehicle, ScraperError, VehicleNotFound
from .registry import register

# Dedicated namespace so all VinAudit activity can be filtered/leveled on its own.
logger = logging.getLogger("scraper.vinaudit")


def _f(value) -> float | None:
    """Best-effort float for raw_data (JSON-serializable), else None."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@register("vinaudit")
class VinAuditProvider(BaseProvider):
    """VinAudit Market Value API provider — on-demand, by VIN."""

    PRICE_KIND = "vinaudit_market"

    # Documented v2 error codes grouped by how the caller should react.
    _ERR_NO_VALUE = {"no_data", "invalid_vehicle"}          # vehicle can't be valued
    # Pre-v2 / legacy wording for the same "no value" outcome (space-separated).
    _ERR_NO_VALUE_LEGACY = {"invalid vin", "invalid_vin", "no data"}
    _ERR_TRANSIENT = {"rate_limit_exceeded", "internal_error"}  # retry later
    _ERR_CONFIG = {"invalid_key", "invalid_request"}        # our key/request is wrong

    # --- Entry point -----------------------------------------------------
    def scrape(self, vin: str) -> ScrapedVehicle:
        """Value the exact VIN. make/model/year come from the returned `vehicle`
        title (VinAudit doesn't split them into separate fields)."""
        data = self._valuate({"vin": vin}, vin)
        year, make, model = self._split_desc(data.get("vehicle"))
        result = self._to_vehicle(data, vin=vin, make=make, model=model, year=year)
        logger.info(
            "VinAudit priced %s (%s): avg %s %s, range %s-%s, %s comps, certainty %s%%.",
            vin, data.get("vehicle"), result.estimated_price, result.currency,
            result.price_low, result.price_high, data.get("count"), data.get("certainty"),
        )
        return result

    def scrape_model(self, *args, **kwargs):
        """Not supported: VinAudit is queried on-demand by VIN only (see
        services.resolve_vin), never through the background model scraper. The
        market-value `id` must be obtained from the Specifications API's selections,
        not constructed, so a make/model lookup is out of scope here."""
        raise ScraperError(
            "VinAudit is queried on-demand by VIN only, not via the model scraper."
        )

    # --- HTTP + error mapping -------------------------------------------
    def _endpoint(self) -> str:
        return str(
            getattr(settings, "VINAUDIT_ENDPOINT", "")
            or "https://marketvalue.vinaudit.com/v2/marketvalue"
        )

    def _http_get(self, url: str, params: dict) -> requests.Response:
        """Do the GET (isolated so tests can stub it). Raises requests errors."""
        session = self.build_session()
        resp = session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def _valuate(self, ident: dict, label: str) -> dict:
        """Call the Market Value API with {'vin': ...} and return the parsed,
        success-checked JSON body."""
        key = str(getattr(settings, "VINAUDIT_API_KEY", "") or "").strip()
        if not key:
            # No key configured -> a config failure, not a "not found". The caller
            # treats ScraperError as retryable and does not cache it as a negative.
            logger.error("VinAudit not queried for %s: VINAUDIT_API_KEY is not set.", label)
            raise ScraperError("VINAUDIT_API_KEY is not configured; skipping VinAudit.")

        params = {
            "key": key,
            "format": "json",
            "period": getattr(settings, "VINAUDIT_PERIOD", 90),
            "country": getattr(settings, "VINAUDIT_COUNTRY", "usa"),
            **ident,
        }
        # `mileage` is a NUMBER. To value at market-average mileage the param must be
        # OMITTED (the API uses average when absent); "average" is not a valid value.
        mileage = str(getattr(settings, "VINAUDIT_MILEAGE", "") or "").strip()
        if mileage and mileage.lower() != "average":
            params["mileage"] = mileage

        endpoint = self._endpoint()
        logger.debug(  # never logs the key
            "VinAudit -> GET %s params=%s", endpoint,
            {k: v for k, v in params.items() if k != "key"},
        )
        start = time.monotonic()
        try:
            resp = self._http_get(endpoint, params)
        except requests.HTTPError as exc:
            elapsed = time.monotonic() - start
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                logger.warning("VinAudit rate-limited (HTTP 429) for %s after %.2fs.", label, elapsed)
                raise BlockedError(f"VinAudit rate-limited (HTTP 429) for {label}.") from exc
            logger.error("VinAudit HTTP %s for %s after %.2fs.", status, label, elapsed)
            raise ScraperError(f"VinAudit HTTP {status} for {label}.") from exc
        except requests.RequestException as exc:
            elapsed = time.monotonic() - start
            logger.warning("VinAudit network error for %s after %.2fs: %s", label, elapsed, exc)
            raise ScraperError(f"VinAudit request failed for {label}: {exc}") from exc

        elapsed = time.monotonic() - start
        slow = getattr(settings, "VINAUDIT_SLOW_SECONDS", 8)
        if elapsed > slow:
            logger.warning("VinAudit slow response (%.2fs) for %s.", elapsed, label)
        else:
            logger.debug("VinAudit responded in %.2fs for %s.", elapsed, label)
        try:
            data = resp.json()
        except ValueError as exc:
            logger.error("VinAudit returned non-JSON for %s (%.2fs).", label, elapsed)
            raise ScraperError(f"VinAudit returned invalid JSON for {label}.") from exc
        return self._check(data, label)

    @classmethod
    def _check(cls, data, label: str) -> dict:
        """Validate the API body, mapping `success:false` error codes to exceptions.

        Only `no_data`/`invalid_vehicle` mean "this vehicle has no value"
        (VehicleNotFound — the caller may cache the negative). A bad key/request
        (ScraperError) or a rate limit / server hiccup (BlockedError) are NOT cached;
        they are retryable. An unknown code is treated as retryable, not as no-value.
        """
        if not isinstance(data, dict):
            raise ScraperError(f"VinAudit returned an unexpected response for {label}.")
        if data.get("success"):
            return data
        code = str(data.get("error") or "unknown_error").strip().lower()
        # Only EXACT documented codes (+ explicit legacy aliases) map to the CACHED
        # negative. No broad substrings here: an undocumented/transient code (e.g. a
        # future "database_error") must NOT be cached as a 30-day "no value" — it
        # falls through to the retryable ScraperError default below.
        if code in cls._ERR_NO_VALUE or code in cls._ERR_NO_VALUE_LEGACY:
            logger.info("VinAudit has no value for %s (%s).", label, code)
            raise VehicleNotFound(f"VinAudit has no value for {label} ({code}).")
        if code in cls._ERR_TRANSIENT or "rate" in code or "limit" in code:
            logger.warning("VinAudit temporarily unavailable for %s (%s).", label, code)
            raise BlockedError(f"VinAudit temporarily unavailable for {label} ({code}).")
        if code in cls._ERR_CONFIG or "key" in code:
            # Actionable: the service is effectively down for us until the key/params
            # are fixed. Logged at ERROR so it stands out from transient hiccups.
            logger.error("VinAudit rejected the request for %s (%s) — check API key/params.", label, code)
            raise ScraperError(f"VinAudit rejected the request for {label} ({code}).")
        # Unknown/undocumented code: retryable (do NOT cache a negative).
        logger.error("VinAudit returned an unknown error for %s (%s).", label, code)
        raise ScraperError(f"VinAudit error for {label} ({code}).")

    # --- Result mapping --------------------------------------------------
    def _to_vehicle(
        self, data: dict, *, vin: str, make: str, model: str, year, trim: str = ""
    ) -> ScrapedVehicle:
        """Map a successful valuation body into a ScrapedVehicle.

        Headline price is `prices.average` (falls back to `mean`); the range is
        `prices.below`..`prices.above`.
        """
        prices = data.get("prices") if isinstance(data.get("prices"), dict) else {}
        estimated = self._money(prices.get("average"))
        if estimated is None:
            estimated = self._money(data.get("mean"))
        if estimated is None:
            logger.info("VinAudit returned success but no usable price for %s.", vin)
            raise VehicleNotFound(f"VinAudit returned no usable price for {vin}.")
        low = self._money(prices.get("below"))
        high = self._money(prices.get("above"))

        country = str(getattr(settings, "VINAUDIT_COUNTRY", "usa") or "usa").lower()
        currency = "CAD" if country == "canada" else "USD"

        return ScrapedVehicle(
            vin=vin or "",
            make=make,
            model=model,
            year=year,
            trim=trim or "",
            estimated_price=estimated,
            price_low=low,
            price_high=high,
            price_kind=self.PRICE_KIND,
            currency=currency,
            source_url=self._reference_url(vin),
            raw_data={
                "provider": "vinaudit",
                "vin": data.get("vin"),
                "id": data.get("id"),
                "vehicle": data.get("vehicle"),
                "mileage": data.get("mileage"),
                "mean": _f(data.get("mean")),
                "stdev": _f(data.get("stdev")),
                "count": data.get("count"),
                "certainty": data.get("certainty"),
                "period": data.get("period"),
                "type": data.get("type"),
                "prices": {
                    "average": _f(prices.get("average")),
                    "below": _f(prices.get("below")),
                    "above": _f(prices.get("above")),
                },
                "adjustments": data.get("adjustments"),
            },
        )

    def _reference_url(self, vin: str) -> str:
        """A keyless, human-usable reference URL (never embeds the API key)."""
        return f"{self._endpoint()}?vin={vin}&format=json"

    # --- Helpers ---------------------------------------------------------
    @staticmethod
    def _split_desc(desc) -> tuple[int | None, str, str]:
        """'2005 Toyota Corolla LE' -> (2005, 'Toyota', 'Corolla LE')."""
        parts = str(desc or "").split()
        year = None
        if parts and len(parts[0]) == 4 and parts[0].isdigit():
            year = int(parts[0])
            parts = parts[1:]
        make = parts[0] if parts else ""
        model = " ".join(parts[1:]) if len(parts) > 1 else ""
        return year, make, model

    @staticmethod
    def _money(value) -> Decimal | None:
        """Parse a numeric price into a positive Decimal (2dp), else None."""
        if value is None:
            return None
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        if dec <= 0:
            return None
        return dec.quantize(Decimal("0.01"))
