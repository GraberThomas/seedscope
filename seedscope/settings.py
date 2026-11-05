"""Settings editable from the interface, stored in the database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db
from .config import Config

GLOBAL_OPTIONS = (30, 60, 120, 300)
TORRENT_OPTIONS = (60, 300, 600, 900, 1800, 3600)
GLOBAL_KEY = "setting_global_interval"
TORRENT_KEY = "setting_torrent_interval"


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
