"""Production migration safeguards for the PostgreSQL-only Viewer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from migration_safety import MigrationSafetyError, migration_settings, postgresql_migration_lock
from storage import normalize_postgresql_psycopg_url
from tests.conftest import TEST_DATABASE_URL


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upgrade(database_url: str, extra_environment: dict[str, str] | None = None):
    environment = {
        "DATABASE_URL": database_url,
        "PATH": os.environ.get("PATH", ""),
        "VIEWER_TESTING": "true",
    }
    environment.update(extra_environment or {})
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_migration_script_has_exactly_one_head():
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == ["0001_viewer_pg"]


def test_provider_postgresql_url_normalizes_to_psycopg3():
    normalized = normalize_postgresql_psycopg_url(
        "postgresql://user:password@example.test:25060/viewer?sslmode=require"
    )
    assert make_url(normalized).drivername == "postgresql+psycopg"


def test_unsupported_provider_driver_is_rejected():
    with pytest.raises(ValueError, match="psycopg"):
        normalize_postgresql_psycopg_url("postgresql+psycopg2://user:password@example.test/viewer")


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_production_migration_accepts_safe_tls_modes(monkeypatch, sslmode):
    monkeypatch.setenv("MIGRATION_ENV", "production")
    monkeypatch.setenv("MIGRATION_EXPECTED_DATABASE_NAME", "viewer")
    _, settings = migration_settings(
        f"postgresql+psycopg://user:password@example.test/viewer?sslmode={sslmode}"
    )
    assert settings.production is True


@pytest.mark.parametrize("database_url", [
    "postgresql+psycopg://user:password@example.test/viewer",
    "sqlite:///:memory:",
])
def test_production_migration_rejects_missing_tls_or_sqlite(monkeypatch, database_url):
    monkeypatch.setenv("MIGRATION_ENV", "production")
    monkeypatch.setenv("MIGRATION_EXPECTED_DATABASE_NAME", "viewer")
    with pytest.raises(MigrationSafetyError):
        migration_settings(database_url)


def test_production_migration_rejects_wrong_expected_database_before_connect(monkeypatch):
    monkeypatch.setenv("MIGRATION_ENV", "production")
    monkeypatch.setenv("MIGRATION_EXPECTED_DATABASE_NAME", "licensing")
    with pytest.raises(MigrationSafetyError, match="identity"):
        migration_settings("postgresql+psycopg://user:password@example.test/viewer?sslmode=require")


def test_postgres_retry_lock_and_wrong_database_protection():
    engine = create_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))

        wrong_name_url = make_url(TEST_DATABASE_URL).update_query_dict({"sslmode": "require"})
        wrong = _upgrade(
            wrong_name_url.render_as_string(hide_password=False),
            {
                "MIGRATION_ENV": "production",
                "MIGRATION_EXPECTED_DATABASE_NAME": "ct_viewer_wrong_target",
            },
        )
        assert wrong.returncode != 0
        assert "identity" in (wrong.stderr + wrong.stdout).lower()
        assert "viewer_tournaments" not in inspect(engine).get_table_names()

        first = _upgrade(TEST_DATABASE_URL)
        second = _upgrade(TEST_DATABASE_URL)
        assert first.returncode == 0
        assert second.returncode == 0

        with engine.connect() as holder:
            _, settings = migration_settings(TEST_DATABASE_URL)
            with postgresql_migration_lock(holder, settings):
                blocked = _upgrade(
                    TEST_DATABASE_URL,
                    {"MIGRATION_ADVISORY_LOCK_TIMEOUT_SECONDS": "1"},
                )
                assert blocked.returncode != 0
                assert "advisory lock" in (blocked.stderr + blocked.stdout).lower()
        released = _upgrade(TEST_DATABASE_URL)
        assert released.returncode == 0
        assert "viewer_tournaments" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
