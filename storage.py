import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("VIEWER_DB_PATH", os.path.join(BASE_DIR, "viewer_host.db"))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS viewer_tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_view_token TEXT NOT NULL UNIQUE,
                viewer_sync_key TEXT NOT NULL,
                tournament_name TEXT,
                td_name TEXT,
                tournament_status TEXT,
                latest_snapshot_json TEXT NOT NULL,
                viewer_enabled INTEGER NOT NULL DEFAULT 1,
                license_status TEXT NOT NULL DEFAULT 'active',
                license_expires_at TEXT,
                last_synced_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_record_by_token(token: str) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM viewer_tournaments
            WHERE public_view_token = ?
            """,
            (token,),
        ).fetchone()

        if not row:
            return None

        record = dict(row)
        try:
            record["latest_snapshot"] = json.loads(record["latest_snapshot_json"] or "{}")
        except Exception:
            record["latest_snapshot"] = {}

        return record
    finally:
        conn.close()


def upsert_snapshot(token: str, sync_key: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    existing = get_record_by_token(token)
    ts = now_iso()

    tournament = snapshot.get("tournament", {}) or {}

    tournament_name = (
        tournament.get("name")
        or tournament.get("tournament_name")
        or f"Tournament {tournament.get('id', '')}".strip()
    )
    td_name = tournament.get("td_name") or ""
    tournament_status = tournament.get("status") or ""
    snapshot_json = json.dumps(snapshot)

    conn = get_conn()
    try:
        if existing:
            if existing["viewer_sync_key"] != sync_key:
                raise PermissionError("Invalid sync key.")

            conn.execute(
                """
                UPDATE viewer_tournaments
                SET tournament_name = ?,
                    td_name = ?,
                    tournament_status = ?,
                    latest_snapshot_json = ?,
                    last_synced_at = ?,
                    updated_at = ?
                WHERE public_view_token = ?
                """,
                (
                    tournament_name,
                    td_name,
                    tournament_status,
                    snapshot_json,
                    ts,
                    ts,
                    token,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO viewer_tournaments (
                    public_view_token,
                    viewer_sync_key,
                    tournament_name,
                    td_name,
                    tournament_status,
                    latest_snapshot_json,
                    viewer_enabled,
                    license_status,
                    license_expires_at,
                    last_synced_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, 'active', NULL, ?, ?, ?)
                """,
                (
                    token,
                    sync_key,
                    tournament_name,
                    td_name,
                    tournament_status,
                    snapshot_json,
                    ts,
                    ts,
                    ts,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    saved = get_record_by_token(token)
    if not saved:
        raise RuntimeError("Snapshot save failed.")

    return saved


def get_snapshot_by_token(token: str) -> dict[str, Any] | None:
    record = get_record_by_token(token)
    if not record:
        return None
    return record.get("latest_snapshot") or {}


def can_accept_sync(record: dict[str, Any]) -> tuple[bool, str]:
    if not record.get("viewer_enabled", 0):
        return False, "Viewer is disabled."

    license_status = (record.get("license_status") or "").strip().lower()
    if license_status and license_status not in {"active", "trial", "grace"}:
        return False, "License is not active."

    return True, "OK"