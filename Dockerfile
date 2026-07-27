# CarScrapper — imagen de PRODUCCIÓN de la app web / API / admin (gunicorn).
#
# OJO con el scraper: el worker usa nodriver + Chrome real y necesita una IP
# RESIDENCIAL (DataDome bloquea IPs de datacenter). Por eso esta imagen NO trae
# Chrome y el worker/crawler vienen apagados en .env.production. Corre el worker
# en la máquina con IP residencial:  python manage.py run_scrape_worker
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_ENV=production

WORKDIR /app

# curl para el HEALTHCHECK; sin toolchain de compilación (psycopg[binary] no lo necesita).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python primero (mejor caché de capas).
COPY requirements.txt requirements-prod.txt ./
RUN pip install -r requirements-prod.txt

# Código de la app.
COPY . .

# Recoge los estáticos (WhiteNoise los sirve). No necesita BD ni secretos:
# settings usa valores por defecto seguros durante el build.
RUN python manage.py collectstatic --noinput

# entrypoint ejecutable + usuario no-root.
RUN chmod +x /app/entrypoint.sh \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# Puerto interno por defecto (DevOps lo cambia con la env PORT; ver gunicorn.conf.py).
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health/" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
# gunicorn lee puerto/workers/timeout desde el entorno vía gunicorn.conf.py.
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
