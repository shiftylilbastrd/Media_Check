"""Backlog candidate discovery + the "check one old library item" routine,
used by worker.py to fill idle time between queued (newly-imported) files
with items Radarr/Sonarr already had before this app existed - most of which
were never checked by the webhook or reconciliation paths (those only see
*new*/recent imports).

There's no batch/interval throttle anymore - worker.py just keeps asking for
the next backlog candidate whenever the new-item queue is empty, and relies
on nice/ionice (see detect.py's decode_test()) to keep that from starving
other services, rather than a manual schedule.

A file is only ever picked once: `scanned` records every file_id this app
has looked at, success or failure, so the worker always picks up where the
last one left off until the whole library has been covered - then there's
simply nothing left to return until new items age into Radarr/Sonarr's own
"has file" state outside the normal import path (e.g. a manual import)."""
import traceback

import requests

from . import actions
from . import arr_client
from . import db
from . import detect
from . import notify


def radarr_total():
    """Cheap total: one /api/v3/movie call, just counts hasFile - no per-file
    lookups. Used by the Queue/Completion-log UI for a scanned/total figure."""
    movies = arr_client.request_json("radarr", "GET", "/api/v3/movie") or []
    return sum(1 for m in movies if m.get("hasFile"))


def sonarr_total():
    """Cheap total: one /api/v3/series call, summed episodeFileCount from each
    series' own statistics - no per-series episode walk needed just to count."""
    series_list = arr_client.request_json("sonarr", "GET", "/api/v3/series") or []
    return sum((s.get("statistics") or {}).get("episodeFileCount", 0) for s in series_list)


def refresh_totals_cache():
    """Refreshes the cached scanned/total figures the Log page reads, so that
    page never makes a live Radarr/Sonarr call itself on page load. With a
    large library (thousands of movies/episodes), those live listing calls
    were adding real, noticeable latency to every page view - called
    periodically by worker.py instead, well off the request path."""
    try:
        db.set_setting("cache_radarr_total", str(radarr_total()))
    except RuntimeError:
        pass  # Radarr not configured yet
    except Exception:
        print(f"[backlog] radarr total refresh failed:\n{traceback.format_exc()}", flush=True)
    try:
        db.set_setting("cache_sonarr_total", str(sonarr_total()))
    except RuntimeError:
        pass  # Sonarr not configured yet
    except Exception:
        print(f"[backlog] sonarr total refresh failed:\n{traceback.format_exc()}", flush=True)
    db.set_setting("cache_totals_at", db.now())


def check_one(source, *, ext_id, label, file_id, path, media_info, size,
              listed_runtime, expect_languages, episode_id=None, download_id=None):
    """Same dispatch order as webhooks.py: broken-file check first (fully
    automatic, no button), otherwise the softer media-info checks
    (Ignore/Fix-now UI). Always marks the file scanned afterward, whatever
    the outcome, so it's never picked again.

    deep=True: a real ffmpeg decode test, not just the cheap zero-fill/format
    check - this file predates the app, so (unlike a queued new import) it's
    never been decode-tested before now."""
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


def next_radarr_candidates(limit):
    """Up to `limit` unscanned Radarr movies, as (label, kwargs-for-check_one)
    pairs - worker.py prefetches a small batch at a time rather than calling
    this once per file, so it isn't re-listing Radarr's whole library on
    every single backlog item."""
    scanned = db.get_scanned_ids("radarr")
    out = []
    for movie, mf in _radarr_candidates(scanned, limit):
        label = f"Movie: {movie.get('title')} ({movie.get('year')})"
        out.append({
            "source": "radarr", "ext_id": movie.get("id"), "label": label,
            "file_id": mf.get("id"), "path": mf.get("path"),
            "media_info": mf.get("mediaInfo") or {}, "size": mf.get("size", 0),
            "listed_runtime": movie.get("runtime", 0), "expect_languages": ["eng"],
        })
    return out


def next_sonarr_candidates(limit):
    """Up to `limit` unscanned Sonarr episodes. Unlike Radarr, each candidate
    needs one more lookup (the episodefile itself) to get path/mediaInfo -
    done here, up front, so a 404/gone file is weeded out (and marked
    scanned, so it's never retried) before worker.py ever sees it."""
    scanned = db.get_scanned_ids("sonarr")
    out = []
    for series, ep, file_id in _sonarr_candidates(scanned, limit):
        label = (
            f"TV: {series.get('title')} "
            f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        )
        try:
            ef = arr_client.request_json("sonarr", "GET", f"/api/v3/episodefile/{file_id}")
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                db.mark_scanned("sonarr", file_id, label=label,
                                 result="skipped: file no longer exists")
                continue
            print(f"[backlog] sonarr file {file_id} lookup failed:\n"
                  f"{traceback.format_exc()}", flush=True)
            continue
        except Exception:
            print(f"[backlog] sonarr file {file_id} lookup failed:\n"
                  f"{traceback.format_exc()}", flush=True)
            continue
        if not ef:
            db.mark_scanned("sonarr", file_id, label=label, result="skipped: file record gone")
            continue
        is_anime = series.get("seriesType") == "anime"
        out.append({
            "source": "sonarr", "ext_id": series.get("id"), "label": label,
            "episode_id": ep.get("id"), "file_id": file_id, "path": ef.get("path"),
            "media_info": ef.get("mediaInfo") or {}, "size": ef.get("size", 0),
            "listed_runtime": series.get("runtime", 0),
            "expect_languages": ["eng", "jpn"] if is_anime else ["eng"],
        })
    return out
