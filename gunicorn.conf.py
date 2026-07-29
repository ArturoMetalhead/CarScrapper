"""Configuración de gunicorn leída desde variables de entorno.

Así DevOps ajusta el servicio sin tocar el código ni el Dockerfile — solo env vars:
  PORT              Puerto interno del contenedor        (por defecto 8000)
  WEB_CONCURRENCY   Nº de workers de gunicorn            (por defecto 3)
  GUNICORN_TIMEOUT  Timeout por request, en segundos     (por defecto 120)
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
accesslog = "-"
errorlog = "-"
