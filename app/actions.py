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

# A brake on how many files get auto-deleted+re-searched in one day, not a
# throughput limit on scanning - the backlog scanner checks far more files
# than this daily, but the overwhelming majority of a normal library is
# fine, so under normal conditions this should rarely if ever be reached.
# It exists so that a systemic false-positive (a bad ffmpeg build, a flaky
# mount, a bug in detect.py) can't bulk-delete a big chunk of the library
# and fire off dozens of re-searches before anyone notices - hitting it is
# meant to be a signal ("check manually"), not a routine ceiling. Override
# via the MAX_AUTO_REPLACE_PER_DAY env var if a one-time backlog sweep is
# legitimately turning up more than this many genuinely broken files.
MAX_AUTO_REPLACE_PER_DAY = int(os.environ.get("MAX_AUTO_REPLACE_PER_DAY", "10"))


def _auto_replace_count_today():
    """Counts today's (UTC) successful auto-replaces, same as the cap check
    uses. A manual reset (reset_auto_replace_counter()) doesn't delete or
    touch any issue row - it just records when the window should start
    counting from instead, so today's full history stays visible via the
    Log's "Auto-fixed" filter or Issues' ?status=auto_fixed, while the cap
    itself re-opens from that point forward."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    reset_at = db.get_setting("auto_replace_cap_reset_at")
    count = 0
    for i in db.list_issues(status="auto_fixed"):
        if i["category"] != "broken":
            continue
        if not i["detected_at"].startswith(today):
            continue
        if reset_at and i["detected_at"] < reset_at:
            continue
        count += 1
    return count


def auto_replace_status():
    """Today's auto-replace count vs. the cap, for display in Settings."""
    return {"count": _auto_replace_count_today(), "cap": MAX_AUTO_REPLACE_PER_DAY}


def reset_auto_replace_counter():
    """Manually re-opens today's auto-replace cap without waiting for UTC
    midnight and without touching any issue row - see _auto_replace_count_today()."""
    db.set_setting("auto_replace_cap_reset_at", db.now())


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
