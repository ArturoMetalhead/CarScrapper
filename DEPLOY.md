# Despliegue de CarScrapper

## Arquitectura (importante)

CarScrapper son **dos piezas** que conviene separar en producción:

| Pieza | Qué hace | Dónde corre |
|---|---|---|
| **Web / API / Admin** | Sirve el dashboard, el panel, la API y el admin. Lee la caché de precios. | **Servidor / Docker** (cualquier IP). ✅ |
| **Worker de scraping** | nodriver + **Chrome real** contra Edmunds/CarGurus. | **Máquina con IP RESIDENCIAL** (DataDome bloquea IPs de datacenter). |

Por eso la imagen Docker es **solo la parte web** (no trae Chrome) y trae el worker/crawler **apagados**. El worker se corre aparte (ver más abajo).

---

## Entornos

Se elige con la variable `DJANGO_ENV`:

- `.env.development` → por defecto (local). `DEBUG=False`, SQLite. Los estáticos del admin los sirve **WhiteNoise**.
- `.env.production` → con `DJANGO_ENV=production`. `DEBUG=False`, Postgres, seguridad HTTPS.
- `.env.example` → plantilla versionada (los dos reales están en `.gitignore`).

---

## Local (desarrollo)

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput   # necesario: WhiteNoise sirve desde aquí
python manage.py runserver
```
→ http://127.0.0.1:8000 (dashboard) · `/panel/` · `/admin/`

> Como `DEBUG=False`, si tocas archivos estáticos vuelve a correr `collectstatic`.

---

## Producción con Docker Compose (web + Postgres)

1. Rellena **`.env.production`**: `SECRET_KEY` (genera una nueva), `ALLOWED_HOSTS`,
   `CSRF_TRUSTED_ORIGINS`, y las flags de seguridad.
   ```bash
   python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
   ```
2. Define la clave de Postgres (la misma que usará la app):
   ```bash
   export POSTGRES_PASSWORD=una-clave-fuerte
   ```
3. Levanta:
   ```bash
   docker compose up -d --build
   ```
   El `entrypoint` aplica migraciones solo; los estáticos ya vienen recogidos en la imagen.
4. Crea el superusuario del admin (una vez):
   ```bash
   docker compose exec web python manage.py createsuperuser
   ```

La app queda en el puerto **8000** por defecto. Ponle delante **nginx/Caddy** con TLS
(las flags `SECURE_SSL_REDIRECT`, HSTS, etc. asumen que terminas HTTPS en el proxy).

### Ajustes rápidos (sin editar archivos)

Estas se pasan como variables de entorno al hacer `docker compose up` (o en un
`.env` junto al compose):

| Variable | Para qué | Def. |
|---|---|---|
| `WEB_PORT` | Puerto del servidor donde se expone la web | 8000 |
| `PORT` | Puerto interno del contenedor | 8000 |
| `WEB_CONCURRENCY` | Nº de workers de gunicorn | 3 |
| `GUNICORN_TIMEOUT` | Timeout por request (s) | 120 |
| `POSTGRES_PASSWORD` | Clave de Postgres | `carscrapper` |

Ejemplo (exponer en el 8080 con 5 workers):
```bash
WEB_PORT=8080 WEB_CONCURRENCY=5 POSTGRES_PASSWORD=xxx docker compose up -d --build
```

El resto (dominios, claves, HTTPS) va en **`.env.production`** — ese es el archivo
"base" que DevOps rellena.

---

## El worker de scraping

> ⚠️ **Necesita una IP RESIDENCIAL.** El worker usa un Chrome real; DataDome
> bloquea IPs de datacenter. En un servidor cloud **no funcionará** salvo que
> uses un **proxy residencial** (`SCRAPER_PROXY` en `.env.production`). Lo normal
> es correr el worker en una máquina con IP residencial (tu escritorio).

El contenedor **web NO scrapea** (`SCRAPER_WORKER_AUTOSTART=False`). El worker es
un servicio aparte que apunta a la **misma base de datos**. Tres formas:

**A) Worker en Docker (mismo compose, opt-in).** La imagen `worker` ya trae
Chromium + Xvfb. Arranca web + db + worker juntos en tu máquina residencial:
```bash
docker compose --profile worker up -d --build
```
(sin `--profile worker`, solo levanta web + db, ideal para el servidor cloud.)

**B) Worker en Docker apuntando a un Postgres remoto** (web en el cloud, worker en
tu escritorio):
```bash
docker compose --profile worker run --rm \
  -e DATABASE_URL=postgres://carscrapper:CLAVE@IP_DEL_SERVIDOR:5432/carscrapper \
  worker
```

**C) Worker nativo** (sin Docker), con tu Chrome instalado:
```bash
export DJANGO_ENV=production          # Windows: $env:DJANGO_ENV="production"
export DATABASE_URL=postgres://carscrapper:...@IP_DEL_SERVIDOR:5432/carscrapper
python manage.py run_scrape_worker
```

En cualquiera de las tres, el worker toma los trabajos `pending`, scrapea y llena
la caché que la web sirve. La primera vez, el perfil de Chrome se "calienta"
(acumula la cookie de DataDome); dale bajo volumen.

---

## Notas

- **Un solo IP residencial aguanta poco volumen** contra DataDome. Mantén el crawler
  suave o apagado (`SCRAPER_CRAWL_ENABLED=False`) salvo que uses proxies residenciales.
- Postgres en prod porque SQLite no aguanta la concurrencia real (worker + web).
- Nunca subas `.env.production` al repo (ya está en `.gitignore`).
