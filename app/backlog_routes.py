from flask import Blueprint, render_template

from . import backlog
from . import db

bp = Blueprint("backlog_ui", __name__)


@bp.route("/backlog")
def index():
    """Shows what the backlog scanner has actually done, so it's not just a
    silent background thread. Totals (radarr_total/sonarr_total) are live
    API calls - cheap ones (one request each, see backlog.py), but still
    best-effort: if Radarr/Sonarr aren't configured or aren't reachable,
    show scanned-so-far without a total rather than erroring the page."""
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
        "backlog.html",
        radarr_scanned=radarr_scanned,
        sonarr_scanned=sonarr_scanned,
        radarr_total=radarr_tot,
        sonarr_total=sonarr_tot,
        batch_size=backlog.batch_size(),
        interval_hours=backlog.interval_hours(),
        recent=recent,
    )
