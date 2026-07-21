"""Worker de scraping en segundo plano y su controlador.

Contiene:
  * La lógica de procesar la cola de `ScrapeJob` (claim atómico + scrape + caché
    + webhook), compartida por el comando `run_scrape_worker` (primer plano) y
    por el hilo que arranca junto a la API.
  * `WorkerController`: gestiona ese bucle en un hilo demonio, con arranque y
    parada en caliente. Se expone un singleton `controller` que usan el arranque
    automático (AppConfig.ready), los endpoints de la API y el comando.

Nota: el worker procesa de uno en uno porque nodriver solo permite un navegador
a la vez. Debe correr en la máquina con IP residencial + Chrome.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from django.conf import settings
from django.utils import timezone

from . import webhooks
from .models import ScrapeJob
from .services import AllSourcesFailed, apply_model_to_vehicles, scrape_model_data

logger = logging.getLogger(__name__)

# Tipo de un logger simple (print, self.stdout.write, o logger.info).
LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def claim_next_job() -> ScrapeJob | None:
    """Toma el siguiente trabajo pendiente y lo marca 'running' atómicamente.

    El update condicionado a status=PENDING evita que dos ejecutores tomen el
    mismo trabajo.
    """
    job = (
        ScrapeJob.objects.filter(status=ScrapeJob.Status.PENDING)
        .order_by("created_at")
        .first()
    )
    if job is None:
        return None
    tomado = ScrapeJob.objects.filter(
        pk=job.pk, status=ScrapeJob.Status.PENDING
    ).update(status=ScrapeJob.Status.RUNNING, started_at=timezone.now())
    if not tomado:
        return None
    job.refresh_from_db()
    return job


def process_job(job: ScrapeJob, log: LogFn = _noop) -> None:
    """Scrapea un trabajo, cachea el resultado, propaga a los VINs y notifica."""
    etiqueta = " ".join(str(x) for x in (job.year, job.make, job.model) if x)
    log(f"-> Scrapeando {etiqueta} (VIN origen: {job.vin or 'n/a'})...")
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
            log(f"   FALLÓ tras {job.attempts} intentos: {exc}")
            webhooks.notify(job, error=True)
        else:
            job.status = ScrapeJob.Status.PENDING  # reencolar para reintento
            job.started_at = None
            job.save(update_fields=["status", "attempts", "last_error", "started_at"])
            log(f"   Reintento {job.attempts}/{max_intentos}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 — un fallo inesperado no debe tumbar el worker
        job.status = ScrapeJob.Status.FAILED
        job.last_error = f"Error inesperado: {exc}"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "attempts", "last_error", "finished_at"])
        logger.exception("Error inesperado procesando job %s", job.pk)
        log(f"   ERROR inesperado: {exc}")
        webhooks.notify(job, error=True)
        return

    job.result = vm
    job.status = ScrapeJob.Status.DONE
    job.finished_at = timezone.now()
    job.save(update_fields=["result", "status", "attempts", "finished_at"])
    n = apply_model_to_vehicles(vm)
    log(
        f"   OK: {etiqueta} -> {vm.estimated_price} {vm.currency} "
        f"({vm.raw_data.get('muestras', '?')} listados; {n} VIN(s) actualizados)."
    )
    webhooks.notify(job, vm)


def run_loop(
    stop_event: threading.Event,
    poll: int | None = None,
    once: bool = False,
    log: LogFn = _noop,
) -> None:
    """Bucle principal: procesa trabajos hasta que se pida parar (o hasta vaciar
    la cola si `once`). Comprueba `stop_event` de forma frecuente."""
    poll = poll or getattr(settings, "SCRAPER_WORKER_POLL_SECONDS", 5)
    while not stop_event.is_set():
        try:
            job = claim_next_job()
        except Exception:  # noqa: BLE001 — problemas transitorios de BD no matan el bucle
            logger.exception("Error tomando el siguiente trabajo")
            job = None

        if job is None:
            if once:
                break
            # Espera interrumpible: si llega stop_event, sale enseguida.
            stop_event.wait(poll)
            continue
        process_job(job, log=log)


class WorkerController:
    """Gestiona el bucle del worker en un hilo demonio, con start/stop en caliente."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._started_at = None

    def start(self, log: LogFn = _noop) -> bool:
        """Arranca el hilo del worker. Devuelve True si arrancó, False si ya corría."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=run_loop,
                args=(self._stop,),
                kwargs={"log": log or _noop},
                name="scrape-worker",
                daemon=True,
            )
            self._thread.start()
            self._started_at = timezone.now()
            logger.info("Worker de scraping arrancado (hilo en segundo plano).")
            return True

    def stop(self, timeout: float = 65.0) -> bool:
        """Pide la parada y espera a que el hilo termine el trabajo en curso.

        Devuelve True si estaba corriendo y se detuvo, False si no corría.
        """
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return False
            self._stop.set()
            hilo = self._thread
        hilo.join(timeout=timeout)
        detenido = not hilo.is_alive()
        if detenido:
            self._started_at = None
            logger.info("Worker de scraping detenido.")
        return detenido

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict:
        """Estado del worker + resumen de la cola."""
        from django.db.models import Count

        conteo = {
            fila["status"]: fila["n"]
            for fila in ScrapeJob.objects.values("status").annotate(n=Count("id"))
        }
        return {
            "running": self.is_running(),
            "started_at": self._started_at,
            "queue": {
                "pending": conteo.get(ScrapeJob.Status.PENDING, 0),
                "running": conteo.get(ScrapeJob.Status.RUNNING, 0),
                "done": conteo.get(ScrapeJob.Status.DONE, 0),
                "failed": conteo.get(ScrapeJob.Status.FAILED, 0),
            },
        }


# Singleton usado por el arranque automático, la API y el comando.
controller = WorkerController()
