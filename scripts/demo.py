#!/usr/bin/env python3
"""Serve the interface on fictional data, without any qBittorrent.

Usage: python scripts/demo.py [--port 8080] [--db /tmp/seedscope-demo.db]
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seedscope import db  # noqa: E402
from seedscope.collector import (  # noqa: E402
    Collector,
    GlobalSample,
    LiveState,
    local_day,
    store_global_sample,
)
from seedscope.config import Config  # noqa: E402
from seedscope.web import make_server  # noqa: E402

GIB = 2**30
DAY = 86400
TRACKERS = ["tracker-alpha.example", "tracker-beta.example", "tracker-gamma.example", "tracker-delta.example"]
TITLES = "Aurore Boreale Cascade Dune Echo Falaise Glacier Horizon Icare Jade Lueur Meridien Nebuleuse Orage Prisme".split()


def seed(conn, config: Config, now: int, rng: random.Random) -> list[dict]:
    collector = Collector(config, None, conn)
    first = now - 400 * DAY
    torrents = []
    for index in range(140):
        title = rng.choice(TITLES)
        size = int(rng.uniform(1, 60) * GIB)
        name = f"{title}.{rng.randint(1990, 2026)}.1080p.DEMO-{index % 90}"
        for copy in range(rng.choice((1, 1, 2, 3))):
            torrents.append({
                "hash": f"{index:032x}{copy:08x}",
                "name": name,
                "size": size,
                "tracker": f"https://{rng.choice(TRACKERS)}/announce",
                "category": "cross-seed-link" if copy else rng.choice(("radarr", "sonarr", "")),
                "tags": "cross-seed" if copy else rng.choice(("", "", "mes-uploads")),
                "added_on": first + rng.randint(0, 300) * DAY,
                "completion_on": first,
                "uploaded": 0,
                "downloaded": size if not copy else 0,
                "ratio": 0.0,
                "seeding_time": rng.randint(1, 300) * DAY,
                "last_activity": 0,
                "state": "stalledUP",
                "num_complete": rng.randint(1, 40),
                "num_incomplete": rng.randint(0, 4),
                "progress": 1.0,
            })
    # One torrent out of four never uploads, to populate the dead weight view.
    weights = [0.0 if rng.random() < 0.25 else rng.random() ** 4 for _ in torrents]
    with conn:
        collector.ingest_torrents(torrents, first)

    uploaded = 800 * GIB
    downloaded = 2600 * GIB
    previous = (uploaded, downloaded)
    for ts in range(first, now, 3600):
        local = time.localtime(ts)
        activity = 0.6 + 0.4 * math.sin((local.tm_hour - 6) / 24 * 2 * math.pi)
        trend = 0.5 + (ts - first) / (now - first)
        bytes_up = int(rng.expovariate(1) * activity * trend * 1.4 * GIB)
        bytes_down = int(rng.expovariate(1) * 0.4 * GIB) if rng.random() < 0.15 else 0
        uploaded += bytes_up
        downloaded += bytes_down
        sample = GlobalSample(ts, bytes_up // 3600, bytes_down // 3600, uploaded, downloaded, rng.randint(10, 60), 8 * 1024 * GIB)
        store_global_sample(conn, sample, previous)
        previous = (uploaded, downloaded)
        if ts % (3600 * 24) == 0:
            conn.commit()
        if now - ts <= DAY:
            for minute in range(1, 60):
                inner = ts + minute * 60
                if inner < now:
                    conn.execute(
                        "INSERT OR REPLACE INTO global_minute VALUES (?, ?, ?, ?, ?, 20, 0)",
                        (inner, int(bytes_up / 3600 * rng.uniform(0.3, 1.7)), bytes_down // 3600, uploaded, downloaded),
                    )
    conn.commit()

    # Spread the same bytes over torrents so that per-tracker views add up.
    total_weight = sum(weights)
    for day_index in range(400):
        ts = first + day_index * DAY
        day = local_day(ts)
        up_day = conn.execute("SELECT COALESCE(SUM(up_bytes), 0) FROM global_hourly WHERE day = ?", (day,)).fetchone()[0]
        for torrent, weight in zip(torrents, weights):
            share = int(up_day * weight / total_weight)
            if share and torrent["added_on"] <= ts:
                torrent["uploaded"] += share
                torrent["last_activity"] = ts
                conn.execute(
                    "INSERT OR REPLACE INTO torrent_daily (hash, day, up, dl) VALUES (?, ?, ?, 0)",
                    (torrent["hash"], day, share),
                )
                if now - ts <= 2 * DAY:
                    conn.execute(
                        "INSERT OR REPLACE INTO torrent_hourly (hash, hour, up, dl) VALUES (?, ?, ?, 0)",
                        (torrent["hash"], now - now % 3600 - (now - ts) // 3600 * 3600, share // 24),
                    )
    for torrent in torrents:
        conn.execute(
            "UPDATE torrents SET uploaded = ?, last_activity = ?, ratio = ? WHERE hash = ?",
            (
                torrent["uploaded"],
                torrent["last_activity"] or torrent["added_on"],
                torrent["uploaded"] / torrent["size"],
                torrent["hash"],
            ),
        )
    conn.commit()
    return torrents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default="/tmp/seedscope-demo.db")
    args = parser.parse_args()

    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(args.db + suffix):
            os.remove(args.db + suffix)
    config = Config("http://demo", "demo", "demo", db_path=args.db, host=args.host, port=args.port)
    conn = db.connect(args.db)
    now = int(time.time())
    torrents = seed(conn, config, now, random.Random(42))

    state = LiveState()
    state.active_ts = now
    state.last_success = now
    state.connection_status = "connected"
    busiest = sorted(torrents, key=lambda t: t["uploaded"], reverse=True)[:8]
    state.active = [
        {
            "hash": t["hash"],
            "name": t["name"],
            "tracker": t["tracker"].split("/")[2],
            "up_speed": (8 - i) * 180_000,
            "dl_speed": 0,
            "ratio": t["uploaded"] / t["size"],
            "leechers": 8 - i,
            "size": t["size"],
        }
        for i, t in enumerate(busiest)
    ]
    print(f"Demo sur http://{args.host}:{args.port}/", flush=True)
    make_server(config, state).serve_forever()


if __name__ == "__main__":
    main()
