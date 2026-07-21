"""Fetch con navegador real anti-DataDome (nodriver).

Edmunds (y otros sitios) usan DataDome, que bloquea con 403 "Access Denied" a
los navegadores automatizados por Playwright/Selenium detectando su conexión
CDP — incluso desde una IP residencial y en modo headful. Se comprobó
empíricamente en este proyecto: Requests, Playwright headless y Playwright
headful reciben 403; un Chrome real manual pasa.

`nodriver` (sucesor de undetected-chromedriver) se conecta a un Chrome real sin
esos rastros de CDP. Combinado con:

  * un PERFIL PERSISTENTE (acumula la cookie `datadome` y "confianza" de IP, de
    modo que en régimen normal pasa al primer intento), y
  * REINTENTO con recarga (un 403 de DataDome deja una cookie fresca; el
    siguiente intento en la misma sesión pasa),

atraviesa el bloqueo de forma gratuita. Es de bajo volumen y depende de correr
desde una IP no fichada (residencial): la cookie va ligada a la IP.

El mixin expone un `fetch` compatible con los parsers (devuelve un objeto con
`.text`, `.url`, `.status_code`, `.ok`), reutilizando `RenderedResponse`.

nodriver es asíncrono; aquí se puentea a la ejecución síncrona de Django
corriendo cada render en su propio event loop.
"""
from __future__ import annotations

import asyncio
import os

from django.conf import settings

from .base import ScraperError, VehicleNotFound
from .playwright_fetch import RenderedResponse

# Marcadores del muro de DataDome / bloqueo, en el HTML o el título.
_BLOCK_MARKERS = (
    "access denied",
    "access to this page has been denied",
    "pardon our interruption",
    "captcha-delivery",
    "geo.captcha-delivery",
    "enable javascript and cookies to continue",
)


def _esta_bloqueado(html: str, titulo: str) -> bool:
    """True si el HTML/título corresponden a la página de bloqueo de DataDome."""
    low = html.lower()
    if "403" in titulo and "denied" in titulo.lower():
        return True
    return any(marcador in low for marcador in _BLOCK_MARKERS)


class NodriverFetchMixin:
    """Aporta un `fetch` que renderiza con un Chrome real vía nodriver.

    Se combina con un provider de parseo (p. ej. GenericProvider) por herencia
    múltiple, poniéndolo primero en el MRO para que su `fetch` tenga prioridad.
    """

    def fetch(self, vin: str) -> RenderedResponse:
        url = self.source.build_url(vin)
        return self._fetch_url(url, f"el VIN {vin}")

    def fetch_model(
        self, make: str, model: str, year=None, trim: str = ""
    ) -> RenderedResponse:
        url = self.source.build_model_url(make, model, year, trim)
        etiqueta = " ".join(str(x) for x in (year, make, model, trim) if x)
        return self._fetch_url(url, f"el modelo {etiqueta}")

    def _fetch_url(self, url: str, contexto: str) -> RenderedResponse:
        wait_selector = (self.source.selectors or {}).get("wait_for")
        response = self._render(url, wait_selector)

        if response.status_code == 404:
            raise VehicleNotFound(
                f"{self.source.name} no tiene datos para {contexto}."
            )
        if not response.ok:
            raise ScraperError(
                f"{self.source.name} respondió estado {response.status_code} "
                f"(posible bloqueo anti-bot)."
            )
        return response

    # --- Config (con defaults sensatos) ----------------------------------
    @property
    def _profile_dir(self) -> str:
        ruta = getattr(settings, "SCRAPER_NODRIVER_PROFILE_DIR", "") or os.path.join(
            str(settings.BASE_DIR), ".chrome_profile_scraper"
        )
        os.makedirs(ruta, exist_ok=True)
        return ruta

    @property
    def _headless(self) -> bool:
        return getattr(settings, "SCRAPER_NODRIVER_HEADLESS", False)

    @property
    def _retries(self) -> int:
        return max(1, getattr(settings, "SCRAPER_NODRIVER_RETRIES", 3))

    @property
    def _settle(self) -> int:
        return max(1, getattr(settings, "SCRAPER_NODRIVER_SETTLE", 6))

    # --- Render ----------------------------------------------------------
    def _render(self, url: str, wait_selector: str | None = None) -> RenderedResponse:
        try:
            import nodriver  # noqa: F401
        except ImportError as exc:
            raise ScraperError(
                "nodriver no está instalado. Instálalo con:\n"
                "  pip install nodriver"
            ) from exc

        # nodriver es async: lo corremos en un event loop propio para no chocar
        # con el bucle global (Django puede llamar desde distintos hilos).
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._render_async(url, wait_selector))
        except ScraperError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalizamos cualquier fallo del navegador
            raise ScraperError(f"Error de nodriver en {url}: {exc}") from exc
        finally:
            self._drenar_loop(loop)
            asyncio.set_event_loop(None)
            loop.close()

    @staticmethod
    def _drenar_loop(loop: "asyncio.AbstractEventLoop") -> None:
        """Cancela tareas pendientes y deja cerrar los subprocesos de nodriver.

        `browser.stop()` mata Chrome pero el cierre de sus transports asyncio
        necesita unos ciclos más del loop. Sin esto, al cerrar el loop de
        inmediato saltan excepciones ruidosas ('Event loop is closed', etc.)
        durante el GC. Aquí las drenamos en silencio.
        """
        try:
            pendientes = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pendientes:
                t.cancel()
            if pendientes:
                loop.run_until_complete(
                    asyncio.gather(*pendientes, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(asyncio.sleep(0.25))
        except Exception:  # noqa: BLE001 — limpieza best-effort
            pass

    async def _render_async(
        self, url: str, wait_selector: str | None
    ) -> RenderedResponse:
        import nodriver as uc

        browser_args = ["--profile-directory=Default"]
        proxy = self.proxy_url
        if proxy:
            # Chrome solo acepta proxy sin credenciales por argumento; con auth
            # habría que usar una extensión. Para IP residencial no hace falta.
            from urllib.parse import urlparse

            partes = urlparse(proxy)
            servidor = partes.hostname or ""
            if partes.port:
                servidor += f":{partes.port}"
            if servidor:
                browser_args.append(f"--proxy-server={servidor}")

        browser = await uc.start(
            headless=self._headless,
            user_data_dir=self._profile_dir,
            browser_args=browser_args,
        )
        try:
            page = await browser.get(url)
            html = ""
            titulo = ""
            for intento in range(1, self._retries + 1):
                await asyncio.sleep(self._settle)
                if wait_selector:
                    try:
                        await page.wait_for(selector=wait_selector, timeout=self.timeout)
                    except Exception:  # noqa: BLE001 — no apareció; seguimos y que el parser decida
                        pass
                html = await page.get_content()
                titulo = await page.evaluate("document.title") or ""
                if not _esta_bloqueado(html, str(titulo)):
                    # Dispara la carga diferida (listados, precios) haciendo
                    # scroll y recapturamos el HTML ya completo.
                    await self._cargar_diferido(page)
                    html = await page.get_content()
                    final_url = await page.evaluate("location.href") or url
                    return RenderedResponse(url=str(final_url), text=html, status_code=200)
                if intento < self._retries:
                    await page.reload()

            # Agotados los reintentos: seguimos bloqueados.
            final_url = await page.evaluate("location.href") or url
            return RenderedResponse(url=str(final_url), text=html, status_code=403)
        finally:
            browser.stop()

    async def _cargar_diferido(self, page) -> None:
        """Hace scroll para forzar la carga diferida (lazy-load) de contenido.

        Muchas páginas (Edmunds incluido) cargan listados/precios solo al hacer
        scroll. Bajamos por la página en varios pasos dando tiempo a renderizar.
        """
        try:
            for _ in range(4):
                await page.evaluate("window.scrollBy(0, document.body.scrollHeight/4)")
                await asyncio.sleep(1.0)
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception:  # noqa: BLE001 — el scroll es best-effort
            pass
