"""Settings editable from the interface, stored in the database."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass

from . import db
from .config import Config

GLOBAL_OPTIONS = (30, 60, 120, 300)
TORRENT_OPTIONS = (60, 300, 600, 900, 1800, 3600)
GLOBAL_KEY = "setting_global_interval"
TORRENT_KEY = "setting_torrent_interval"

# Days to keep, 0 meaning without limit. Views only use fine data on ranges of 30 days or
# less, so the shortest option cannot leave a hole in them.
RETENTION_OPTIONS = (30, 90, 180, 365, 730, 1825, 0)
RETENTION_SOURCES = {
    "minute": ("global_minute", "ts", ""),
    "torrent_hourly": ("torrent_hourly", "hour", ""),
    "sample_fine": ("torrent_samples", "ts", " AND tier = 0"),
    "sample_hourly": ("torrent_samples", "ts", " AND tier = 1"),
}


class InvalidSetting(ValueError):
    """A submitted setting is not one of the allowed values."""


@dataclass(frozen=True)
class Intervals:
    global_interval: int
    torrent_interval: int


def _stored(conn: sqlite3.Connection, key: str, default: int) -> int:
    raw = db.get_meta(conn, key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def load(conn: sqlite3.Connection, config: Config) -> Intervals:
    """Saved values win; the environment only provides the defaults."""
    return Intervals(
        _stored(conn, GLOBAL_KEY, config.global_interval),
        _stored(conn, TORRENT_KEY, config.torrent_interval),
    )


def validate(global_interval: object, torrent_interval: object) -> Intervals:
    if type(global_interval) is not int or global_interval not in GLOBAL_OPTIONS:
        raise InvalidSetting("Fréquence globale non autorisée")
    if type(torrent_interval) is not int or torrent_interval not in TORRENT_OPTIONS:
        raise InvalidSetting("Fréquence par torrent non autorisée")
    if torrent_interval < global_interval:
        raise InvalidSetting("Les torrents ne peuvent pas être collectés plus souvent que les stats globales")
    return Intervals(global_interval, torrent_interval)


def save(conn: sqlite3.Connection, intervals: Intervals) -> None:
    with conn:
        db.set_meta(conn, GLOBAL_KEY, intervals.global_interval)
        db.set_meta(conn, TORRENT_KEY, intervals.torrent_interval)


@dataclass(frozen=True)
class Retention:
    """Nothing is ever deleted unless the user chose a limit, hence the defaults."""

    minute: int = 0
    torrent_hourly: int = 0
    sample_fine: int = 0
    sample_hourly: int = 0


def load_retention(conn: sqlite3.Connection) -> Retention:
    return Retention(**{name: _stored(conn, f"retention_{name}", 0) for name in RETENTION_SOURCES})


def validate_retention(body: dict[str, object]) -> Retention:
    values = {}
    for name in RETENTION_SOURCES:
        value = body.get(name)
        if type(value) is not int or value not in RETENTION_OPTIONS:
            raise InvalidSetting("Durée de conservation non autorisée")
        values[name] = value
    return Retention(**values)


def save_retention(conn: sqlite3.Connection, retention: Retention) -> None:
    with conn:
        for name, value in asdict(retention).items():
            db.set_meta(conn, f"retention_{name}", value)


def tightened(current: Retention, new: Retention) -> list[str]:
    """Data classes whose new limit is stricter: these need an explicit agreement."""
    before, after = asdict(current), asdict(new)
    return [
        name
        for name in RETENTION_SOURCES
        if after[name] != 0 and (before[name] == 0 or after[name] < before[name])
    ]


def _expired_clause(name: str, days: int, now: int) -> tuple[str, str, int]:
    table, column, extra = RETENTION_SOURCES[name]
    return table, f"{column} < ?{extra}", now - days * 86400


def count_expired(conn: sqlite3.Connection, retention: Retention, names: list[str], now: int) -> dict[str, int]:
    """How many rows the given limits would delete right now."""
    days = asdict(retention)
    counts = {}
    for name in names:
        table, clause, limit = _expired_clause(name, days[name], now)
        counts[name] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", (limit,)).fetchone()[0]
    return counts


def delete_expired(conn: sqlite3.Connection, retention: Retention, now: int) -> None:
    """Apply the limits the user agreed to. A limit of 0 never deletes anything."""
    days = asdict(retention)
    with conn:
        for name in RETENTION_SOURCES:
            if days[name] > 0:
                table, clause, limit = _expired_clause(name, days[name], now)
                conn.execute(f"DELETE FROM {table} WHERE {clause}", (limit,))


def storage(conn: sqlite3.Connection, db_path: str) -> dict[str, object]:
    """Rows stored per data class, and the size of the database on disk."""
    rows = {}
    for name, (table, _column, extra) in RETENTION_SOURCES.items():
        rows[name] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE 1 = 1{extra}").fetchone()[0]
    size = 0
    for suffix in ("", "-wal"):
        if os.path.exists(db_path + suffix):
            size += os.path.getsize(db_path + suffix)
    return {"rows": rows, "bytes": size}
