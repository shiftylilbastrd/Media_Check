"""Radarr/Sonarr Connect -> Webhook receivers. Point each app's Webhook
connection (triggered on "On Import" and "On Upgrade") at:
    http://<athena>:8585/webhooks/radarr
    http://<athena>:8585/webhooks/sonarr

Every payload is logged raw to WEBHOOK_DEBUG_LOG first, before any parsing
is attempted - so if the field names here turn out to be wrong for your
Radarr/Sonarr version, we can read the real payload back out and fix the
parsing, instead of guessing twice."""
import json
import os
import traceback
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify

from . import actions
from . import db
from . import detect
from . import notify

bp = Blueprint("webhooks", __name__)

DEBUG_LOG = os.environ.get("WEBHOOK_DEBUG_LOG", "/data/webhook-raw.log")


def _log_raw(source, payload):
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"\n--- {datetime.now(timezone.utc).isoformat()} {source} ---\n")
            f.write(json.dumps(payload, indent=2, default=str))
            f.write("\n")
    except Exception:
        pass  # logging must never be why a webhook fails


@bp.route("/webhooks/radarr", methods=["POST"])
def radarr_webhook():
    payload = request.get_json(force=True, silent=True) or {}
    _log_raw("radarr", payload)
    if payload.get("eventType") != "Download":
        return jsonify(ok=True, skipped=payload.get("eventType")), 200
    try:
        movie = payload.get("movie", {})
        mf = payload.get("movieFile", {})
        media_info = mf.get("mediaInfo") or {}
        label = f"Movie: {movie.get('title')} ({movie.get('year')})"
        host_path = mf.get("path") or movie.get("folderPath")

        bad = detect.broken_reason(host_path) if host_path else None
        if bad:
            issue_id, acted = actions.auto_replace_broken(
                "radarr", movie.get("id"), label, bad,
                file_id=mf.get("id"), download_id=payload.get("downloadId"),
                path=host_path,
            )
            if acted:
                notify.send(f"media-check-tools: {label}", db.get_issue(issue_id)["reason"])
        else:
            problems = detect.check_media_info(
                media_info, mf.get("size", 0), movie.get("runtime", 0),
                expect_languages=["eng"],
            )
            new_problems = [
                (category, reason) for category, reason in problems
                if db.find_issue("radarr", mf.get("id"), category) is None
            ]
            for category, reason in problems:
                db.record_issue(
                    "radarr", movie.get("id"), label, category, reason,
                    file_id=mf.get("id"), download_id=payload.get("downloadId"),
                    path=host_path,
                )
            if new_problems:
                body = "\n".join(f"- {reason}" for _, reason in new_problems)
                notify.send(f"media-check-tools: {label}", body)
            # Passed the fast shallow check - queue it for the real ffmpeg
            # decode test, which is too slow to run inline on this live HTTP
            # response. The worker thread picks new queue items up ahead of
            # backlog work, so this gets decode-tested soon, not whenever the
            # next poll happens to run.
            if host_path:
                db.enqueue(
                    "radarr", mf.get("id"), label,
                    ext_id=movie.get("id"), download_id=payload.get("downloadId"),
                    path=host_path,
                )
    except Exception:
        _log_raw("radarr-parse-error", {"traceback": traceback.format_exc()})
    return jsonify(ok=True), 200


@bp.route("/webhooks/sonarr", methods=["POST"])
def sonarr_webhook():
    payload = request.get_json(force=True, silent=True) or {}
    _log_raw("sonarr", payload)
    if payload.get("eventType") != "Download":
        return jsonify(ok=True, skipped=payload.get("eventType")), 200
    try:
        series = payload.get("series", {})
        episodes = payload.get("episodes", [])
        ef = payload.get("episodeFile", {})
        media_info = ef.get("mediaInfo") or {}
        ep = episodes[0] if episodes else {}
        is_anime = series.get("seriesType") == "anime"
        label = (
            f"TV: {series.get('title')} "
            f"S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}"
        )
        host_path = ef.get("path")

        bad = detect.broken_reason(host_path) if host_path else None
        if bad:
            issue_id, acted = actions.auto_replace_broken(
                "sonarr", series.get("id"), label, bad,
                episode_id=ep.get("id"), file_id=ef.get("id"),
                download_id=payload.get("downloadId"), path=host_path,
            )
            if acted:
                notify.send(f"media-check-tools: {label}", db.get_issue(issue_id)["reason"])
        else:
            problems = detect.check_media_info(
                media_info, ef.get("size", 0), series.get("runtime", 0),
                expect_languages=["eng", "jpn"] if is_anime else ["eng"],
            )
            new_problems = [
                (category, reason) for category, reason in problems
                if db.find_issue("sonarr", ef.get("id"), category) is None
            ]
            for category, reason in problems:
                db.record_issue(
                    "sonarr", series.get("id"), label, category, reason,
                    episode_id=ep.get("id"), file_id=ef.get("id"),
                    download_id=payload.get("downloadId"), path=host_path,
                )
            if new_problems:
                body = "\n".join(f"- {reason}" for _, reason in new_problems)
                notify.send(f"media-check-tools: {label}", body)
            # See radarr_webhook() - queue for the real decode test instead
            # of running it inline on this live HTTP response.
            if host_path:
                db.enqueue(
                    "sonarr", ef.get("id"), label,
                    ext_id=series.get("id"), episode_id=ep.get("id"),
                    download_id=payload.get("downloadId"), path=host_path,
                )
    except Exception:
        _log_raw("sonarr-parse-error", {"traceback": traceback.format_exc()})
    return jsonify(ok=True), 200
