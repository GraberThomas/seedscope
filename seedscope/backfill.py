"""One-shot import of global history from an existing Prometheus.

Per-torrent series are ignored: the exporter labels them by name only, which merges
cross-seeds. Usage: python -m seedscope.backfill --prometheus http://prometheus:9090
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Callable

from . import db
from .collector import GlobalSample, store_global_sample

METRICS = {
    "up_speed": "qbittorrent_global_upload_speed_bytes",
    "dl_speed": "qbittorrent_global_download_speed_bytes",
    "alltime_ul": "qbittorrent_global_alltime_uploaded_bytes",
    "alltime_dl": "qbittorrent_global_alltime_downloaded_bytes",
}
STEP = 60
CHUNK = 7 * 86400  # stays under the Prometheus limit of 11,000 points per query


def fetch_range(base_url: str, metric: str, start: int, end: int) -> dict[int, int]:
    query = urllib.parse.urlencode({"query": metric, "start": start, "end": end, "step": STEP})
    with urllib.request.urlopen(f"{base_url}/api/v1/query_range?{query}", timeout=60) as response:
        results = json.load(response)["data"]["result"]
    if not results:
        return {}
    return {int(ts): int(float(value)) for ts, value in results[0]["values"]}


def import_history(
    conn: sqlite3.Connection,
    fetch: Callable[[str, int, int], dict[int, int]],
    start: int,
    end: int,
) -> int:
    """Insert samples between ``start`` and ``end``; returns the number imported."""
    imported = 0
    previous: tuple[int, int] | None = None
    for chunk_start in range(start, end, CHUNK):
        chunk_end = min(chunk_start + CHUNK - STEP, end)
        columns = {
            field: fetch(metric, chunk_start, chunk_end) for field, metric in METRICS.items()
        }
        with conn:
            for ts in sorted(columns["alltime_ul"]):
                if ts not in columns["alltime_dl"]:
                    continue
                sample = GlobalSample(
                    ts=ts,
                    up_speed=columns["up_speed"].get(ts, 0),
                    dl_speed=columns["dl_speed"].get(ts, 0),
                    alltime_ul=columns["alltime_ul"][ts],
                    alltime_dl=columns["alltime_dl"][ts],
                )
                store_global_sample(conn, sample, previous)
                previous = (sample.alltime_ul, sample.alltime_dl)
                imported += 1
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", required=True, help="URL de Prometheus")
    parser.add_argument("--days", type=int, default=30, help="profondeur maximale, en jours")
    parser.add_argument("--db", default=os.environ.get("SEEDSCOPE_DB", "/data/seedscope.db"))
    args = parser.parse_args()

    conn = db.connect(args.db)
    if db.get_meta(conn, "backfill_done"):
        raise SystemExit("Import deja effectue, rien a faire.")
    first = conn.execute("SELECT MIN(ts) FROM global_minute").fetchone()[0]
    now = int(time.time())
    end = (first or now) - STEP
    start = now - args.days * 86400
    base_url = args.prometheus.rstrip("/")
    count = import_history(conn, lambda m, s, e: fetch_range(base_url, m, s, e), start, end)
    with conn:
        db.set_meta(conn, "backfill_done", now)
    print(f"{count} echantillons importes.")


if __name__ == "__main__":
    main()
