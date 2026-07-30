"""
Django settings for the CarScrapper project.

Based on Django 6.0. Sensitive values are read from a .env file (see
.env.example) using django-environ.
"""
import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
    CORS_ALLOWED_ORIGINS=(list, []),
)
# Load the env file for the selected environment. DJANGO_ENV=production loads
# .env.production; anything else (the default) loads .env.development. Both are
# private (gitignored); .env.example is the committed reference. Falls back to a
# plain .env, and finally to the real OS environment variables.
DJANGO_ENV = os.environ.get("DJANGO_ENV", "development")
for _env_file in (BASE_DIR / f".env.{DJANGO_ENV}", BASE_DIR / ".env"):
    if _env_file.exists():
        environ.Env.read_env(_env_file)
        break

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Fail hard in PRODUCTION if SECRET_KEY was left at an insecure placeholder — a
# known key lets anyone forge sessions / signed cookies / password-reset tokens.
# Gated on DJANGO_ENV (not DEBUG), because local dev runs with DEBUG=False too.
if DJANGO_ENV == "production" and SECRET_KEY in (
    "dev-insecure-change-me",
    "dev-insecure-local-key-change-me",
    "CAMBIA-ESTO-por-una-clave-larga-unica-y-secreta",
    "change-this-to-a-secret-key",
    "",
):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a strong, unique value in production (DEBUG=False)."
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    # Local apps
    "scraper",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Serves static files from the app itself (so DEBUG=False still serves the
    # admin CSS). Must go right after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level templates (searched before app templates) so we can
        # override admin templates, e.g. templates/admin/base_site.html.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# SQLite by default for development. Switch to Postgres in production.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

# SQLite is single-writer; the background worker + crawler threads contend for it,
# surfacing as "database is locked". WAL lets readers run alongside the writer, a
# busy timeout makes writers WAIT for the lock instead of erroring, and IMMEDIATE
# transactions take the write lock up front (avoids mid-transaction lock upgrades).
if "sqlite3" in DATABASES["default"].get("ENGINE", ""):
    _sqlite_opts = DATABASES["default"].setdefault("OPTIONS", {})
    _sqlite_opts.setdefault("timeout", 30)
    _sqlite_opts.setdefault("init_command", "PRAGMA journal_mode=WAL;")
    _sqlite_opts.setdefault("transaction_mode", "IMMEDIATE")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise serves the collected static files from the app, so DEBUG=False
# still serves the admin CSS (no dev static handler needed). Run `collectstatic`.
# The compressed-manifest backend fingerprints files for far-future caching.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Production hardening (only applied when DEBUG is False) -------------------
# Extra origins allowed to POST (admin/login over HTTPS behind a domain).
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
if not DEBUG:
    # App runs behind a TLS-terminating proxy (nginx / load balancer).
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # These need real HTTPS; default OFF so a local DEBUG=False run over http
    # still works. Turn them ON in the server env (.env.production).
    SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
    SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
    CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
    SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    # Cierre por defecto: todo endpoint exige autenticación salvo que declare
    # AllowAny explícito (los del flujo del cliente: health + lookups + status).
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "CarScrapper API",
    "DESCRIPTION": "VIN lookup with background scraping, cache and webhook notifications.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")

# Base URL of the site to scrape and the User-Agent to use.
SCRAPER_BASE_URL = env("SCRAPER_BASE_URL", default="")
SCRAPER_USER_AGENT = env(
    "SCRAPER_USER_AGENT",
    default=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
)
SCRAPER_TIMEOUT = env.int("SCRAPER_TIMEOUT", default=20)
# Apply playwright-stealth in browser-based providers (if installed).
SCRAPER_USE_STEALTH = env.bool("SCRAPER_USE_STEALTH", default=True)
# Optional proxy for all scraping (residential or a service).
# Format: http://user:password@host:port  (or without credentials).
SCRAPER_PROXY = env("SCRAPER_PROXY", default="")

# The Edmunds provider (and the generic 'nodriver' one) use a real Chrome via
# nodriver to get past DataDome, which blocks Playwright/Selenium.
# Persistent profile: accumulates the `datadome` cookie and IP "trust" so in
# steady state it passes on the first try. Must be a folder dedicated to the
# scraper (not your personal Chrome profile). Defaults to a hidden folder in the
# project.
SCRAPER_NODRIVER_PROFILE_DIR = env(
    "SCRAPER_NODRIVER_PROFILE_DIR",
    default=str(BASE_DIR / ".chrome_profile_scraper"),
)
# Headful (visible window) is what was verified against DataDome; headless is
# detected and blocked. Keep this False.
SCRAPER_NODRIVER_HEADLESS = env.bool("SCRAPER_NODRIVER_HEADLESS", default=False)
# Hide the headful window off-screen (no visible window) while still passing
# DataDome. This is the recommended way to run "in the background" — headless
# gets blocked. Only applies when SCRAPER_NODRIVER_HEADLESS is False.
SCRAPER_NODRIVER_HIDE_WINDOW = env.bool("SCRAPER_NODRIVER_HIDE_WINDOW", default=True)
# Retries with reload: a DataDome 403 sets a fresh cookie that lets the next
# attempt in the same session pass.
SCRAPER_NODRIVER_RETRIES = env.int("SCRAPER_NODRIVER_RETRIES", default=3)
# Seconds to wait after navigating, so the JS challenge settles.
SCRAPER_NODRIVER_SETTLE = env.int("SCRAPER_NODRIVER_SETTLE", default=6)
# Container-only: Chrome needs --no-sandbox/--disable-dev-shm-usage to launch
# inside Docker, and an explicit binary path. Set in the worker image, not locally.
SCRAPER_NODRIVER_NO_SANDBOX = env.bool("SCRAPER_NODRIVER_NO_SANDBOX", default=False)
SCRAPER_NODRIVER_BINARY = env("SCRAPER_NODRIVER_BINARY", default="")
# Absolute cap (s) for a single nodriver render. Prevents a hung Chrome from
# freezing the (single) worker thread forever.
SCRAPER_NODRIVER_HARD_TIMEOUT = env.int("SCRAPER_NODRIVER_HARD_TIMEOUT", default=120)

# Edmunds: overlay REAL market data from the /for-sale/ inventory page (min from
# actual listings + "Average price"). Adds a 2nd page fetch per model; set False
# to use only the MSRP page. MIN_LISTINGS guards against thin/blocked inventory.
SCRAPER_EDMUNDS_USE_INVENTORY = env.bool("SCRAPER_EDMUNDS_USE_INVENTORY", default=True)
SCRAPER_EDMUNDS_MIN_LISTINGS = env.int("SCRAPER_EDMUNDS_MIN_LISTINGS", default=5)
# Also fetch the descending (dearest-first) inventory page for the real maximum.
# True = 2 inventory requests (real min AND max); False = 1 (ascending only,
# uses the MSRP top for the maximum). More requests = higher block risk.
SCRAPER_EDMUNDS_INVENTORY_BOTH_ENDS = env.bool("SCRAPER_EDMUNDS_INVENTORY_BOTH_ENDS", default=True)
# Plausible car-price band (USD) used only to drop garbage/mis-parsed numbers when
# aggregating listings. The max is a high safety ceiling (10M) — NOT a real price
# cap — so exotics/collectors (e.g. a $1.4M Porsche) show their true top price.
SCRAPER_PRICE_MIN = env.int("SCRAPER_PRICE_MIN", default=1000)
SCRAPER_PRICE_MAX = env.int("SCRAPER_PRICE_MAX", default=10_000_000)

# --- VinAudit Market Value API -------------------------------------------
# Paid HTTP JSON API that returns a car's market value (below/average/above) for a
# VIN — the data behind vinaudit.com's car value calculator. Queried ONLY on-demand,
# per VIN, from resolve_vin (never by the background worker/crawler). Without a key
# the on-demand path is skipped (seed_sources marks the source inactive). Get a key
# at https://www.vinaudit.com/vehicle-market-value-api (VA_DEMO_KEY no longer works).
VINAUDIT_API_KEY = env("VINAUDIT_API_KEY", default="")
# Full endpoint. Default is the current v2 API; switch to the legacy
# https://marketvalue.vinaudit.com/getmarketvalue.php if your account uses it.
VINAUDIT_ENDPOINT = env(
    "VINAUDIT_ENDPOINT", default="https://marketvalue.vinaudit.com/v2/marketvalue"
)
# Market/currency: usa -> USD, canada -> CAD.
VINAUDIT_COUNTRY = env("VINAUDIT_COUNTRY", default="usa")
# Days of sales history to analyze (1-365).
VINAUDIT_PERIOD = env.int("VINAUDIT_PERIOD", default=90)
# Odometer to value at: a NUMBER of miles, or "average" (the default) to value at
# market-average mileage. Per the v2 API, "average" is sent by OMITTING the param.
VINAUDIT_MILEAGE = env("VINAUDIT_MILEAGE", default="average")
# Per-VIN cache window (days). VinAudit is queried on-demand at most once per VIN
# per this window; a VIN looked up again sooner is served from cache (no API call).
# A re-query only happens when the user requests a VIN whose data is older than this.
VINAUDIT_TTL_DAYS = env.int("VINAUDIT_TTL_DAYS", default=30)
# Log a WARNING when a VinAudit response takes longer than this many seconds
# (to spot the API degrading before it fully fails).
VINAUDIT_SLOW_SECONDS = env.int("VINAUDIT_SLOW_SECONDS", default=8)

# Timeout for NHTSA VIN decoding.
SCRAPER_VIN_DECODE_TIMEOUT = env.int("SCRAPER_VIN_DECODE_TIMEOUT", default=15)
# Hours a market data row (VehicleModel) is considered fresh before requeueing
# its re-scraping.
SCRAPER_CACHE_TTL_HOURS = env.int("SCRAPER_CACHE_TTL_HOURS", default=24)
# Dead-letter: after this many consecutive scrape failures a VehicleModel stops
# being auto-refreshed (a retired model would otherwise burn IP quota forever).
SCRAPER_MODEL_MAX_FAILURES = env.int("SCRAPER_MODEL_MAX_FAILURES", default=5)
# Worker: starts alongside the API (background thread). Set to False to run it
# separately with `manage.py run_scrape_worker`.
SCRAPER_WORKER_AUTOSTART = env.bool("SCRAPER_WORKER_AUTOSTART", default=True)
# Seconds the autostarted worker/crawler threads wait before their first DB query,
# so app initialization finishes first (avoids Django's app-init DB-access warning).
SCRAPER_STARTUP_DELAY = env.int("SCRAPER_STARTUP_DELAY", default=2)
# Worker: seconds between polls when the queue is empty.
SCRAPER_WORKER_POLL_SECONDS = env.int("SCRAPER_WORKER_POLL_SECONDS", default=5)
# Seconds to wait AFTER each scrape (with jitter). A single residential IP only
# survives LOW volume against DataDome; back-to-back scraping gets it flagged
# (403). Higher = safer/slower. 0 = no throttle (only for a single lookup).
SCRAPER_WORKER_DELAY = env.int("SCRAPER_WORKER_DELAY", default=300)
# Max attempts per job before marking it failed.
SCRAPER_JOB_MAX_ATTEMPTS = env.int("SCRAPER_JOB_MAX_ATTEMPTS", default=3)
# On a DataDome 403 the worker first ROTATES its Chrome profile (fresh session /
# new datadome cookie) up to this many times — a block is usually on the cookie,
# not the IP (a manual browser on the same IP still works), so this recovers fast.
SCRAPER_BLOCK_ROTATIONS = env.int("SCRAPER_BLOCK_ROTATIONS", default=3)
# Seconds to wait before retrying after a profile rotation (grows per attempt).
SCRAPER_BLOCK_ROTATE_WAIT = env.int("SCRAPER_BLOCK_ROTATE_WAIT", default=15)
# If fresh profiles keep getting blocked it's likely IP-level: back off (doubling
# from SCRAPER_BLOCK_COOLDOWN up to _MAX), auto-resuming when access returns.
SCRAPER_BLOCK_COOLDOWN = env.int("SCRAPER_BLOCK_COOLDOWN", default=300)
SCRAPER_BLOCK_COOLDOWN_MAX = env.int("SCRAPER_BLOCK_COOLDOWN_MAX", default=3600)

# Proactively discover models (via NHTSA), scrape and keep them fresh. The worker
# self-regulates: if DataDome starts blocking (403), it cools down and resumes
# automatically when unblocked. Defaults are tuned VERY GENTLE (a single
# residential IP survives only low volume against DataDome): a 6h cycle, small
# batches, and a 5-min gap between background scrapes. Raise them only if you have
# proxies / more IPs. Set SCRAPER_CRAWL_ENABLED=False to disable it entirely.
SCRAPER_CRAWL_ENABLED = env.bool("SCRAPER_CRAWL_ENABLED", default=True)
# Makes to crawl (empty -> the mainstream default list in crawler.MAINSTREAM_MAKES).
SCRAPER_CRAWL_MAKES = env.list("SCRAPER_CRAWL_MAKES", default=[])
# How many recent model years to cover (e.g. 3 -> the last 3 years).
SCRAPER_CRAWL_YEARS_BACK = env.int("SCRAPER_CRAWL_YEARS_BACK", default=3)
# Planner cycle interval (s): how often to top up the queue and refresh stale (6h).
SCRAPER_CRAWL_PLAN_INTERVAL = env.int("SCRAPER_CRAWL_PLAN_INTERVAL", default=21600)
# Top up crawl jobs only when fewer than this many are pending.
SCRAPER_CRAWL_QUEUE_MIN = env.int("SCRAPER_CRAWL_QUEUE_MIN", default=5)
# Max models seeded/refreshed per planner cycle.
SCRAPER_CRAWL_BATCH = env.int("SCRAPER_CRAWL_BATCH", default=8)
# How long the discovered model frontier is cached before re-discovering.
SCRAPER_CRAWL_DISCOVERY_TTL_HOURS = env.int("SCRAPER_CRAWL_DISCOVERY_TTL_HOURS", default=24)

# URL POSTed to when a background scrape finishes. Can be overridden per request
# (webhook_url field) or per job.
SCRAPER_WEBHOOK_URL = env("SCRAPER_WEBHOOK_URL", default="")
SCRAPER_WEBHOOK_TIMEOUT = env.int("SCRAPER_WEBHOOK_TIMEOUT", default=10)

# --- Logging -------------------------------------------------------------
# Level for the app's own loggers ("scraper.*", incl. "scraper.vinaudit"). Set
# SCRAPER_LOG_LEVEL=DEBUG to also see requests/cache-hits/latency; INFO (default)
# shows queries, prices and failures; WARNING shows only degradations/errors.
SCRAPER_LOG_LEVEL = env("SCRAPER_LOG_LEVEL", default="INFO")

# Django applies DEFAULT_LOGGING first, then this on top (disable_existing_loggers
# False), so we only ADD an always-on console handler + our app logger. The Django
# default console handler is gated to DEBUG=True; ours logs in production too, so
# VinAudit failures are visible on the server.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "app": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "app_console": {"class": "logging.StreamHandler", "formatter": "app"},
    },
    "loggers": {
        # All scraper logs, including the VinAudit on-demand path ("scraper.vinaudit").
        "scraper": {
            "handlers": ["app_console"],
            "level": SCRAPER_LOG_LEVEL,
            "propagate": False,
        },
    },
}
