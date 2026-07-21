"""Generic provider configurable via CSS selectors.

Reads the source config (`ScraperSource.selectors`) and extracts the fields from
the HTML. Works for most sites without writing new code: just create/edit the
source in the admin and adjust the selectors.
"""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup

from .base import BaseProvider, ScrapedVehicle, ScraperError, VehicleNotFound, parse_price
from .nodriver_fetch import NodriverFetchMixin
from .playwright_fetch import PlaywrightFetchMixin
from .registry import register


@register("generic")
class GenericProvider(BaseProvider):
    """Extracts data using the source's CSS selector map."""

    def fetch(self, vin: str) -> requests.Response:
        return self._get(self.source.build_url(vin), f"VIN {vin}")

    def fetch_model(
        self, make: str, model: str, year=None, trim: str = ""
    ) -> requests.Response:
        url = self.source.build_model_url(make, model, year, trim)
        label = " ".join(str(x) for x in (year, make, model, trim) if x)
        return self._get(url, f"model {label}")

    def _get(self, url: str, context: str) -> requests.Response:
        session = self.build_session()
        try:
            response = session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ScraperError(f"Network error in {self.source.name}: {exc}") from exc

        if response.status_code == 404:
            raise VehicleNotFound(f"{self.source.name} has no data for {context}.")
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} responded status {response.status_code}."
            )
        return response

    def parse(self, response: requests.Response, vin: str) -> ScrapedVehicle:
        selectors = self.source.selectors or {}
        soup = BeautifulSoup(response.text, "lxml")

        def text(field: str) -> str:
            selector = selectors.get(field)
            if not selector:
                return ""
            el = soup.select_one(selector)
            return el.get_text(strip=True) if el else ""

        # Respect the site's "not found" marker if configured.
        not_found = selectors.get("not_found")
        if not_found and soup.select_one(not_found):
            raise VehicleNotFound(
                f"{self.source.name} reports no data for VIN {vin}."
            )

        year_txt = text("year")
        mileage_txt = text("mileage")
        return ScrapedVehicle(
            vin=vin,
            make=text("make"),
            model=text("model"),
            year=int(year_txt) if year_txt.isdigit() else None,
            trim=text("trim"),
            mileage=int("".join(c for c in mileage_txt if c.isdigit()))
            if any(c.isdigit() for c in mileage_txt) else None,
            estimated_price=parse_price(text("estimated_price")),
            currency=text("currency") or "USD",
            source_url=response.url,
            raw_data={"http_status": response.status_code},
        )


@register("playwright")
class PlaywrightGenericProvider(PlaywrightFetchMixin, GenericProvider):
    """Like GenericProvider but renders JS with headless Chromium.

    Useful for fallback sites that also load data via JavaScript. Uses the same
    CSS selectors; only how it gets the HTML changes.
    """


@register("nodriver")
class NodriverGenericProvider(NodriverFetchMixin, GenericProvider):
    """Like GenericProvider but renders with a real Chrome (nodriver).

    For fallback sites protected by anti-bots (DataDome, Cloudflare) that block
    Playwright/Selenium. Uses the same CSS selectors; only how it gets the HTML
    changes.
    """
