"""Local SQLite store: URL queue, inspection history, background jobs, robots.txt snapshots."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS url_queue (
    site_url TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT,
    source_detail TEXT,
    added_at TEXT NOT NULL,
    last_inspected_at TEXT,
    latest_id INTEGER,
    PRIMARY KEY (site_url, url)
);
CREATE INDEX IF NOT EXISTS ix_queue_due ON url_queue (site_url, last_inspected_at);

CREATE TABLE IF NOT EXISTS inspections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url TEXT NOT NULL,
    url TEXT NOT NULL,
    inspected_at TEXT NOT NULL,
    pt_date TEXT NOT NULL,
    verdict TEXT,
    coverage_state TEXT,
    is_indexed INTEGER,
    indexing_state TEXT,
    page_fetch_state TEXT,
    robots_txt_state TEXT,
    last_crawl_time TEXT,
    crawled_as TEXT,
    google_canonical TEXT,
    user_canonical TEXT,
    sitemaps TEXT,
    referring_urls TEXT,
    rich_verdict TEXT,
    rich_results TEXT,
    rich_issue_count INTEGER,
    error TEXT,
    raw BLOB
);
CREATE INDEX IF NOT EXISTS ix_insp_url ON inspections (site_url, url, id);
CREATE INDEX IF NOT EXISTS ix_insp_day ON inspections (site_url, pt_date);

CREATE TABLE IF NOT EXISTS inspection_jobs (
    site_url TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1,
    daily_limit INTEGER NOT NULL,
    stale_days INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    paused_until TEXT,
    last_message TEXT
);

CREATE TABLE IF NOT EXISTS robots_contents (
    sha256 TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robots_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    status_code INTEGER,
    final_url TEXT,
    redirects TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    fetch_ms INTEGER,
    error TEXT,
    changed INTEGER
);
CREATE INDEX IF NOT EXISTS ix_robots_host ON robots_checks (host, id);

CREATE TABLE IF NOT EXISTS robots_watch (
    host TEXT PRIMARY KEY,
    interval_hours INTEGER NOT NULL,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_site ON notes (site_url, kind);

CREATE TABLE IF NOT EXISTS crawl_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    signature TEXT NOT NULL,
    host_hint TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    size_bytes INTEGER,
    bytes_read INTEGER,
    lines INTEGER,
    google_lines INTEGER,
    verified INTEGER,
    not_google_ip INTEGER,
    unchecked INTEGER,
    unparsed INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS crawl_daily (
    host TEXT NOT NULL,
    pt_date TEXT NOT NULL,
    response TEXT NOT NULL,
    file_type TEXT NOT NULL,
    bot_type TEXT NOT NULL,
    purpose TEXT NOT NULL,
    verification TEXT NOT NULL,
    requests INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    rt_sum_ms REAL NOT NULL,
    rt_count INTEGER NOT NULL,
    PRIMARY KEY (host, pt_date, response, file_type, bot_type, purpose, verification)
);

CREATE TABLE IF NOT EXISTS crawl_urls (
    host TEXT NOT NULL,
    url TEXT NOT NULL,
    response TEXT NOT NULL,
    bot_type TEXT NOT NULL,
    verification TEXT NOT NULL,
    hits INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (host, url, response, bot_type, verification)
);

CREATE TABLE IF NOT EXISTS crawl_seen (
    host TEXT NOT NULL,
    url TEXT NOT NULL,
    first_seen TEXT,
    PRIMARY KEY (host, url)
);
"""

_init_lock = threading.Lock()
_initialised: set[str] = set()


def db_path() -> Path:
    return data_dir() / "gsc.sqlite3"


@contextmanager
def connect():
    path = str(db_path())
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        with _init_lock:
            if path not in _initialised:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                _initialised.add(path)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
