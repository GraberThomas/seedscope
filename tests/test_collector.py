import itertools
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import GIB, HASH_A, HASH_B, PASSKEY, torrent

from seedscope import db, settings
from seedscope.collector import (
    Collector,
    GlobalSample,
    content_key,
    counter_delta,
    store_global_sample,
    tracker_host,
)
from seedscope.config import Config


class FakeClient:
    def __init__(self) -> None:
        self.state = {}
        self.list = []
        self.active = []

    def server_state(self):
        return self.state

    def torrents(self, active_only=False):
        return self.active if active_only else self.list


def make_config(path: str, **overrides) -> Config:
    return Config(
        qbt_url="http://qbit",
        qbt_username="user",
        qbt_password="secret",
        db_path=path,
        **overrides,
    )


class SlowLoopClient(FakeClient):
    """Counts polls, and stops the loop after a given number of them."""

    def __init__(self, stop, wake, stop_after):
        super().__init__()
        self.stop = stop
        self.wake = wake
        self.stop_after = stop_after
        self.full_polls = 0
        self.global_polls = 0

    def login(self):
        pass

    def server_state(self):
        self.global_polls += 1
        return {"alltime_ul": 1, "alltime_dl": 1}

    def torrents(self, active_only=False):
        if not active_only:
            self.full_polls += 1
        if active_only:
            if self.global_polls >= self.stop_after:
                self.stop.set()
                self.wake.set()
            else:
                self.wake.set()  # do not wait for the interval during the test
        return []


class PureFunctionsTest(unittest.TestCase):
    def test_tracker_host_drops_the_passkey(self):
        self.assertEqual(tracker_host(f"https://Tk.Example.org:443/a/{PASSKEY}"), "tk.example.org")
        self.assertEqual(tracker_host(""), "")

    def test_counter_reset_never_goes_negative(self):
        self.assertEqual(counter_delta(100, 150), 50)
        self.assertEqual(counter_delta(150, 10), 0)

    def test_content_key_groups_large_torrents_by_size_only(self):
        self.assertEqual(content_key("A", 10 * GIB), content_key("B", 10 * GIB))
        self.assertNotEqual(content_key("A", 1000), content_key("B", 1000))


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "test.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.client = FakeClient()
        self.collector = Collector(make_config(self.path), self.client, self.conn)

    def ingest(self, torrents, now):
        with self.conn:
            self.collector.ingest_torrents(torrents, now)

    def daily(self, torrent_hash):
        return self.conn.execute(
            "SELECT COALESCE(SUM(up), 0), COALESCE(SUM(dl), 0) FROM torrent_daily WHERE hash = ?",
            (torrent_hash,),
        ).fetchone()

    def test_first_poll_is_a_baseline(self):
        self.ingest([torrent(uploaded=5 * GIB, downloaded=GIB)], 10_000)
        self.assertEqual(tuple(self.daily(HASH_A)), (0, 0))
        row = self.conn.execute("SELECT uploaded FROM torrents").fetchone()
        self.assertEqual(row[0], 5 * GIB)

    def test_later_polls_record_the_growth(self):
        self.ingest([torrent(uploaded=100)], 10_000)
        self.ingest([torrent(uploaded=350)], 10_300)
        self.ingest([torrent(uploaded=400)], 10_600)
        self.assertEqual(self.daily(HASH_A)[0], 300)

    def test_torrent_added_after_last_poll_counts_from_zero(self):
        self.ingest([torrent(uploaded=100)], 10_000)
        new = torrent(HASH_B, uploaded=70, downloaded=30, added_on=10_100)
        self.ingest([torrent(uploaded=100), new], 10_300)
        self.assertEqual(tuple(self.daily(HASH_B)), (70, 30))

    def test_old_torrent_seen_late_does_not_create_a_spike(self):
        self.ingest([torrent()], 10_000)
        self.ingest([torrent(), torrent(HASH_B, uploaded=9 * GIB, added_on=500)], 10_300)
        self.assertEqual(self.daily(HASH_B)[0], 0)

    def test_removal_keeps_the_history_and_readding_clears_the_flag(self):
        self.ingest([torrent(uploaded=100)], 10_000)
        self.ingest([torrent(uploaded=200)], 10_300)
        self.ingest([], 10_600)
        row = self.conn.execute("SELECT removed_at FROM torrents").fetchone()
        self.assertEqual(row[0], 10_600)
        self.assertEqual(self.daily(HASH_A)[0], 100)
        self.ingest([torrent(uploaded=0)], 10_900)
        self.ingest([torrent(uploaded=10)], 11_200)
        row = self.conn.execute("SELECT removed_at FROM torrents").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(self.daily(HASH_A)[0], 110)

    def test_blank_tracker_keeps_the_known_host(self):
        self.ingest([torrent()], 10_000)
        self.ingest([torrent(tracker="")], 10_300)
        row = self.conn.execute("SELECT tracker_host FROM torrents").fetchone()
        self.assertEqual(row[0], "tk.example.org")

    def test_passkey_is_never_stored(self):
        self.ingest([torrent()], 10_000)
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = Path(self.path).read_bytes()
        self.assertNotIn(PASSKEY.encode(), raw)

    def test_aliases_merge_hosts_and_apply_to_history(self):
        self.ingest([torrent()], 10_000)
        aliased = Collector(
            make_config(self.path, tracker_aliases={"tk.example.org": "Example"}),
            self.client,
            self.conn,
        )
        row = self.conn.execute("SELECT tracker, tracker_host FROM torrents").fetchone()
        self.assertEqual(tuple(row), ("Example", "tk.example.org"))
        self.assertEqual(aliased._alias("other.org"), "other.org")

    def test_global_hourly_rollup_sums_deltas(self):
        first = GlobalSample(7200, 10, 1, 1000, 500)
        second = GlobalSample(7260, 30, 3, 1600, 500)
        with self.conn:
            store_global_sample(self.conn, first, None)
            store_global_sample(self.conn, second, (1000, 500))
        row = self.conn.execute("SELECT * FROM global_hourly").fetchone()
        self.assertEqual(row["up_bytes"], 600)
        self.assertEqual(row["samples"], 2)
        self.assertEqual(row["up_speed_max"], 30)
        self.assertEqual(row["alltime_ul"], 1600)

    def samples(self):
        return [
            tuple(row)
            for row in self.conn.execute("SELECT ts, tier, uploaded FROM torrent_samples ORDER BY ts")
        ]

    def test_idle_torrents_are_sampled_hourly_not_at_every_poll(self):
        base = 3600 * 24 * 100
        self.ingest([torrent(uploaded=10)], base)  # first poll
        self.ingest([torrent(uploaded=10)], base + 300)  # unchanged: nothing stored
        self.ingest([torrent(uploaded=20)], base + 600)  # changed: fine sample
        self.ingest([torrent(uploaded=20)], base + 3600)  # new hour
        self.ingest([torrent(uploaded=20)], base + 86400)  # new day
        self.assertEqual(
            self.samples(),
            [
                (base, 2, 10),
                (base + 600, 0, 20),
                (base + 3600, 1, 20),
                (base + 86400, 2, 20),
            ],
        )

    def test_swarm_changes_create_a_sample_and_popularity_is_stored(self):
        base = 3600 * 24 * 100
        self.ingest([torrent(num_incomplete=1)], base)
        self.ingest([torrent(num_incomplete=4, popularity=1.25)], base + 300)
        row = self.conn.execute(
            "SELECT leechers, popularity FROM torrent_samples WHERE ts = ?", (base + 300,)
        ).fetchone()
        self.assertEqual(tuple(row), (4, 1.25))
        current = self.conn.execute("SELECT popularity FROM torrents").fetchone()[0]
        self.assertEqual(current, 1.25)

    def test_prune_thins_samples_by_tier(self):
        now = 1000 * 86400
        for age_days, tier in ((31, 0), (29, 0), (181, 1), (179, 1), (900, 2)):
            self.conn.execute(
                "INSERT INTO torrent_samples VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0)",
                (HASH_A, now - age_days * 86400, tier),
            )
        self.collector.prune(now)
        ages = sorted(
            (now - row[0]) // 86400
            for row in self.conn.execute("SELECT ts FROM torrent_samples")
        )
        self.assertEqual(ages, [29, 179, 900])

    def test_run_reads_saved_intervals_at_every_cycle(self):
        stop = threading.Event()
        client = SlowLoopClient(stop, self.collector.state.wake, stop_after=3)
        self.collector.client = client
        settings.save(self.conn, settings.Intervals(30, 3600))
        self.collector.run(stop)
        self.assertEqual(client.global_polls, 3)
        self.assertEqual(client.full_polls, 1)  # the next one is an hour away

    def test_run_collects_torrents_again_once_the_interval_is_lowered(self):
        stop = threading.Event()
        client = SlowLoopClient(stop, self.collector.state.wake, stop_after=2)
        self.collector.client = client
        settings.save(self.conn, settings.Intervals(30, 60))
        with mock.patch("seedscope.collector.time.time", side_effect=itertools.count(1000, 61)):
            self.collector.run(stop)
        self.assertEqual(client.full_polls, 2)

    def test_manual_request_forces_a_full_collection(self):
        stop = threading.Event()
        client = SlowLoopClient(stop, self.collector.state.wake, stop_after=2)
        self.collector.client = client
        settings.save(self.conn, settings.Intervals(30, 3600))
        self.assertTrue(self.collector.state.request_full_collection(1000.0, cooldown=10))
        self.collector.run(stop)
        self.assertEqual(client.full_polls, 1)  # first cycle: forced, and due anyway
        self.assertFalse(self.collector.state.force_full)
        self.assertGreater(self.collector.state.snapshot()["last_full_success"], 0)
        self.assertFalse(self.collector.state.request_full_collection(1005.0, cooldown=10))
        self.assertTrue(self.collector.state.request_full_collection(1011.0, cooldown=10))

    def test_manual_request_collects_torrents_before_the_interval_elapsed(self):
        stop = threading.Event()
        state = self.collector.state

        class RequestingClient(SlowLoopClient):
            def torrents(inner, active_only=False):  # noqa: N805
                result = super().torrents(active_only)
                if active_only and inner.global_polls == 1:
                    state.request_full_collection(1000.0, cooldown=10)
                return result

        client = RequestingClient(stop, state.wake, stop_after=2)
        self.collector.client = client
        settings.save(self.conn, settings.Intervals(30, 3600))
        self.collector.run(stop)
        self.assertEqual(client.full_polls, 2)

    def test_live_rows_carry_their_content(self):
        self.client.active = [torrent(upspeed=5, dlspeed=7, eta=90, progress=0.5, num_seeds=3)]
        self.collector.poll_live(1000)
        row = self.collector.state.snapshot()["active"][0]
        self.assertEqual((row["dl_speed"], row["eta"], row["progress"], row["seeds"]), (7, 90, 0.5, 3))
        self.assertEqual(self.collector.state.snapshot()["active"][0]["content_key"], content_key("Demo.Movie.2024", 10 * GIB))

    def test_tick_collects_everything_and_prune_respects_retention(self):
        self.client.state = {
            "alltime_ul": 100,
            "alltime_dl": 50,
            "up_info_speed": 5,
            "dl_info_speed": 2,
            "connection_status": "connected",
        }
        self.client.list = [torrent()]
        self.client.active = [torrent(upspeed=500), torrent(HASH_B, upspeed=900)]
        now = 400 * 86400
        self.conn.execute(
            "INSERT INTO global_minute (ts, up_speed, dl_speed, alltime_ul, alltime_dl) "
            "VALUES (?, 0, 0, 0, 0)",
            (now - 91 * 86400,),
        )
        self.conn.execute(
            "INSERT INTO torrent_hourly (hash, hour, up, dl) VALUES (?, ?, 1, 1)",
            (HASH_A, now - 366 * 86400),
        )
        self.collector.tick(now, include_torrents=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM global_minute").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM torrent_hourly").fetchone()[0], 0)
        snapshot = self.collector.state.snapshot()
        self.assertEqual(snapshot["active"][0]["hash"], HASH_B)
        self.assertNotIn(PASSKEY, str(snapshot))
        self.assertEqual(snapshot["last_success"], now)


if __name__ == "__main__":
    unittest.main()
