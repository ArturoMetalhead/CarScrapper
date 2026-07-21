"""
Configuración de Django para el proyecto CarScrapper.

Basado en Django 6.0. Las variables sensibles se leen desde un archivo .env
(ver .env.example) usando django-environ.
"""
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Variables de entorno -------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
    CORS_ALLOWED_ORIGINS=(list, []),
)
# Lee el archivo .env si existe (en desarrollo). En producción se usan
# variables de entorno reales del sistema.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# --- Aplicaciones ---------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Terceros
    "rest_framework",
    "corsheaders",
    # Apps propias
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

# --- Base de datos --------------------------------------------------------
# SQLite por defecto para desarrollo. Cambiar a Postgres en producción.
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    )
}

# --- Validación de contraseñas -------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internacionalización -------------------------------------------------
LANGUAGE_CODE = "es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Archivos estáticos ---------------------------------------------------
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
}

# --- CORS -----------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")

# --- Configuración del scraper -------------------------------------------
# URL base del sitio al que se le hará scraping y el User-Agent a usar.
SCRAPER_BASE_URL = env("SCRAPER_BASE_URL", default="")
SCRAPER_USER_AGENT = env(
    "SCRAPER_USER_AGENT",
    default=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
)
SCRAPER_TIMEOUT = env.int("SCRAPER_TIMEOUT", default=20)
# Aplica playwright-stealth en los providers con navegador (si está instalado).
SCRAPER_USE_STEALTH = env.bool("SCRAPER_USE_STEALTH", default=True)
# Proxy opcional para todo el scraping (residencial o de un servicio).
# Formato: http://usuario:password@host:puerto  (o sin credenciales).
SCRAPER_PROXY = env("SCRAPER_PROXY", default="")
