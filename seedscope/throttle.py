"""Optional upload throttling while qBittorrent downloads.

When enabled, SeedScope lowers qBittorrent's normal global upload limit as soon as a
torrent is really downloading, and puts back the previous limit once nothing has been
downloading for a while. It never touches the alternative limits nor any torrent.

Ownership is kept in the database, so a restart resumes where it stopped. A limit the user
changed by hand always wins: SeedScope then lets go and waits for the downloads to end.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol

from . import db

log = logging.getLogger("seedscope.throttle")

MIB = 1024 * 1024
LIMIT_OPTIONS = (256 * 1024, 512 * 1024, MIB, 2 * MIB, 5 * MIB, 10 * MIB, 20 * MIB)
DELAY_OPTIONS = (60, 120, 300, 600, 1800)
DOWNLOADING_STATES = frozenset({"downloading", "forcedDL"})

ENABLED_KEY = "throttle_enabled"
LIMIT_KEY = "throttle_limit"
DELAY_KEY = "throttle_delay"
ACTIVE_KEY = "throttle_active"  # "1" while SeedScope owns the limit
SAVED_KEY = "throttle_saved_limit"  # the user's limit, to put back
APPLIED_KEY = "throttle_applied_limit"  # what SeedScope set, to detect manual changes
LAST_DOWNLOAD_KEY = "throttle_last_download"
HELD_KEY = "throttle_held"  # "1" after a manual change, until downloads end


class InvalidThrottle(ValueError):
    """A submitted throttling setting is not one of the allowed values."""


class LimitClient(Protocol):
    def upload_limit(self) -> int: ...
    def set_upload_limit(self, limit: int) -> None: ...


@dataclass(frozen=True)
class ThrottleSettings:
    enabled: bool = False
    limit: int = MIB
    delay: int = 120


def _int(conn: sqlite3.Connection, key: str, default: int) -> int:
    raw = db.get_meta(conn, key)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def load(conn: sqlite3.Connection) -> ThrottleSettings:
    defaults = ThrottleSettings()
    limit = _int(conn, LIMIT_KEY, defaults.limit)
    delay = _int(conn, DELAY_KEY, defaults.delay)
    return ThrottleSettings(
        enabled=_int(conn, ENABLED_KEY, 0) == 1,
        limit=limit if limit in LIMIT_OPTIONS else defaults.limit,
        delay=delay if delay in DELAY_OPTIONS else defaults.delay,
    )


def validate(body: Mapping[str, object]) -> ThrottleSettings:
    enabled, limit, delay = body.get("enabled"), body.get("limit"), body.get("delay")
    if type(enabled) is not bool:
        raise InvalidThrottle("Activation invalide")
    if type(limit) is not int or limit not in LIMIT_OPTIONS:
        raise InvalidThrottle("Limite d'envoi non autorisée")
    if type(delay) is not int or delay not in DELAY_OPTIONS:
        raise InvalidThrottle("Délai non autorisé")
    return ThrottleSettings(enabled, limit, delay)


def save(conn: sqlite3.Connection, value: ThrottleSettings) -> None:
    with conn:
        db.set_meta(conn, ENABLED_KEY, int(value.enabled))
        db.set_meta(conn, LIMIT_KEY, value.limit)
        db.set_meta(conn, DELAY_KEY, value.delay)


def is_downloading(torrents: Iterable[Mapping[str, Any]]) -> bool:
    """A torrent really receiving data; stalled or queued ones do not count."""
    return any(
        t.get("state") in DOWNLOADING_STATES and int(t.get("dlspeed") or 0) > 0 for t in torrents
    )


def _start_event(conn: sqlite3.Connection, now: int, limit: int, previous: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO throttle_events (start, end, up_limit, previous_limit, ended_by) "
        "VALUES (?, NULL, ?, ?, '')",
        (now, limit, previous),
    )


def _end_event(conn: sqlite3.Connection, now: int, ended_by: str) -> None:
    conn.execute(
        "UPDATE throttle_events SET end = ?, ended_by = ? WHERE end IS NULL", (now, ended_by)
    )


def _release(conn: sqlite3.Connection, client: LimitClient, now: int, ended_by: str) -> None:
    """Give the user's limit back, unless they changed it by hand meanwhile."""
    current = client.upload_limit()
    applied = _int(conn, APPLIED_KEY, -1)
    if current == applied:
        client.set_upload_limit(_int(conn, SAVED_KEY, 0))
    else:
        ended_by = "manual"
    with conn:
        db.set_meta(conn, ACTIVE_KEY, 0)
        _end_event(conn, now, ended_by)
    log.info("Bridage de l'envoi levé (%s)", ended_by)


def step(
    conn: sqlite3.Connection,
    client: LimitClient,
    active_torrents: Iterable[Mapping[str, Any]],
    now: int,
) -> None:
    """One decision, run after each live poll."""
    config = load(conn)
    active = _int(conn, ACTIVE_KEY, 0) == 1
    downloading = config.enabled and is_downloading(active_torrents)
    if downloading:
        with conn:
            db.set_meta(conn, LAST_DOWNLOAD_KEY, now)
    elif _int(conn, HELD_KEY, 0) == 1:
        with conn:
            db.set_meta(conn, HELD_KEY, 0)

    if active:
        if not config.enabled:
            _release(conn, client, now, "disabled")
            return
        current = client.upload_limit()
        if current != _int(conn, APPLIED_KEY, -1):
            # Changed by hand: the user's choice stays, and becomes the one to put back.
            with conn:
                db.set_meta(conn, ACTIVE_KEY, 0)
                db.set_meta(conn, HELD_KEY, 1)
                _end_event(conn, now, "manual")
            log.info("Limite d'envoi modifiée à la main : SeedScope n'y touche plus")
            return
        if not downloading and now - _int(conn, LAST_DOWNLOAD_KEY, now) >= config.delay:
            _release(conn, client, now, "idle")
            return
        if current != config.limit:
            client.set_upload_limit(config.limit)  # the user chose another throttled value
            with conn:
                db.set_meta(conn, APPLIED_KEY, config.limit)
                conn.execute(
                    "UPDATE throttle_events SET up_limit = ? WHERE end IS NULL", (config.limit,)
                )
        return

    if not downloading or _int(conn, HELD_KEY, 0) == 1:
        return
    current = client.upload_limit()
    if current and current <= config.limit:
        return  # already at least as strict: nothing to do
    client.set_upload_limit(config.limit)
    with conn:
        db.set_meta(conn, SAVED_KEY, current)
        db.set_meta(conn, APPLIED_KEY, config.limit)
        db.set_meta(conn, ACTIVE_KEY, 1)
        _start_event(conn, now, config.limit, current)
    log.info("Téléchargement en cours : envoi bridé à %d o/s", config.limit)


def release_now(conn: sqlite3.Connection, client: LimitClient, now: int) -> None:
    """Put the user's limit back right away, for instance when SeedScope stops."""
    if _int(conn, ACTIVE_KEY, 0) == 1:
        _release(conn, client, now, "stopped")


def status(conn: sqlite3.Connection, now: int, events: int = 20) -> dict[str, Any]:
    config = load(conn)
    rows = conn.execute(
        "SELECT start, end, up_limit, previous_limit, ended_by FROM throttle_events "
        "ORDER BY start DESC LIMIT ?",
        (events,),
    ).fetchall()
    week = now - 7 * 86400
    seconds = conn.execute(
        "SELECT COALESCE(SUM(MIN(COALESCE(end, ?), ?) - MAX(start, ?)), 0) FROM throttle_events "
        "WHERE COALESCE(end, ?) > ?",
        (now, now, week, now, week),
    ).fetchone()[0]
    return {
        "enabled": config.enabled,
        "limit": config.limit,
        "delay": config.delay,
        "active": _int(conn, ACTIVE_KEY, 0) == 1,
        "held": _int(conn, HELD_KEY, 0) == 1,
        "week_seconds": int(seconds),
        "events": [dict(row) for row in rows],
    }
