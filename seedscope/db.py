"""SQLite storage: schema, migrations and connections."""

from __future__ import annotations

import os
import sqlite3
import urllib.parse

MIGRATIONS = [
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

    CREATE TABLE torrents (
        hash TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        tracker TEXT NOT NULL DEFAULT '',
        tracker_host TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        content_key TEXT NOT NULL,
        added_on INTEGER NOT NULL DEFAULT 0,
        completion_on INTEGER NOT NULL DEFAULT 0,
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        removed_at INTEGER,
        uploaded INTEGER NOT NULL DEFAULT 0,
        downloaded INTEGER NOT NULL DEFAULT 0,
        ratio REAL NOT NULL DEFAULT 0,
        seeding_time INTEGER NOT NULL DEFAULT 0,
        time_active INTEGER NOT NULL DEFAULT 0,
        last_activity INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT '',
        num_complete INTEGER NOT NULL DEFAULT 0,
        num_incomplete INTEGER NOT NULL DEFAULT 0,
        progress REAL NOT NULL DEFAULT 0
    ) WITHOUT ROWID;
    CREATE INDEX torrents_content ON torrents (content_key);
    CREATE INDEX torrents_tracker ON torrents (tracker);

    CREATE TABLE global_minute (
        ts INTEGER PRIMARY KEY,
        up_speed INTEGER NOT NULL,
        dl_speed INTEGER NOT NULL,
        alltime_ul INTEGER NOT NULL,
        alltime_dl INTEGER NOT NULL,
        peers INTEGER NOT NULL DEFAULT 0,
        free_space INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID;

    CREATE TABLE global_hourly (
        hour INTEGER PRIMARY KEY,
        day TEXT NOT NULL,
        weekday INTEGER NOT NULL,
        hour_of_day INTEGER NOT NULL,
        up_bytes INTEGER NOT NULL DEFAULT 0,
        dl_bytes INTEGER NOT NULL DEFAULT 0,
        up_speed_sum INTEGER NOT NULL DEFAULT 0,
        dl_speed_sum INTEGER NOT NULL DEFAULT 0,
        up_speed_max INTEGER NOT NULL DEFAULT 0,
        dl_speed_max INTEGER NOT NULL DEFAULT 0,
        samples INTEGER NOT NULL DEFAULT 0,
        alltime_ul INTEGER NOT NULL DEFAULT 0,
        alltime_dl INTEGER NOT NULL DEFAULT 0
    ) WITHOUT ROWID;
    CREATE INDEX global_hourly_day ON global_hourly (day);

    CREATE TABLE torrent_hourly (
        hash TEXT NOT NULL,
        hour INTEGER NOT NULL,
        up INTEGER NOT NULL DEFAULT 0,
        dl INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hash, hour)
    ) WITHOUT ROWID;
    CREATE INDEX torrent_hourly_hour ON torrent_hourly (hour);

    CREATE TABLE torrent_daily (
        hash TEXT NOT NULL,
        day TEXT NOT NULL,
        up INTEGER NOT NULL DEFAULT 0,
        dl INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (hash, day)
    ) WITHOUT ROWID;
    CREATE INDEX torrent_daily_day ON torrent_daily (day);
    """,
    """
    ALTER TABLE torrents ADD COLUMN popularity REAL NOT NULL DEFAULT 0;

    -- tier: 0 = changed value, 1 = first poll of the hour, 2 = first poll of the day.
    CREATE TABLE torrent_samples (
        hash TEXT NOT NULL,
        ts INTEGER NOT NULL,
        tier INTEGER NOT NULL,
        uploaded INTEGER NOT NULL,
        downloaded INTEGER NOT NULL,
        ratio REAL NOT NULL,
        popularity REAL NOT NULL,
        seeders INTEGER NOT NULL,
        leechers INTEGER NOT NULL,
        PRIMARY KEY (hash, ts)
    ) WITHOUT ROWID;
    CREATE INDEX torrent_samples_tier ON torrent_samples (tier, ts);
    """,
]


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA cache_size = -2000")  # 2 MiB: RAM matters on this NAS
    conn.execute("PRAGMA temp_store = FILE")


def connect(path: str) -> sqlite3.Connection:
    """Open the read-write connection and apply pending migrations."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA wal_autocheckpoint = 200")
    _configure(conn)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {index}")
    return conn


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open a query-only connection, so the web layer cannot alter data."""
    uri = "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    _configure(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
