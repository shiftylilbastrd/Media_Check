from flask import Blueprint, render_template, request, redirect, url_for, flash

from . import db
from . import arr_client

bp = Blueprint("issues", __name__)


@bp.route("/")
def index():
    """Open/Ignored/All - but "All" never includes auto_fixed rows, even
    though they're real issues rows under the hood. Those were handled
    automatically with no interaction ever needed, so they don't belong on
    a page meant for things that need a look - the Log page (filtered to
    "Auto-fixed") is where that history lives instead. A direct
    ?status=auto_fixed link still works, for digging into one by hand."""
    status_filter = request.args.get("status", "open")
    if status_filter == "all":
        issues = [i for i in db.list_issues() if i["status"] != "auto_fixed"]
    else:
        issues = db.list_issues(status=status_filter)
    return render_template("issues.html", issues=issues, status_filter=status_filter)


@bp.route("/issues/<int:issue_id>/ignore", methods=["POST"])
def ignore(issue_id):
    db.set_issue_status(issue_id, "ignored")
    return redirect(url_for("issues.index", status=request.args.get("status", "open")))


@bp.route("/issues/<int:issue_id>/fix", methods=["POST"])
def fix(issue_id):
    issue = db.get_issue(issue_id)
    if not issue:
        return redirect(url_for("issues.index"))
    try:
        arr_client.trigger_search(issue["source"], issue["ext_id"], issue.get("episode_id"))
        db.set_issue_status(issue_id, "search_triggered")
        flash(f"New search triggered for: {issue['label']}")
    except Exception as e:
        flash(f"Couldn't trigger a search: {e}")
    return redirect(url_for("issues.index", status=request.args.get("status", "open")))
