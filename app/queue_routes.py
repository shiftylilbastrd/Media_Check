from flask import Blueprint, render_template

from . import backlog
from . import db

bp = Blueprint("queue_ui", __name__)


@bp.route("/queue")
def index():
    """What's waiting on the worker: newly-imported files first (oldest
    queued = next up), then, once that's empty, the worker fills idle time
    from the pre-existing library instead - there's nothing to list for that
    part ahead of time (it's derived live from Radarr/Sonarr each time), so
    this page just notes that's what's happening when the queue is empty."""
    pending = db.list_queue(limit=100)
    return render_template("queue.html", pending=pending, pending_count=len(pending))


@bp.route("/log")
def log():
    """What the worker has actually completed - newest first - plus overall
    scanned/total progress per source. Totals are live, cheap API calls
    (one request each - see backlog.py); best-effort, so an unreachable or
    unconfigured Radarr/Sonarr just means no total shown, not an error."""
    radarr_scanned = db.count_scanned("radarr")
    sonarr_scanned = db.count_scanned("sonarr")
    try:
        radarr_tot = backlog.radarr_total()
    except Exception:
        radarr_tot = None
    try:
        sonarr_tot = backlog.sonarr_total()
    except Exception:
        sonarr_tot = None
    recent = db.list_recent_scanned(limit=50)
    return render_template(
        "log.html",
        radarr_scanned=radarr_scanned, sonarr_scanned=sonarr_scanned,
        radarr_total=radarr_tot, sonarr_total=sonarr_tot, recent=recent,
    )
