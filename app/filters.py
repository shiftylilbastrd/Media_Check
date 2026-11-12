"""Jinja filters, registered in create_app(): human-readable relative
timestamps and check durations for the Issues/Queue/Log pages, instead of
raw ISO strings/millisecond counts nobody wants to parse in their head."""
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


def duration(ms):
    """1500 -> '1.5s', 75000 -> '1m 15s'. None/0 (older rows from before
    this was tracked, or a check that was skipped rather than actually run)
    renders as an em dash rather than '0s', which would misleadingly imply
    an instant check."""
    if not ms:
        return "—"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{int(seconds)}s"
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m {seconds}s"
