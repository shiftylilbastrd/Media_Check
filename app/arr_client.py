"""Thin Radarr/Sonarr API client. Same calls the old bash script made
(DELETE moviefile/episodefile, POST history/failed to blocklist, POST
command to search/rescan) - just centralized here, reading config from the
app's own Settings page instead of an arr's config.xml."""
import requests

from . import db

TIMEOUT = 30


def _base(app_name):
    url = db.get_setting(f"{app_name}_url")
    key = db.get_setting(f"{app_name}_api_key")
    if not url or not key:
        raise RuntimeError(
            f"{app_name.capitalize()} isn't configured yet - add its URL and "
            f"API key on the Settings page first."
        )
    return url.rstrip("/"), key


def request_json(app_name, method, path, **kwargs):
    url, key = _base(app_name)
    headers = kwargs.pop("headers", {})
    headers["X-Api-Key"] = key
    resp = requests.request(method, url + path, headers=headers, timeout=TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else None


def trigger_search(source, ext_id, episode_id=None):
    """The 'Fix now' action, and the final step of the automatic broken-file
    replace: ask Radarr/Sonarr to search again. Does NOT delete or blocklist
    anything by itself - see delete_file()/blocklist_release() for that."""
    if source == "radarr":
        return request_json(
            "radarr", "POST", "/api/v3/command",
            json={"name": "MoviesSearch", "movieIds": [ext_id]},
        )
    episode_id = episode_id or ext_id
    return request_json(
        "sonarr", "POST", "/api/v3/command",
        json={"name": "EpisodeSearch", "episodeIds": [episode_id]},
    )


def delete_file(source, file_id):
    endpoint = "moviefile" if source == "radarr" else "episodefile"
    return request_json(source, "DELETE", f"/api/v3/{endpoint}/{file_id}")


def blocklist_release(source, download_id):
    """Find the grab history entry for a downloadId and mark it failed
    (blocklisted), same as arr_replace() did in the old script. Returns False
    if there's no grab record to blocklist (not an error - just nothing to do)."""
    if not download_id:
        return False
    data = request_json(
        source, "GET",
        f"/api/v3/history?downloadId={download_id}&eventType=1&pageSize=5",
    )
    records = (data or {}).get("records") or []
    if not records:
        return False
    request_json(source, "POST", f"/api/v3/history/failed/{records[0]['id']}")
    return True


def rescan(source, series_or_movie_id):
    if source == "radarr":
        return request_json(
            "radarr", "POST", "/api/v3/command",
            json={"name": "RescanMovie", "movieId": series_or_movie_id},
        )
    return request_json(
        "sonarr", "POST", "/api/v3/command",
        json={"name": "RescanSeries", "seriesId": series_or_movie_id},
    )
