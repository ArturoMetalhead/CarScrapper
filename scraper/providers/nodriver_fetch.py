"""Anti-DataDome fetch with a real browser (nodriver).

Edmunds (and other sites) use DataDome, which blocks Playwright/Selenium-driven
browsers with a 403 "Access Denied" by detecting their CDP connection — even
from a residential IP and in headful mode. Verified empirically in this project:
Requests, headless Playwright and headful Playwright all get 403; a real manual
Chrome passes.

`nodriver` (successor to undetected-chromedriver) connects to a real Chrome
without those CDP traces. Combined with:

  * a PERSISTENT PROFILE (accumulates the `datadome` cookie and IP "trust", so in
    steady state it passes on the first try), and
  * RETRY with reload (a DataDome 403 sets a fresh cookie; the next attempt in
    the same session passes),

it gets through the block for free. It is low-volume and depends on running from
a non-flagged (residential) IP: the cookie is bound to the IP.

The mixin exposes a parser-compatible `fetch` (an object with `.text`, `.url`,
`.status_code`, `.ok`), reusing `RenderedResponse`. nodriver is async; each
render runs in its own event loop to bridge to Django's sync execution.
"""
from __future__ import annotations

import asyncio
import os

from django.conf import settings

from .base import BlockedError, ScraperError, VehicleNotFound
from .playwright_fetch import RenderedResponse

# DataDome / block-wall markers, in the HTML or the title.
_BLOCK_MARKERS = (
    "access denied",
    "access to this page has been denied",
    "pardon our interruption",
    "captcha-delivery",
    "geo.captcha-delivery",
    "enable javascript and cookies to continue",
)


def _is_blocked(html: str, title: str) -> bool:
    """True if the HTML/title correspond to the DataDome block page."""
    if "403" in title and "denied" in title.lower():
        return True
    low = html.lower()
    return any(marker in low for marker in _BLOCK_MARKERS)


def profile_dir() -> str:
    """Directory of the scraper's persistent Chrome profile."""
    path = getattr(settings, "SCRAPER_NODRIVER_PROFILE_DIR", "") or os.path.join(
        str(settings.BASE_DIR), ".chrome_profile_scraper"
    )
    os.makedirs(path, exist_ok=True)
    return path


def reset_profile() -> bool:
    """Delete the Chrome profile so the next launch starts a FRESH session.

    A DataDome block is usually on the session/cookie (a manual browser on the
    same IP still works), so dropping the banned `datadome` cookie and warming a
    clean profile gets back in. Safe between scrapes (Chrome is closed then).
    """
    import shutil

    path = profile_dir()
    try:
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


class NodriverFetchMixin:
    """Provides a `fetch` that renders with a real Chrome via nodriver.

    Combined with a parsing provider (e.g. GenericProvider) through multiple
    inheritance, placed first in the MRO so its `fetch` takes precedence.
    """

    def fetch(self, vin: str) -> RenderedResponse:
        return self._fetch_url(self.source.build_url(vin), f"VIN {vin}")

    def fetch_model(
        self, make: str, model: str, year=None, trim: str = ""
    ) -> RenderedResponse:
        url = self.source.build_model_url(make, model, year, trim)
        label = " ".join(str(x) for x in (year, make, model, trim) if x)
        return self._fetch_url(url, f"model {label}")

    def _fetch_url(self, url: str, context: str) -> RenderedResponse:
        wait_selector = (self.source.selectors or {}).get("wait_for")
        response = self._render(url, wait_selector)

        if response.status_code == 404:
            raise VehicleNotFound(f"{self.source.name} has no data for {context}.")
        if response.status_code == 403:
            raise BlockedError(
                f"{self.source.name} blocked the request (403) for {context}."
            )
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} responded status {response.status_code}."
            )
        return response

    @property
    def _profile_dir(self) -> str:
        return profile_dir()

    @property
    def _headless(self) -> bool:
        return getattr(settings, "SCRAPER_NODRIVER_HEADLESS", False)

    @property
    def _hide_window(self) -> bool:
        """Hide the (headful) window off-screen so nothing shows on screen.

        Headless mode is detected by DataDome, but a real headful browser moved
        off-screen passes the block and shows no window. Anti-throttling flags
        keep it rendering even though the window is not visible.
        """
        return getattr(settings, "SCRAPER_NODRIVER_HIDE_WINDOW", True)

    @property
    def _retries(self) -> int:
        return max(1, getattr(settings, "SCRAPER_NODRIVER_RETRIES", 3))

    @property
    def _settle(self) -> int:
        return max(1, getattr(settings, "SCRAPER_NODRIVER_SETTLE", 6))

    def _render(self, url: str, wait_selector: str | None = None) -> RenderedResponse:
        try:
            import nodriver  # noqa: F401
        except ImportError as exc:
            raise ScraperError(
                "nodriver is not installed. Install it with:\n  pip install nodriver"
            ) from exc

        # nodriver is async: run it in its own event loop so it does not clash
        # with the global one (Django may call from different threads).
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._render_async(url, wait_selector))
        except ScraperError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize any browser failure
            raise ScraperError(f"nodriver error at {url}: {exc}") from exc
        finally:
            self._drain_loop(loop)
            asyncio.set_event_loop(None)
            loop.close()

    @staticmethod
    def _drain_loop(loop: "asyncio.AbstractEventLoop") -> None:
        """Cancel pending tasks and let nodriver's subprocesses close.

        `browser.stop()` kills Chrome but closing its asyncio transports needs a
        few more loop cycles. Without this, closing the loop immediately raises
        noisy exceptions ('Event loop is closed', etc.) during GC. Drain quietly.
        """
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(asyncio.sleep(0.25))
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass

    async def _render_async(
        self, url: str, wait_selector: str | None
    ) -> RenderedResponse:
        import nodriver as uc

        browser_args = ["--profile-directory=Default"]
        if not self._headless and self._hide_window:
            # Real browser (passes DataDome) but window off-screen + no render
            # throttling, so it stays invisible yet renders fully.
            browser_args += [
                "--window-position=-32000,-32000",
                "--window-size=1366,900",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-background-timer-throttling",
            ]
        proxy = self.proxy_url
        if proxy:
            # Chrome only accepts a credential-less proxy via argument; auth would
            # need an extension. Not required for a residential IP.
            from urllib.parse import urlparse

            parts = urlparse(proxy)
            server = parts.hostname or ""
            if parts.port:
                server += f":{parts.port}"
            if server:
                browser_args.append(f"--proxy-server={server}")

        browser = await uc.start(
            headless=self._headless,
            user_data_dir=self._profile_dir,
            browser_args=browser_args,
        )
        try:
            page = await browser.get(url)
            html = ""
            title = ""
            for attempt in range(1, self._retries + 1):
                await asyncio.sleep(self._settle)
                if wait_selector:
                    try:
                        await page.wait_for(selector=wait_selector, timeout=self.timeout)
                    except Exception:  # noqa: BLE001 — not present; let the parser decide
                        pass
                html = await page.get_content()
                title = await page.evaluate("document.title") or ""
                if not _is_blocked(html, str(title)):
                    # Trigger lazy-loaded content (listings, prices) by scrolling,
                    # then recapture the full HTML.
                    await self._load_lazy(page)
                    html = await page.get_content()
                    final_url = await page.evaluate("location.href") or url
                    return RenderedResponse(url=str(final_url), text=html, status_code=200)
                if attempt < self._retries:
                    await page.reload()

            # Retries exhausted: still blocked.
            final_url = await page.evaluate("location.href") or url
            return RenderedResponse(url=str(final_url), text=html, status_code=403)
        finally:
            browser.stop()

    async def _load_lazy(self, page) -> None:
        """Scroll to force lazy-loaded content to render.

        Many pages (Edmunds included) load listings/prices only on scroll. Go
        down in steps, giving time to render.
        """
        try:
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                await asyncio.sleep(1.0)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001 — scrolling is best-effort
            pass
