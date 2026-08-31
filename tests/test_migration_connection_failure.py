"""Viewer migration connection failures must not expose credential URLs."""

from __future__ import annotations

from tests.test_migration_safety import _upgrade


def test_connection_failure_is_sanitized():
    result = _upgrade("postgresql+psycopg://migration_user:never-log-me@127.0.0.1:1/viewer")
    output = result.stderr + result.stdout
    assert result.returncode != 0
    assert "could not connect or execute" in output
    assert "never-log-me" not in output
