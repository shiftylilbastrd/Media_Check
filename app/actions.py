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
    """Always succeeds in the sense that it records *something* - a failure
    partway through becomes an open issue explaining what to check manually,
    same as the old script's fallback messages."""
    if _auto_replace_count_today() >= MAX_AUTO_REPLACE_PER_DAY:
        return db.record_issue(
            source, ext_id, label, "broken",
            f"{reason} - NOT auto-replaced (more than {MAX_AUTO_REPLACE_PER_DAY} "
            f"today, check manually)",
            episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        )

    try:
        arr_client.delete_file(source, file_id)
    except Exception as e:
        return db.record_issue(
            source, ext_id, label, "broken",
            f"{reason} - could not delete it through {source.capitalize()}: {e}",
            episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        )

    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            return db.record_issue(
                source, ext_id, label, "broken",
                f"{reason} - deleted in {source.capitalize()} but still on disk: {e}",
                episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
            )

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

    return db.record_issue(
        source, ext_id, label, "broken", f"{reason} - deleted, {how}",
        episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        status="auto_fixed",
    )
