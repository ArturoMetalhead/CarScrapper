"""Worker de scraping en PRIMER PLANO (uso manual / debug).

Normalmente el worker arranca solo junto a la API (ver SCRAPER_WORKER_AUTOSTART
y `scraper.worker.controller`). Este comando lo corre en primer plano, útil para
depurar o si desactivaste el arranque automático.

IMPORTANTE: no lo ejecutes a la vez que el worker automático de la API: ambos
compartirían el perfil de Chrome y chocarían. Usa uno u otro.

Uso:
    python manage.py run_scrape_worker            # bucle continuo (Ctrl+C para parar)
    python manage.py run_scrape_worker --once      # procesa la cola y termina
    python manage.py run_scrape_worker --poll 10   # intervalo de sondeo (s)
"""
from __future__ import annotations

import signal
import threading

from django.core.management.base import BaseCommand

from scraper.worker import run_loop


class Command(BaseCommand):
    help = "Procesa la cola de scraping en primer plano (por modelo) y notifica por webhook."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="Procesa los trabajos pendientes y termina (no hace bucle).",
        )
        parser.add_argument(
            "--poll", type=int, default=None,
            help="Segundos entre sondeos cuando la cola está vacía (def. SCRAPER_WORKER_POLL_SECONDS).",
        )

    def handle(self, *args, **opciones):
        stop_event = threading.Event()

        def pedir_parada(*_a):
            self.stdout.write("\nParada solicitada; terminando el trabajo actual...")
            stop_event.set()

        signal.signal(signal.SIGINT, pedir_parada)
        try:
            signal.signal(signal.SIGTERM, pedir_parada)
        except (AttributeError, ValueError):
            pass  # SIGTERM no siempre está disponible en Windows

        modo = "once" if opciones["once"] else "continuo"
        self.stdout.write(self.style.SUCCESS(f"Worker en primer plano iniciado (modo={modo})."))
        run_loop(
            stop_event,
            poll=opciones["poll"],
            once=opciones["once"],
            log=lambda m: self.stdout.write(m),
        )
        self.stdout.write("Worker detenido.")
