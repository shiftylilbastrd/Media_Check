from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import db

bp = Blueprint("settings", __name__)

# (key, display label, placeholder, is_secret)
FIELDS = [
    ("radarr_url", "Radarr URL", "http://192.168.1.13:7878", False),
    ("radarr_api_key", "Radarr API Key", "", True),
    ("sonarr_url", "Sonarr URL", "http://192.168.1.13:8989", False),
    ("sonarr_api_key", "Sonarr API Key", "", True),
    ("plex_url", "Plex URL", "http://192.168.1.13:32400", False),
    ("plex_token", "Plex Token", "", True),
    ("pushover_token", "Pushover Application Token", "", True),
    ("pushover_user_key", "Pushover User Key", "", True),
    ("backlog_scan_batch_size", "Backlog scan: files per pass", "20", False),
    ("backlog_scan_interval_hours", "Backlog scan: hours between passes", "24", False),
]


@bp.route("/settings", methods=["GET", "POST"])
def edit():
    if request.method == "POST":
        for key, _, _, _ in FIELDS:
            db.set_setting(key, request.form.get(key, "").strip())
        flash("Settings saved.")
        return redirect(url_for("settings.edit"))
    values = {key: db.get_setting(key, "") for key, _, _, _ in FIELDS}
    return render_template("settings.html", fields=FIELDS, values=values)
