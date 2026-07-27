#!/bin/sh
set -e

# Chrome HEADFUL (obligatorio: DataDome detecta el headless) necesita un display.
# `xvfb-run` cuelga en contenedores, así que arrancamos Xvfb a mano en :99 con
# profundidad 24-bit (Chrome falla con los 8-bit por defecto de xvfb-run).
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
sleep 1

echo "[worker] Xvfb listo en :99. Arrancando: $*"
exec "$@"
