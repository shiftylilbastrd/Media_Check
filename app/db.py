import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/media-check-tools.db")

# Statuses an issue can be in:
#   open             - needs a look (shown on the main page with Ignore/Fix now)
#   ignored          - user dismissed this specific item, won't be re-flagged
#   search_triggered - user clicked "Fix now"; a new search was requested
#   auto_fixed       - handled automatically (currently: broken/zero-filled
#                      file deleted+blocklisted+re-searched). No button needed.


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scanned (
            source TEXT NOT NULL,
            file_id INTEGER NOT NULL,
            scanned_at TEXT NOT NULL,
            PRIMARY KEY (source, file_id)
        )
        """
    )
    # label/result added later - an existing DB's `scanned` table predates them,
    # and CREATE TABLE IF NOT EXISTS above won't add columns to it. Add them if
    # missing so upgrading doesn't require wiping the table.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(scanned)")}
    if "label" not in existing_cols:
        conn.execute("ALTER TABLE scanned ADD COLUMN label TEXT")
    if "result" not in existing_cols:
        conn.execute("ALTER TABLE scanned ADD COLUMN result TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- 'radarr' or 'sonarr'
            ext_id INTEGER,                -- movieId (radarr) or seriesId (sonarr)
            episode_id INTEGER,            -- episodeId (sonarr only)
            file_id INTEGER,               -- moviefile/episodefile id
            download_id TEXT,
            label TEXT NOT NULL,           -- "Movie: Title (Year)" / "TV: Title SxxEyy"
            category TEXT NOT NULL,        -- no_audio / no_english_audio / dv_no_hdr10 /
                                            -- runtime / small_size / decode_error / broken
            reason TEXT NOT NULL,
            path TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            detected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, file_id, category)
        )
        """
    )
    conn.commit()
    conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def record_issue(
    source, ext_id, label, category, reason, *,
    episode_id=None, file_id=None, download_id=None, path=None, status="open",
):
    """Insert a new issue, or refresh the reason text on an existing *open* one
    for the same file+category. Never re-opens something already ignored,
    fixed, or auto-fixed - that would defeat the point of Ignore."""
    conn = get_conn()
    existing = conn.execute(
        "SELECT id, status FROM issues WHERE source=? AND file_id=? AND category=?",
        (source, file_id, category),
    ).fetchone()
    ts = now()
    if existing:
        if existing["status"] != "open":
            conn.close()
            return existing["id"]
        conn.execute(
            "UPDATE issues SET reason=?, updated_at=? WHERE id=?",
            (reason, ts, existing["id"]),
        )
        conn.commit()
        conn.close()
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO issues (source, ext_id, episode_id, file_id, download_id, label, "
        "category, reason, path, status, detected_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (source, ext_id, episode_id, file_id, download_id, label, category, reason,
         path, status, ts, ts),
    )
    conn.commit()
    issue_id = cur.lastrowid
    conn.close()
    return issue_id


def get_scanned_ids(source):
    """All file_ids already scanned (ever, regardless of outcome) for a
    source - pulled once per backlog run and checked in-memory, rather than
    one query per candidate file."""
    conn = get_conn()
    rows = conn.execute("SELECT file_id FROM scanned WHERE source=?", (source,)).fetchall()
    conn.close()
    return {r["file_id"] for r in rows}


def mark_scanned(source, file_id, label=None, result=None):
    """Record that this file has been checked at least once - regardless of
    whether a problem was found - so the backlog scanner never re-picks it.

    label/result are purely for the Backlog UI page (what got checked, and
    what came of it - None/empty result means "looked clean"). They don't
    affect scan-skip logic at all, which is keyed on (source, file_id) alone.
    ON CONFLICT updates rather than no-ops, so a second mark_scanned() call
    for the same file (shouldn't normally happen, since it's only ever
    called once a file's skipped afterward) still refreshes the display
    instead of silently doing nothing."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO scanned (source, file_id, scanned_at, label, result) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source, file_id) DO UPDATE SET "
        "scanned_at=excluded.scanned_at, label=excluded.label, result=excluded.result",
        (source, file_id, now(), label, result),
    )
    conn.commit()
    conn.close()


def count_scanned(source):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM scanned WHERE source=?", (source,)).fetchone()
    conn.close()
    return row["n"]


def list_recent_scanned(limit=30):
    """Most recent backlog-scan activity across both sources, newest first -
    this is what the Backlog UI page shows so there's visible proof of what
    the scanner's actually doing, not just a silent file count."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT source, file_id, label, result, scanned_at FROM scanned "
        "ORDER BY scanned_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_issues(status=None):
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM issues WHERE status=? ORDER BY detected_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM issues ORDER BY detected_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_issue(source, file_id, category):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM issues WHERE source=? AND file_id=? AND category=?",
        (source, file_id, category),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_issue(issue_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_issue_status(issue_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE issues SET status=?, updated_at=? WHERE id=?",
        (status, now(), issue_id),
    )
    conn.commit()
    conn.close()
