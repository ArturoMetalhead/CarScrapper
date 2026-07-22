"""Edmunds provider (https://www.edmunds.com/).

Edmunds is JavaScript-heavy and protected by DataDome, so this provider renders
with a real Chrome via `NodriverFetchMixin`. Two entry points:

  * `parse` (by VIN): extract structured data from embedded JSON-LD blocks,
    falling back to the GenericProvider CSS selectors.
  * `parse_model` (by model): the model page exposes no single price nor JSON-LD
    price, so it aggregates the listing prices (median) as the market estimate.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from statistics import median

from bs4 import BeautifulSoup

from .base import ScrapedVehicle, VehicleNotFound, parse_price
from .generic import GenericProvider
from .nodriver_fetch import NodriverFetchMixin
from .registry import register

# Plausible car price range (USD) to filter out noise when aggregating.
_PRICE_MIN = 1000
_PRICE_MAX = 200000
_PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+)")
# Edmunds' own labeled values: "Edmunds suggests you pay $X" and a "$X - $Y" range.
_SUGGEST_RE = re.compile(r"suggests?\s+you\s+pay[^$]{0,25}\$([\d,]{4,})", re.I)
_RANGE_RE = re.compile(r"\$([\d,]{4,})\s*[-–—]\s*\$([\d,]{4,})")


@register("edmunds")
class EdmundsProvider(NodriverFetchMixin, GenericProvider):
    """Edmunds-specific scraper.

    Uses `NodriverFetchMixin` (real Chrome via nodriver, persistent profile and
    retry) to get past DataDome, which 403s Playwright/Selenium-driven browsers.
    """

    def scrape_model(
        self, make: str, model: str, year=None, trim: str = "", series: str = ""
    ) -> ScrapedVehicle:
        """Scrape a model, trying Edmunds' naming variants.

        NHTSA returns engine-variant model names (e.g. BMW "328i", Mercedes
        "C300") but Edmunds groups them by series/class ("3-series", "c-class").
        We try, in order: the NHTSA "Series" (best signal, e.g. "3-Series"),
        make-specific normalizations, then the original name. The result keeps
        the ORIGINAL model name so it matches the VIN's decoded model in cache.
        """
        candidates = self._model_candidates(make, model, series)
        for candidate in candidates:
            response = self.fetch_model(make, candidate, year, trim)
            result = self.parse_model(response, make, model, year, trim)
            if result.estimated_price is not None:
                if not result.source_url:
                    result.source_url = self.source.build_model_url(make, candidate, year, trim)
                return result
        raise VehicleNotFound(
            f"{self.source.name}: no price for {make} {model} {year} "
            f"(tried: {', '.join(candidates)})."
        )

    @staticmethod
    def _model_candidates(make: str, model: str, series: str = "") -> list[str]:
        """Edmunds model slugs to try, most-likely first.

        Uses NHTSA "Series" if present (BMW "3-Series"); otherwise make-specific
        rules: BMW "328i"->"3-series", Mercedes "C300"->"c-class". Falls back to
        the original model name. Other makes are used as-is.
        """
        m = (model or "").strip()
        make_l = (make or "").lower()
        cands: list[str] = []
        if series and series.strip():
            cands.append(series.strip())
        if make_l == "bmw":
            mm = re.match(r"^(\d)\d{2}", m)
            if mm:
                cands.append(f"{mm.group(1)}-series")
        elif "mercedes" in make_l:
            mm = re.match(r"^([a-zA-Z]{1,3})\d{2,3}", m)
            if mm:
                cands.append(f"{mm.group(1).lower()}-class")
        if m:
            cands.append(m)
        seen, out = set(), []
        for c in cands:
            k = c.lower()
            if k not in seen:
                seen.add(k)
                out.append(c)
        return out or [m]

    def parse(self, response, vin: str) -> ScrapedVehicle:
        soup = BeautifulSoup(response.text, "lxml")
        data = self._extract_json_ld(soup)
        if data:
            result = self._from_json_ld(data, vin, response)
            if result.estimated_price or result.make:
                return result
        return super().parse(response, vin)

    def parse_model(
        self, response, make: str, model: str, year=None, trim: str = ""
    ) -> ScrapedVehicle:
        """Extract the model/year market price using Edmunds' own numbers.

        Priority:
          1. "Edmunds suggests you pay $X" (new cars) -> headline price.
          2. A "$X - $Y" range (MSRP range for new cars) -> midpoint as headline.
          3. Median of the used listings (`.heading-3`, configurable via
             selectors['model_price_nodes']) -> headline for used cars.

        `price_low`/`price_high` hold the range (explicit if present, otherwise
        the listing spread) and `price_kind` records the provenance.
        """
        soup = BeautifulSoup(response.text, "lxml")
        selectors = self.source.selectors or {}
        text = soup.get_text(" ", strip=True)

        suggested = None
        m = _SUGGEST_RE.search(text)
        if m:
            suggested = self._num(m.group(1))

        candidate_ranges: list[tuple[Decimal, Decimal]] = []
        for rm in _RANGE_RE.finditer(text):
            lo, hi = self._num(rm.group(1)), self._num(rm.group(2))
            if lo is not None and hi is not None and lo <= hi:
                candidate_ranges.append((lo, hi))

        selector = selectors.get("model_price_nodes", ".heading-3")
        prices: list[Decimal] = []
        for node in soup.select(selector):
            price = self._valid_price(node.get_text(" ", strip=True))
            if price is not None:
                prices.append(price)
        listing_median = listing_low = listing_high = None
        if prices:
            listing_median, listing_low, listing_high = self._robust_listing_stats(prices)

        # Pick the headline price, its range and provenance.
        low = high = None
        if suggested is not None:
            estimated, kind = suggested, "edmunds_suggested"
            # Only keep a range consistent with the suggested price (avoids
            # picking up stray "$1,140 - ..." monthly-payment/fee ranges).
            low, high = self._pick_range(candidate_ranges, suggested)
        elif candidate_ranges:
            low, high = candidate_ranges[0]
            estimated, kind = Decimal(round((low + high) / 2)), "msrp_range_mid"
        elif listing_median is not None:
            estimated, kind = listing_median, "used_listings_median"
            low, high = listing_low, listing_high
        else:
            estimated, kind = None, ""

        return ScrapedVehicle(
            vin="",
            make=make,
            model=model,
            year=year,
            trim=trim or "",
            estimated_price=estimated,
            price_low=low,
            price_high=high,
            price_kind=kind,
            currency="USD",
            source_url=response.url,
            raw_data={
                "price_kind": kind,
                "suggested": float(suggested) if suggested is not None else None,
                "range": [float(low), float(high)] if low is not None and high is not None else None,
                "listing_samples": len(prices),
                "listing_median": float(listing_median) if listing_median is not None else None,
            },
        )

    @staticmethod
    def _pick_range(
        ranges: list[tuple[Decimal, Decimal]], reference: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        """Choose the range consistent with the reference (suggested) price.

        Prefers a range that contains the reference; otherwise a plausible
        MSRP-like one near it. Rejects bogus ranges (e.g. a low monthly payment).
        """
        for lo, hi in ranges:
            if lo <= reference <= hi:
                return lo, hi
        for lo, hi in ranges:
            if lo >= reference * Decimal("0.5") and hi >= reference:
                return lo, hi
        return None, None

    @staticmethod
    def _robust_listing_stats(prices: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
        """Return (median, low, high) trimming outliers from the listing prices.

        A model-year page also shows cross-sell / other-year listings whose
        prices pollute the raw min/max (e.g. a $53k newer BMW on a 2013 3-Series
        page). We keep only prices within a band around the median before taking
        the spread, so the range reflects the actual model/year.
        """
        med = median(prices)
        low_b, high_b = med * Decimal("0.35"), med * Decimal("2.5")
        band = [p for p in prices if low_b <= p <= high_b] or list(prices)
        return Decimal(round(median(band))), min(band), max(band)

    @staticmethod
    def _num(raw: str) -> Decimal | None:
        """Parse a comma-thousands number and validate against the price range."""
        try:
            value = Decimal(raw.replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        return value if _PRICE_MIN <= value <= _PRICE_MAX else None

    @staticmethod
    def _valid_price(text: str) -> Decimal | None:
        """Extract the first price from text and validate against the range.

        Edmunds prices use the comma as a THOUSANDS separator (US format:
        "$15,779"). So strip commas and parse as an integer, rather than using
        parse_price (which would treat a single comma as a decimal).
        """
        m = _PRICE_RE.search(text)
        if not m:
            return None
        try:
            value = Decimal(m.group(1).replace(",", ""))
        except (InvalidOperation, ValueError):
            return None
        if not (_PRICE_MIN <= value <= _PRICE_MAX):
            return None
        return value

    def _extract_json_ld(self, soup: BeautifulSoup) -> dict | None:
        """Find a JSON-LD block describing a Vehicle/Car/Product."""
        for tag in soup.find_all("script", type="application/ld+json"):
            content = tag.string or tag.get_text()
            if not content:
                continue
            try:
                data = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if not isinstance(item, dict):
                    continue
                type_ = item.get("@type", "")
                types = type_ if isinstance(type_, list) else [type_]
                if any(t in ("Vehicle", "Car", "Product") for t in types):
                    return item
        return None

    def _from_json_ld(self, data: dict, vin: str, response) -> ScrapedVehicle:
        offer = data.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}

        price = offer.get("price") or data.get("price")
        currency = offer.get("priceCurrency") or "USD"
        year = data.get("modelDate") or data.get("productionDate") or data.get("vehicleModelDate")

        return ScrapedVehicle(
            vin=vin,
            make=self._name(data.get("brand")) or data.get("manufacturer", ""),
            model=data.get("model", "") if isinstance(data.get("model"), str) else "",
            year=int(str(year)[:4]) if year and str(year)[:4].isdigit() else None,
            trim=data.get("vehicleConfiguration", ""),
            mileage=self._miles(data.get("mileageFromOdometer")),
            estimated_price=parse_price(str(price)) if price is not None else None,
            currency=currency,
            source_url=response.url,
            raw_data={"json_ld": data},
        )

    @staticmethod
    def _name(value) -> str:
        if isinstance(value, dict):
            return value.get("name", "")
        return value or "" if isinstance(value, str) else ""

    @staticmethod
    def _miles(value) -> int | None:
        if isinstance(value, dict):
            value = value.get("value")
        if value is None:
            return None
        digits = "".join(c for c in str(value) if c.isdigit())
        return int(digits) if digits else None
