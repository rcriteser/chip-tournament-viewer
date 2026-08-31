# Chip Tournament Viewer

The public Viewer is a Flask/Gunicorn service that accepts authenticated desktop
snapshots and serves them through public token URLs. PostgreSQL is the only
persistence backend. The service never reads or writes `viewer_host.db`, and it
will fail at startup if `DATABASE_URL` is missing or is not a
`postgresql+psycopg` URL.

## Production runtime

Viewer runs as an always-on Gunicorn WSGI service. Pin the App Platform Python
runtime with `runtime.txt`:

    python-3.12.13

The service uses `gunicorn==26.2.0` and the existing global WSGI callable
`app:app`. Set `VIEWER_ENV=production` and a PostgreSQL `DATABASE_URL` with
explicit TLS (`sslmode=require`, `verify-ca`, or `verify-full`). Viewer refuses
SQLite in every environment and refuses a non-TLS PostgreSQL URL in production.

Use this DigitalOcean App Platform command:

    gunicorn \
      --worker-tmp-dir /dev/shm \
      --bind "0.0.0.0:${PORT}" \
      --workers 2 \
      --worker-class sync \
      --threads 1 \
      --timeout 30 \
      --graceful-timeout 30 \
      --keep-alive 5 \
      --access-logfile - \
      --error-logfile - \
      --log-level info \
      --access-logformat '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms' \
      app:app

Gunicorn owns `$PORT` binding; do not use the direct `app.run()` server in
production. Do not add `--preload`: engines are lazy and keyed by worker PID,
so each worker owns its PostgreSQL pool. The two sync workers use one thread
each. With the default pool size of 2 and overflow of 1, they can use at most
about six web-process PostgreSQL connections. Gunicorn and application logs go
to stdout/stderr, and the custom access format intentionally excludes query
strings. The public token is part of the URL model; sync headers and snapshot
bodies are never access-logged.

`--worker-tmp-dir /dev/shm` is required by DigitalOcean App Platform's
Gunicorn-in-Docker environment. Omit only that option for local macOS testing.
Migrations remain a separate pre-deploy operation; never append `alembic
upgrade head` to the web command. `/health` is the liveness path: it is
unauthenticated, non-mutating, and database-independent.

## Configuration and migrations

Set `DATABASE_URL` before running either the application or Alembic:

    export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require'
    alembic upgrade head
    gunicorn --workers 2 --threads 1 app:app

`sslmode=require` is the initial production TLS setting. Prefer
`sslmode=verify-full` once the managed database CA and hostname validation are
configured.

The initial per-Gunicorn-worker pool defaults are deliberately conservative:
`VIEWER_DB_POOL_SIZE=2`, `VIEWER_DB_MAX_OVERFLOW=1`,
`VIEWER_DB_POOL_TIMEOUT=30`, and `VIEWER_DB_POOL_RECYCLE=1800`, with pool
pre-ping always enabled. With two workers, this permits up to six application
connections during brief overflow. Adjust worker and pool values together to
stay within the managed PostgreSQL connection limit.

Run migrations as a DigitalOcean pre-deploy job using a DDL-capable Viewer
database role. Run the Viewer service with a separate DML-only Viewer role.
Neither role needs access to the Licensing database or its tables.

## Local Gunicorn restart smoke

Use only the guarded Docker database below. Set test-only values, then run the
migration separately before the web process:

    export DATABASE_URL='postgresql+psycopg://ct_viewer_test:ct_viewer_test@127.0.0.1:55433/ct_viewer_test'
    export VIEWER_TESTING=true
    export PORT=5051
    alembic upgrade head
    gunicorn \
      --bind "0.0.0.0:${PORT}" --workers 2 --worker-class sync --threads 1 \
      --timeout 30 --graceful-timeout 30 --keep-alive 5 \
      --access-logfile - --error-logfile - --log-level info \
      --access-logformat '%(h)s "%(m)s %(U)s %(H)s" %(s)s %(B)s %(M)sms' app:app

Verify `/health`, a valid sync, public API/page responses, and a wrong-key 403.
Stop Gunicorn without stopping PostgreSQL, restart the same command, and verify
the exact persisted public snapshot remains. For worker replacement coverage,
TERM one worker, wait for Gunicorn to replace it, then verify the replacement
serves the unchanged PostgreSQL-backed snapshot.

## Local PostgreSQL tests

The complete test suite requires the dedicated disposable PostgreSQL database.
It has destructive guards: `VIEWER_POSTGRES_TEST_ENABLED` must be `true`, the
URL must use PostgreSQL on a loopback host, and its database name must begin
with `ct_viewer_test`. Tests never reset `DATABASE_URL`.

    docker run --rm --name chip-tournament-viewer-postgres-test \\
      -e POSTGRES_DB=ct_viewer_test \\
      -e POSTGRES_USER=ct_viewer_test \\
      -e POSTGRES_PASSWORD=ct_viewer_test \\
      -p 127.0.0.1:55433:5432 postgres:17

In another terminal:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    export VIEWER_POSTGRES_TEST_ENABLED=true
    export VIEWER_POSTGRES_TEST_DATABASE_URL='postgresql+psycopg://ct_viewer_test:ct_viewer_test@127.0.0.1:55433/ct_viewer_test'
    .venv/bin/python -m pytest

When complete, stop the container:

    docker stop chip-tournament-viewer-postgres-test

The test suite verifies migrations, first-sync contention, same-key retries,
restart durability, two-app visibility, and public reads during updates. It
does not implement retention/deletion or hosted Viewer disable/revocation;
those remain explicit product/security follow-ups.
