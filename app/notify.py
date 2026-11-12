"""Pushover notifications - best-effort, same spirit as the old script's
NOTIFY_CMD/DISCORD_WEBHOOK: never let a notification failure break the
actual detection/fix pipeline."""
import requests

from . import db

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT = 15


def send(title, message, priority=0):
    token = db.get_setting("pushover_token")
    user = db.get_setting("pushover_user_key")
    if not token or not user:
        return False
    try:
        requests.post(
            PUSHOVER_URL,
            data={
                "token": token,
                "user": user,
                "title": title[:250],
                "message": message[:1024],
                "priority": priority,
            },
            timeout=TIMEOUT,
        )
        return True
    except Exception:
        return False
