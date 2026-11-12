"""Daily fallback poll - a safety net, not the primary trigger. The webhook
receivers in webhooks.py handle normal operation (near-instant, on import);
this just re-checks the last ~26h of import history once a day in case a
webhook delivery was missed (arr restart mid-notification, network blip, this
app being down at that moment)."""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

from . import actions
from . import arr_client
from . import db
from . import detect
from . import notify

POLL_INTERVAL_SECONDS = 24 * 3600
LOOKBACK_HOURS = 26  # same overlap the old hourly script used


def _poll_radarr():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    history = arr_client.request_json(
        "radarr", "GET",
        f"/api/v3/history/since?date={since}&eventType=downloadFolderImported",
    ) or []
    seen = set()
    for rec in history:
        try:
            file_id = (rec.get("data") or {}).get("fileId")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            mf = arr_client.request_json("radarr", "GET", f"/api/v3/moviefile/{file_id}")
            if not mf:
                continue
            movie = arr_client.request_json("radarr", "GET", f"/api/v3/movie/{rec['movieId']}")
            if not movie:
                continue
            label = f"Movie: {movie.get('title')} ({movie.get('year')})"
            host_path = mf.get("path")
            bad = detect.broken_reason(host_path, deep=True) if host_path else None
            if bad:
                issue_id, acted = actions.auto_replace_broken(
                    "radarr", movie.get("id"), label, bad,
                    file_id=file_id, download_id=rec.get("downloadId"), path=host_path,
                )
                if acted:
                    notify.send(f"media-check-tools: {label}", db.get_issue(issue_id)["reason"])
                continue
            problems = detect.check_media_info(
                mf.get("mediaInfo") or {}, mf.get("size", 0), movie.get("runtime", 0),
                expect_languages=["eng"],
            )
            new_problems = [
                (category, reason) for category, reason in problems
                if db.find_issue("radarr", file_id, category) is None
            ]
            for category, reason in problems:
                db.record_issue(
                    "radarr", movie.get("id"), label, category, reason,
                    file_id=file_id, download_id=rec.get("downloadId"), path=host_path,
                )
            if new_problems:
                body = "\n".join(f"- {reason}" for _, reason in new_problems)
                notify.send(f"media-check-tools: {label}", body)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue  # this one file/movie no longer exists - nothing left to check
            print(f"[scheduler] radarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        except Exception:
            # one bad record should never stop the rest of this poll from running
            print(f"[scheduler] radarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)


def _poll_sonarr():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    history = arr_client.request_json(
        "sonarr", "GET",
        f"/api/v3/history/since?date={since}&eventType=downloadFolderImported",
    ) or []
    seen = set()
    for rec in history:
        try:
            file_id = (rec.get("data") or {}).get("fileId")
            if not file_id or file_id in seen:
                continue
            seen.add(file_id)
            ef = arr_client.request_json("sonarr", "GET", f"/api/v3/episodefile/{file_id}")
            if not ef:
                continue
            ep = arr_client.request_json("sonarr", "GET", f"/api/v3/episode/{rec['episodeId']}")
            series = arr_client.request_json("sonarr", "GET", f"/api/v3/series/{rec['seriesId']}")
            if not ep or not series:
                continue
            is_anime = series.get("seriesType") == "anime"
            label = (
                f"TV: {series.get('title')} "
                f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
            )
            host_path = ef.get("path")
            bad = detect.broken_reason(host_path, deep=True) if host_path else None
            if bad:
                issue_id, acted = actions.auto_replace_broken(
                    "sonarr", series.get("id"), label, bad,
                    episode_id=rec.get("episodeId"), file_id=file_id,
                    download_id=rec.get("downloadId"), path=host_path,
                )
                if acted:
                    notify.send(f"media-check-tools: {label}", db.get_issue(issue_id)["reason"])
                continue
            problems = detect.check_media_info(
                ef.get("mediaInfo") or {}, ef.get("size", 0), series.get("runtime", 0),
                expect_languages=["eng", "jpn"] if is_anime else ["eng"],
            )
            new_problems = [
                (category, reason) for category, reason in problems
                if db.find_issue("sonarr", file_id, category) is None
            ]
            for category, reason in problems:
                db.record_issue(
                    "sonarr", series.get("id"), label, category, reason,
                    episode_id=rec.get("episodeId"), file_id=file_id,
                    download_id=rec.get("downloadId"), path=host_path,
                )
            if new_problems:
                body = "\n".join(f"- {reason}" for _, reason in new_problems)
                notify.send(f"media-check-tools: {label}", body)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue  # this one episode/series no longer exists - nothing left to check
            print(f"[scheduler] sonarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        except Exception:
            print(f"[scheduler] sonarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)


def _poll_once():
    for name, fn in (("radarr", _poll_radarr), ("sonarr", _poll_sonarr)):
        try:
            fn()
        except RuntimeError:
            pass  # not configured yet - nothing to poll until Settings is filled in
        except Exception:
            print(f"[scheduler] {name} poll failed:\n{traceback.format_exc()}", flush=True)


def start_scheduler():
    def loop():
        while True:
            _poll_once()
            time.sleep(POLL_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True).start()
