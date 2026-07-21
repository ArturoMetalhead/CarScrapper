# CarScrapper API

API base en Django + Django REST Framework para hacer webscraping de vehículos.
A partir del **VIN** de un auto, consulta un sitio externo, extrae la información
(sobre todo el **precio estimado**) y la guarda en base de datos.

## Requisitos

- Python 3.11+
- Dependencias en `requirements.txt`

## Puesta en marcha

```bash
# 1. Entorno virtual (ya creado en .venv)
.\.venv\Scripts\Activate.ps1        # Windows PowerShell

# 2. Instalar dependencias (si hiciera falta)
pip install -r requirements.txt

# 3. Configurar variables de entorno
copy .env.example .env              # y editar valores

# 4. Migraciones
python manage.py migrate

# 5. (Opcional) superusuario para el admin
python manage.py createsuperuser

# 6. Levantar el servidor
python manage.py runserver
```

Tras migrar, precarga las fuentes por defecto (Edmunds + una de respaldo):

```bash
python manage.py seed_sources
```

## Endpoints

| Método | Ruta                        | Descripción                                       |
|--------|-----------------------------|---------------------------------------------------|
| GET    | `/api/health/`              | Comprobación de estado.                           |
| GET    | `/api/sources/`             | Lista las fuentes de scraping configuradas.       |
| POST   | `/api/vehicles/lookup/`     | Recibe un VIN, scrapea con fallback y devuelve el auto. |
| GET    | `/api/vehicles/`            | Lista los vehículos ya scrapeados.                |
| GET    | `/api/vehicles/<vin>/`      | Detalle de un vehículo por VIN.                   |
| —      | `/admin/`                   | Panel de administración de Django.                |

## Fuentes configurables con fallback

Las fuentes (páginas) viven en el modelo `ScraperSource` y se administran desde
`/admin/`. Cada fuente tiene:

- **base_url** y **vin_path_template** (ej. `/inventory/vin/{vin}/`).
- **priority**: menor número = se intenta primero.
- **is_active**: activar/desactivar sin borrar.
- **provider_key**: `generic` (extrae por selectores CSS) o uno personalizado
  como `edmunds`.
- **selectors**: mapa JSON campo → selector CSS.

Al consultar un VIN, el servicio recorre las fuentes activas por prioridad. Si
una falla (red, HTTP, parseo o no tiene el VIN), **pasa automáticamente a la
siguiente** sin romper el flujo. Puedes agregar, reordenar o desactivar fuentes
desde el admin sin tocar código.

### Añadir un sitio con lógica propia

Si un sitio necesita algo más que selectores CSS, crea una subclase de
`BaseProvider` en `scraper/providers/`, regístrala con `@register("mi_clave")`
y pon esa clave en `provider_key` de la fuente. Ver `providers/edmunds.py`.

## Providers con navegador (Playwright)

Para sitios que cargan datos por JavaScript, hay providers que renderizan la
página con Chromium headless antes de parsear:

- `edmunds`: Playwright + extracción de JSON-LD (con respaldo a selectores CSS).
- `playwright`: genérico (Playwright + selectores CSS de la fuente).

Instalación del navegador (una vez):

```bash
pip install playwright
python -m playwright install chromium
```

Opcional en la config de la fuente: `selectors["wait_for"]` = selector CSS a
esperar antes de leer el HTML (para contenido que tarda en cargar).

### Nota importante sobre Edmunds

Edmunds tiene una **protección anti-bots robusta**. En pruebas reales devuelve
`403` con página de bloqueo **incluso con Chromium headless y playwright-stealth**
(probado: el resultado es idéntico con y sin stealth). El motivo: el bloqueo
ocurre a nivel **HTTP en el borde**, detectando la IP de datacenter / huella TLS
*antes* de ejecutar JavaScript, así que las evasiones a nivel de navegador
(stealth) no aplican.

Lo único que lo resuelve es **cambiar la IP** que se bloquea:

1. **Proxies residenciales** — IPs residenciales rotativas en vez de la del
   servidor (datacenter). El provider acepta un proxy vía `SCRAPER_PROXY`.
2. **Servicio de scraping** (ScraperAPI, Zyte, etc.) — resuelve IP + anti-bot
   por ti; se integra como un provider más (muchos funcionan como proxy, así
   que también sirve `SCRAPER_PROXY`).

`playwright-stealth` queda integrado igualmente: ayuda con sitios de fallback
que sí dependen de la huella de navegador. Se puede desactivar con
`SCRAPER_USE_STEALTH=False`.

La arquitectura de fallback ya cubre el caso: si Edmunds bloquea, el sistema
pasa automáticamente a la siguiente fuente configurada.

### Ejemplo de lookup

```bash
curl -X POST http://127.0.0.1:8000/api/vehicles/lookup/ \
  -H "Content-Type: application/json" \
  -d '{"vin": "1HGCM82633A004352"}'
```

Usa `?refresh=true` para forzar un nuevo scraping aunque el vehículo ya exista.

## Dónde va la lógica de scraping

El scraping vive en [`scraper/services.py`](scraper/services.py). La función
`scrape_vehicle(vin)` ya tiene la estructura (sesión HTTP, headers, timeout,
parseo con BeautifulSoup); solo falta **ajustar los selectores CSS** al HTML
del sitio objetivo y definir `SCRAPER_BASE_URL` en el `.env`.
```
