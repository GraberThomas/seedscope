"""Read-only queries behind the JSON API."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any

from .collector import local_day

DAY = 86400
RANGES = {"24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY, "1y": 365 * DAY, "all": None}
HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
MAX_STACK_SERIES = 7  # the palette has 8 slots: the last one is kept for "others"


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
    }


def _series_rows(conn: sqlite3.Connection, range_key: str, now: int) -> tuple[str, list[sqlite3.Row]]:
    seconds = RANGES[range_key]
    start = now - seconds if seconds else 0
    if range_key == "24h":
        return "minute", conn.execute(
            "SELECT ts, up_speed AS up, dl_speed AS dl, alltime_ul, alltime_dl "
            "FROM global_minute WHERE ts >= ? ORDER BY ts",
            (start,),
        ).fetchall()
    if range_key in ("7d", "30d"):
        return "hour", conn.execute(
            "SELECT hour AS ts, up_speed_sum / MAX(samples, 1) AS up, "
            "dl_speed_sum / MAX(samples, 1) AS dl, alltime_ul, alltime_dl "
            "FROM global_hourly WHERE hour >= ? ORDER BY hour",
            (start,),
        ).fetchall()
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


def _buckets(conn: sqlite3.Connection, range_key: str, now: int) -> tuple[str, list[Any]]:
    """Return the bucket size used by volume charts and all of its keys, gaps included."""
    if range_key == "24h":
        end = now - now % 3600
        return "hour", [end - 3600 * i for i in range(23, -1, -1)]
    today = local_day(now)
    if range_key in ("7d", "30d"):
        days = RANGES[range_key] // DAY
        return "day", _day_keys(local_day(now - (days - 1) * DAY), today)
    if range_key == "1y":
        return "month", _month_keys(local_day(now - 364 * DAY), today)
    first = conn.execute("SELECT MIN(day) FROM global_hourly").fetchone()[0] or today
    # min(): a clock moved backwards must not leave an empty bucket list.
    return "month", _month_keys(min(first, today), today)


def timeseries(conn: sqlite3.Connection, range_key: str, now: int) -> dict[str, Any]:
    _check_range(range_key)
    resolution, rows = _series_rows(conn, range_key, now)
    kind, keys = _buckets(conn, range_key, now)
    if kind == "hour":
        volume_rows = conn.execute(
            "SELECT hour AS k, SUM(up_bytes) AS up, SUM(dl_bytes) AS dl "
            "FROM global_hourly WHERE hour >= ? GROUP BY k",
            (keys[0],),
        )
    elif kind == "day":
        volume_rows = conn.execute(
            "SELECT day AS k, SUM(up_bytes) AS up, SUM(dl_bytes) AS dl "
            "FROM global_hourly WHERE day >= ? GROUP BY k",
            (keys[0],),
        )
    else:
        volume_rows = conn.execute(
            "SELECT SUBSTR(day, 1, 7) AS k, SUM(up_bytes) AS up, SUM(dl_bytes) AS dl "
            "FROM global_hourly WHERE day >= ? GROUP BY k",
            (keys[0] + "-01",),
        )
    volume = {row["k"]: row for row in volume_rows}
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
        "volume": {
            "kind": kind,
            "keys": keys,
            "up": [volume[key]["up"] if key in volume else 0 for key in keys],
            "dl": [volume[key]["dl"] if key in volume else 0 for key in keys],
        },
    }


def calendar(conn: sqlite3.Connection, now: int, days: int = 371) -> dict[str, Any]:
    start = local_day(now - (days - 1) * DAY)
    rows = conn.execute(
        "SELECT day, SUM(up_bytes) AS up FROM global_hourly WHERE day >= ? GROUP BY day ORDER BY day",
        (start,),
    ).fetchall()
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
    "up_7d": "up_7d",
    "up_30d": "up_30d",
    "added_on": "t.added_on",
    "seeding_time": "t.seeding_time",
    "last_activity": "t.last_activity",
    "tracker": "t.tracker COLLATE NOCASE",
    "category": "t.category COLLATE NOCASE",
}
TORRENT_COLUMNS = (
    "t.hash, t.name, t.tracker, t.category, t.tags, t.size, t.added_on, t.completion_on, "
    "t.removed_at, t.uploaded, t.downloaded, t.ratio, t.seeding_time, t.last_activity, t.state, "
    "t.num_complete, t.num_incomplete, t.progress"
)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _windows_join(now: int) -> tuple[str, list[str]]:
    return (
        "LEFT JOIN (SELECT hash, SUM(up) AS up FROM torrent_daily WHERE day >= ? GROUP BY hash) d7 "
        "ON d7.hash = t.hash "
        "LEFT JOIN (SELECT hash, SUM(up) AS up FROM torrent_daily WHERE day >= ? GROUP BY hash) d30 "
        "ON d30.hash = t.hash",
        [local_day(now - 6 * DAY), local_day(now - 29 * DAY)],
    )


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
    join, join_params = _windows_join(now)
    direction = "DESC" if descending else "ASC"
    rows = conn.execute(
        f"SELECT {TORRENT_COLUMNS}, COALESCE(d7.up, 0) AS up_7d, COALESCE(d30.up, 0) AS up_30d "
        f"FROM torrents t {join} {clause} "
        f"ORDER BY {TORRENT_SORTS[sort]} {direction}, t.hash LIMIT ? OFFSET ?",
        [*join_params, *params, limit, offset],
    ).fetchall()
    return {"total": total, "rows": [dict(row) for row in rows]}


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


def torrent_detail(conn: sqlite3.Connection, torrent_hash: str, now: int) -> dict[str, Any] | None:
    if not HASH_RE.match(torrent_hash):
        raise BadRequest("Hash invalide")
    row = conn.execute(
        f"SELECT {TORRENT_COLUMNS}, t.content_key, t.first_seen, t.time_active "
        "FROM torrents t WHERE t.hash = ?",
        (torrent_hash,),
    ).fetchone()
    if row is None:
        return None
    daily = conn.execute(
        "SELECT day, up, dl FROM torrent_daily WHERE hash = ? ORDER BY day", (torrent_hash,)
    ).fetchall()
    siblings = conn.execute(
        "SELECT hash, name, tracker, uploaded, ratio FROM torrents "
        "WHERE content_key = ? AND hash != ? AND removed_at IS NULL ORDER BY uploaded DESC",
        (row["content_key"], torrent_hash),
    ).fetchall()
    # Daily deltas are walked backwards from the current counter to rebuild a total.
    remaining = row["uploaded"]
    cumulative: list[int] = []
    for entry in reversed(daily):
        cumulative.append(remaining)
        remaining = max(0, remaining - entry["up"])
    cumulative.reverse()
    detail = dict(row)
    del detail["content_key"]
    return {
        "torrent": detail,
        "days": [entry["day"] for entry in daily],
        "up": [entry["up"] for entry in daily],
        "dl": [entry["dl"] for entry in daily],
        "uploaded_total": cumulative,
        "siblings": [dict(item) for item in siblings],
    }


CONTENT_SORTS = {
    "name": "name COLLATE NOCASE",
    "size": "size",
    "uploaded": "uploaded",
    "efficiency": "efficiency",
    "up_30d": "up_30d",
    "last_activity": "last_activity",
    "torrents": "torrents",
}


def list_contents(
    conn: sqlite3.Connection,
    now: int,
    *,
    sort: str = "efficiency",
    descending: bool = True,
    dead_days: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Group torrents by content: deleting one cross-seed alone frees no disk space."""
    if sort not in CONTENT_SORTS:
        raise BadRequest(f"Tri inconnu : {sort}")
    having = ""
    params: list[Any] = [local_day(now - 29 * DAY)]
    if dead_days is not None:
        limit_ts = now - dead_days * DAY
        having = "HAVING last_activity < ? AND added_on < ?"
        params += [limit_ts, limit_ts]
    base = f"""
        SELECT MIN(t.added_on) AS added_on, t.name AS name, MAX(t.size) AS size,
            COUNT(*) AS torrents, GROUP_CONCAT(DISTINCT t.tracker) AS trackers,
            SUM(t.uploaded) AS uploaded, SUM(t.uploaded) * 1.0 / MAX(MAX(t.size), 1) AS efficiency,
            SUM(COALESCE(d30.up, 0)) AS up_30d,
            MAX(CASE WHEN t.last_activity > 0 THEN t.last_activity ELSE t.added_on END) AS last_activity,
            t.hash AS hash
        FROM torrents t
        LEFT JOIN (SELECT hash, SUM(up) AS up FROM torrent_daily WHERE day >= ? GROUP BY hash) d30
            ON d30.hash = t.hash
        WHERE t.removed_at IS NULL
        GROUP BY t.content_key
        {having}
    """
    totals = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS size FROM ({base})", params
    ).fetchone()
    direction = "DESC" if descending else "ASC"
    rows = conn.execute(
        f"SELECT * FROM ({base}) ORDER BY {CONTENT_SORTS[sort]} {direction}, hash LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "total": totals["n"],
        "total_size": totals["size"],
        "all_size": _unique_size(conn),
        "rows": [dict(row) for row in rows],
    }


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

    kind, keys = _buckets(conn, range_key, now)
    if kind == "hour":
        source = (
            f"SELECT t.{dimension} AS key, b.hour AS bucket, SUM(b.up) AS up "
            f"FROM torrent_hourly b JOIN torrents t ON t.hash = b.hash "
            "WHERE b.hour >= ? GROUP BY key, bucket"
        )
        rows = conn.execute(source, (keys[0],)).fetchall()
    else:
        expression = "b.day" if kind == "day" else "SUBSTR(b.day, 1, 7)"
        first = keys[0] if kind == "day" else keys[0] + "-01"
        rows = conn.execute(
            f"SELECT t.{dimension} AS key, {expression} AS bucket, SUM(b.up) AS up "
            f"FROM torrent_daily b JOIN torrents t ON t.hash = b.hash "
            "WHERE b.day >= ? GROUP BY key, bucket",
            (first,),
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
        period_total[row["key"]] = period_total.get(row["key"], 0) + row["up"]
        position = index_of.get(row["bucket"])
        if position is None:
            continue
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
