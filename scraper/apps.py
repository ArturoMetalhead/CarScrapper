import os
import sys

from django.apps import AppConfig


class ScraperConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scraper"
    verbose_name = "Vehicle scraper"

    def ready(self):
        """Start the scraping worker alongside the API (if enabled).

        Skips startup for management commands (migrate, shell, tests, the
        foreground worker itself, etc.) and avoids duplicating it with
        runserver's autoreloader.
        """
        from django.conf import settings

        if not getattr(settings, "SCRAPER_WORKER_AUTOSTART", True):
            return
        if not self._should_start():
            return

        from .worker import controller

        controller.start()

    @staticmethod
    def _should_start() -> bool:
        argv = sys.argv

        # Commands where we do NOT want the background worker.
        excluded = {
            "migrate", "makemigrations", "collectstatic", "shell", "dbshell",
            "test", "createsuperuser", "seed_sources", "run_scrape_worker",
            "warm_nodriver_profile", "loaddata", "dumpdata", "check",
        }
        if any(cmd in argv for cmd in excluded):
            return False

        if "runserver" in argv:
            # With autoreload only the child process (RUN_MAIN=true) should start
            # it, not the reloader parent. With --noreload there is no RUN_MAIN.
            if os.environ.get("RUN_MAIN") == "true":
                return True
            if "--noreload" in argv:
                return True
            return False

        # WSGI/ASGI servers (gunicorn, uvicorn, daphne): no 'runserver' in argv.
        return True
