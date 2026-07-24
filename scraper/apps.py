import os
import sys

from django.apps import AppConfig


class ScraperConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scraper"
    verbose_name = "Scraper de vehículos"

    def ready(self):
        """Start the scraping worker alongside the API (if enabled).

        Skips startup for management commands (migrate, shell, tests, the
        foreground worker itself, etc.) and avoids duplicating it with
        runserver's autoreloader.
        """
        from django.conf import settings

        if not self._should_start():
            return

        if getattr(settings, "SCRAPER_WORKER_AUTOSTART", True):
            from .worker import controller

            controller.start()

        if getattr(settings, "SCRAPER_CRAWL_ENABLED", True):
            from .crawler import planner

            planner.start()

    @staticmethod
    def _should_start() -> bool:
        """Only autostart in a real server process (never in mgmt commands/scripts)."""
        # WSGI/ASGI servers: their binary is argv[0] (gunicorn, uvicorn, ...).
        argv0 = os.path.basename(sys.argv[0]).lower()
        if any(server in argv0 for server in ("gunicorn", "uvicorn", "daphne", "hypercorn")):
            return True

        # `manage.py runserver`: start only in the actual serving process, not the
        # autoreloader parent (RUN_MAIN unset). With --noreload there is no parent.
        if "runserver" in sys.argv:
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in sys.argv

        # Everything else (migrate, shell, tests, python -c, cron commands): no.
        return False
