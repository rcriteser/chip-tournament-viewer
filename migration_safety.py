"""Standalone safety controls shared by Viewer Alembic invocations."""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.engine import Connection, URL, make_url


SERVICE_NAME = "viewer"
# Stable and deliberately distinct from the Licensing migration-lock key.
ADVISORY_LOCK_ID = 5_262_540_617_002
TLS_MODES = {"require", "verify-ca", "verify-full"}
DEFAULT_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 600
_VALID_ENVS = {"development", "test", "production"}
logger = logging.getLogger("alembic.migration_safety")


class MigrationSafetyError(RuntimeError):
    """A safe, operator-actionable migration preflight failure."""


@dataclass(frozen=True)
class MigrationSettings:
    environment: str
    expected_database_name: str | None
    advisory_lock_timeout_seconds: int
    ddl_lock_timeout_seconds: int

    @property
    def production(self) -> bool:
        return self.environment == "production"


def _timeout_setting(name: str) -> int:
    raw = os.getenv(name, str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise MigrationSafetyError(f"{name} must be an integer between 1 and {MAX_TIMEOUT_SECONDS}.") from exc
    if not 1 <= value <= MAX_TIMEOUT_SECONDS:
        raise MigrationSafetyError(f"{name} must be between 1 and {MAX_TIMEOUT_SECONDS}.")
    return value


def _migration_environment() -> str:
    value = os.getenv("MIGRATION_ENV", "development").strip().lower()
    if value not in _VALID_ENVS:
        raise MigrationSafetyError("MIGRATION_ENV must be development, test, or production.")
    return value


def _expected_database_name() -> str:
    value = os.getenv("MIGRATION_EXPECTED_DATABASE_NAME", "")
    if not value or value != value.strip() or any(character.isspace() for character in value):
        raise MigrationSafetyError(
            "MIGRATION_EXPECTED_DATABASE_NAME must be a non-empty database name without whitespace."
        )
    return value


def migration_settings(database_url: str) -> tuple[URL, MigrationSettings]:
    """Validate deployment-mode controls without exposing a database URL."""

    try:
        parsed = make_url(database_url)
    except Exception as exc:  # SQLAlchemy owns URL parse specifics.
        raise MigrationSafetyError("DATABASE_URL must be a valid database URL.") from exc

    environment = _migration_environment()
    expected_database_name = _expected_database_name() if environment == "production" else None
    settings = MigrationSettings(
        environment=environment,
        expected_database_name=expected_database_name,
        advisory_lock_timeout_seconds=_timeout_setting("MIGRATION_ADVISORY_LOCK_TIMEOUT_SECONDS"),
        ddl_lock_timeout_seconds=_timeout_setting("MIGRATION_DDL_LOCK_TIMEOUT_SECONDS"),
    )
    if settings.production:
        if parsed.get_backend_name() != "postgresql" or parsed.drivername != "postgresql+psycopg":
            raise MigrationSafetyError("Production migrations require a postgresql+psycopg DATABASE_URL.")
        if not parsed.host or not parsed.database:
            raise MigrationSafetyError("Production DATABASE_URL must include a PostgreSQL host and database name.")
        sslmode = str(parsed.query.get("sslmode", "")).lower()
        if sslmode not in TLS_MODES:
            raise MigrationSafetyError(
                "Production DATABASE_URL must require PostgreSQL TLS via "
                "sslmode=require, verify-ca, or verify-full."
            )
        if parsed.database != expected_database_name:
            raise MigrationSafetyError("Migration database identity does not match MIGRATION_EXPECTED_DATABASE_NAME.")
    return parsed, settings


def sanitized_database_identity(database_url: URL) -> str:
    host = database_url.host or "unknown-host"
    port = f":{database_url.port}" if database_url.port else ""
    database = database_url.database or "unknown-database"
    return f"host={host}{port} database={database}"


def verify_connected_database(connection: Connection, settings: MigrationSettings) -> None:
    if not settings.production:
        return
    actual_database = connection.execute(text("SELECT current_database()")).scalar_one()
    if actual_database != settings.expected_database_name:
        raise MigrationSafetyError("Connected migration database does not match MIGRATION_EXPECTED_DATABASE_NAME.")


@contextmanager
def postgresql_migration_lock(
    connection: Connection, settings: MigrationSettings, *, lock_id: int = ADVISORY_LOCK_ID
) -> Iterator[None]:
    """Hold a bounded PostgreSQL advisory lock on Alembic's own connection."""

    if connection.dialect.name != "postgresql":
        yield
        return

    connection.execute(text(f"SET lock_timeout = '{settings.ddl_lock_timeout_seconds}s'"))
    deadline = time.monotonic() + settings.advisory_lock_timeout_seconds
    acquired = False
    while True:
        acquired = bool(
            connection.execute(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": lock_id}).scalar_one()
        )
        if acquired:
            logger.info("migration_lock_acquired service=%s", SERVICE_NAME)
            break
        if time.monotonic() >= deadline:
            logger.error(
                "migration_lock_timeout service=%s timeout_seconds=%s",
                SERVICE_NAME,
                settings.advisory_lock_timeout_seconds,
            )
            raise MigrationSafetyError(
                f"Viewer migration advisory lock was not acquired within "
                f"{settings.advisory_lock_timeout_seconds} seconds."
            )
        time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))

    # Keep the session-scoped lock, but let Alembic own a fresh DDL transaction.
    connection.commit()
    try:
        yield
    finally:
        if acquired:
            connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id})
            logger.info("migration_lock_released service=%s", SERVICE_NAME)
