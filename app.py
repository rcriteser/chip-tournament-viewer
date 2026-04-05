import os
from flask import Flask, jsonify, render_template, request
from storage import (
    can_accept_sync,
    get_record_by_token,
    get_snapshot_by_token,
    init_db,
    upsert_snapshot,
)

app = Flask(__name__)

APP_NAME = os.getenv("APP_NAME", "Chip Tournament Viewer")
SYNC_HEADER_NAME = "X-Viewer-Sync-Key"


@app.before_request
def ensure_db() -> None:
    init_db()


@app.route("/")
def home():
    return render_template("home.html", app_name=APP_NAME)


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": APP_NAME})


@app.route("/viewer/<token>")
def viewer_page(token: str):
    record = get_record_by_token(token)
    if not record:
        return render_template(
            "viewer.html",
            app_name=APP_NAME,
            token=token,
            not_found=True,
        ), 404

    return render_template(
        "viewer.html",
        app_name=APP_NAME,
        token=token,
        not_found=False,
    )


@app.route("/api/viewer/<token>")
def api_viewer_snapshot(token: str):
    snapshot = get_snapshot_by_token(token)
    if not snapshot:
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

    existing = get_record_by_token(token)
    if existing:
        allowed, reason = can_accept_sync(existing)
        if not allowed:
            return jsonify({"ok": False, "message": reason}), 403

    try:
        upsert_snapshot(token, sync_key, payload)
        return jsonify({"ok": True, "message": "Sync OK"})
    except PermissionError as e:
        return jsonify({"ok": False, "message": str(e)}), 403
    except Exception as e:
        return jsonify({"ok": False, "message": f"Sync failed: {e}"}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)