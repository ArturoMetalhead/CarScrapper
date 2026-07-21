"""Calienta / verifica el perfil de Chrome usado por nodriver contra Edmunds.

DataDome trata mejor a un perfil "recurrente" (con cookie datadome y confianza
de IP acumulada). Este comando lanza el navegador real contra una página de
Edmunds y reporta si atraviesa el bloqueo, dejando el perfil sembrado para los
scrapes posteriores.

Uso:
    python manage.py warm_nodriver_profile
    python manage.py warm_nodriver_profile --url https://www.edmunds.com/used-all/
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from scraper.models import ScraperSource
from scraper.providers.nodriver_fetch import NodriverFetchMixin
from scraper.providers.base import BaseProvider, ScraperError


class _Sonda(NodriverFetchMixin, BaseProvider):
    """Provider mínimo: solo usa el fetch de nodriver para probar una URL."""

    def parse(self, response, vin):  # no se usa aquí
        raise NotImplementedError


class Command(BaseCommand):
    help = "Calienta y verifica el perfil de Chrome (nodriver) contra Edmunds."

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default="https://www.edmunds.com/used-all/",
            help="URL a probar (por defecto una página protegida de Edmunds).",
        )

    def handle(self, *args, **opciones):
        url = opciones["url"]
        # Fuente en memoria que apunta directamente a la URL dada.
        fuente = ScraperSource(
            name="warm-up",
            slug="warm-up",
            base_url=url,
            vin_path_template="",
            provider_key="nodriver",
            selectors={},
        )
        sonda = _Sonda(fuente)

        self.stdout.write(f"Probando {url} con nodriver (perfil persistente)...")
        try:
            resp = sonda._render(url)
        except ScraperError as exc:
            raise SystemExit(self.style.ERROR(f"Fallo del navegador: {exc}"))

        tam = len(resp.text)
        if resp.ok and tam > 100_000:
            self.stdout.write(
                self.style.SUCCESS(
                    f"OK: bypass conseguido (status {resp.status_code}, {tam} chars). "
                    "Perfil calentado y listo."
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"BLOQUEADO o contenido insuficiente (status {resp.status_code}, "
                    f"{tam} chars). Reintenta el comando; un 403 siembra la cookie "
                    "que hace pasar el siguiente intento."
                )
            )
