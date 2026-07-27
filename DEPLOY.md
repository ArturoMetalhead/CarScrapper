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

## El worker de scraping (IP residencial)

En tu máquina con IP residencial y Chrome instalado (tu escritorio), apuntando a la
**misma base de datos** que la web:

```bash
export DJANGO_ENV=production        # Windows: $env:DJANGO_ENV="production"
export DATABASE_URL=postgres://carscrapper:...@IP_DEL_SERVIDOR:5432/carscrapper
python manage.py run_scrape_worker
```

Alternativa: dejar todo (web + worker) en el escritorio con `.env.development` y
`SCRAPER_WORKER_AUTOSTART=True` — es lo que ya tienes en local.

---

## Notas

- **Un solo IP residencial aguanta poco volumen** contra DataDome. Mantén el crawler
  suave o apagado (`SCRAPER_CRAWL_ENABLED=False`) salvo que uses proxies residenciales.
- Postgres en prod porque SQLite no aguanta la concurrencia real (worker + web).
- Nunca subas `.env.production` al repo (ya está en `.gitignore`).
