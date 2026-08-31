import os
import logging
from logging.config import fileConfig

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import engine_from_config, pool

from migration_safety import (
    SERVICE_NAME,
    MigrationSafetyError,
    migration_settings,
    postgresql_migration_lock,
    sanitized_database_identity,
    verify_connected_database,
)
from storage import metadata, normalize_postgresql_psycopg_url, validate_postgresql_url


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
target_metadata = metadata
logger = logging.getLogger("alembic.migration")


def _database_url() -> str:
    url = os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("DATABASE_URL is required to run Viewer migrations.")
    normalized = normalize_postgresql_psycopg_url(url)
    validate_postgresql_url(normalized)
    return normalized


def _single_head() -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise MigrationSafetyError(
            f"Viewer migrations require exactly one Alembic head; found {len(heads)}."
        )
    return heads[0]


def _current_revision(connection) -> str:
    return MigrationContext.configure(connection).get_current_revision() or "base"


def _run_migrations_with_safety(connection, database_url: str) -> None:
    """Run Alembic while holding the lock on this exact connection."""

    parsed_url, settings = migration_settings(database_url)
    expected_head = _single_head()
    identity = sanitized_database_identity(parsed_url)
    try:
        verify_connected_database(connection, settings)
        with postgresql_migration_lock(connection, settings):
            current_revision = _current_revision(connection)
            logger.info(
                "migration_start service=%s %s current_revision=%s target_revision=%s",
                SERVICE_NAME,
                identity,
                current_revision,
                expected_head,
            )
            # The revision read starts SQLAlchemy's implicit transaction; let
            # Alembic begin and commit the actual DDL transaction itself.
            if connection.in_transaction():
                connection.commit()
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
            if connection.in_transaction():
                connection.commit()
            final_revision = _current_revision(connection)
            requested_revision = context.get_revision_argument()
            if requested_revision in {"head", "heads"} and final_revision != expected_head:
                raise MigrationSafetyError(
                    "Viewer migration completed without reaching the expected Alembic head."
                )
            logger.info(
                "migration_success service=%s %s current_revision=%s",
                SERVICE_NAME,
                identity,
                final_revision,
            )
    except MigrationSafetyError:
        raise
    except Exception:
        logger.error("migration_failed service=%s %s", SERVICE_NAME, identity)
        raise RuntimeError("Viewer migration failed; inspect sanitized migration logs.") from None


def _connect_and_run_migrations(connectable, database_url: str) -> None:
    """Avoid surfacing driver connection strings in deployment failures."""

    parsed_url, _ = migration_settings(database_url)
    identity = sanitized_database_identity(parsed_url)
    try:
        with connectable.connect() as connection:
            _run_migrations_with_safety(connection, database_url)
    except MigrationSafetyError:
        raise
    except Exception:
        logger.error("migration_connection_or_execution_failed service=%s %s", SERVICE_NAME, identity)
        raise RuntimeError(
            "Viewer migration could not connect or execute; inspect sanitized migration logs."
        ) from None


def run_migrations_offline() -> None:
    database_url = _database_url()
    migration_settings(database_url)
    _single_head()
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    database_url = _database_url()
    # Production URL/TLS/expected-name checks fail before a connection or DDL.
    migration_settings(database_url)
    _single_head()
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)
    _connect_and_run_migrations(connectable, database_url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
