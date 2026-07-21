import os
import sys

from django.apps import AppConfig


class ScraperConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scraper"
    verbose_name = "Scraper de vehículos"

    def ready(self):
        """Arranca el worker de scraping junto a la API (si está habilitado).

        Se salta el arranque en comandos de gestión (migrate, shell, tests, el
        propio worker en primer plano, etc.) y evita duplicarlo con el
        autoreloader de runserver.
        """
        from django.conf import settings

        if not getattr(settings, "SCRAPER_WORKER_AUTOSTART", True):
            return

        if not self._debe_arrancar():
            return

        from .worker import controller

        controller.start()

    @staticmethod
    def _debe_arrancar() -> bool:
        argv = sys.argv

        # Comandos donde NO queremos el worker de fondo.
        comandos_excluidos = {
            "migrate", "makemigrations", "collectstatic", "shell", "dbshell",
            "test", "createsuperuser", "seed_sources", "run_scrape_worker",
            "warm_nodriver_profile", "loaddata", "dumpdata", "check",
        }
        if any(cmd in argv for cmd in comandos_excluidos):
            return False

        if "runserver" in argv:
            # Con autoreload, solo el proceso hijo (RUN_MAIN=true) debe arrancarlo;
            # el proceso padre (reloader) no. Con --noreload no hay RUN_MAIN.
            if os.environ.get("RUN_MAIN") == "true":
                return True
            if "--noreload" in argv:
                return True
            return False

        # Servidores WSGI/ASGI (gunicorn, uvicorn, daphne): sin 'runserver' en argv.
        return True
