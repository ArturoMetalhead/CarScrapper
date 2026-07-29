#!/bin/sh
set -e

# Chrome HEADFUL (obligatorio: DataDome detecta el headless) necesita un display.
# `xvfb-run` cuelga en contenedores, así que arrancamos Xvfb a mano en :99 con
# profundidad 24-bit (Chrome falla con los 8-bit por defecto de xvfb-run).
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
sleep 1

# Limpia locks obsoletos del perfil de Chrome (de un contenedor anterior que dejó
# el SingletonLock apuntando a otro host) — si no, Chrome no arranca.
rm -f /app/.chrome_profile_scraper/Singleton* 2>/dev/null || true

# No arrancar contra una BD sin migrar (modos B/C, worker aparte): esperar a que
# las migraciones estén aplicadas (las corre el contenedor web) en vez de
# estrellarse contra tablas inexistentes a mitad del loop.
n=0
until python manage.py migrate --check >/dev/null 2>&1; do
  n=$((n + 1))
  if [ "$n" -ge 60 ]; then
    echo "[worker] La BD no está migrada/accesible tras 60 intentos. Abortando." >&2
    exit 1
  fi
  echo "[worker] Esperando a que la BD esté migrada..."
  sleep 2
done

echo "[worker] Xvfb listo en :99 y BD migrada. Arrancando: $*"
exec "$@"
