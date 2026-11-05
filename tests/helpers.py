"""Shared test fixtures."""

from __future__ import annotations

import os

os.environ["TZ"] = "UTC"

import time  # noqa: E402

time.tzset()

HASH_A = "a" * 40
HASH_B = "b" * 40
HASH_C = "c" * 40
PASSKEY = "abcdef0123456789secretpasskey"
GIB = 2**30


def torrent(torrent_hash: str = HASH_A, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "hash": torrent_hash,
        "name": "Demo.Movie.2024",
        "tracker": f"https://tk.example.org/announce/{PASSKEY}",
        "category": "radarr",
        "tags": "",
        "size": 10 * GIB,
        "added_on": 1000,
        "completion_on": 1100,
        "uploaded": 0,
        "downloaded": 0,
        "ratio": 0.0,
        "seeding_time": 100,
        "time_active": 200,
        "last_activity": 1500,
        "state": "stalledUP",
        "num_complete": 5,
        "num_incomplete": 1,
        "progress": 1.0,
    }
    base.update(overrides)
    return base
