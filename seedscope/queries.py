"""Read-only queries behind the JSON API."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any

from .collector import counter_delta, local_day

DAY = 86400
RANGES = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": DAY,
    "3d": 3 * DAY,
    "7d": 7 * DAY,
    "30d": 30 * DAY,
    "90d": 90 * DAY,
    "180d": 180 * DAY,
    "1y": 365 * DAY,
    "all": None,
}
# Line charts: source table and bucket size, chosen to stay near 1,000 points or less.
SERIES_PLAN = {
    "1h": ("minute", 60),
    "6h": ("minute", 60),
    "24h": ("minute", 60),
    "3d": ("minute", 300),
    "7d": ("minute", 900),
    "30d": ("hour", 3600),
    "90d": ("hour", 3 * 3600),
    "180d": ("hour", 6 * 3600),
    "1y": ("day", 0),
    "all": ("day", 0),
}
# Column charts: bucket kind and bucket count.
VOLUME_PLAN = {
    "1h": ("min5", 12),
    "6h": ("min30", 12),
    "24h": ("hour", 24),
    "3d": ("hour6", 12),
    "7d": ("day", 7),
    "30d": ("day", 30),
    "90d": ("week", 13),
    "180d": ("week", 26),
    "1y": ("month", 12),
    "all": ("month", None),
}
# Per-torrent deltas exist per hour at best: short ranges cannot go finer.
TORRENT_VOLUME_OVERRIDES = {"1h": ("hour", 2), "6h": ("hour", 7)}
STEPS = {"min5": 300, "min30": 1800, "hour": 3600, "hour6": 6 * 3600}
HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
MAX_STACK_SERIES = 7  # the palette has 8 slots: the last one is kept for "others"
MINUTE_TOTALS_MAX = 7 * DAY


class BadRequest(ValueError):
    """The client sent an invalid parameter."""


def _check_range(value: str) -> str:
    if value not in RANGES:
        raise BadRequest(f"Periode inconnue : {value}")
    return value


def _sum_between(conn: sqlite3.Connection, start: int, end: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(up_bytes), 0) FROM global_hourly WHERE hour >= ? AND hour < ?",
        (start, end),
    ).fetchone()
    return row[0]


def _unique_size(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM ("
        "SELECT MAX(size) AS size FROM torrents WHERE removed_at IS NULL GROUP BY content_key)"
    ).fetchone()[0]


def summary(conn: sqlite3.Connection, now: int, live: dict[str, Any]) -> dict[str, Any]:
    latest = conn.execute("SELECT * FROM global_minute ORDER BY ts DESC LIMIT 1").fetchone()
    first_hour = conn.execute("SELECT MIN(hour) FROM global_hourly").fetchone()[0]
    counts = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS size, COUNT(DISTINCT content_key) AS contents "
        "FROM torrents WHERE removed_at IS NULL"
    ).fetchone()
    today = conn.execute(
        "SELECT COALESCE(SUM(up_bytes), 0), COALESCE(SUM(dl_bytes), 0) "
        "FROM global_hourly WHERE day = ?",
        (local_day(now),),
    ).fetchone()

    current_hour = now - now % 3600
    end = current_hour + 3600
    periods: dict[str, dict[str, Any]] = {}
    for key, days in (("7d", 7), ("30d", 30)):
        start = end - days * DAY
        previous_start = start - days * DAY
        previous = None
        if first_hour is not None and first_hour <= previous_start:
            previous = _sum_between(conn, previous_start, start)
        periods[key] = {"up": _sum_between(conn, start, end), "previous": previous}

    alltime_ul = latest["alltime_ul"] if latest else 0
    alltime_dl = latest["alltime_dl"] if latest else 0
    return {
        "now": now,
        "last_sample": latest["ts"] if latest else None,
        "first_hour": first_hour,
        "ratio": round(alltime_ul / alltime_dl, 3) if alltime_dl else None,
        "alltime_ul": alltime_ul,
        "alltime_dl": alltime_dl,
        "up_speed": latest["up_speed"] if latest else 0,
        "dl_speed": latest["dl_speed"] if latest else 0,
        "peers": latest["peers"] if latest else 0,
        "free_space": latest["free_space"] if latest else 0,
        "up_today": today[0],
        "dl_today": today[1],
        "up_7d": periods["7d"]["up"],
        "up_7d_previous": periods["7d"]["previous"],
        "up_30d": periods["30d"]["up"],
        "up_30d_previous": periods["30d"]["previous"],
        "torrents": counts["n"],
        "contents": counts["contents"],
        "size_total": counts["size"],
        "size_unique": _unique_size(conn),
        "active": len(live.get("active", [])),
        "connection_status": live.get("connection_status", ""),
        "collector_error": live.get("last_error", ""),
        "collector_last_success": live.get("last_success", 0),
        "last_full_success": live.get("last_full_success", 0),
    }


def _series_rows(conn: sqlite3.Connection, range_key: str, now: int) -> tuple[str, list[sqlite3.Row]]:
    seconds = RANGES[range_key]
    start = now - seconds if seconds else 0
    source, step = SERIES_PLAN[range_key]
    if source == "minute":
        rows = conn.execute(
            "SELECT ts - ts % :step AS ts, CAST(AVG(up_speed) AS INTEGER) AS up, "
            "CAST(AVG(dl_speed) AS INTEGER) AS dl, MAX(alltime_ul) AS alltime_ul, "
            "MAX(alltime_dl) AS alltime_dl FROM global_minute WHERE ts >= :start "
            "GROUP BY ts - ts % :step ORDER BY 1",
            {"step": step, "start": start},
        ).fetchall()
        return ("minute" if step == 60 else f"{step // 60}min"), rows
    if source == "hour":
        rows = conn.execute(
            "SELECT hour - hour % :step AS ts, SUM(up_speed_sum) / MAX(SUM(samples), 1) AS up, "
            "SUM(dl_speed_sum) / MAX(SUM(samples), 1) AS dl, MAX(alltime_ul) AS alltime_ul, "
            "MAX(alltime_dl) AS alltime_dl FROM global_hourly WHERE hour >= :start "
            "GROUP BY hour - hour % :step ORDER BY 1",
            {"step": step, "start": start},
        ).fetchall()
        return ("hour" if step == 3600 else f"{step // 3600}h"), rows
    return "day", conn.execute(
        "SELECT MIN(hour) AS ts, SUM(up_speed_sum) / MAX(SUM(samples), 1) AS up, "
        "SUM(dl_speed_sum) / MAX(SUM(samples), 1) AS dl, "
        "MAX(alltime_ul) AS alltime_ul, MAX(alltime_dl) AS alltime_dl "
        "FROM global_hourly WHERE hour >= ? GROUP BY day ORDER BY day",
        (start,),
    ).fetchall()


def _day_keys(first: str, last: str) -> list[str]:
    day = dt.date.fromisoformat(first)
    end = dt.date.fromisoformat(last)
    keys = []
    while day <= end:
        keys.append(day.isoformat())
        day += dt.timedelta(days=1)
    return keys


def _month_keys(first: str, last: str) -> list[str]:
    year, month = int(first[:4]), int(first[5:7])
    keys = []
    while f"{year:04d}-{month:02d}" <= last[:7]:
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return keys


def _week_key(day: str) -> str:
    """Monday of the week containing ``day``."""
    date = dt.date.fromisoformat(day)
    return (date - dt.timedelta(days=date.weekday())).isoformat()


def _buckets(
    conn: sqlite3.Connection, range_key: str, now: int, per_torrent: bool = False
) -> tuple[str, list[Any]]:
    """Return the bucket kind used by column charts and all of its keys, gaps included."""
    plan = (TORRENT_VOLUME_OVERRIDES.get(range_key) if per_torrent else None) or VOLUME_PLAN[range_key]
    kind, count = plan
    if kind in STEPS:
        step = STEPS[kind]
        end = now - now % step
        return kind, [end - step * i for i in range(count - 1, -1, -1)]
    today = local_day(now)
    if kind == "day":
        return kind, _day_keys(local_day(now - (count - 1) * DAY), today)
    if kind == "week":
        last = dt.date.fromisoformat(_week_key(today))
        return kind, [(last - dt.timedelta(weeks=i)).isoformat() for i in range(count - 1, -1, -1)]
    if count is not None:
        return kind, _month_keys(local_day(now - 364 * DAY), today)
    first = conn.execute("SELECT MIN(day) FROM global_hourly").fetchone()[0] or today
    # min(): a clock moved backwards must not leave an empty bucket list.
    return kind, _month_keys(min(first, today), today)


def _bucket_key(kind: str, value: Any) -> Any:
    if kind in STEPS:
        return value - value % STEPS[kind]
    if kind == "day":
        return value
    if kind == "week":
        return _week_key(value)
    return value[:7]


def _first_day(kind: str, keys: list[str]) -> str:
    return keys[0] + "-01" if kind == "month" else keys[0]


def _global_volume(conn: sqlite3.Connection, kind: str, keys: list[Any]) -> dict[str, list[int]]:
    position = {key: index for index, key in enumerate(keys)}
    up = [0] * len(keys)
    dl = [0] * len(keys)

    def add(bucket: Any, up_bytes: int, dl_bytes: int) -> None:
        index = position.get(bucket)
        if index is not None:
            up[index] += up_bytes
            dl[index] += dl_bytes

    if kind in ("min5", "min30"):
        # Hourly rollups are too coarse here: rebuild deltas from the counters.
        previous = None
        for row in conn.execute(
            "SELECT ts, payload_ul, alltime_dl FROM global_minute WHERE ts >= ? ORDER BY ts",
            (keys[0] - STEPS[kind],),
        ):
            if previous is not None:
                add(
                    _bucket_key(kind, row["ts"]),
                    counter_delta(previous["payload_ul"], row["payload_ul"]),
                    counter_delta(previous["alltime_dl"], row["alltime_dl"]),
                )
            previous = row
    elif kind in STEPS:
        for row in conn.execute(
            "SELECT hour, up_bytes, dl_bytes FROM global_hourly WHERE hour >= ?", (keys[0],)
        ):
            add(_bucket_key(kind, row["hour"]), row["up_bytes"], row["dl_bytes"])
    else:
        for row in conn.execute(
            "SELECT day, SUM(up_bytes) AS up, SUM(dl_bytes) AS dl "
            "FROM global_hourly WHERE day >= ? GROUP BY day",
            (_first_day(kind, keys),),
        ):
            add(_bucket_key(kind, row["day"]), row["up"], row["dl"])
    return {"up": up, "dl": dl}


def _period_totals(conn: sqlite3.Connection, start: int, end: int) -> dict[str, int]:
    """Bytes transferred and peak speed between two timestamps."""
    if end - start <= MINUTE_TOTALS_MAX:
        first = conn.execute(
            "SELECT payload_ul, alltime_dl FROM global_minute WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT 1",
            (start, end),
        ).fetchone()
        last = conn.execute(
            "SELECT payload_ul, alltime_dl FROM global_minute WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (start, end),
        ).fetchone()
        peak = conn.execute(
            "SELECT COALESCE(MAX(up_speed), 0) FROM global_minute WHERE ts >= ? AND ts <= ?",
            (start, end),
        ).fetchone()[0]
        if first is None:
            return {"up": 0, "dl": 0, "peak": 0}
        return {
            "up": counter_delta(first["payload_ul"], last["payload_ul"]),
            "dl": counter_delta(first["alltime_dl"], last["alltime_dl"]),
            "peak": peak,
        }
    row = conn.execute(
        "SELECT COALESCE(SUM(up_bytes), 0) AS up, COALESCE(SUM(dl_bytes), 0) AS dl, "
        "COALESCE(MAX(up_speed_max), 0) AS peak FROM global_hourly WHERE hour >= ? AND hour <= ?",
        (start - start % 3600, end),
    ).fetchone()
    return dict(row)


def _period_stats(conn: sqlite3.Connection, range_key: str, now: int) -> dict[str, Any]:
    seconds = RANGES[range_key]
    first = conn.execute("SELECT MIN(hour) FROM global_hourly").fetchone()[0]
    start = now - seconds if seconds else (first if first is not None else now)
    totals = _period_totals(conn, start, now)
    previous = None
    if seconds and first is not None and first <= start - seconds:
        previous = _period_totals(conn, start - seconds, start)["up"]
    covered = max(1, now - max(start, first if first is not None else now))
    return {
        "up": totals["up"],
        "dl": totals["dl"],
        "ratio": round(totals["up"] / totals["dl"], 3) if totals["dl"] else None,
        "avg_up": totals["up"] // covered,
        "peak_up": totals["peak"],
        "previous_up": previous,
    }


def timeseries(conn: sqlite3.Connection, range_key: str, now: int) -> dict[str, Any]:
    _check_range(range_key)
    resolution, rows = _series_rows(conn, range_key, now)
    kind, keys = _buckets(conn, range_key, now)
    return {
        "resolution": resolution,
        "ts": [row["ts"] for row in rows],
        "up": [row["up"] for row in rows],
        "dl": [row["dl"] for row in rows],
        "alltime_ul": [row["alltime_ul"] for row in rows],
        "alltime_dl": [row["alltime_dl"] for row in rows],
        "ratio": [
            round(row["alltime_ul"] / row["alltime_dl"], 4) if row["alltime_dl"] else None
            for row in rows
        ],
        "volume": {"kind": kind, "keys": keys, **_global_volume(conn, kind, keys)},
        "stats": _period_stats(conn, range_key, now),
    }


def calendar(conn: sqlite3.Connection, now: int, days: int = 371) -> dict[str, Any]:
    start = local_day(now - (days - 1) * DAY)
    rows = conn.execute(
        "SELECT day, SUM(up_bytes) AS up FROM global_hourly WHERE day >= ? GROUP BY day ORDER BY day",
        (start,),
    ).fetchall()
    # Start at the first recorded day: a year of empty cells would push the real data out of view.
    if rows:
        start = rows[0]["day"]
    return {"start": start, "end": local_day(now), "days": [dict(row) for row in rows]}


def heatmap(conn: sqlite3.Connection, now: int, days: int = 90) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT weekday, hour_of_day AS hour, AVG(up_bytes) AS up, COUNT(*) AS samples "
        "FROM global_hourly WHERE hour >= ? GROUP BY weekday, hour_of_day",
        (now - days * DAY,),
    ).fetchall()
    return {"days": days, "cells": [dict(row) for row in rows]}


TORRENT_SORTS = {
    "name": "t.name COLLATE NOCASE",
    "size": "t.size",
    "ratio": "t.ratio",
    "uploaded": "t.uploaded",
    "downloaded": "t.downloaded",
    "up_period": "up_period",
    "added_on": "t.added_on",
    "seeding_time": "t.seeding_time",
    "last_activity": "t.last_activity",
    "tracker": "t.tracker COLLATE NOCASE",
    "category": "t.category COLLATE NOCASE",
    "popularity": "t.popularity",
    "seeders": "t.num_complete",
    "leechers": "t.num_incomplete",
}
TORRENT_COLUMNS = (
    "t.hash, t.name, t.tracker, t.category, t.tags, t.size, t.added_on, t.completion_on, "
    "t.removed_at, t.uploaded, t.downloaded, t.ratio, t.seeding_time, t.last_activity, t.state, "
    "t.num_complete, t.num_incomplete, t.progress, t.popularity"
)
WINDOWS = (1, 7, 30, 90, 365)
SPARK_DAYS = 30


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _window_join(now: int, window: int) -> tuple[str, list[Any]]:
    """Join exposing ``w.up``: bytes sent per torrent over the last ``window`` days."""
    if window not in WINDOWS:
        raise BadRequest(f"Fenetre inconnue : {window}")
    if window == 1:
        source = "SELECT hash, SUM(up) AS up FROM torrent_hourly WHERE hour >= ? GROUP BY hash"
        value: Any = now - DAY
    else:
        source = "SELECT hash, SUM(up) AS up FROM torrent_daily WHERE day >= ? GROUP BY hash"
        value = local_day(now - (window - 1) * DAY)
    return f"LEFT JOIN ({source}) w ON w.hash = t.hash", [value]


def _sparklines(conn: sqlite3.Connection, hashes: list[str], now: int) -> dict[str, list[int]]:
    """Bytes sent per day over the last 30 days, for the rows of one table page."""
    if not hashes:
        return {}
    days = _day_keys(local_day(now - (SPARK_DAYS - 1) * DAY), local_day(now))
    position = {day: index for index, day in enumerate(days)}
    result = {torrent_hash: [0] * len(days) for torrent_hash in hashes}
    marks = ",".join("?" * len(hashes))
    for row in conn.execute(
        f"SELECT hash, day, up FROM torrent_daily WHERE hash IN ({marks}) AND day >= ?",
        [*hashes, days[0]],
    ):
        if row["day"] in position:
            result[row["hash"]][position[row["day"]]] = row["up"]
    return result


def list_torrents(
    conn: sqlite3.Connection,
    now: int,
    *,
    sort: str = "uploaded",
    descending: bool = True,
    q: str = "",
    tracker: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    status: str = "active",
    window: int = 7,
    known_swarm: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    if sort not in TORRENT_SORTS:
        raise BadRequest(f"Tri inconnu : {sort}")
    if status not in ("active", "removed", "all"):
        raise BadRequest(f"Statut inconnu : {status}")
    where: list[str] = []
    params: list[Any] = []
    if status == "active":
        where.append("t.removed_at IS NULL")
    elif status == "removed":
        where.append("t.removed_at IS NOT NULL")
    if known_swarm:
        where.append("t.num_complete >= 0")
    if q:
        where.append("t.name LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(q)}%")
    if tracker is not None:
        where.append("t.tracker = ?")
        params.append(tracker)
    if category is not None:
        where.append("t.category = ?")
        params.append(category)
    if tag is not None:
        where.append("(', ' || t.tags || ',') LIKE ? ESCAPE '\\'")
        params.append(f"%, {_like_escape(tag)},%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM torrents t {clause}", params).fetchone()[0]
    join, join_params = _window_join(now, window)
    direction = "DESC" if descending else "ASC"
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT {TORRENT_COLUMNS}, COALESCE(w.up, 0) AS up_period "
            f"FROM torrents t {join} {clause} "
            f"ORDER BY {TORRENT_SORTS[sort]} {direction}, t.hash LIMIT ? OFFSET ?",
            [*join_params, *params, limit, offset],
        )
    ]
    sparks = _sparklines(conn, [row["hash"] for row in rows], now)
    for row in rows:
        row["spark"] = sparks[row["hash"]]
    return {"total": total, "window": window, "rows": rows}


def filters(conn: sqlite3.Connection) -> dict[str, Any]:
    def distinct(column: str) -> list[str]:
        return [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT {column} FROM torrents WHERE removed_at IS NULL "
                f"ORDER BY {column} COLLATE NOCASE"
            )
        ]

    tags = sorted(
        {tag.strip() for value in distinct("tags") for tag in value.split(",") if tag.strip()},
        key=str.lower,
    )
    return {"trackers": distinct("tracker"), "categories": distinct("category"), "tags": tags}


def _sample_tier(range_key: str) -> int:
    """Finest sample tier kept for the whole range, which also caps the point count."""
    seconds = RANGES[range_key]
    if seconds is not None and seconds <= 30 * DAY:
        return 0
    if seconds is not None and seconds <= 180 * DAY:
        return 1
    return 2


def _swarm_value(value: int) -> int | None:
    return value if value >= 0 else None  # qBittorrent answers -1 when it does not know


def _torrent_volume(
    conn: sqlite3.Connection, torrent_hash: str, range_key: str, now: int
) -> dict[str, Any]:
    kind, keys = _buckets(conn, range_key, now, per_torrent=True)
    position = {key: index for index, key in enumerate(keys)}
    up = [0] * len(keys)
    dl = [0] * len(keys)
    if kind in STEPS:
        rows = conn.execute(
            "SELECT hour AS bucket, up, dl FROM torrent_hourly WHERE hash = ? AND hour >= ?",
            (torrent_hash, keys[0]),
        )
    else:
        rows = conn.execute(
            "SELECT day AS bucket, up, dl FROM torrent_daily WHERE hash = ? AND day >= ?",
            (torrent_hash, _first_day(kind, keys)),
        )
    for row in rows:
        index = position.get(_bucket_key(kind, row["bucket"]))
        if index is not None:
            up[index] += row["up"]
            dl[index] += row["dl"]
    return {"kind": kind, "keys": keys, "up": up, "dl": dl}


def torrent_detail(
    conn: sqlite3.Connection, torrent_hash: str, range_key: str, now: int
) -> dict[str, Any] | None:
    if not HASH_RE.match(torrent_hash):
        raise BadRequest("Hash invalide")
    _check_range(range_key)
    row = conn.execute(
        f"SELECT {TORRENT_COLUMNS}, t.content_key, t.first_seen, t.last_seen, t.time_active "
        "FROM torrents t WHERE t.hash = ?",
        (torrent_hash,),
    ).fetchone()
    if row is None:
        return None
    seconds = RANGES[range_key]
    start = now - seconds if seconds else 0
    samples = [
        dict(sample)
        for sample in conn.execute(
            "SELECT ts, uploaded, ratio, popularity, seeders, leechers "
            "FROM torrent_samples WHERE hash = ? AND ts >= ? AND tier >= ? ORDER BY ts",
            (torrent_hash, start, _sample_tier(range_key)),
        )
    ]
    # Samples are stored on change: extend the last known values to both ends of the
    # range, so that an idle torrent shows a flat line instead of an empty chart.
    before = conn.execute(
        "SELECT uploaded, ratio, popularity, seeders, leechers FROM torrent_samples "
        "WHERE hash = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (torrent_hash, start),
    ).fetchone()
    if before is not None:
        samples.insert(0, {"ts": start, **dict(before)})
    last_ts = samples[-1]["ts"] if samples else None
    if row["removed_at"] is None and last_ts is not None and row["last_seen"] > last_ts:
        samples.append(
            {
                "ts": row["last_seen"],
                "uploaded": row["uploaded"],
                "ratio": row["ratio"],
                "popularity": row["popularity"],
                "seeders": row["num_complete"],
                "leechers": row["num_incomplete"],
            }
        )
    first_sample = conn.execute(
        "SELECT MIN(ts) FROM torrent_samples WHERE hash = ?", (torrent_hash,)
    ).fetchone()[0]
    siblings = conn.execute(
        "SELECT hash, name, tracker, uploaded, ratio, popularity FROM torrents "
        "WHERE content_key = ? AND hash != ? AND removed_at IS NULL ORDER BY uploaded DESC",
        (row["content_key"], torrent_hash),
    ).fetchall()
    volume = _torrent_volume(conn, torrent_hash, range_key, now)
    detail = dict(row)
    age = max(1, now - row["added_on"]) if row["added_on"] > 0 else None
    return {
        "torrent": detail,
        "first_sample": first_sample,
        "up_per_day": row["uploaded"] * DAY // age if age else None,
        "up_period": sum(volume["up"]),
        "series": {
            "ts": [s["ts"] for s in samples],
            "uploaded": [s["uploaded"] for s in samples],
            "ratio": [s["ratio"] for s in samples],
            "popularity": [s["popularity"] for s in samples],
            "seeders": [_swarm_value(s["seeders"]) for s in samples],
            "leechers": [_swarm_value(s["leechers"]) for s in samples],
        },
        "volume": volume,
        "siblings": [dict(item) for item in siblings],
    }


CONTENT_SORTS = {
    "name": "name COLLATE NOCASE",
    "size": "size",
    "uploaded": "uploaded",
    "downloaded": "downloaded",
    "ratio": "ratio",
    "efficiency": "efficiency",
    "up_period": "up_period",
    "last_activity": "last_activity",
    "added_on": "added_on",
    "seeding_time": "seeding_time",
    "torrents": "torrents",
    "popularity": "popularity",
    "seeders": "seeders",
    "leechers": "leechers",
}


def _content_sparklines(conn: sqlite3.Connection, keys: list[str], now: int) -> dict[str, list[int]]:
    """Bytes sent per day over the last 30 days, for the rows of one table page."""
    if not keys:
        return {}
    days = _day_keys(local_day(now - (SPARK_DAYS - 1) * DAY), local_day(now))
    position = {day: index for index, day in enumerate(days)}
    result = {key: [0] * len(days) for key in keys}
    marks = ",".join("?" * len(keys))
    for row in conn.execute(
        f"SELECT t.content_key AS key, d.day AS day, SUM(d.up) AS up FROM torrent_daily d "
        f"JOIN torrents t ON t.hash = d.hash WHERE t.content_key IN ({marks}) AND d.day >= ? "
        "GROUP BY t.content_key, d.day",
        [*keys, days[0]],
    ):
        if row["day"] in position:
            result[row["key"]][position[row["day"]]] = row["up"]
    return result


def list_contents(
    conn: sqlite3.Connection,
    now: int,
    *,
    sort: str = "efficiency",
    descending: bool = True,
    q: str = "",
    tracker: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    dead_days: int | None = None,
    known_swarm: bool = False,
    window: int = 30,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Group torrents by content: deleting one cross-seed alone frees no disk space."""
    if sort not in CONTENT_SORTS:
        raise BadRequest(f"Tri inconnu : {sort}")
    join, params = _window_join(now, window)
    # A content matches when any of its torrents does, and all of them stay in the totals.
    matches: list[str] = []
    if q:
        matches.append("name LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(q)}%")
    if tracker is not None:
        matches.append("tracker = ?")
        params.append(tracker)
    if category is not None:
        matches.append("category = ?")
        params.append(category)
    if tag is not None:
        matches.append("(', ' || tags || ',') LIKE ? ESCAPE '\\'")
        params.append(f"%, {_like_escape(tag)},%")
    filters_sql = "".join(
        f" AND t.content_key IN (SELECT content_key FROM torrents WHERE removed_at IS NULL AND {match})"
        for match in matches
    )
    conditions: list[str] = []
    if dead_days is not None:
        limit_ts = now - dead_days * DAY
        conditions.append("last_activity < ? AND added_on < ?")
        params += [limit_ts, limit_ts]
    if known_swarm:
        conditions.append("seeders >= 0")
    having = f"HAVING {' AND '.join(conditions)}" if conditions else ""
    base = f"""
        SELECT t.content_key AS content_key, MIN(t.added_on) AS added_on, MIN(t.name) AS name,
            MAX(t.size) AS size, COUNT(*) AS torrents, GROUP_CONCAT(DISTINCT t.tracker) AS trackers,
            SUM(t.uploaded) AS uploaded, SUM(t.downloaded) AS downloaded,
            CASE WHEN SUM(t.downloaded) > 0 THEN SUM(t.uploaded) * 1.0 / SUM(t.downloaded) END AS ratio,
            SUM(t.uploaded) * 1.0 / MAX(MAX(t.size), 1) AS efficiency,
            SUM(COALESCE(w.up, 0)) AS up_period,
            MAX(CASE WHEN t.last_activity > 0 THEN t.last_activity ELSE t.added_on END) AS last_activity,
            MAX(t.seeding_time) AS seeding_time, MAX(t.popularity) AS popularity,
            MAX(t.num_complete) AS seeders, SUM(MAX(t.num_incomplete, 0)) AS leechers,
            MIN(t.hash) AS hash
        FROM torrents t
        {join}
        WHERE t.removed_at IS NULL{filters_sql}
        GROUP BY t.content_key
        {having}
    """
    totals = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS size FROM ({base})", params
    ).fetchone()
    direction = "DESC" if descending else "ASC"
    rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM ({base}) ORDER BY {CONTENT_SORTS[sort]} {direction}, hash LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
    ]
    sparks = _content_sparklines(conn, [row["content_key"] for row in rows], now)
    for row in rows:
        row["spark"] = sparks[row["content_key"]]
    return {
        "total": totals["n"],
        "total_size": totals["size"],
        "all_size": _unique_size(conn),
        "window": window,
        "rows": rows,
    }


def contents_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS contents, COALESCE(SUM(size), 0) AS size,
            COALESCE(SUM(uploaded), 0) AS uploaded,
            COALESCE(SUM(uploaded = 0), 0) AS never_uploaded,
            COALESCE(SUM(CASE WHEN uploaded = 0 THEN size ELSE 0 END), 0) AS never_uploaded_size,
            COALESCE(SUM(torrents > 1), 0) AS cross_seeded
        FROM (
            SELECT MAX(size) AS size, SUM(uploaded) AS uploaded, COUNT(*) AS torrents
            FROM torrents WHERE removed_at IS NULL GROUP BY content_key
        )
        """
    ).fetchone()
    result = dict(row)
    result["efficiency"] = round(row["uploaded"] / row["size"], 3) if row["size"] else None
    return result


def top_contents(conn: sqlite3.Connection, range_key: str, now: int, limit: int = 10) -> dict[str, Any]:
    """Contents that sent the most over a range; hourly data only on short ranges, as its
    retention can be limited to 30 days."""
    _check_range(range_key)
    seconds = RANGES[range_key]
    if seconds is not None and seconds <= 30 * DAY:
        start = now - seconds
        source = "SELECT hash, SUM(up) AS up FROM torrent_hourly WHERE hour >= ? GROUP BY hash"
        value: Any = start - start % 3600
    else:
        source = "SELECT hash, SUM(up) AS up FROM torrent_daily WHERE day >= ? GROUP BY hash"
        value = local_day(now - seconds) if seconds else "0000-00-00"
    rows = conn.execute(
        f"""
        SELECT t.content_key AS content_key, MIN(t.name) AS name, MAX(t.size) AS size,
            COUNT(*) AS torrents, GROUP_CONCAT(DISTINCT t.tracker) AS trackers,
            COALESCE(SUM(w.up), 0) AS up_period, SUM(t.uploaded) AS uploaded,
            SUM(t.uploaded) * 1.0 / MAX(MAX(t.size), 1) AS efficiency,
            MAX(t.popularity) AS popularity
        FROM torrents t LEFT JOIN ({source}) w ON w.hash = t.hash
        GROUP BY t.content_key HAVING COALESCE(SUM(w.up), 0) > 0
        ORDER BY up_period DESC, content_key LIMIT ?
        """,
        (value, limit),
    ).fetchall()
    return {"rows": [dict(row) for row in rows]}


def _tracker_slots(conn: sqlite3.Connection) -> dict[str, int]:
    """Same colors as the tracker page: top trackers by upload, ordered by name."""
    top = [
        row[0]
        for row in conn.execute(
            "SELECT tracker FROM torrents GROUP BY tracker ORDER BY SUM(uploaded) DESC LIMIT ?",
            (MAX_STACK_SERIES,),
        )
    ]
    return {key: index for index, key in enumerate(sorted(top, key=str.lower))}


CONTENT_CURVE_POINTS = 400


def _content_series(
    conn: sqlite3.Connection,
    torrents: list[dict[str, Any]],
    size: int,
    slots: dict[str, int],
    range_key: str,
    now: int,
) -> dict[str, Any]:
    """Add up per-torrent samples into one curve, holding each torrent's last known value."""
    hashes = [torrent["hash"] for torrent in torrents]
    tracker_of = {t["hash"]: (t["tracker"] if t["tracker"] in slots else None) for t in torrents}
    seconds = RANGES[range_key]
    start = now - seconds if seconds else 0
    columns = "uploaded, popularity, seeders, leechers"
    state: dict[str, dict[str, Any]] = {}
    has_baseline = False
    for torrent in torrents:
        before = conn.execute(
            f"SELECT {columns} FROM torrent_samples WHERE hash = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
            (torrent["hash"], start),
        ).fetchone()
        has_baseline = has_baseline or before is not None
        # No older value: its first sample avoids a jump when this torrent appears in the sum.
        first = before or conn.execute(
            f"SELECT {columns} FROM torrent_samples WHERE hash = ? ORDER BY ts LIMIT 1",
            (torrent["hash"],),
        ).fetchone()
        state[torrent["hash"]] = (
            dict(first)
            if first
            else {
                "uploaded": torrent["uploaded"],
                "popularity": torrent["popularity"],
                "seeders": torrent["num_complete"],
                "leechers": torrent["num_incomplete"],
            }
        )

    points: list[dict[str, Any]] = []
    keys = sorted({tracker_of[h] for h in hashes}, key=lambda key: (key is None, (key or "").lower()))

    def snapshot(ts: int) -> dict[str, Any]:
        seeders = [s["seeders"] for s in state.values() if s["seeders"] >= 0]
        leechers = [s["leechers"] for s in state.values() if s["leechers"] >= 0]
        by_tracker = {key: 0 for key in keys}
        for torrent_hash, values in state.items():
            by_tracker[tracker_of[torrent_hash]] += values["uploaded"]
        uploaded = sum(by_tracker.values())
        return {
            "ts": ts,
            "uploaded": uploaded,
            "efficiency": uploaded / max(size, 1),
            "popularity": max(s["popularity"] for s in state.values()),
            "seeders": sum(seeders) if seeders else None,
            "leechers": sum(leechers) if leechers else None,
            "by_tracker": by_tracker,
        }

    if has_baseline:
        points.append(snapshot(start))
    span = seconds if seconds else max(now - min(t["first_seen"] for t in torrents), 60)
    step = max(60, span // CONTENT_CURVE_POINTS)
    marks = ",".join("?" * len(hashes))
    last_bucket = None
    for sample in conn.execute(
        f"SELECT hash, ts, {columns} FROM torrent_samples WHERE hash IN ({marks}) "
        "AND ts >= ? AND tier >= ? ORDER BY ts",
        [*hashes, start, _sample_tier(range_key)],
    ):
        state[sample["hash"]] = dict(sample)
        bucket = sample["ts"] // step
        # The very first point stays, so that a recent content still draws a line.
        if bucket == last_bucket and len(points) >= 2:
            points[-1] = snapshot(sample["ts"])
        else:
            points.append(snapshot(sample["ts"]))
        last_bucket = bucket

    # Close the curve on the current values, as samples are only stored on change.
    active = [t for t in torrents if t["removed_at"] is None]
    if active and points and max(t["last_seen"] for t in active) > points[-1]["ts"]:
        for torrent in active:
            state[torrent["hash"]] = {
                "uploaded": torrent["uploaded"],
                "popularity": torrent["popularity"],
                "seeders": torrent["num_complete"],
                "leechers": torrent["num_incomplete"],
            }
        points.append(snapshot(max(t["last_seen"] for t in active)))

    return {
        "ts": [point["ts"] for point in points],
        "uploaded": [point["uploaded"] for point in points],
        "efficiency": [point["efficiency"] for point in points],
        "popularity": [point["popularity"] for point in points],
        "seeders": [point["seeders"] for point in points],
        "leechers": [point["leechers"] for point in points],
        "by_tracker": [
            {
                "key": key,
                "slot": slots[key] if key is not None else MAX_STACK_SERIES,
                "values": [point["by_tracker"][key] for point in points],
            }
            for key in keys
        ],
    }


def _content_volume(
    conn: sqlite3.Connection,
    torrents: list[dict[str, Any]],
    slots: dict[str, int],
    range_key: str,
    now: int,
) -> dict[str, Any]:
    kind, keys = _buckets(conn, range_key, now, per_torrent=True)
    position = {key: index for index, key in enumerate(keys)}
    tracker_of = {t["hash"]: (t["tracker"] if t["tracker"] in slots else None) for t in torrents}
    names = sorted(set(tracker_of.values()), key=lambda key: (key is None, (key or "").lower()))
    series = {name: [0] * len(keys) for name in names}
    dl = [0] * len(keys)
    marks = ",".join("?" * len(torrents))
    hashes = [t["hash"] for t in torrents]
    if kind in STEPS:
        rows = conn.execute(
            f"SELECT hash, hour AS bucket, up, dl FROM torrent_hourly WHERE hash IN ({marks}) AND hour >= ?",
            [*hashes, keys[0]],
        )
    else:
        rows = conn.execute(
            f"SELECT hash, day AS bucket, up, dl FROM torrent_daily WHERE hash IN ({marks}) AND day >= ?",
            [*hashes, _first_day(kind, keys)],
        )
    for row in rows:
        index = position.get(_bucket_key(kind, row["bucket"]))
        if index is not None:
            series[tracker_of[row["hash"]]][index] += row["up"]
            dl[index] += row["dl"]
    return {
        "kind": kind,
        "keys": keys,
        "series": [
            {
                "key": name,
                "slot": slots[name] if name is not None else MAX_STACK_SERIES,
                "values": series[name],
            }
            for name in names
        ],
        "dl": dl,
    }


def content_detail(
    conn: sqlite3.Connection, key: str, range_key: str, now: int
) -> dict[str, Any] | None:
    _check_range(range_key)
    if not key or len(key) > 1000:
        raise BadRequest("Contenu invalide")
    every = [
        dict(row)
        for row in conn.execute(
            f"SELECT {TORRENT_COLUMNS}, t.first_seen, t.last_seen FROM torrents t "
            "WHERE t.content_key = ? ORDER BY t.uploaded DESC",
            (key,),
        )
    ]
    if not every:
        return None
    active = [t for t in every if t["removed_at"] is None]
    size = max(t["size"] for t in every)
    slots = _tracker_slots(conn)
    sparks = _sparklines(conn, [t["hash"] for t in every], now)
    for torrent in every:
        torrent["spark"] = sparks[torrent["hash"]]
    volume = _content_volume(conn, every, slots, range_key, now)
    series = _content_series(conn, every, size, slots, range_key, now)
    uploaded = sum(t["uploaded"] for t in every)
    downloaded = sum(t["downloaded"] for t in every)
    scope = active or every
    added = min((t["added_on"] for t in every if t["added_on"] > 0), default=0)
    first_sample = conn.execute(
        "SELECT MIN(s.ts) FROM torrent_samples s JOIN torrents t ON t.hash = s.hash "
        "WHERE t.content_key = ?",
        (key,),
    ).fetchone()[0]
    return {
        "content": {
            "key": key,
            "name": min(t["name"] for t in every),
            "size": size,
            "torrents": len(every),
            "active": len(active),
            "trackers": sorted({t["tracker"] for t in scope}, key=str.lower),
            "categories": sorted({t["category"] for t in scope if t["category"]}, key=str.lower),
            "uploaded": uploaded,
            "downloaded": downloaded,
            "ratio": round(uploaded / downloaded, 3) if downloaded else None,
            "efficiency": round(uploaded / max(size, 1), 3),
            "up_per_day": uploaded * DAY // max(1, now - added) if added else None,
            "popularity": max(t["popularity"] for t in scope),
            "seeders": max(t["num_complete"] for t in scope),
            "leechers": sum(max(t["num_incomplete"], 0) for t in scope),
            "added_on": added,
            "last_activity": max(t["last_activity"] for t in every),
            "seeding_time": max(t["seeding_time"] for t in every),
        },
        "first_sample": first_sample,
        "up_period": sum(sum(item["values"]) for item in volume["series"]),
        "series": series,
        "volume": volume,
        "torrents": every,
    }


def popularity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Current demand, overall and per tracker."""
    totals = conn.execute(
        """
        SELECT COALESCE(SUM(num_incomplete > 0), 0) AS wanted,
            COALESCE(SUM(MAX(num_incomplete, 0)), 0) AS leechers,
            COALESCE(SUM(num_complete BETWEEN 0 AND 1), 0) AS lonely,
            COALESCE(AVG(popularity), 0) AS popularity
        FROM torrents WHERE removed_at IS NULL
        """
    ).fetchone()
    trackers = conn.execute(
        """
        SELECT tracker AS key, COUNT(*) AS torrents, AVG(popularity) AS popularity,
            SUM(MAX(num_incomplete, 0)) AS leechers,
            AVG(CASE WHEN num_complete >= 0 THEN num_complete END) AS seeders
        FROM torrents WHERE removed_at IS NULL
        GROUP BY tracker ORDER BY popularity DESC
        """
    ).fetchall()
    return {**dict(totals), "trackers": [dict(row) for row in trackers]}


def breakdown(conn: sqlite3.Connection, dimension: str, range_key: str, now: int) -> dict[str, Any]:
    if dimension not in ("tracker", "category"):
        raise BadRequest(f"Dimension inconnue : {dimension}")
    _check_range(range_key)
    totals = conn.execute(
        f"""
        SELECT {dimension} AS key,
            SUM(removed_at IS NULL) AS torrents,
            SUM(uploaded) AS uploaded,
            SUM(downloaded) AS downloaded,
            (SELECT COALESCE(SUM(size), 0) FROM (
                SELECT MAX(size) AS size FROM torrents i
                WHERE i.{dimension} = o.{dimension} AND i.removed_at IS NULL
                GROUP BY i.content_key)) AS size_unique
        FROM torrents o GROUP BY {dimension} ORDER BY uploaded DESC
        """
    ).fetchall()

    kind, keys = _buckets(conn, range_key, now, per_torrent=True)
    if kind in STEPS:
        rows = conn.execute(
            f"SELECT t.{dimension} AS key, b.hour AS bucket, SUM(b.up) AS up "
            "FROM torrent_hourly b JOIN torrents t ON t.hash = b.hash "
            "WHERE b.hour >= ? GROUP BY key, bucket",
            (keys[0],),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT t.{dimension} AS key, b.day AS bucket, SUM(b.up) AS up "
            "FROM torrent_daily b JOIN torrents t ON t.hash = b.hash "
            "WHERE b.day >= ? GROUP BY key, bucket",
            (_first_day(kind, keys),),
        ).fetchall()

    ranked = [row["key"] for row in totals]
    shown = set(ranked[:MAX_STACK_SERIES])
    # Slots follow the name, not the rank, so a tracker keeps its color over time.
    slots = {key: index for index, key in enumerate(sorted(shown, key=str.lower))}
    index_of = {bucket: position for position, bucket in enumerate(keys)}
    grouped: dict[str, list[int]] = {key: [0] * len(keys) for key in slots}
    others = [0] * len(keys)
    period_total: dict[str, int] = {}
    for row in rows:
        position = index_of.get(_bucket_key(kind, row["bucket"]))
        if position is None:
            continue
        period_total[row["key"]] = period_total.get(row["key"], 0) + row["up"]
        target = grouped[row["key"]] if row["key"] in shown else others
        target[position] += row["up"]
    series = [
        {"key": key, "slot": slots[key], "values": grouped[key]}
        for key in sorted(slots, key=str.lower)
    ]
    if any(others):
        series.append({"key": None, "slot": MAX_STACK_SERIES, "values": others})
    return {
        "kind": kind,
        "keys": keys,
        "series": series,
        "rows": [
            {
                **dict(row),
                "ratio": round(row["uploaded"] / row["downloaded"], 3) if row["downloaded"] else None,
                "up_period": period_total.get(row["key"], 0),
            }
            for row in totals
        ],
    }
