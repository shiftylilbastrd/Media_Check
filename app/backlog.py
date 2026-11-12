"""Backlog scanner: works through the library Radarr/Sonarr already has
imported - most of which predates this app and was never checked by the
webhook or daily-poll paths (those only see *new* imports/recent history) -
at a throttled rate so it doesn't hammer the disk or ffmpeg all at once.

A file is only ever picked once: `scanned` records every file_id this app
has looked at, success or failure, so each run picks up where the last one
left off until the whole library has been covered - then it just idles
(nothing left unscanned) until new library items age into Radarr/Sonarr's
own "has file" state outside the normal import path (e.g. a manual import).

Batch size and interval are both configurable from Settings - "per day" was
the starting idea, but there's no reason to hardcode the period."""
import threading
import time
import traceback

import requests

from . import actions
from . import arr_client
from . import db
from . import detect
from . import notify

DEFAULT_BATCH_SIZE = 20
DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1  # safety floor - a 0 or negative setting would busy-loop


def batch_size():
    try:
        return max(1, int(db.get_setting("backlog_scan_batch_size", DEFAULT_BATCH_SIZE)))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def interval_hours():
    try:
        hours = float(db.get_setting("backlog_scan_interval_hours", DEFAULT_INTERVAL_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_INTERVAL_HOURS
    return max(MIN_INTERVAL_HOURS, hours)


def interval_seconds():
    return interval_hours() * 3600


def radarr_total():
    """Cheap total: one /api/v3/movie call, just counts hasFile - no per-file
    lookups. Used by the Backlog UI page for a scanned/total progress figure."""
    movies = arr_client.request_json("radarr", "GET", "/api/v3/movie") or []
    return sum(1 for m in movies if m.get("hasFile"))


def sonarr_total():
    """Cheap total: one /api/v3/series call, summed episodeFileCount from each
    series' own statistics - no per-series episode walk needed just to count."""
    series_list = arr_client.request_json("sonarr", "GET", "/api/v3/series") or []
    return sum((s.get("statistics") or {}).get("episodeFileCount", 0) for s in series_list)


def _check_one(source, *, ext_id, label, file_id, path, media_info, size,
                listed_runtime, expect_languages, episode_id=None, download_id=None):
    """Same dispatch order as webhooks.py/scheduler.py: broken-file check
    first (fully automatic, no button), otherwise the softer media-info
    checks (Ignore/Fix-now UI). Always marks the file scanned afterward,
    whatever the outcome, so it's never picked again.

    deep=True: this is specifically what the backlog scanner is for - a real
    ffmpeg decode test against the library that predates this app, not just
    the cheap zero-fill/format check. Runs on its own background thread, so
    the per-file cost (seconds of real decode time) doesn't block anything."""
    bad = detect.broken_reason(path, deep=True) if path else None
    if bad:
        issue_id, acted = actions.auto_replace_broken(
            source, ext_id, label, bad,
            episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
        )
        if acted:
            notify.send(f"media-check-tools (backlog): {label}", db.get_issue(issue_id)["reason"])
        result = f"broken: {bad}"
    else:
        problems = detect.check_media_info(media_info, size, listed_runtime, expect_languages)
        new_problems = [
            (category, reason) for category, reason in problems
            if db.find_issue(source, file_id, category) is None
        ]
        for category, reason in problems:
            db.record_issue(
                source, ext_id, label, category, reason,
                episode_id=episode_id, file_id=file_id, download_id=download_id, path=path,
            )
        if new_problems:
            body = "\n".join(f"- {reason}" for _, reason in new_problems)
            notify.send(f"media-check-tools (backlog): {label}", body)
        result = "; ".join(reason for _, reason in problems) if problems else None
    db.mark_scanned(source, file_id, label=label, result=result)


def _radarr_candidates(already_scanned, limit):
    movies = arr_client.request_json("radarr", "GET", "/api/v3/movie") or []
    out = []
    for movie in movies:
        mf = movie.get("movieFile")
        if not mf or not movie.get("hasFile"):
            continue
        file_id = mf.get("id")
        if not file_id or file_id in already_scanned:
            continue
        out.append((movie, mf))
        if len(out) >= limit:
            break
    return out


def _sonarr_candidates(already_scanned, limit):
    """Sonarr's /series doesn't carry per-episode files, so this costs one
    /episode call per series - acceptable for a background, bounded-rate
    scan. Stops as soon as `limit` unscanned files are found."""
    series_list = arr_client.request_json("sonarr", "GET", "/api/v3/series") or []
    out = []
    for series in series_list:
        if len(out) >= limit:
            break
        if not (series.get("statistics") or {}).get("episodeFileCount"):
            continue
        episodes = arr_client.request_json(
            "sonarr", "GET", f"/api/v3/episode?seriesId={series['id']}"
        ) or []
        for ep in episodes:
            if len(out) >= limit:
                break
            file_id = ep.get("episodeFileId")
            if not file_id or not ep.get("hasFile") or file_id in already_scanned:
                continue
            out.append((series, ep, file_id))
    return out


def _scan_radarr(limit):
    scanned = db.get_scanned_ids("radarr")
    candidates = _radarr_candidates(scanned, limit)
    for movie, mf in candidates:
        label = f"Movie: {movie.get('title')} ({movie.get('year')})"
        try:
            _check_one(
                "radarr", ext_id=movie.get("id"), label=label,
                file_id=mf.get("id"), path=mf.get("path"),
                media_info=mf.get("mediaInfo") or {}, size=mf.get("size", 0),
                listed_runtime=movie.get("runtime", 0), expect_languages=["eng"],
            )
        except Exception:
            # one bad file should never stop the rest of this pass
            print(f"[backlog] radarr file {mf.get('id')} check failed:\n"
                  f"{traceback.format_exc()}", flush=True)
    return len(candidates)


def _scan_sonarr(limit):
    scanned = db.get_scanned_ids("sonarr")
    candidates = _sonarr_candidates(scanned, limit)
    for series, ep, file_id in candidates:
        label = (
            f"TV: {series.get('title')} "
            f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        )
        try:
            ef = arr_client.request_json("sonarr", "GET", f"/api/v3/episodefile/{file_id}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # file got deleted/replaced between listing episodes and looking
                # this one up - nothing left to check, and no point retrying it
                db.mark_scanned("sonarr", file_id, label=label, result="skipped: file no longer exists")
                continue
            print(f"[backlog] sonarr file {file_id} lookup failed:\n"
                  f"{traceback.format_exc()}", flush=True)
            continue
        except Exception:
            # one bad lookup should never stop the rest of this pass
            print(f"[backlog] sonarr file {file_id} lookup failed:\n"
                  f"{traceback.format_exc()}", flush=True)
            continue
        if not ef:
            # file record gone - nothing to check, don't retry
            db.mark_scanned("sonarr", file_id, label=label, result="skipped: file record gone")
            continue
        is_anime = series.get("seriesType") == "anime"
        try:
            _check_one(
                "sonarr", ext_id=series.get("id"), label=label,
                episode_id=ep.get("id"), file_id=file_id, path=ef.get("path"),
                media_info=ef.get("mediaInfo") or {}, size=ef.get("size", 0),
                listed_runtime=series.get("runtime", 0),
                expect_languages=["eng", "jpn"] if is_anime else ["eng"],
            )
        except Exception:
            print(f"[backlog] sonarr file {file_id} check failed:\n"
                  f"{traceback.format_exc()}", flush=True)
    return len(candidates)


def run_backlog_scan_once():
    """One throttled pass. Radarr gets the full budget first, Sonarr gets
    whatever's left - simple and predictable, not an attempt at perfect
    fairness between the two libraries."""
    budget = batch_size()
    done = 0
    try:
        done += _scan_radarr(budget)
    except RuntimeError:
        pass  # Radarr not configured yet
    except Exception:
        print(f"[backlog] radarr scan failed:\n{traceback.format_exc()}", flush=True)
    remaining = max(0, budget - done)
    if remaining:
        try:
            done += _scan_sonarr(remaining)
        except RuntimeError:
            pass  # Sonarr not configured yet
        except Exception:
            print(f"[backlog] sonarr scan failed:\n{traceback.format_exc()}", flush=True)
    if done:
        print(f"[backlog] checked {done} previously-unscanned file(s) this pass", flush=True)
    return done


def start_backlog_scanner():
    def loop():
        while True:
            try:
                run_backlog_scan_once()
            except Exception:
                print(f"[backlog] pass failed:\n{traceback.format_exc()}", flush=True)
            time.sleep(interval_seconds())

    threading.Thread(target=loop, daemon=True).start()
