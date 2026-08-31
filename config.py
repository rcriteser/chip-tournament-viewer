"""Configuration loading for the PostgreSQL-only public Viewer."""

from __future__ import annotations

import os
from typing import Any

from storage import validate_postgresql_url


def _integer_setting(name: str, default: int, overrides: dict[str, Any]) -> int:
    raw_value = overrides.get(name, os.getenv(name, str(default)))
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if value < 0:
        raise RuntimeError(f"{name} must not be negative.")
    return value


def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load settings and fail fast when PostgreSQL configuration is missing."""
    overrides = overrides or {}
    testing = bool(overrides.get("TESTING", False)) or os.getenv("VIEWER_TESTING") == "true"
    database_url = overrides.get("DATABASE_URL") or os.getenv("DATABASE_URL")
    if testing and not database_url:
        database_url = os.getenv("VIEWER_POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is required and must be a postgresql+psycopg URL; "
            "Viewer does not support SQLite."
        )
    try:
        validate_postgresql_url(str(database_url))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    return {
        "APP_NAME": overrides.get("APP_NAME", os.getenv("APP_NAME", "Chip Tournament Viewer")),
        "DATABASE_URL": str(database_url),
        "TESTING": testing,
        "VIEWER_DB_POOL_SIZE": _integer_setting("VIEWER_DB_POOL_SIZE", 2, overrides),
        "VIEWER_DB_MAX_OVERFLOW": _integer_setting("VIEWER_DB_MAX_OVERFLOW", 1, overrides),
        "VIEWER_DB_POOL_TIMEOUT": _integer_setting("VIEWER_DB_POOL_TIMEOUT", 30, overrides),
        "VIEWER_DB_POOL_RECYCLE": _integer_setting("VIEWER_DB_POOL_RECYCLE", 1800, overrides),
    }
