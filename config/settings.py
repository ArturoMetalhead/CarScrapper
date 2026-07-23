"""
Django settings for the CarScrapper project.

Based on Django 6.0. Sensitive values are read from a .env file (see
.env.example) using django-environ.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
    CORS_ALLOWED_ORIGINS=(list, []),
)
# Read the .env file if present (development). In production, use real system
# environment variables.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

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
        "DIRS": [],
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
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

# Edmunds: overlay REAL market data from the /for-sale/ inventory page (min from
# actual listings + "Average price"). Adds a 2nd page fetch per model; set False
# to use only the MSRP page. MIN_LISTINGS guards against thin/blocked inventory.
SCRAPER_EDMUNDS_USE_INVENTORY = env.bool("SCRAPER_EDMUNDS_USE_INVENTORY", default=True)
SCRAPER_EDMUNDS_MIN_LISTINGS = env.int("SCRAPER_EDMUNDS_MIN_LISTINGS", default=5)
# Also fetch the descending (dearest-first) inventory page for the real maximum.
# True = 2 inventory requests (real min AND max); False = 1 (ascending only,
# uses the MSRP top for the maximum). More requests = higher block risk.
SCRAPER_EDMUNDS_INVENTORY_BOTH_ENDS = env.bool("SCRAPER_EDMUNDS_INVENTORY_BOTH_ENDS", default=True)
# Plausible car-price band (USD) used to filter noise when aggregating listings.
# Default max is 500k to cover luxury/exotics; raise it further if needed.
SCRAPER_PRICE_MIN = env.int("SCRAPER_PRICE_MIN", default=1000)
SCRAPER_PRICE_MAX = env.int("SCRAPER_PRICE_MAX", default=500000)

# Timeout for NHTSA VIN decoding.
SCRAPER_VIN_DECODE_TIMEOUT = env.int("SCRAPER_VIN_DECODE_TIMEOUT", default=15)
# Hours a market data row (VehicleModel) is considered fresh before requeueing
# its re-scraping.
SCRAPER_CACHE_TTL_HOURS = env.int("SCRAPER_CACHE_TTL_HOURS", default=24)
# Worker: starts alongside the API (background thread). Set to False to run it
# separately with `manage.py run_scrape_worker`.
SCRAPER_WORKER_AUTOSTART = env.bool("SCRAPER_WORKER_AUTOSTART", default=True)
# Worker: seconds between polls when the queue is empty.
SCRAPER_WORKER_POLL_SECONDS = env.int("SCRAPER_WORKER_POLL_SECONDS", default=5)
# Seconds to wait AFTER each scrape (with jitter). A single residential IP only
# survives LOW volume against DataDome; back-to-back scraping gets it flagged
# (403). Higher = safer/slower. 0 = no throttle (only for a single lookup).
SCRAPER_WORKER_DELAY = env.int("SCRAPER_WORKER_DELAY", default=45)
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
# automatically when unblocked. Keep the volume gentle via SCRAPER_WORKER_DELAY.
SCRAPER_CRAWL_ENABLED = env.bool("SCRAPER_CRAWL_ENABLED", default=True)
# Makes to crawl (empty -> the mainstream default list in crawler.MAINSTREAM_MAKES).
SCRAPER_CRAWL_MAKES = env.list("SCRAPER_CRAWL_MAKES", default=[])
# How many recent model years to cover (e.g. 8 -> the last 8 years).
SCRAPER_CRAWL_YEARS_BACK = env.int("SCRAPER_CRAWL_YEARS_BACK", default=8)
# Planner cycle interval (s): how often to top up the queue and refresh stale.
SCRAPER_CRAWL_PLAN_INTERVAL = env.int("SCRAPER_CRAWL_PLAN_INTERVAL", default=900)
# Top up crawl jobs only when fewer than this many are pending.
SCRAPER_CRAWL_QUEUE_MIN = env.int("SCRAPER_CRAWL_QUEUE_MIN", default=20)
# Max models seeded/refreshed per planner cycle.
SCRAPER_CRAWL_BATCH = env.int("SCRAPER_CRAWL_BATCH", default=50)
# How long the discovered model frontier is cached before re-discovering.
SCRAPER_CRAWL_DISCOVERY_TTL_HOURS = env.int("SCRAPER_CRAWL_DISCOVERY_TTL_HOURS", default=24)

# URL POSTed to when a background scrape finishes. Can be overridden per request
# (webhook_url field) or per job.
SCRAPER_WEBHOOK_URL = env("SCRAPER_WEBHOOK_URL", default="")
SCRAPER_WEBHOOK_TIMEOUT = env.int("SCRAPER_WEBHOOK_TIMEOUT", default=10)
