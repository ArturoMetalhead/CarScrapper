#!/bin/sh
set -e

# Espera a la base de datos aplicando migraciones (reintenta hasta que esté lista).
echo "[entrypoint] Aplicando migraciones..."
n=0
until python manage.py migrate --noinput; do
  n=$((n + 1))
  if [ "$n" -ge 30 ]; then
    echo "[entrypoint] La BD no respondió tras 30 intentos. Abortando." >&2
    exit 1
  fi
  echo "[entrypoint] BD no lista, reintento $n/30 en 2s..."
  sleep 2
done

echo "[entrypoint] Migraciones OK. Arrancando: $*"
exec "$@"
