"""Worker de scraping en segundo plano.

Procesa la cola de `ScrapeJob` de uno en uno (nodriver solo permite un navegador
a la vez), scrapea el dato de mercado por modelo, lo cachea, propaga el precio a
los VINs de ese modelo y avisa por webhook al frontend.

Uso:
    python manage.py run_scrape_worker            # bucle continuo
    python manage.py run_scrape_worker --once      # procesa la cola y termina
    python manage.py run_scrape_worker --poll 10   # intervalo de sondeo (s)

Pensado para correr como proceso aparte y de larga vida en la máquina con la IP
residencial y Chrome (la misma que atraviesa DataDome).
"""
from __future__ import annotations

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from scraper import webhooks
from scraper.models import ScrapeJob
from scraper.services import AllSourcesFailed, apply_model_to_vehicles, scrape_model_data


class Command(BaseCommand):
    help = "Procesa la cola de scraping en segundo plano (por modelo) y notifica por webhook."

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
        self._parar = False
        # Salida limpia con Ctrl+C / SIGTERM.
        signal.signal(signal.SIGINT, self._pedir_parada)
        try:
            signal.signal(signal.SIGTERM, self._pedir_parada)
        except (AttributeError, ValueError):
            pass  # SIGTERM no disponible en algunos entornos Windows

        poll = opciones["poll"] or getattr(settings, "SCRAPER_WORKER_POLL_SECONDS", 5)
        una_vez = opciones["once"]
        self.stdout.write(self.style.SUCCESS(
            f"Worker de scraping iniciado (poll={poll}s, modo={'once' if una_vez else 'continuo'})."
        ))

        while not self._parar:
            job = self._siguiente_job()
            if job is None:
                if una_vez:
                    self.stdout.write("Cola vacía. Fin (--once).")
                    break
                time.sleep(poll)
                continue
            self._procesar(job)

        self.stdout.write("Worker detenido.")

    # --- Internos --------------------------------------------------------
    def _pedir_parada(self, *args):
        self.stdout.write("\nParada solicitada; terminando el trabajo actual...")
        self._parar = True

    def _siguiente_job(self) -> ScrapeJob | None:
        """Toma el siguiente trabajo pendiente y lo marca 'running' atómicamente."""
        job = (
            ScrapeJob.objects.filter(status=ScrapeJob.Status.PENDING)
            .order_by("created_at")
            .first()
        )
        if job is None:
            return None
        # Marca running solo si sigue pendiente (evita doble toma si hubiera 2 workers).
        actualizadas = ScrapeJob.objects.filter(
            pk=job.pk, status=ScrapeJob.Status.PENDING
        ).update(
            status=ScrapeJob.Status.RUNNING,
            started_at=timezone.now(),
        )
        if not actualizadas:
            return None
        job.refresh_from_db()
        return job

    def _procesar(self, job: ScrapeJob) -> None:
        etiqueta = " ".join(str(x) for x in (job.year, job.make, job.model) if x)
        self.stdout.write(f"-> Scrapeando {etiqueta} (VIN origen: {job.vin or 'n/a'})...")
        job.attempts += 1

        max_intentos = getattr(settings, "SCRAPER_JOB_MAX_ATTEMPTS", 3)
        try:
            vm = scrape_model_data(job.make, job.model, job.year, job.trim)
        except AllSourcesFailed as exc:
            job.last_error = str(exc)
            if job.attempts >= max_intentos:
                job.status = ScrapeJob.Status.FAILED
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
                self.stdout.write(self.style.ERROR(
                    f"   FALLÓ tras {job.attempts} intentos: {exc}"
                ))
                webhooks.notify(job, error=True)
            else:
                # Reencola para reintento posterior.
                job.status = ScrapeJob.Status.PENDING
                job.started_at = None
                job.save(update_fields=["status", "attempts", "last_error", "started_at"])
                self.stdout.write(self.style.WARNING(
                    f"   Reintento {job.attempts}/{max_intentos}: {exc}"
                ))
            return
        except Exception as exc:  # noqa: BLE001 — cualquier fallo inesperado no debe tumbar el worker
            job.status = ScrapeJob.Status.FAILED
            job.last_error = f"Error inesperado: {exc}"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
            self.stdout.write(self.style.ERROR(f"   ERROR inesperado: {exc}"))
            webhooks.notify(job, error=True)
            return

        # Éxito.
        job.result = vm
        job.status = ScrapeJob.Status.DONE
        job.finished_at = timezone.now()
        job.save(update_fields=["result", "status", "attempts", "finished_at"])
        n = apply_model_to_vehicles(vm)
        self.stdout.write(self.style.SUCCESS(
            f"   OK: {etiqueta} -> {vm.estimated_price} {vm.currency} "
            f"({vm.raw_data.get('muestras', '?')} listados; {n} VIN(s) actualizados)."
        ))
        webhooks.notify(job, vm)
