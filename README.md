# Chip Tournament Viewer

The public Viewer is a Flask/Gunicorn service that accepts authenticated desktop
snapshots and serves them through public token URLs. PostgreSQL is the only
persistence backend. The service never reads or writes `viewer_host.db`, and it
will fail at startup if `DATABASE_URL` is missing or is not a
`postgresql+psycopg` URL.

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
