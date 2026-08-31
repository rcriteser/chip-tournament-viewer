from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic import command
from sqlalchemy import func, inspect, select
from sqlalchemy.dialects.postgresql import JSONB

import app as app_module
import storage
from app import create_app
from storage import dispose_engines, get_record_by_token, get_snapshot_by_token, viewer_tournaments
from tests.conftest import TEST_DATABASE_URL, alembic_config


def post_sync(client, token, key, payload):
    return client.post(
        f"/api/viewer-sync/{token}", json=payload, headers={"X-Viewer-Sync-Key": key}
    )


def test_health_is_liveness_only_without_database_connection():
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "postgresql+psycopg://test:test@127.0.0.1:1/ct_viewer_test",
        }
    )
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_config_requires_postgresql_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("VIEWER_POSTGRES_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("VIEWER_TESTING", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        create_app()
    with pytest.raises(RuntimeError, match="SQLite is not supported"):
        create_app({"DATABASE_URL": "sqlite:///viewer_host.db"})


def test_production_config_requires_postgresql_tls():
    url = "postgresql+psycopg://test:test@127.0.0.1:1/ct_viewer_test"
    with pytest.raises(RuntimeError, match="must require PostgreSQL TLS"):
        create_app({"DATABASE_URL": url, "VIEWER_ENV": "production"})

    app = create_app({
        "DATABASE_URL": f"{url}?sslmode=require",
        "VIEWER_ENV": "production",
    })
    assert app.debug is False


def test_testing_config_allows_guarded_non_tls_postgresql():
    app = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    assert app.config["VIEWER_ENV"] == "testing"
    assert app.debug is False


def test_factory_does_not_create_an_engine_before_a_database_request():
    dispose_engines()
    create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    assert storage._engines == {}


def test_first_sync_public_api_and_viewer_page(client, sync_payload):
    token, key = "public-token", "sync-key"
    payload = sync_payload(token)
    assert post_sync(client, token, key, payload).status_code == 200

    public_response = client.get(f"/api/viewer/{token}")
    assert public_response.status_code == 200
    assert public_response.get_json() == payload
    assert "sync-key" not in public_response.get_data(as_text=True)
    assert client.get(f"/viewer/{token}").status_code == 200


def test_sync_validation_and_unknown_token(client, sync_payload):
    token = "validation-token"
    assert client.post(f"/api/viewer-sync/{token}", json=sync_payload(token)).status_code == 401
    assert (
        client.post(
            f"/api/viewer-sync/{token}", data="{not-json", content_type="application/json",
            headers={"X-Viewer-Sync-Key": "key"},
        ).status_code
        == 400
    )
    assert (
        post_sync(client, token, "key", {"public_view_token": "another-token"}).status_code == 400
    )
    assert client.get("/api/viewer/missing-token").status_code == 404
    assert client.get("/viewer/missing-token").status_code == 404


def test_same_key_retry_updates_complete_snapshot(app, client, sync_payload):
    token, key = "retry-token", "retry-key"
    first, second = sync_payload(token, "first"), sync_payload(token, "second")
    assert post_sync(client, token, key, first).status_code == 200
    assert post_sync(client, token, key, second).status_code == 200
    with app.app_context():
        record = get_record_by_token(token)
    assert record is not None
    assert record["viewer_sync_key"] == key
    assert record["latest_snapshot"] == second


def test_wrong_key_cannot_mutate_snapshot(app, client, sync_payload):
    token = "protected-token"
    original, malicious = sync_payload(token, "original"), sync_payload(token, "malicious")
    assert post_sync(client, token, "correct-key", original).status_code == 200
    response = post_sync(client, token, "wrong-key", malicious)
    assert response.status_code == 403
    assert response.get_json()["message"] == "Invalid sync key."
    with app.app_context():
        assert get_snapshot_by_token(token) == original


def test_disabled_or_invalid_license_is_rejected_atomically(app, client, sync_payload):
    token, key = "disabled-token", "disabled-key"
    assert post_sync(client, token, key, sync_payload(token)).status_code == 200
    with app.app_context():
        from storage import get_engine

        with get_engine().begin() as connection:
            connection.execute(
                viewer_tournaments.update()
                .where(viewer_tournaments.c.public_view_token == token)
                .values(viewer_enabled=False)
            )
    response = post_sync(client, token, key, sync_payload(token, "after-disabled"))
    assert response.status_code == 403
    assert response.get_json()["message"] == "Viewer is disabled."


def test_unexpected_exception_is_generic(client, sync_payload, monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("sensitive database detail")

    monkeypatch.setattr(app_module, "upsert_snapshot", fail)
    response = post_sync(client, "failure-token", "failure-key", sync_payload("failure-token"))
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "message": "Sync failed."}
    assert "sensitive" not in response.get_data(as_text=True)


def test_clean_migration_creates_expected_postgresql_schema():
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    from sqlalchemy import create_engine

    engine = create_engine(TEST_DATABASE_URL)
    try:
        inspector = inspect(engine)
        assert "viewer_tournaments" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("viewer_tournaments")}
        assert isinstance(columns["latest_snapshot"]["type"], JSONB)
        assert columns["created_at"]["type"].timezone is True
        assert columns["updated_at"]["type"].timezone is True
        assert columns["viewer_enabled"]["nullable"] is False
        assert columns["license_status"]["nullable"] is False
        assert columns["viewer_enabled"]["default"] is not None
        assert columns["license_status"]["default"] is not None
        assert any(
            constraint["name"] == "uq_viewer_tournaments_public_view_token"
            for constraint in inspector.get_unique_constraints("viewer_tournaments")
        )
    finally:
        engine.dispose()


def test_concurrent_first_sync_different_keys(sync_payload):
    token = "contention-token"
    apps = [create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL}) for _ in range(2)]
    keys = ["key-a", "key-b"]
    barrier = Barrier(2)

    def attempt(index):
        with apps[index].test_client() as client:
            barrier.wait()
            return keys[index], post_sync(client, token, keys[index], sync_payload(token, keys[index]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))
    assert sorted(response.status_code for _, response in outcomes) == [200, 403]
    winning_key = next(key for key, response in outcomes if response.status_code == 200)
    with apps[0].app_context():
        from storage import get_engine

        record = get_record_by_token(token)
        with get_engine().connect() as connection:
            row_count = connection.scalar(
                select(func.count()).select_from(viewer_tournaments).where(
                    viewer_tournaments.c.public_view_token == token
                )
            )
    assert record is not None
    assert record["viewer_sync_key"] == winning_key
    assert row_count == 1


def test_concurrent_same_key_syncs_are_both_valid(sync_payload):
    token, key = "same-key-token", "same-key"
    apps = [create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL}) for _ in range(2)]
    barrier = Barrier(2)

    def attempt(marker):
        with apps[marker].test_client() as client:
            barrier.wait()
            return post_sync(client, token, key, sync_payload(token, f"snapshot-{marker}"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(attempt, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    with apps[0].app_context():
        from storage import get_engine

        snapshot = get_snapshot_by_token(token)
        with get_engine().connect() as connection:
            row_count = connection.scalar(
                select(func.count()).select_from(viewer_tournaments).where(
                    viewer_tournaments.c.public_view_token == token
                )
            )
        assert get_record_by_token(token) is not None
    assert snapshot is not None
    assert snapshot["stats"]["marker"] in {"snapshot-0", "snapshot-1"}
    assert row_count == 1


def test_restart_durability(sync_payload):
    token, key = "restart-token", "restart-key"
    first_app = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    payload = sync_payload(token, "persisted")
    assert post_sync(first_app.test_client(), token, key, payload).status_code == 200
    dispose_engines()
    second_app = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    response = second_app.test_client().get(f"/api/viewer/{token}")
    assert response.status_code == 200
    assert response.get_json() == payload


def test_two_instances_share_state_and_enforce_key(sync_payload):
    token, key = "multi-instance-token", "multi-instance-key"
    app_a = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    app_b = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    initial, updated = sync_payload(token, "initial"), sync_payload(token, "updated")
    assert post_sync(app_a.test_client(), token, key, initial).status_code == 200
    assert app_b.test_client().get(f"/api/viewer/{token}").get_json() == initial
    assert post_sync(app_b.test_client(), token, key, updated).status_code == 200
    assert app_a.test_client().get(f"/api/viewer/{token}").get_json() == updated
    assert post_sync(app_a.test_client(), token, "not-the-key", initial).status_code == 403


def test_public_reads_see_only_complete_json_documents(app, sync_payload):
    token, key = "read-concurrency-token", "read-concurrency-key"
    initial = sync_payload(token, "initial")
    assert post_sync(app.test_client(), token, key, initial).status_code == 200
    allowed_markers = {"initial"}
    updates = [sync_payload(token, f"update-{index}") for index in range(15)]
    allowed_markers.update(item["stats"]["marker"] for item in updates)
    writer_app = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})
    reader_app = create_app({"TESTING": True, "DATABASE_URL": TEST_DATABASE_URL})

    def write_updates():
        with writer_app.test_client() as client:
            return [post_sync(client, token, key, payload).status_code for payload in updates]

    def read_snapshots():
        with reader_app.test_client() as client:
            responses = [client.get(f"/api/viewer/{token}") for _ in range(30)]
        return [response.get_json() for response in responses]

    with ThreadPoolExecutor(max_workers=2) as executor:
        write_result = executor.submit(write_updates)
        read_result = executor.submit(read_snapshots)
        assert write_result.result() == [200] * len(updates)
        snapshots = read_result.result()
    assert all(snapshot["stats"]["marker"] in allowed_markers for snapshot in snapshots)
    assert all(snapshot["tables"] and snapshot["players"] for snapshot in snapshots)
