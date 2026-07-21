"""Fetch con navegador headless (Playwright).

Renderiza páginas con JavaScript (como Edmunds) usando Chromium headless y
devuelve un objeto compatible con lo que esperan los parsers (`.text`, `.url`,
`.status_code`, `.ok`), para reutilizar la misma lógica de parseo que los
providers basados en Requests.

El import de Playwright es diferido (dentro de `_render`) para que el paquete
de providers cargue aunque el binario del navegador todavía no esté instalado;
el error solo aparece al intentar scrapear, con instrucciones claras.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings

from .base import ScraperError, VehicleNotFound

# Flags para reducir la huella de automatización (anti-bot básico).
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]
_STEALTH_INIT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


@dataclass
class RenderedResponse:
    """Respuesta mínima compatible con los parsers (imita requests.Response)."""

    url: str
    text: str
    status_code: int

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class PlaywrightFetchMixin:
    """Aporta un `fetch` que renderiza la página con Chromium headless.

    Se combina con un provider de parseo (p. ej. GenericProvider) vía herencia
    múltiple, poniéndolo primero en el MRO para que su `fetch` tenga prioridad.
    """

    def fetch(self, vin: str) -> RenderedResponse:
        url = self.source.build_url(vin)
        # Selector opcional a esperar antes de leer el HTML (definido en la
        # config de la fuente como selectors["wait_for"]).
        wait_selector = (self.source.selectors or {}).get("wait_for")
        response = self._render(url, wait_selector)

        if response.status_code == 404:
            raise VehicleNotFound(
                f"{self.source.name} no tiene datos para el VIN {vin}."
            )
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} respondió estado {response.status_code}."
            )
        return response

    def _playwright_proxy(self) -> dict | None:
        """Convierte SCRAPER_PROXY (URL) al formato de proxy de Playwright."""
        url = self.proxy_url
        if not url:
            return None
        partes = urlparse(url)
        servidor = f"{partes.scheme}://{partes.hostname}"
        if partes.port:
            servidor += f":{partes.port}"
        proxy = {"server": servidor}
        if partes.username:
            proxy["username"] = partes.username
        if partes.password:
            proxy["password"] = partes.password
        return proxy

    def _playwright_cm(self):
        """Devuelve el context manager de Playwright, con stealth si aplica.

        Si `SCRAPER_USE_STEALTH` está activo y playwright-stealth instalado,
        envuelve Playwright para aplicar las evasiones anti-detección a todas
        las páginas automáticamente. Si no, usa Playwright normal.
        """
        from playwright.sync_api import sync_playwright

        if getattr(settings, "SCRAPER_USE_STEALTH", True):
            try:
                from playwright_stealth import Stealth

                return Stealth().use_sync(sync_playwright())
            except ImportError:
                pass  # sin stealth; seguimos con Playwright normal
        return sync_playwright()

    def _render(self, url: str, wait_selector: str | None = None) -> RenderedResponse:
        try:
            from playwright.sync_api import TimeoutError as PWTimeoutError
        except ImportError as exc:
            raise ScraperError(
                "Playwright no está disponible. Instálalo con:\n"
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
                # Línea base anti-detección (por si playwright-stealth no está).
                context.add_init_script(_STEALTH_INIT)
                page = context.new_page()
                try:
                    nav = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    status = nav.status if nav else 200
                    if wait_selector:
                        try:
                            page.wait_for_selector(wait_selector, timeout=timeout_ms)
                        except PWTimeoutError:
                            # El selector no apareció; devolvemos lo que haya y
                            # que el parser decida (o caiga a la siguiente fuente).
                            pass
                    html = page.content()
                    final_url = page.url
                finally:
                    browser.close()
                return RenderedResponse(url=final_url, text=html, status_code=status)
        except ScraperError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalizamos cualquier fallo del navegador
            raise ScraperError(f"Error de Playwright en {url}: {exc}") from exc
