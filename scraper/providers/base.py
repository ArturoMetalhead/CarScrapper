"""Base class and shared structures for providers."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.conf import settings


class ScraperError(Exception):
    """Error during scraping (network, HTTP, parsing, config, etc.)."""


class VehicleNotFound(ScraperError):
    """The source responded but has no data for that VIN."""


class AllSourcesFailed(ScraperError):
    """None of the configured sources could resolve the VIN."""

    def __init__(self, vin: str, errors: dict[str, str]):
        self.vin = vin
        self.errors = errors
        detail = "; ".join(f"{source}: {msg}" for source, msg in errors.items())
        super().__init__(f"No source could resolve VIN {vin}. Detail -> {detail}")


@dataclass
class ScrapedVehicle:
    """Scraping result, ready to persist into the Vehicle model."""

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
        """Turn the dataclass into kwargs to create/update the model."""
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
    """Extract a decimal value from text like '$18,500' or '18.500,00'."""
    if not text:
        return None
    clean = "".join(ch for ch in text if ch.isdigit() or ch in ".,")
    if not clean:
        return None
    # Normalize: drop thousands separators, keep the decimal point.
    if "," in clean and "." in clean:
        # The last separator that appears is the decimal one.
        if clean.rfind(",") > clean.rfind("."):
            clean = clean.replace(".", "").replace(",", ".")
        else:
            clean = clean.replace(",", "")
    elif "," in clean:
        clean = clean.replace(",", ".") if clean.count(",") == 1 else clean.replace(",", "")
    try:
        return Decimal(clean)
    except InvalidOperation:
        return None


class BaseProvider:
    """Base contract for a scraping provider.

    Subclasses implement `fetch`/`parse` (and optionally `fetch_model`/
    `parse_model`). `scrape` and `scrape_model` are the templates orchestrating
    them.
    """

    def __init__(self, source):
        # `source` is a scraper.models.ScraperSource instance.
        self.source = source

    def scrape(self, vin: str) -> ScrapedVehicle:
        response = self.fetch(vin)
        result = self.parse(response, vin)
        if not result.source_url:
            result.source_url = self.source.build_url(vin)
        return result

    def scrape_model(
        self, make: str, model: str, year: int | None = None, trim: str = ""
    ) -> ScrapedVehicle:
        """Scrape market data for a MODEL (used by the background worker).

        Returns a `ScrapedVehicle` with an empty vin: only make/model/year/trim
        and `estimated_price` matter.
        """
        response = self.fetch_model(make, model, year, trim)
        result = self.parse_model(response, make, model, year, trim)
        if not result.source_url:
            result.source_url = self.source.build_model_url(make, model, year, trim)
        return result

    # --- To be implemented by subclasses ---------------------------------
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

    # --- Shared utilities ------------------------------------------------
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
        """Proxy URL to use (empty if none). See SCRAPER_PROXY."""
        return getattr(settings, "SCRAPER_PROXY", "") or ""
