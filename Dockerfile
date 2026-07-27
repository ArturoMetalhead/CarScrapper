# ============================================================================
# CarScrapper — imagen multi-stage.
#   target `web`    : app web / API / admin con gunicorn + WhiteNoise (ligera).
#   target `worker` : worker de scraping (nodriver + Chrome + Xvfb).
#
# IMPORTANTE (worker): el scraping necesita una IP RESIDENCIAL. En un servidor
# cloud (IP de datacenter) DataDome lo bloquea igual; usa un proxy residencial
# (SCRAPER_PROXY) o corre el worker en la máquina con IP residencial.
# ============================================================================

# ---- Base: Python + dependencias + código + estáticos (compartida) ----------
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_ENV=production

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements-prod.txt

COPY . .

# Estáticos (WhiteNoise los sirve). No necesita BD ni secretos en el build.
RUN python manage.py collectstatic --noinput \
    && chmod +x /app/entrypoint.sh /app/worker-entrypoint.sh

# ---- Worker: añade Chrome (chromium) + Xvfb para nodriver -------------------
FROM base AS worker

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium xvfb fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Le decimos a nodriver qué binario usar y que lance Chrome apto para contenedor.
ENV SCRAPER_NODRIVER_BINARY=/usr/bin/chromium \
    SCRAPER_NODRIVER_NO_SANDBOX=True \
    DISPLAY=:99

# Corre como root: Chrome (--no-sandbox) y el perfil montado necesitan escribir.
# El entrypoint arranca Xvfb (display virtual) y luego el worker.
ENTRYPOINT ["/app/worker-entrypoint.sh"]
CMD ["python", "manage.py", "run_scrape_worker"]

# ---- Web (target por defecto): gunicorn + WhiteNoise, sin Chrome, no-root ---
FROM base AS web

RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# Puerto interno por defecto (DevOps lo cambia con la env PORT; ver gunicorn.conf.py).
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health/" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# gunicorn lee puerto/workers/timeout desde el entorno vía gunicorn.conf.py.
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
