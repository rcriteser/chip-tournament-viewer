# Viewer database deployment

## Owner and command

The deployed Viewer App Platform application, `ct-viewer-prod`, has exactly one
pre-deploy migration job, `ct-viewer-migrate`. Its exact command is
`python -m alembic upgrade head`. Only this job owns Viewer migrations.
Gunicorn, `create_app`, startup hooks, requests, post-deploy hooks, workers,
and Desktop must not run migrations.

The job uses `ct_viewer_migrator` against `ct_viewer_prod` through the
DigitalOcean VPC/private PostgreSQL hostname with `sslmode=require`. It has
completed revision `0001_viewer_pg`; `alembic_version` and
`viewer_tournaments` are owned by `ct_viewer_migrator`.

## Migration controls

The job receives only an encrypted Viewer DDL `DATABASE_URL` and non-secret
`MIGRATION_ENV=production`,
`MIGRATION_EXPECTED_DATABASE_NAME=ct_viewer_prod`,
`MIGRATION_ADVISORY_LOCK_TIMEOUT_SECONDS=30`, and
`MIGRATION_DDL_LOCK_TIMEOUT_SECONDS=30`.

Viewer safely normalizes provider `postgresql://` to `postgresql+psycopg://` and
rejects SQLite/other drivers. Production requires host, exact expected database,
server-side `current_database()` match, and TLS `sslmode=require`, `verify-ca`,
or `verify-full` before DDL. The same Alembic SQLAlchemy connection acquires a
stable Viewer-only advisory lock, polls for no more than 30 seconds by default,
sets PostgreSQL `lock_timeout`, executes DDL, and releases the lock. Sanitized
logs include only service, host/port, database, revisions, lock state, and
result—never credential URLs, sync keys, snapshots, or secrets.

## Roles, isolation, and verification

`ct_viewer_migrator` has CONNECT only to `ct_viewer_prod`, schema USAGE/CREATE,
ownership of migration-created objects, and the DDL, sequence, data-migration,
and `alembic_version` rights required by Alembic. `ct_viewer_web` has CONNECT
only to `ct_viewer_prod`, schema USAGE, SELECT/INSERT/UPDATE/DELETE, and
required sequence usage; it has no CREATE/ALTER/DROP or schema ownership.
Default privileges grant web-role access to migration-created tables and
sequences. The tested web-role ACLs confirm `ct_viewer_web` can connect to
`ct_viewer_prod` but not `ct_licensing_prod`.

Exactly one Alembic head is required; current/target/final revisions are logged
and final revision is checked. Re-running the command at head is the safe retry.
Use PRE_DEPLOY verification plus post-deploy smoke testing; keep `/health`
liveness-only and database-independent.

## Compatibility and recovery

Use expand/contract: additive compatible schema, old/new-compatible code,
separate backfill, then a later contract release. Prefer tables, nullable
columns, safe defaults, compatible indexes/constraints, and additive enum/check
values. Rename by add, dual-support, backfill, switch, then later removal. Add
NOT NULL only after data is populated and code guarantees it. Preflight/repair
duplicates before unique constraints. Review normal index locks; large/high-write
tables may need `CREATE INDEX CONCURRENTLY` with separate transaction handling.
Large data work belongs in a resumable batched maintenance operation, not
PRE_DEPLOY. Do not rewrite historical migrations without proven correctness need.

On migration failure, do not bypass PRE_DEPLOY or launch the new web release.
Keep old code authoritative; inspect logs/revision/schema and transaction state;
test a correction on a disposable/restored copy, then redeploy. If schema and
revision disagree, halt and obtain DBA/reviewer help; repair forward or restore
deliberately, never blindly rerun or edit `alembic_version`.

Never automatically downgrade production. If migration succeeds but new app code
fails, keep the DB forward and restore prior code only when compatible with the
additive schema. Viewer downgrade drops Viewer state, so downgrade functions are
for development/tests/controlled recovery analysis, not normal rollback. Before
first live migration and non-additive/high-risk work, confirm backup/PITR,
identity, recovery owner, restore procedure, and recovery point/time. Manual
production migration is exceptional, reviewed, recorded, and uses these same
safeguards; ad-hoc SQL must not fake Alembic state.
