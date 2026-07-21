"""FOREGROUND scraping worker (manual / debug use).

Normally the worker starts alongside the API (see SCRAPER_WORKER_AUTOSTART and
`scraper.worker.controller`). This command runs it in the foreground, useful for
debugging or if you disabled the autostart.

IMPORTANT: do not run it at the same time as the API's automatic worker: both
would share the Chrome profile and clash. Use one or the other.

Usage:
    python manage.py run_scrape_worker            # continuous loop (Ctrl+C to stop)
    python manage.py run_scrape_worker --once      # process the queue and exit
    python manage.py run_scrape_worker --poll 10   # poll interval (s)
"""
from __future__ import annotations

import signal
import threading

from django.core.management.base import BaseCommand

from scraper.worker import run_loop


class Command(BaseCommand):
    help = "Process the scraping queue in the foreground (per model) and notify via webhook."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="Process pending jobs and exit (no loop).",
        )
        parser.add_argument(
            "--poll", type=int, default=None,
            help="Seconds between polls when the queue is empty (def. SCRAPER_WORKER_POLL_SECONDS).",
        )

    def handle(self, *args, **options):
        stop_event = threading.Event()

        def request_stop(*_a):
            self.stdout.write("\nStop requested; finishing the current job...")
            stop_event.set()

        signal.signal(signal.SIGINT, request_stop)
        try:
            signal.signal(signal.SIGTERM, request_stop)
        except (AttributeError, ValueError):
            pass  # SIGTERM is not always available on Windows

        mode = "once" if options["once"] else "continuous"
        self.stdout.write(self.style.SUCCESS(f"Foreground worker started (mode={mode})."))
        run_loop(
            stop_event,
            poll=options["poll"],
            once=options["once"],
            log=lambda m: self.stdout.write(m),
        )
        self.stdout.write("Worker stopped.")
