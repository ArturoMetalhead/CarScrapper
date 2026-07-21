"""
Django settings for the CarScrapper project.

Based on Django 6.0. Sensitive values are read from a .env file (see
.env.example) using django-environ.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Environment variables ------------------------------------------------
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

# --- Applications ---------------------------------------------------------
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

# --- Database -------------------------------------------------------------
# SQLite by default for development. Switch to Postgres in production.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

# --- Password validation --------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalization -------------------------------------------------
LANGUAGE_CODE = "es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ---------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Django REST Framework ------------------------------------------------
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

# --- OpenAPI / Swagger (drf-spectacular) ----------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE": "CarScrapper API",
    "DESCRIPTION": "VIN lookup with background scraping, cache and webhook notifications.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- CORS -----------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")

# --- Scraper configuration ------------------------------------------------
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

# --- nodriver (anti-DataDome) ---------------------------------------------
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
# Headful (visible window) is what was verified against DataDome. Headless may
# fail and needs xvfb on a headless server. Change at your own risk.
SCRAPER_NODRIVER_HEADLESS = env.bool("SCRAPER_NODRIVER_HEADLESS", default=False)
# Retries with reload: a DataDome 403 sets a fresh cookie that lets the next
# attempt in the same session pass.
SCRAPER_NODRIVER_RETRIES = env.int("SCRAPER_NODRIVER_RETRIES", default=3)
# Seconds to wait after navigating, so the JS challenge settles.
SCRAPER_NODRIVER_SETTLE = env.int("SCRAPER_NODRIVER_SETTLE", default=6)

# --- VIN lookup, cache and background queue -------------------------------
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
# Max attempts per job before marking it failed.
SCRAPER_JOB_MAX_ATTEMPTS = env.int("SCRAPER_JOB_MAX_ATTEMPTS", default=3)

# --- Frontend notification webhook ----------------------------------------
# URL POSTed to when a background scrape finishes. Can be overridden per request
# (webhook_url field) or per job.
SCRAPER_WEBHOOK_URL = env("SCRAPER_WEBHOOK_URL", default="")
SCRAPER_WEBHOOK_TIMEOUT = env.int("SCRAPER_WEBHOOK_TIMEOUT", default=10)
