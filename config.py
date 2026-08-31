"""Configuration loading for the PostgreSQL-only public Viewer."""

from __future__ import annotations

import os
from typing import Any

from storage import normalize_postgresql_psycopg_url, validate_postgresql_url


PRODUCTION_TLS_MODES = {"require", "verify-ca", "verify-full"}


def _integer_setting(name: str, default: int, overrides: dict[str, Any]) -> int:
    raw_value = overrides.get(name, os.getenv(name, str(default)))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < 0:
        raise RuntimeError(f"{name} must not be negative.")
    return value


def _viewer_environment(overrides: dict[str, Any], testing: bool) -> str:
    """Return a small explicit runtime mode for production TLS enforcement."""

    if testing and "VIEWER_ENV" not in overrides:
        return "testing"
    value = str(overrides.get("VIEWER_ENV", os.getenv("VIEWER_ENV", "development"))).lower()
    if value not in {"development", "production"}:
        raise RuntimeError("VIEWER_ENV must be development or production.")
    return value


def _validate_production_tls(database_url: str, viewer_env: str) -> None:
    """Require explicit PostgreSQL TLS only for production Viewer runtime."""

    if viewer_env != "production":
        return
    url = validate_postgresql_url(database_url)
    sslmode = str(url.query.get("sslmode", "")).lower()
    if sslmode not in PRODUCTION_TLS_MODES:
        raise RuntimeError(
            "Production DATABASE_URL must require PostgreSQL TLS via "
            "sslmode=require, verify-ca, or verify-full."
        )


def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load settings and fail fast when PostgreSQL configuration is missing."""
    overrides = overrides or {}
    testing = bool(overrides.get("TESTING", False)) or os.getenv("VIEWER_TESTING") == "true"
    viewer_env = _viewer_environment(overrides, testing)
    database_url = overrides.get("DATABASE_URL") or os.getenv("DATABASE_URL")
    if testing and not database_url:
        database_url = os.getenv("VIEWER_POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is required and must be a postgresql+psycopg URL; "
            "Viewer does not support SQLite."
        )
    try:
        database_url = normalize_postgresql_psycopg_url(str(database_url))
        validate_postgresql_url(database_url)
        _validate_production_tls(database_url, viewer_env)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "APP_NAME": overrides.get("APP_NAME", os.getenv("APP_NAME", "Chip Tournament Viewer")),
        "DATABASE_URL": str(database_url),
        "VIEWER_ENV": viewer_env,
        "TESTING": testing,
        "DEBUG": False,
        "VIEWER_DB_POOL_SIZE": _integer_setting("VIEWER_DB_POOL_SIZE", 2, overrides),
        "VIEWER_DB_MAX_OVERFLOW": _integer_setting("VIEWER_DB_MAX_OVERFLOW", 1, overrides),
        "VIEWER_DB_POOL_TIMEOUT": _integer_setting("VIEWER_DB_POOL_TIMEOUT", 30, overrides),
        "VIEWER_DB_POOL_RECYCLE": _integer_setting("VIEWER_DB_POOL_RECYCLE", 1800, overrides),
    }
