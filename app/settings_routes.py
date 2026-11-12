import requests
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from . import db

bp = Blueprint("settings", __name__)

TIMEOUT = 10

# Each section: (title, [(key, label, placeholder, is_secret), ...], test_service or None)
SECTIONS = [
    ("Radarr", [
        ("radarr_url", "Radarr URL", "http://192.168.1.13:7878", False),
        ("radarr_api_key", "Radarr API Key", "", True),
    ], "radarr"),
    ("Sonarr", [
        ("sonarr_url", "Sonarr URL", "http://192.168.1.13:8989", False),
        ("sonarr_api_key", "Sonarr API Key", "", True),
    ], "sonarr"),
    ("Plex", [
        ("plex_url", "Plex URL", "http://192.168.1.13:32400", False),
        ("plex_token", "Plex Token", "", True),
    ], "plex"),
    ("Pushover", [
        ("pushover_token", "Pushover Application Token", "", True),
        ("pushover_user_key", "Pushover User Key", "", True),
    ], "pushover"),
    ("Backlog scan", [
        ("backlog_scan_batch_size", "Files per pass", "20", False),
        ("backlog_scan_interval_hours", "Hours between passes", "24", False),
    ], None),
]

# Flattened (key, label, placeholder, is_secret) - used for save/load, same as before.
FIELDS = [field for _, fields, _ in SECTIONS for field in fields]

# Which posted JSON keys the test endpoint expects for each service, and which
# settings-page field each maps to (so Test can check the values currently in
# the form, not whatever was last saved).
TEST_FIELD_MAP = {
    "radarr": {"url": "radarr_url", "api_key": "radarr_api_key"},
    "sonarr": {"url": "sonarr_url", "api_key": "sonarr_api_key"},
    "plex": {"url": "plex_url", "token": "plex_token"},
    "pushover": {"token": "pushover_token", "user_key": "pushover_user_key"},
}


@bp.route("/settings", methods=["GET", "POST"])
def edit():
    if request.method == "POST":
        for key, _, _, _ in FIELDS:
            db.set_setting(key, request.form.get(key, "").strip())
        flash("Settings saved.")
        return redirect(url_for("settings.edit"))
    values = {key: db.get_setting(key, "") for key, _, _, _ in FIELDS}
    return render_template(
        "settings.html", sections=SECTIONS, values=values, test_fields=TEST_FIELD_MAP,
    )


@bp.route("/settings/test/<service>", methods=["POST"])
def test(service):
    """Tests the values currently typed into the form (not the saved ones),
    so Ryan can check a key before hitting Save. Never touches the database."""
    data = request.get_json(silent=True) or {}

    if service in ("radarr", "sonarr"):
        url = (data.get("url") or "").strip().rstrip("/")
        key = (data.get("api_key") or "").strip()
        if not url or not key:
            return jsonify(ok=False, message="URL and API key are both required")
        try:
            r = requests.get(
                f"{url}/api/v3/system/status", headers={"X-Api-Key": key}, timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            return jsonify(ok=False, message=f"Couldn't connect: {e}")
        if r.status_code == 200:
            version = (r.json() or {}).get("version", "")
            return jsonify(ok=True, message=f"Connected ({service.capitalize()} {version})".strip())
        if r.status_code == 401:
            return jsonify(ok=False, message="Unauthorized - check the API key")
        return jsonify(ok=False, message=f"HTTP {r.status_code} - check the URL")

    if service == "plex":
        url = (data.get("url") or "").strip().rstrip("/")
        token = (data.get("token") or "").strip()
        if not url or not token:
            return jsonify(ok=False, message="URL and token are both required")
        try:
            r = requests.get(
                f"{url}/library/sections",
                headers={"X-Plex-Token": token, "Accept": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            return jsonify(ok=False, message=f"Couldn't connect: {e}")
        if r.status_code == 200:
            return jsonify(ok=True, message="Connected")
        if r.status_code == 401:
            return jsonify(ok=False, message="Unauthorized - check the token")
        return jsonify(ok=False, message=f"HTTP {r.status_code} - check the URL")

    if service == "pushover":
        token = (data.get("token") or "").strip()
        user = (data.get("user_key") or "").strip()
        if not token or not user:
            return jsonify(ok=False, message="Application token and user key are both required")
        try:
            r = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": token, "user": user,
                    "title": "media-check-tools", "message": "Test notification from Settings",
                },
                timeout=15,
            )
        except requests.exceptions.RequestException as e:
            return jsonify(ok=False, message=f"Couldn't connect: {e}")
        body = r.json() if r.content else {}
        if r.status_code == 200 and body.get("status") == 1:
            return jsonify(ok=True, message="Sent - check your device")
        errors = ", ".join(body.get("errors") or []) or f"HTTP {r.status_code}"
        return jsonify(ok=False, message=errors)

    return jsonify(ok=False, message="Unknown service"), 404
