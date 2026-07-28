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

echo "[worker] Xvfb listo en :99. Arrancando: $*"
exec "$@"
