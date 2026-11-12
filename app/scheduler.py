"""Reconciliation sweep - a safety net, not the primary trigger. The webhook
receivers in webhooks.py handle normal operation (near-instant: fast checks
inline, then queued for worker.py's real decode test); this just re-checks
the last ~26h of import history every few hours in case a webhook delivery
was ever missed entirely (arr restart mid-notification, network blip, this
app being down at that moment) - anything already scanned or already queued
is left alone, this only enqueues what's missing."""
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

from . import arr_client
from . import db

RECONCILE_INTERVAL_SECONDS = 6 * 3600
LOOKBACK_HOURS = 26  # same overlap the old hourly script used


def _reconcile_radarr():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    history = arr_client.request_json(
        "radarr", "GET",
        f"/api/v3/history/since?date={since}&eventType=downloadFolderImported",
    ) or []
    scanned = db.get_scanned_ids("radarr")
    seen = set()
    found = 0
    for rec in history:
        try:
            file_id = (rec.get("data") or {}).get("fileId")
            if not file_id or file_id in seen or file_id in scanned:
                continue
            seen.add(file_id)
            mf = arr_client.request_json("radarr", "GET", f"/api/v3/moviefile/{file_id}")
            if not mf:
                continue
            movie = arr_client.request_json("radarr", "GET", f"/api/v3/movie/{rec['movieId']}")
            if not movie:
                continue
            label = f"{movie.get('title')} ({movie.get('year')})"
            db.enqueue(
                "radarr", file_id, label,
                ext_id=movie.get("id"), download_id=rec.get("downloadId"), path=mf.get("path"),
            )
            found += 1
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue  # this one file/movie no longer exists - nothing left to check
            print(f"[reconcile] radarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        except Exception:
            print(f"[reconcile] radarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
    return found


def _reconcile_sonarr():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    history = arr_client.request_json(
        "sonarr", "GET",
        f"/api/v3/history/since?date={since}&eventType=downloadFolderImported",
    ) or []
    scanned = db.get_scanned_ids("sonarr")
    seen = set()
    found = 0
    for rec in history:
        try:
            file_id = (rec.get("data") or {}).get("fileId")
            if not file_id or file_id in seen or file_id in scanned:
                continue
            seen.add(file_id)
            ef = arr_client.request_json("sonarr", "GET", f"/api/v3/episodefile/{file_id}")
            if not ef:
                continue
            ep = arr_client.request_json("sonarr", "GET", f"/api/v3/episode/{rec['episodeId']}")
            series = arr_client.request_json("sonarr", "GET", f"/api/v3/series/{rec['seriesId']}")
            if not ep or not series:
                continue
            label = (
                f"{series.get('title')} "
                f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
            )
            db.enqueue(
                "sonarr", file_id, label,
                ext_id=series.get("id"), episode_id=rec.get("episodeId"),
                download_id=rec.get("downloadId"), path=ef.get("path"),
            )
            found += 1
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                continue  # this one episode/series no longer exists - nothing left to check
            print(f"[reconcile] sonarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
        except Exception:
            print(f"[reconcile] sonarr history record {rec.get('id')} failed:\n"
                  f"{traceback.format_exc()}", flush=True)
    return found


def reconcile_once():
    total = 0
    for name, fn in (("radarr", _reconcile_radarr), ("sonarr", _reconcile_sonarr)):
        try:
            total += fn()
        except RuntimeError:
            pass  # not configured yet - nothing to reconcile until Settings is filled in
        except Exception:
            print(f"[reconcile] {name} sweep failed:\n{traceback.format_exc()}", flush=True)
    if total:
        print(f"[reconcile] queued {total} import(s) missed by the webhook path", flush=True)
    return total


def start_scheduler():
    def loop():
        while True:
            try:
                reconcile_once()
            except Exception:
                print(f"[reconcile] pass failed:\n{traceback.format_exc()}", flush=True)
            time.sleep(RECONCILE_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True).start()
