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
    if "duration_ms" not in existing_cols:
        conn.execute("ALTER TABLE scanned ADD COLUMN duration_ms INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            ext_id INTEGER,
            episode_id INTEGER,
            file_id INTEGER NOT NULL,
            download_id TEXT,
            label TEXT NOT NULL,
            path TEXT,
            enqueued_at TEXT NOT NULL,
            UNIQUE(source, file_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,          -- 'radarr' or 'sonarr'
            ext_id INTEGER,                -- movieId (radarr) or seriesId (sonarr)
            episode_id INTEGER,            -- episodeId (sonarr only)
            file_id INTEGER,               -- moviefile/episodefile id
            download_id TEXT,
            label TEXT NOT NULL,           -- "Title (Year)" / "Title SxxEyy" - source column
                                            -- already says radarr/sonarr, so no Movie:/TV: prefix
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
    _migrate_strip_label_prefixes(conn)
    _migrate_scanned_autofix_bucket(conn)
    conn.close()


def _migrate_strip_label_prefixes(conn):
    """One-time cleanup: rows written before the Movie:/TV: prefix was
    dropped from label construction still have it baked into their stored
    label text (changing how a label gets built doesn't touch rows already
    in the DB). Strips it from every table that stores a label, once, so
    existing history matches what new rows look like. Guarded by a settings
    flag so this doesn't re-scan every table on every startup forever."""
    if conn.execute(
        "SELECT value FROM settings WHERE key='migrated_label_prefix_removed'"
    ).fetchone():
        return
    for table in ("scanned", "queue", "issues"):
        for prefix in ("Movie: ", "TV: "):
            conn.execute(
                f"UPDATE {table} SET label = SUBSTR(label, ?) "
                f"WHERE label LIKE ? || '%'",
                (len(prefix) + 1, prefix),
            )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('migrated_label_prefix_removed', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()


def _migrate_scanned_autofix_bucket(conn):
    """One-time cleanup: `scanned` rows written before the Log gained a
    separate "auto-fixed" bucket still have their old `result` text
    ("broken: <reason>") even for files that were in fact successfully
    auto-replaced - changing how new rows get classified doesn't touch rows
    already in the DB. For every scanned row still saying "broken:" whose
    matching issues row (same source/file_id, category='broken') is actually
    status=auto_fixed, rewrite the Log's result to the "auto-fixed:" bucket
    so old history matches what new rows look like. Guarded by a settings
    flag so this doesn't re-join scanned/issues on every startup forever."""
    if conn.execute(
        "SELECT value FROM settings WHERE key='migrated_scanned_autofix_bucket'"
    ).fetchone():
        return
    rows = conn.execute(
        "SELECT s.source AS source, s.file_id AS file_id, i.reason AS reason "
        "FROM scanned s JOIN issues i "
        "ON i.source = s.source AND i.file_id = s.file_id AND i.category = 'broken' "
        "WHERE s.result LIKE 'broken:%' AND i.status = 'auto_fixed'"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE scanned SET result = ? WHERE source = ? AND file_id = ?",
            (f"auto-fixed: {row['reason']}", row["source"], row["file_id"]),
        )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('migrated_scanned_autofix_bucket', '1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()


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


def mark_scanned(source, file_id, label=None, result=None, duration_ms=None):
    """Record that this file has been checked at least once - regardless of
    whether a problem was found - so the backlog scanner never re-picks it.

    label/result/duration_ms are purely for the Log UI page (what got
    checked, what came of it - None/empty result means "looked clean" - and
    how long the check took). They don't affect scan-skip logic at all,
    which is keyed on (source, file_id) alone. ON CONFLICT updates rather
    than no-ops, so a second mark_scanned() call for the same file
    (shouldn't normally happen, since it's only ever called once a file's
    skipped afterward) still refreshes the display instead of silently
    doing nothing."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO scanned (source, file_id, scanned_at, label, result, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, file_id) DO UPDATE SET "
        "scanned_at=excluded.scanned_at, label=excluded.label, result=excluded.result, "
        "duration_ms=excluded.duration_ms",
        (source, file_id, now(), label, result, duration_ms),
    )
    conn.commit()
    conn.close()


def count_scanned(source):
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM scanned WHERE source=?", (source,)).fetchone()
    conn.close()
    return row["n"]


def enqueue(source, file_id, label, *, ext_id=None, episode_id=None,
            download_id=None, path=None):
    """Add a newly-imported file to the deep-check queue (real ffmpeg decode
    test) - called from webhooks.py right after the live, fast checks pass,
    so the slow decode test runs soon after on the worker thread instead of
    blocking the webhook response. UNIQUE(source, file_id) + INSERT OR IGNORE
    means a duplicate webhook delivery for the same file is a no-op here,
    same dedup principle as everything else in this app."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO queue "
        "(source, ext_id, episode_id, file_id, download_id, label, path, enqueued_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (source, ext_id, episode_id, file_id, download_id, label, path, now()),
    )
    conn.commit()
    conn.close()


def pop_queue_item():
    """Pop the oldest queued item (FIFO by id, which tracks enqueue order) for
    the worker to process, or None if the queue's empty. Select-then-delete
    in one connection - fine for a single-consumer worker, no need for
    SELECT...FOR UPDATE-style locking."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM queue ORDER BY id ASC LIMIT 1").fetchone()
    if row:
        conn.execute("DELETE FROM queue WHERE id=?", (row["id"],))
        conn.commit()
    conn.close()
    return dict(row) if row else None


def count_queue():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS n FROM queue").fetchone()
    conn.close()
    return row["n"]


def list_queue(limit=50, offset=0):
    """Pending queue items, oldest (next up) first - for the Queue UI page."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM queue ORDER BY id ASC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Result-filter buckets for the Log page - matched against the `result`
# column's own conventions (NULL = clean, "broken:"/"auto-fixed:"/"skipped:"
# prefixes, anything else = a soft media-info issue joined by "; ").
# "auto-fixed" is its own bucket, separate from "broken": those files were
# handled automatically (deleted, blocklisted, re-searched) with no
# interaction needed, so they don't belong on the Issues page - "broken"
# means still open and needing a manual Ignore/Fix-now call (e.g. past the
# daily auto-replace cap).
RESULT_FILTERS = {
    "clean": "result IS NULL",
    "broken": "result LIKE 'broken:%'",
    "auto_fixed": "result LIKE 'auto-fixed:%'",
    "skipped": "result LIKE 'skipped:%'",
    "issue": "result IS NOT NULL AND result NOT LIKE 'broken:%' "
             "AND result NOT LIKE 'auto-fixed:%' AND result NOT LIKE 'skipped:%'",
}


def _scanned_filter_clause(source=None, result=None, q=None):
    clauses, params = [], []
    if source:
        clauses.append("source=?")
        params.append(source)
    if result and result in RESULT_FILTERS:
        clauses.append(RESULT_FILTERS[result])
    if q:
        clauses.append("label LIKE ? ESCAPE '\\'")
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def list_recent_scanned(limit=30, offset=0, source=None, result=None, q=None):
    """Most recent backlog-scan activity, newest first, optionally filtered
    by source ('radarr'/'sonarr'), result bucket (RESULT_FILTERS), and/or a
    free-text title search (q, matched against label) - this is what the
    Log UI page shows so there's visible proof of what the worker's
    actually doing, not just a silent file count."""
    where, params = _scanned_filter_clause(source, result, q)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT source, file_id, label, result, scanned_at, duration_ms FROM scanned"
        f"{where} ORDER BY scanned_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_recent_scanned(source=None, result=None, q=None):
    """Matches list_recent_scanned()'s filtering, for the Log page's
    pagination total when a filter is active."""
    where, params = _scanned_filter_clause(source, result, q)
    conn = get_conn()
    row = conn.execute(f"SELECT COUNT(*) AS n FROM scanned{where}", params).fetchone()
    conn.close()
    return row["n"]


def iter_all_scanned(source=None, result=None, q=None):
    """Every matching row, newest first, no limit - for CSV export of the
    full filtered list rather than just the current page."""
    where, params = _scanned_filter_clause(source, result, q)
    conn = get_conn()
    rows = conn.execute(
        f"SELECT source, file_id, label, result, scanned_at, duration_ms FROM scanned"
        f"{where} ORDER BY scanned_at DESC",
        params,
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


def count_issues(status=None):
    conn = get_conn()
    if status:
        row = conn.execute("SELECT COUNT(*) AS n FROM issues WHERE status=?", (status,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM issues").fetchone()
    conn.close()
    return row["n"]


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
