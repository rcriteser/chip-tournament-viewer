"""PostgreSQL persistence for the public Viewer.

This module deliberately uses SQLAlchemy Core rather than an ORM. Viewer
state is shared only through PostgreSQL; it never falls back to a local file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from flask import current_app
from sqlalchemy import (
    BIGINT,
    Boolean,
    Column,
    DateTime,
    Identity,
    MetaData,
    Table,
    Text,
    and_,
    create_engine,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.engine import Engine, URL, make_url


metadata = MetaData()
viewer_tournaments = Table(
    "viewer_tournaments",
    metadata,
    Column("id", BIGINT, Identity(), primary_key=True),
    Column("public_view_token", Text, nullable=False, unique=True),
    Column("viewer_sync_key", Text, nullable=False),
    Column("tournament_name", Text),
    Column("td_name", Text),
    Column("tournament_status", Text),
    Column("latest_snapshot", JSONB, nullable=False),
    Column("viewer_enabled", Boolean, nullable=False, server_default="true"),
    Column("license_status", Text, nullable=False, server_default="active"),
    Column("license_expires_at", DateTime(timezone=True)),
    Column("last_synced_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_engines: dict[tuple[int, str, int, int, int, int], Engine] = {}
_engine_lock = Lock()
_ALLOWED_LICENSE_STATUSES = ("active", "trial", "grace")


class SyncRejectedError(PermissionError):
    """A sync request failed authorization or current-state checks."""


def _pool_settings() -> tuple[int, int, int, int]:
    return (
        int(current_app.config["VIEWER_DB_POOL_SIZE"]),
        int(current_app.config["VIEWER_DB_MAX_OVERFLOW"]),
        int(current_app.config["VIEWER_DB_POOL_TIMEOUT"]),
        int(current_app.config["VIEWER_DB_POOL_RECYCLE"]),
    )


def _database_url() -> str:
    return str(current_app.config["DATABASE_URL"])


def validate_postgresql_url(value: str) -> URL:
    """Validate a Viewer URL without ever accepting SQLite."""
    try:
        url = make_url(value)
    except Exception as exc:  # pragma: no cover - SQLAlchemy owns parsing details
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL.") from exc
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use PostgreSQL; SQLite is not supported.")
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver.")
    if not url.host or not url.database:
        raise ValueError("DATABASE_URL must include a PostgreSQL host and database name.")
    return url


def normalize_postgresql_psycopg_url(value: str) -> str:
    """Normalize a provider PostgreSQL URL to the installed Psycopg 3 driver."""

    try:
        url = make_url(value.strip())
    except Exception as exc:  # pragma: no cover - SQLAlchemy owns parsing details
        raise ValueError("DATABASE_URL must be a valid PostgreSQL URL.") from exc
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use PostgreSQL; SQLite is not supported.")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    elif url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver.")
    if not url.host or not url.database:
        raise ValueError("DATABASE_URL must include a PostgreSQL host and database name.")
    return url.render_as_string(hide_password=False)


def get_engine() -> Engine:
    """Return a per-process engine, avoiding pool inheritance under Gunicorn."""
    database_url = _database_url()
    validate_postgresql_url(database_url)
    pool_size, max_overflow, timeout, recycle = _pool_settings()
    key = (os.getpid(), database_url, pool_size, max_overflow, timeout, recycle)
    with _engine_lock:
        engine = _engines.get(key)
        if engine is None:
            engine = create_engine(
                database_url,
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=timeout,
                pool_recycle=recycle,
                pool_pre_ping=True,
                hide_parameters=True,
            )
            _engines[key] = engine
        return engine


def dispose_engines() -> None:
    """Dispose pooled connections. Used by controlled shutdowns and tests."""
    with _engine_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _snapshot_values(snapshot: dict[str, Any], timestamp: datetime) -> dict[str, Any]:
    tournament = snapshot.get("tournament", {}) or {}
    tournament_name = (
        tournament.get("name")
        or tournament.get("tournament_name")
        or f"Tournament {tournament.get('id', '')}".strip()
    )
    return {
        "tournament_name": tournament_name,
        "td_name": tournament.get("td_name") or "",
        "tournament_status": tournament.get("status") or "",
        "latest_snapshot": snapshot,
        "last_synced_at": timestamp,
        "updated_at": timestamp,
    }


def _record_from_row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def get_record_by_token(token: str) -> dict[str, Any] | None:
    """Return internal row data for server-side use only."""
    statement = select(viewer_tournaments).where(
        viewer_tournaments.c.public_view_token == token
    )
    with get_engine().connect() as connection:
        row = connection.execute(statement).first()
    return _record_from_row(row) if row else None


def get_snapshot_by_token(token: str) -> dict[str, Any] | None:
    """Return the public snapshot without exposing metadata or sync keys."""
    statement = select(viewer_tournaments.c.latest_snapshot).where(
        viewer_tournaments.c.public_view_token == token
    )
    with get_engine().connect() as connection:
        snapshot = connection.execute(statement).scalar_one_or_none()
    return snapshot


def _rejection_reason(connection: Any, token: str, sync_key: str) -> str:
    """Classify a rejected conditional update without exposing stored data."""
    statement = select(
        viewer_tournaments.c.viewer_enabled,
        viewer_tournaments.c.license_status,
    ).where(
        and_(
            viewer_tournaments.c.public_view_token == token,
            viewer_tournaments.c.viewer_sync_key == sync_key,
        )
    )
    row = connection.execute(statement).first()
    if row is None:
        return "Invalid sync key."
    if not row.viewer_enabled:
        return "Viewer is disabled."
    return "License is not active."


def upsert_snapshot(token: str, sync_key: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Atomically establish/authenticate ownership and save a full snapshot.

    INSERT .. ON CONFLICT first establishes ownership. On every conflict, a
    conditional UPDATE checks the immutable sync key and current Viewer state
    in the same transaction. This permits idempotent same-key retries and
    makes competing first syncs deterministic.
    """
    timestamp = _now_utc()
    values = _snapshot_values(snapshot, timestamp)
    with get_engine().begin() as connection:
        inserted_id = connection.execute(
            pg_insert(viewer_tournaments)
            .values(
                public_view_token=token,
                viewer_sync_key=sync_key,
                viewer_enabled=True,
                license_status="active",
                license_expires_at=None,
                created_at=timestamp,
                **values,
            )
            .on_conflict_do_nothing(index_elements=["public_view_token"])
            .returning(viewer_tournaments.c.id)
        ).scalar_one_or_none()
        if inserted_id is None:
            result = connection.execute(
                update(viewer_tournaments)
                .where(
                    and_(
                        viewer_tournaments.c.public_view_token == token,
                        viewer_tournaments.c.viewer_sync_key == sync_key,
                        viewer_tournaments.c.viewer_enabled.is_(True),
                        viewer_tournaments.c.license_status.in_(_ALLOWED_LICENSE_STATUSES),
                    )
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise SyncRejectedError(_rejection_reason(connection, token, sync_key))

    saved = get_record_by_token(token)
    if saved is None:  # pragma: no cover - defensive database-integrity guard
        raise RuntimeError("Viewer snapshot was not persisted.")
    return saved


def can_accept_sync(record: dict[str, Any]) -> tuple[bool, str]:
    """Retained for server-side callers; persistence enforces this atomically."""
    if not record.get("viewer_enabled", False):
        return False, "Viewer is disabled."
    if str(record.get("license_status") or "").strip().lower() not in _ALLOWED_LICENSE_STATUSES:
        return False, "License is not active."
    return True, "OK"
