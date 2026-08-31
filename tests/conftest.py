"""PostgreSQL-only test setup with safeguards for destructive database work."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def test_database_url() -> str:
    if os.getenv("VIEWER_POSTGRES_TEST_ENABLED") != "true":
        raise RuntimeError("Set VIEWER_POSTGRES_TEST_ENABLED=true to run destructive Viewer tests.")
    database_url = os.getenv("VIEWER_POSTGRES_TEST_DATABASE_URL")
    if not database_url:
        raise RuntimeError("VIEWER_POSTGRES_TEST_DATABASE_URL is required for Viewer tests.")
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql" or parsed.drivername != "postgresql+psycopg":
        raise RuntimeError("Viewer tests require a postgresql+psycopg test URL.")
    if (parsed.host or "").lower() not in LOOPBACK_HOSTS:
        raise RuntimeError("Viewer tests may reset only a loopback PostgreSQL database.")
    if not re.fullmatch(r"ct_viewer_test(?:_[a-z0-9_]+)?", parsed.database or "", re.IGNORECASE):
        raise RuntimeError("Viewer tests require a database named ct_viewer_test or ct_viewer_test_<suffix>.")
    return database_url


TEST_DATABASE_URL = test_database_url()
os.environ["VIEWER_TESTING"] = "true"
# Alembic intentionally sees the same explicitly guarded test URL.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def alembic_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


@pytest.fixture(scope="session", autouse=True)
def migrate_test_database():
    command.upgrade(alembic_config(), "head")
    yield
    from storage import dispose_engines

    dispose_engines()


@pytest.fixture(autouse=True)
def empty_test_database():
    """Reset only the explicitly validated database before every test."""
    engine = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE TABLE viewer_tournaments RESTART IDENTITY"))
    finally:
        engine.dispose()


@pytest.fixture
def app():
    from app import create_app

    return create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sync_payload():
    def build(token: str, marker: str = "initial") -> dict:
        return {
            "public_view_token": token,
            "snapshot_version": 1,
            "generated_at": "2026-08-30T12:00:00+00:00",
            "tournament": {
                "id": 42,
                "name": f"Friday Night {marker}",
                "td_name": "Tournament Director",
                "status": "active",
            },
            "stats": {"players": 8, "marker": marker},
            "tables": [{"number": 1, "marker": marker}],
            "queue": [{"player": "Ada", "marker": marker}],
            "players": [{"name": "Ada", "marker": marker}],
            "winner": None,
        }

    return build
