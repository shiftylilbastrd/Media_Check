"""The one background worker: drains the deep-check queue (newly-imported
files, oldest first) and, whenever that's empty, fills the idle time by
working through the pre-existing library (backlog candidates) instead -
replacing what used to be three separate, uncoordinated mechanisms (a live
webhook doing only a shallow check, a once-a-day poll, and a throttled
backlog scanner on its own schedule).

Why one worker instead of three paths: a freshly-imported file used to only
get the real ffmpeg decode test up to ~26h later (the next daily poll) or
never, if the daily poll's own "already handled" guard reused a stale
result. Queueing it straight from the webhook and draining that queue first,
continuously, means a new import gets decode-tested as soon as this worker
reaches it - no schedule, no next-poll wait. There's no batch-size/interval
throttle controlling any of this anymore; nice/ionice (see
detect.py's decode_test()) is what keeps this from starving other services
on the box, not a manual schedule - see README for the reasoning."""
import threading
import time
import traceback

from . import actions
from . import backlog
from . import db
from . import detect
from . import notify

IDLE_SLEEP_SECONDS = 60  # nothing queued, nothing left in the backlog either - check back in a bit
BACKLOG_PREFETCH = 10  # small batches, so Radarr/Sonarr's full library isn't re-listed every file
TOTALS_REFRESH_SECONDS = 1800  # how often the Log page's scanned/total figures get refreshed

_last_totals_refresh = 0

# In-memory only - refilled from Radarr/Sonarr as needed. Losing these on a
# restart is harmless: they're just "next up" candidates, re-derived from
# whatever's still unscanned.
_radarr_buffer = []
_sonarr_buffer = []


def _process_queue_item(item):
    """A queued item already got its fast/shallow check + soft-issue check
    done synchronously in webhooks.py - this is purely the real decode test
    (full broken_reason(deep=True), which also re-verifies the cheap zero-
    fill/format checks in case anything changed since the webhook fired)."""
    bad = detect.broken_reason(item["path"], deep=True) if item["path"] else None
    if bad:
        issue_id, acted = actions.auto_replace_broken(
            item["source"], item["ext_id"], item["label"], bad,
            episode_id=item["episode_id"], file_id=item["file_id"],
            download_id=item["download_id"], path=item["path"],
        )
        if acted:
            notify.send(f"media-check: {item['label']}", db.get_issue(issue_id)["reason"])
        result = f"broken: {bad}"
    else:
        result = None
    db.mark_scanned(item["source"], item["file_id"], label=item["label"], result=result)


def _refill_radarr():
    global _radarr_buffer
    if _radarr_buffer:
        return
    try:
        _radarr_buffer = backlog.next_radarr_candidates(BACKLOG_PREFETCH)
    except RuntimeError:
        pass  # Radarr not configured yet
    except Exception:
        print(f"[worker] radarr candidate lookup failed:\n{traceback.format_exc()}", flush=True)


def _refill_sonarr():
    global _sonarr_buffer
    if _sonarr_buffer:
        return
    try:
        _sonarr_buffer = backlog.next_sonarr_candidates(BACKLOG_PREFETCH)
    except RuntimeError:
        pass  # Sonarr not configured yet
    except Exception:
        print(f"[worker] sonarr candidate lookup failed:\n{traceback.format_exc()}", flush=True)


def run_once():
    """One unit of work: a queued new import if there is one, else one
    backlog item (Radarr first, then Sonarr), else nothing. Returns True if
    something was actually processed, so the caller knows whether to loop
    again immediately or back off."""
    item = db.pop_queue_item()
    if item:
        try:
            _process_queue_item(item)
        except Exception:
            print(f"[worker] queue item {item['source']}/{item['file_id']} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        return True

    _refill_radarr()
    if _radarr_buffer:
        candidate = _radarr_buffer.pop(0)
        try:
            backlog.check_one(**candidate)
        except Exception:
            print(f"[worker] radarr backlog item {candidate.get('file_id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        return True

    _refill_sonarr()
    if _sonarr_buffer:
        candidate = _sonarr_buffer.pop(0)
        try:
            backlog.check_one(**candidate)
        except Exception:
            print(f"[worker] sonarr backlog item {candidate.get('file_id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        return True

    return False


def _maybe_refresh_totals():
    """Refreshes the Log page's cached scanned/total figures on a timer,
    well off the request path - see backlog.refresh_totals_cache(). Runs
    once immediately on startup too, so a fresh deploy isn't stuck showing
    nothing for 30 minutes."""
    global _last_totals_refresh
    if time.time() - _last_totals_refresh < TOTALS_REFRESH_SECONDS:
        return
    try:
        backlog.refresh_totals_cache()
    except Exception:
        print(f"[worker] totals refresh failed:\n{traceback.format_exc()}", flush=True)
    _last_totals_refresh = time.time()


def start_worker():
    def loop():
        while True:
            _maybe_refresh_totals()
            try:
                did_work = run_once()
            except Exception:
                print(f"[worker] pass failed:\n{traceback.format_exc()}", flush=True)
                did_work = False
            if not did_work:
                time.sleep(IDLE_SLEEP_SECONDS)

    threading.Thread(target=loop, daemon=True).start()
