"""Periodic collection of qBittorrent counters into SQLite."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import db, settings
from .config import Config
from .qbit import QbitAuthError, QbitClient, QbitError

log = logging.getLogger("seedscope.collector")

# qBittorrent bans an IP after repeated failed logins, which would also lock out
# the other services sharing this address: wait long enough never to trigger it.
AUTH_FAILURE_BACKOFF = 1800
ERROR_BACKOFF = 60
PRUNE_INTERVAL = 3600
CONTENT_BY_SIZE_MIN = 50 * 1024 * 1024


def tracker_host(url: str) -> str:
    """Keep the hostname only: announce URLs embed the private passkey."""
    if not url:
        return ""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def content_key(name: str, size: int) -> str:
    """Group torrents sharing the same data (cross-seeds, renamed or not).

    An identical byte count on a large payload means the same content in practice.
    Small payloads can collide by chance, so their name is part of the key.
    """
    return f"s:{size}" if size >= CONTENT_BY_SIZE_MIN else f"n:{size}:{name}"


def counter_delta(previous: int, current: int) -> int:
    """Growth of a monotonic counter; a reset never yields a negative value."""
    return max(0, current - previous)


def local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


@dataclass(frozen=True)
class GlobalSample:
    ts: int
    up_speed: int
    dl_speed: int
    alltime_ul: int
    alltime_dl: int
    peers: int = 0
    free_space: int = 0


@dataclass
class LiveState:
    """In-memory state shared with the web layer."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    active: list[dict[str, Any]] = field(default_factory=list)
    active_ts: int = 0
    connection_status: str = ""
    last_success: int = 0
    last_error: str = ""
    wake: threading.Event = field(default_factory=threading.Event)  # settings or manual request
    force_full: bool = False  # a manual refresh asked for a complete collection
    last_full_success: int = 0
    last_manual_request: float = 0.0
    force_prune: bool = False  # the user just agreed to a shorter retention

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "active": list(self.active),
                "active_ts": self.active_ts,
                "connection_status": self.connection_status,
                "last_success": self.last_success,
                "last_error": self.last_error,
                "last_full_success": self.last_full_success,
            }

    def request_full_collection(self, now: float, cooldown: float) -> bool:
        """Ask for an immediate full collection; False while the cooldown is running."""
        with self.lock:
            if now - self.last_manual_request < cooldown:
                return False
            self.last_manual_request = now
            self.force_full = True
        self.wake.set()
        return True

    def request_prune(self) -> None:
        with self.lock:
            self.force_prune = True
        self.wake.set()

    def take_force_prune(self) -> bool:
        with self.lock:
            forced = self.force_prune
            self.force_prune = False
            return forced

    def take_force_full(self) -> bool:
        with self.lock:
            forced = self.force_full
            self.force_full = False
            return forced


def store_global_sample(
    conn: sqlite3.Connection, sample: GlobalSample, previous: tuple[int, int] | None
) -> None:
    """Store one sample and add its counter growth to the hourly rollup."""
    up_delta = counter_delta(previous[0], sample.alltime_ul) if previous else 0
    dl_delta = counter_delta(previous[1], sample.alltime_dl) if previous else 0
    conn.execute(
        "INSERT OR REPLACE INTO global_minute "
        "(ts, up_speed, dl_speed, alltime_ul, alltime_dl, peers, free_space) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sample.ts,
            sample.up_speed,
            sample.dl_speed,
            sample.alltime_ul,
            sample.alltime_dl,
            sample.peers,
            sample.free_space,
        ),
    )
    hour = sample.ts - sample.ts % 3600
    local = time.localtime(hour)
    conn.execute(
        """
        INSERT INTO global_hourly (
            hour, day, weekday, hour_of_day, up_bytes, dl_bytes, up_speed_sum, dl_speed_sum,
            up_speed_max, dl_speed_max, samples, alltime_ul, alltime_dl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT (hour) DO UPDATE SET
            up_bytes = up_bytes + excluded.up_bytes,
            dl_bytes = dl_bytes + excluded.dl_bytes,
            up_speed_sum = up_speed_sum + excluded.up_speed_sum,
            dl_speed_sum = dl_speed_sum + excluded.dl_speed_sum,
            up_speed_max = MAX(up_speed_max, excluded.up_speed_max),
            dl_speed_max = MAX(dl_speed_max, excluded.dl_speed_max),
            samples = samples + 1,
            alltime_ul = MAX(alltime_ul, excluded.alltime_ul),
            alltime_dl = MAX(alltime_dl, excluded.alltime_dl)
        """,
        (
            hour,
            time.strftime("%Y-%m-%d", local),
            local.tm_wday,
            local.tm_hour,
            up_delta,
            dl_delta,
            sample.up_speed,
            sample.dl_speed,
            sample.up_speed,
            sample.dl_speed,
            sample.alltime_ul,
            sample.alltime_dl,
        ),
    )


class Collector:
    def __init__(
        self,
        config: Config,
        client: QbitClient,
        conn: sqlite3.Connection,
        state: LiveState | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.conn = conn
        self.state = state or LiveState()
        self._last_prune = 0.0
        self._apply_aliases()

    def _alias(self, host: str) -> str:
        return self.config.tracker_aliases.get(host, host)

    def _apply_aliases(self) -> None:
        """Re-label stored torrents, so that editing the aliases applies to history."""
        with self.conn:
            for row in self.conn.execute(
                "SELECT DISTINCT tracker_host FROM torrents"
            ).fetchall():
                self.conn.execute(
                    "UPDATE torrents SET tracker = ? WHERE tracker_host = ?",
                    (self._alias(row[0]), row[0]),
                )

    def poll_global(self, now: int) -> None:
        server = self.client.server_state()
        row = self.conn.execute(
            "SELECT alltime_ul, alltime_dl FROM global_minute ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        sample = GlobalSample(
            ts=now,
            up_speed=int(server.get("up_info_speed") or 0),
            dl_speed=int(server.get("dl_info_speed") or 0),
            alltime_ul=int(server.get("alltime_ul") or 0),
            alltime_dl=int(server.get("alltime_dl") or 0),
            peers=int(server.get("total_peer_connections") or 0),
            free_space=int(server.get("free_space_on_disk") or 0),
        )
        with self.conn:
            store_global_sample(self.conn, sample, tuple(row) if row else None)
        with self.state.lock:
            self.state.connection_status = str(server.get("connection_status") or "")

    def poll_live(self, now: int) -> None:
        active = self.client.torrents(active_only=True)
        rows = sorted(
            (
                {
                    "hash": t["hash"],
                    "name": t.get("name", ""),
                    "content_key": content_key(str(t.get("name") or ""), int(t.get("size") or 0)),
                    "tracker": self._alias(tracker_host(t.get("tracker", ""))),
                    "up_speed": int(t.get("upspeed") or 0),
                    "dl_speed": int(t.get("dlspeed") or 0),
                    "ratio": float(t.get("ratio") or 0),
                    "leechers": int(t.get("num_leechs") or 0),
                    "seeds": int(t.get("num_seeds") or 0),
                    "size": int(t.get("size") or 0),
                    "progress": float(t.get("progress") or 0),
                    "eta": int(t.get("eta") or 0),
                }
                for t in active
            ),
            key=lambda item: (item["up_speed"], item["dl_speed"]),
            reverse=True,
        )
        with self.state.lock:
            self.state.active = rows
            self.state.active_ts = now

    def poll_torrents(self, now: int) -> None:
        torrents = self.client.torrents()
        with self.conn:
            self.ingest_torrents(torrents, now)

    def ingest_torrents(self, torrents: Iterable[Mapping[str, Any]], now: int) -> None:
        """Merge a full torrent list. Must be called inside a transaction."""
        last_poll_raw = db.get_meta(self.conn, "last_torrent_poll")
        last_poll = int(last_poll_raw) if last_poll_raw else None
        known = {
            row["hash"]: row
            for row in self.conn.execute(
                "SELECT hash, uploaded, downloaded, tracker_host, removed_at, num_complete, "
                "num_incomplete FROM torrents"
            )
        }
        hour = now - now % 3600
        day = local_day(now)
        tier = 0
        if last_poll is None or last_poll // 3600 != now // 3600:
            tier = 1
        if last_poll is None or local_day(last_poll) != day:
            tier = 2
        seen: set[str] = set()
        for torrent in torrents:
            torrent_hash = torrent["hash"]
            seen.add(torrent_hash)
            uploaded = int(torrent.get("uploaded") or 0)
            downloaded = int(torrent.get("downloaded") or 0)
            added_on = int(torrent.get("added_on") or 0)
            previous = known.get(torrent_hash)
            if previous is not None:
                up_delta = counter_delta(previous["uploaded"], uploaded)
                dl_delta = counter_delta(previous["downloaded"], downloaded)
                # A re-added torrent restarts from zero: resume from its new counter.
            elif last_poll is not None and added_on >= last_poll:
                up_delta, dl_delta = uploaded, downloaded
            else:
                # Counters from before collection started cannot be dated.
                up_delta = dl_delta = 0

            host = tracker_host(str(torrent.get("tracker") or ""))
            if not host and previous is not None:
                host = previous["tracker_host"]  # blank until the tracker is contacted
            name = str(torrent.get("name") or "")
            size = int(torrent.get("size") or 0)
            ratio = float(torrent.get("ratio") or 0)
            popularity = float(torrent.get("popularity") or 0)
            seeders = int(torrent.get("num_complete") or 0)
            leechers = int(torrent.get("num_incomplete") or 0)
            self.conn.execute(
                """
                INSERT INTO torrents (
                    hash, name, tracker, tracker_host, category, tags, size, content_key,
                    added_on, completion_on, first_seen, last_seen, removed_at, uploaded,
                    downloaded, ratio, seeding_time, time_active, last_activity, state,
                    num_complete, num_incomplete, progress, popularity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (hash) DO UPDATE SET
                    name = excluded.name,
                    tracker = excluded.tracker,
                    tracker_host = excluded.tracker_host,
                    category = excluded.category,
                    tags = excluded.tags,
                    size = excluded.size,
                    content_key = excluded.content_key,
                    added_on = excluded.added_on,
                    completion_on = excluded.completion_on,
                    last_seen = excluded.last_seen,
                    removed_at = NULL,
                    uploaded = excluded.uploaded,
                    downloaded = excluded.downloaded,
                    ratio = excluded.ratio,
                    seeding_time = excluded.seeding_time,
                    time_active = excluded.time_active,
                    last_activity = excluded.last_activity,
                    state = excluded.state,
                    num_complete = excluded.num_complete,
                    num_incomplete = excluded.num_incomplete,
                    progress = excluded.progress,
                    popularity = excluded.popularity
                """,
                (
                    torrent_hash,
                    name,
                    self._alias(host),
                    host,
                    str(torrent.get("category") or ""),
                    str(torrent.get("tags") or ""),
                    size,
                    content_key(name, size),
                    added_on,
                    int(torrent.get("completion_on") or 0),
                    now,
                    now,
                    uploaded,
                    downloaded,
                    ratio,
                    int(torrent.get("seeding_time") or 0),
                    int(torrent.get("time_active") or 0),
                    int(torrent.get("last_activity") or 0),
                    str(torrent.get("state") or ""),
                    seeders,
                    leechers,
                    float(torrent.get("progress") or 0),
                    popularity,
                ),
            )
            # Idle torrents are the vast majority: store them hourly, not every poll.
            changed = (
                previous is None
                or previous["uploaded"] != uploaded
                or previous["downloaded"] != downloaded
                or previous["num_complete"] != seeders
                or previous["num_incomplete"] != leechers
            )
            if tier or changed:
                self.conn.execute(
                    "INSERT OR REPLACE INTO torrent_samples "
                    "(hash, ts, tier, uploaded, downloaded, ratio, popularity, seeders, leechers) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (torrent_hash, now, tier, uploaded, downloaded, ratio, popularity, seeders, leechers),
                )
            if up_delta or dl_delta:
                for table, column, bucket in (
                    ("torrent_hourly", "hour", hour),
                    ("torrent_daily", "day", day),
                ):
                    self.conn.execute(
                        f"INSERT INTO {table} (hash, {column}, up, dl) VALUES (?, ?, ?, ?) "
                        f"ON CONFLICT (hash, {column}) DO UPDATE SET "
                        "up = up + excluded.up, dl = dl + excluded.dl",
                        (torrent_hash, bucket, up_delta, dl_delta),
                    )

        for torrent_hash, row in known.items():
            if torrent_hash not in seen and row["removed_at"] is None:
                self.conn.execute(
                    "UPDATE torrents SET removed_at = ? WHERE hash = ?", (now, torrent_hash)
                )
        db.set_meta(self.conn, "last_torrent_poll", now)

    def prune(self, now: int) -> None:
        """Delete only what the retention chosen by the user allows: nothing by default."""
        settings.delete_expired(self.conn, settings.load_retention(self.conn), now)
        self._last_prune = now

    def tick(self, now: int, include_torrents: bool) -> None:
        self.poll_global(now)
        self.poll_live(now)
        if include_torrents:
            self.poll_torrents(now)
        if self.state.take_force_prune() or now - self._last_prune >= PRUNE_INTERVAL:
            self.prune(now)
        with self.state.lock:
            self.state.last_success = now
            self.state.last_error = ""
            if include_torrents:
                self.state.last_full_success = now

    def run(self, stop: threading.Event) -> None:
        """Poll until ``stop`` is set. Errors are logged and never end the loop."""
        last_torrent_poll = 0.0
        logged_in = False
        while not stop.is_set():
            started = time.time()
            intervals = settings.load(self.conn, self.config)
            wait = intervals.global_interval
            auth_failed = False
            try:
                if not logged_in:
                    self.client.login()
                    logged_in = True
                # One second of tolerance, or a slight drift would skip a whole cycle.
                forced = self.state.take_force_full()
                include_torrents = forced or started - last_torrent_poll >= intervals.torrent_interval - 1
                self.tick(int(started), include_torrents)
                if include_torrents:
                    last_torrent_poll = started
            except QbitAuthError as exc:
                logged_in = False
                auth_failed = True
                self._record_error(f"Authentification refusee : {exc}")
            except QbitError as exc:
                wait = ERROR_BACKOFF
                self._record_error(str(exc))
            except sqlite3.Error as exc:
                wait = ERROR_BACKOFF
                self._record_error(f"Base de donnees : {exc}")
            if auth_failed:
                # Saving settings must not shorten this wait: retries could get the IP banned.
                stop.wait(AUTH_FAILURE_BACKOFF)
                continue
            self.state.wake.wait(max(1.0, wait - (time.time() - started)))
            self.state.wake.clear()

    def _record_error(self, message: str) -> None:
        log.warning("%s", message)
        with self.state.lock:
            self.state.last_error = message
