import csv
import io

from flask import Blueprint, Response, render_template, request

from . import db
from .filters import duration

bp = Blueprint("queue_ui", __name__)

PER_PAGE_CHOICES = [25, 50, 100, 200]
DEFAULT_PER_PAGE = 50


def _page_params():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page", DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        per_page = DEFAULT_PER_PAGE
    if per_page not in PER_PAGE_CHOICES:
        per_page = DEFAULT_PER_PAGE
    return page, per_page


@bp.route("/queue")
def index():
    """What's waiting on the worker: newly-imported files first (oldest
    queued = next up), then, once that's empty, the worker fills idle time
    from the pre-existing library instead - there's nothing to list for that
    part ahead of time (it's derived live from Radarr/Sonarr each time), so
    this page just notes that's what's happening when the queue is empty."""
    page, per_page = _page_params()
    total = db.count_queue()
    pending = db.list_queue(limit=per_page, offset=(page - 1) * per_page)
    return render_template(
        "queue.html", pending=pending, total=total, page=page, per_page=per_page,
        per_page_choices=PER_PAGE_CHOICES,
    )


def _log_filter_params():
    """source/result/q, parsed and validated the same way for both the /log
    page and its CSV export, so the two never drift apart on what counts as
    a valid filter value."""
    source = request.args.get("source") or None
    if source not in ("radarr", "sonarr"):
        source = None
    result = request.args.get("result") or None
    if result not in db.RESULT_FILTERS:
        result = None
    q = (request.args.get("q") or "").strip() or None
    return source, result, q


@bp.route("/log")
def log():
    """What the worker has actually completed - newest first - plus overall
    scanned/total progress per source. Totals are read from a cache the
    worker refreshes periodically (backlog.refresh_totals_cache()), never
    fetched live here - with a large library, those live Radarr/Sonarr
    listing calls were adding real latency to every page load.

    source/result/q filter the list (and the pagination total) via query
    params - e.g. /log?source=radarr&result=broken&q=avengers - leaving any
    of them unset (or "all"/blank) shows everything, same as before filters
    existed."""
    page, per_page = _page_params()
    source, result, q = _log_filter_params()

    radarr_scanned = db.count_scanned("radarr")
    sonarr_scanned = db.count_scanned("sonarr")
    radarr_tot_raw = db.get_setting("cache_radarr_total")
    sonarr_tot_raw = db.get_setting("cache_sonarr_total")
    radarr_tot = int(radarr_tot_raw) if radarr_tot_raw is not None else None
    sonarr_tot = int(sonarr_tot_raw) if sonarr_tot_raw is not None else None
    totals_at = db.get_setting("cache_totals_at")
    total_scanned = radarr_scanned + sonarr_scanned
    filtered_total = db.count_recent_scanned(source=source, result=result, q=q)
    recent = db.list_recent_scanned(limit=per_page, offset=(page - 1) * per_page,
                                     source=source, result=result, q=q)
    return render_template(
        "log.html",
        radarr_scanned=radarr_scanned, sonarr_scanned=sonarr_scanned,
        radarr_total=radarr_tot, sonarr_total=sonarr_tot, totals_at=totals_at,
        recent=recent, total=filtered_total, overall_total=total_scanned,
        page=page, per_page=per_page, per_page_choices=PER_PAGE_CHOICES,
        source_filter=source or "", result_filter=result or "", q_filter=q or "",
    )


@bp.route("/log/export.csv")
def log_export():
    """Download the *entire* filtered list (not just the current page) as
    CSV - same source/result/q query params as /log, so "download this
    filtered view" just means hitting this route with the page's current
    query string."""
    source, result, q = _log_filter_params()
    rows = db.iter_all_scanned(source=source, result=result, q=q)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Item", "Source", "Result", "Duration", "Scanned at (UTC)"])
    for row in rows:
        writer.writerow([
            row["label"] or f"file #{row['file_id']}",
            row["source"].capitalize(),
            row["result"] or "clean",
            duration(row["duration_ms"]),
            row["scanned_at"],
        ])

    filename_bits = ["media-check-log"]
    if source:
        filename_bits.append(source)
    if result:
        filename_bits.append(result)
    filename = "-".join(filename_bits) + ".csv"

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
