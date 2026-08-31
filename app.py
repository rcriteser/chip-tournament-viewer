import os
from typing import Any

from flask import Flask, jsonify, render_template, request

from config import load_config
from storage import SyncRejectedError, get_record_by_token, get_snapshot_by_token, upsert_snapshot


SYNC_HEADER_NAME = "X-Viewer-Sync-Key"


def create_app(config_overrides: dict[str, Any] | None = None) -> Flask:
    """Build a Viewer app configured exclusively for PostgreSQL persistence."""
    app_config = load_config(config_overrides)
    app = Flask(__name__)
    app.config.from_mapping(app_config)

    @app.route("/")
    def home():
        return render_template("home.html", app_name=app.config["APP_NAME"])

    @app.route("/health")
    def health():
        # Liveness only: this route intentionally does not connect to or alter the DB.
        return jsonify({"ok": True, "service": app.config["APP_NAME"]})

    @app.route("/viewer/<token>")
    def viewer_page(token: str):
        record = get_record_by_token(token)
        if not record:
            return render_template(
                "viewer.html", app_name=app.config["APP_NAME"], token=token, not_found=True
            ), 404
        return render_template(
            "viewer.html", app_name=app.config["APP_NAME"], token=token, not_found=False
        )

    @app.route("/api/viewer/<token>")
    def api_viewer_snapshot(token: str):
        snapshot = get_snapshot_by_token(token)
        if snapshot is None:
            return jsonify({"error": "Viewer page not found."}), 404
        return jsonify(snapshot)

    @app.route("/api/viewer-sync/<token>", methods=["POST"])
    def api_viewer_sync(token: str):
        sync_key = (request.headers.get(SYNC_HEADER_NAME) or "").strip()
        if not sync_key:
            return jsonify({"ok": False, "message": "Missing sync key."}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "Invalid JSON payload."}), 400
        payload_token = str(payload.get("public_view_token") or "").strip()
        if payload_token != token:
            return jsonify({"ok": False, "message": "Token mismatch."}), 400
        try:
            upsert_snapshot(token, sync_key, payload)
        except SyncRejectedError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 403
        except Exception:
            # Never return database/provider details, snapshots, or credentials.
            app.logger.exception("Viewer synchronization failed.")
            return jsonify({"ok": False, "message": "Sync failed."}), 500
        return jsonify({"ok": True, "message": "Sync OK"})

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
