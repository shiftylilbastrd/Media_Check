"""Side-effecting actions against Radarr/Sonarr - currently just the
automatic broken-file replace flow, ported from arr_replace() in the old
bash script: delete the file record, remove it from disk if it's still
there, blocklist the release, and trigger a new search. No user interaction
needed - by definition the file is actually unplayable, so there's nothing
to "Ignore" or decide about."""
import os
from datetime import datetime, timezone

from . import arr_client
from . import db

MAX_AUTO_REPLACE_PER_DAY = 10


def _auto_replace_count_today():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        1 for i in db.list_issues(status="auto_fixed")
        if i["category"] == "broken" and i["detected_at"].startswith(today)
    )


def auto_replace_broken(source, ext_id, label, reason, *, episode_id=None,
                         file_id=None, download_id=None, path=None):
    """Returns (issue_id, acted). `acted` is False when this exact file was
    already handled (successfully, or given up on past the daily cap) by a
    previous run - the daily fallback poll re-scans a ~26h window, so without
    this guard it would re-attempt deleting an already-deleted file every day
    it stays in that window. A failed-delete/still-on-disk result stays
    'open', so those ARE retried on the next run - only a genuinely finished
    outcome is skipped."""
    existing = db.find_issue(source, file_id, "broken")
    if existing and existing["status"] != "open":
        return existing["id"], False

    if _auto_replace_count_today() >= MAX_AUTO_REPLACE_PER_DAY:
        issue_id = db.record_issue(
            source, ext_id, label, "broken",
            f"{reason} - NOT auto-replaced (more than {MAX_AUTO_REPLACE_PER_DAY} "
            f"today, check manually)",
            episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        )
        return issue_id, True

    try:
        arr_client.delete_file(source, file_id)
    except Exception as e:
        issue_id = db.record_issue(
            source, ext_id, label, "broken",
            f"{reason} - could not delete it through {source.capitalize()}: {e}",
            episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        )
        return issue_id, True

    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            issue_id = db.record_issue(
                source, ext_id, label, "broken",
                f"{reason} - deleted in {source.capitalize()} but still on disk: {e}",
                episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
            )
            return issue_id, True

    try:
        blocklisted = arr_client.blocklist_release(source, download_id)
    except Exception:
        blocklisted = False
    how = "release blocklisted" if blocklisted else "release not blocklisted (no grab record)"

    try:
        arr_client.trigger_search(source, ext_id, episode_id)
        how += ", new search started"
    except Exception as e:
        how += f", new search failed to start: {e}"

    issue_id = db.record_issue(
        source, ext_id, label, "broken", f"{reason} - deleted, {how}",
        episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        status="auto_fixed",
    )
    return issue_id, True
