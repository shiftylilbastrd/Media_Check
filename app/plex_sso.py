"""Official Plex account-linking flow ("Sign in with Plex" - the same
PIN-based flow Tautulli/Overseerr use), as an alternative to pasting the
Plex Token into Settings by hand."""
import urllib.parse
import uuid

import requests

from . import db

PRODUCT_NAME = "Media Check"
TIMEOUT = 15


def client_identifier():
    """A stable id for this app instance, persisted in our settings table -
    plex.tv ties a sign-in pin to whichever client id requested it, so this
    has to stay the same across requests and restarts."""
    cid = db.get_setting("plex_client_id")
    if not cid:
        cid = uuid.uuid4().hex
        db.set_setting("plex_client_id", cid)
    return cid


def _headers():
    return {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_identifier(),
        "X-Plex-Product": PRODUCT_NAME,
    }


def create_pin():
    r = requests.post("https://plex.tv/api/v2/pins", data={"strong": "true"},
                       headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    pin = r.json()
    return pin["id"], pin["code"]


def auth_url(code, forward_url):
    q = urllib.parse.urlencode({
        "clientID": client_identifier(),
        "code": code,
        "context[device][product]": PRODUCT_NAME,
        "forwardUrl": forward_url,
    })
    return "https://app.plex.tv/auth#?" + q


def check_pin(pin_id):
    """Polls plex.tv for whether the pin above has been approved yet.
    Returns the auth token once it has been, otherwise None."""
    r = requests.get(f"https://plex.tv/api/v2/pins/{pin_id}", headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    pin = r.json()
    return pin.get("authToken") or None
