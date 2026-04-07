"""
Django settings for config project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-change-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() in ("1", "true", "yes")

_allowed = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dbinstances",
]

MIDDLEWARE = [
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

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"
_static_root = os.environ.get("DJANGO_STATIC_ROOT", "").strip()
if _static_root:
    _p = Path(_static_root).expanduser()
    STATIC_ROOT = (
        _p.resolve() if _p.is_absolute() else (BASE_DIR / _p).resolve()
    )
else:
    STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_ROOT = Path(
    os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")),
).expanduser()
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = (BASE_DIR / MEDIA_ROOT).resolve()
else:
    MEDIA_ROOT = MEDIA_ROOT.resolve()

MEDIA_URL = os.environ.get("DJANGO_MEDIA_URL", "/media/")

SQL_IMPORT_MAX_UPLOAD_BYTES = int(
    os.environ.get("SQL_IMPORT_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)),
)
SQL_IMPORT_MYSQL_TIMEOUT_SEC = int(
    os.environ.get("SQL_IMPORT_MYSQL_TIMEOUT_SEC", "3600"),
)
SQL_IMPORT_ZIP_MAX_UNCOMPRESSED_BYTES = int(
    os.environ.get(
        "SQL_IMPORT_ZIP_MAX_UNCOMPRESSED_BYTES",
        str(2 * SQL_IMPORT_MAX_UPLOAD_BYTES),
    ),
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CELERY_BROKER_URL = os.environ.get(
    "CELERY_BROKER_URL",
    "redis://127.0.0.1:6379/0",
)
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND",
    CELERY_BROKER_URL,
)
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(
    os.environ.get("CELERY_TASK_TIME_LIMIT", "3600"),
)
CELERY_TASK_SOFT_TIME_LIMIT = int(
    os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "3300"),
)
