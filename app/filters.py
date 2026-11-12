"""Jinja filters, registered in create_app(). Just the one so far:
human-readable relative timestamps for the Issues/Queue/Log pages, instead
of a raw ISO string nobody wants to parse in their head."""
from datetime import datetime, timezone


def timeago(iso_str):
    """'2026-11-12T06:18:03+00:00' -> '5m ago'. Falls back to the original
    string if it can't be parsed - never crash a page render over a
    timestamp. Purely relative (no timezone conversion needed), so this
    works the same regardless of where the server or viewer actually is."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    return dt.strftime("%b %d, %Y")
