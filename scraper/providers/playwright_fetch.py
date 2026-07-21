"""Headless browser fetch (Playwright).

Renders JavaScript pages with headless Chromium and returns an object compatible
with what parsers expect (`.text`, `.url`, `.status_code`, `.ok`), reusing the
same parsing logic as the Requests-based providers.

The Playwright import is deferred (inside `_render`) so the providers package
loads even if the browser binary is not installed yet; the error only surfaces
when scraping is attempted, with clear instructions.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from .base import ScraperError, VehicleNotFound

# Flags to reduce the automation fingerprint (basic anti-bot).
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]
_STEALTH_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


@dataclass
class RenderedResponse:
    """Minimal parser-compatible response (mimics requests.Response)."""

    url: str
    text: str
    status_code: int

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class PlaywrightFetchMixin:
    """Provides a `fetch` that renders the page with headless Chromium.

    Combined with a parsing provider (e.g. GenericProvider) via multiple
    inheritance, placed first in the MRO so its `fetch` takes precedence.
    """

    def fetch(self, vin: str) -> RenderedResponse:
        url = self.source.build_url(vin)
        # Optional selector to wait for before reading the HTML (defined in the
        # source config as selectors["wait_for"]).
        wait_selector = (self.source.selectors or {}).get("wait_for")
        response = self._render(url, wait_selector)

        if response.status_code == 404:
            raise VehicleNotFound(f"{self.source.name} has no data for VIN {vin}.")
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} responded status {response.status_code}."
            )
        return response

    def _playwright_proxy(self) -> dict | None:
        """Convert SCRAPER_PROXY (URL) into Playwright's proxy format."""
        url = self.proxy_url
        if not url:
            return None
        parts = urlparse(url)
        server = f"{parts.scheme}://{parts.hostname}"
        if parts.port:
            server += f":{parts.port}"
        proxy = {"server": server}
        if parts.username:
            proxy["username"] = parts.username
        if parts.password:
            proxy["password"] = parts.password
        return proxy

    def _playwright_cm(self):
        """Return the Playwright context manager, with stealth if applicable."""
        from playwright.sync_api import sync_playwright

        if getattr(settings, "SCRAPER_USE_STEALTH", True):
            try:
                from playwright_stealth import Stealth

                return Stealth().use_sync(sync_playwright())
            except ImportError:
                pass  # no stealth; continue with plain Playwright
        return sync_playwright()

    def _render(self, url: str, wait_selector: str | None = None) -> RenderedResponse:
        try:
            from playwright.sync_api import TimeoutError as PWTimeoutError
        except ImportError as exc:
            raise ScraperError(
                "Playwright is not available. Install it with:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            ) from exc

        timeout_ms = self.timeout * 1000
        try:
            with self._playwright_cm() as p:
                browser = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
                context_kwargs = {
                    "user_agent": settings.SCRAPER_USER_AGENT,
                    "locale": "en-US",
                    "viewport": {"width": 1366, "height": 768},
                }
                proxy = self._playwright_proxy()
                if proxy:
                    context_kwargs["proxy"] = proxy
                context = browser.new_context(**context_kwargs)
                # Anti-detection baseline (in case playwright-stealth is absent).
                context.add_init_script(_STEALTH_INIT)
                page = context.new_page()
                try:
                    nav = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    status = nav.status if nav else 200
                    if wait_selector:
                        try:
                            page.wait_for_selector(wait_selector, timeout=timeout_ms)
                        except PWTimeoutError:
                            # Selector never appeared; return what we have and let
                            # the parser decide (or fall back to the next source).
                            pass
                    html = page.content()
                    final_url = page.url
                finally:
                    browser.close()
                return RenderedResponse(url=final_url, text=html, status_code=status)
        except ScraperError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize any browser failure
            raise ScraperError(f"Playwright error at {url}: {exc}") from exc
