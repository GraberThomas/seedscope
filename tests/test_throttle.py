import tempfile
import unittest
from pathlib import Path

from tests.helpers import HASH_A, torrent

from seedscope import db, throttle
from seedscope.collector import Collector, LiveState
from seedscope.config import Config
from seedscope.qbit import QbitError

MIB = throttle.MIB
DOWNLOADING = [torrent(HASH_A, state="downloading", dlspeed=5 * MIB)]
SEEDING = [torrent(HASH_A, state="uploading", upspeed=MIB)]


class FakeLimits:
    def __init__(self, limit=0):
        self.limit = limit
        self.writes = []
        self.fail = False

    def upload_limit(self):
        if self.fail:
            raise QbitError("injoignable")
        return self.limit

    def set_upload_limit(self, limit):
        if self.fail:
            raise QbitError("injoignable")
        self.limit = limit
        self.writes.append(limit)


class ThrottleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = db.connect(str(Path(tmp.name) / "t.db"))
        self.addCleanup(self.conn.close)
        self.client = FakeLimits()

    def enable(self, limit=MIB, delay=120):
        throttle.save(self.conn, throttle.ThrottleSettings(True, limit, delay))

    def step(self, torrents, now):
        throttle.step(self.conn, self.client, torrents, now)

    def events(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM throttle_events ORDER BY start")]

    def test_disabled_by_default_and_never_calls_qbittorrent(self):
        self.client.fail = True
        self.step(DOWNLOADING, 1000)
        self.assertFalse(throttle.load(self.conn).enabled)
        self.assertEqual(self.events(), [])

    def test_only_a_torrent_really_receiving_counts(self):
        self.assertFalse(throttle.is_downloading([torrent(state="stalledDL", dlspeed=0)]))
        self.assertFalse(throttle.is_downloading([torrent(state="queuedDL", dlspeed=0)]))
        self.assertFalse(throttle.is_downloading([torrent(state="downloading", dlspeed=0)]))
        self.assertFalse(throttle.is_downloading([torrent(state="uploading", dlspeed=10)]))
        self.assertTrue(throttle.is_downloading([torrent(state="forcedDL", dlspeed=10)]))

    def test_throttles_then_restores_the_previous_limit_after_the_delay(self):
        self.client.limit = 0
        self.enable(limit=2 * MIB, delay=120)
        self.step(DOWNLOADING, 1000)
        self.assertEqual(self.client.limit, 2 * MIB)
        self.step(SEEDING, 1060)
        self.assertEqual(self.client.limit, 2 * MIB, "still within the delay")
        self.step(DOWNLOADING, 1100)
        self.step(SEEDING, 1200)
        self.assertEqual(self.client.limit, 2 * MIB, "the delay restarts with each download")
        self.step(SEEDING, 1220)
        self.assertEqual(self.client.limit, 0)
        self.assertEqual(self.client.writes, [2 * MIB, 0])
        [event] = self.events()
        self.assertEqual((event["start"], event["end"], event["ended_by"]), (1000, 1220, "idle"))
        self.assertEqual(event["previous_limit"], 0)

    def test_a_stricter_limit_of_the_user_is_left_alone(self):
        self.client.limit = 512 * 1024
        self.enable(limit=MIB)
        self.step(DOWNLOADING, 1000)
        self.assertEqual(self.client.writes, [])

    def test_a_manual_change_wins_until_downloads_end(self):
        self.client.limit = 0
        self.enable()
        self.step(DOWNLOADING, 1000)
        self.client.limit = 3 * MIB  # changed in qBittorrent by hand
        self.step(DOWNLOADING, 1060)
        self.step(DOWNLOADING, 1120)
        self.assertEqual(self.client.limit, 3 * MIB)
        self.assertEqual(self.events()[0]["ended_by"], "manual")
        self.step(SEEDING, 1180)  # downloads over: the hold is lifted, nothing restored
        self.assertEqual(self.client.limit, 3 * MIB)
        self.step(DOWNLOADING, 1240)
        self.assertEqual(self.client.limit, MIB, "a new download throttles again")
        self.assertEqual(self.events()[1]["previous_limit"], 3 * MIB)

    def test_manual_change_before_release_is_not_overwritten(self):
        self.enable(delay=60)
        self.step(DOWNLOADING, 1000)
        self.step(SEEDING, 1030)
        self.client.limit = 4 * MIB
        self.step(SEEDING, 1100)
        self.assertEqual(self.client.limit, 4 * MIB)

    def test_disabling_restores_at_once(self):
        self.client.limit = 0
        self.enable()
        self.step(DOWNLOADING, 1000)
        throttle.save(self.conn, throttle.ThrottleSettings(False, MIB, 120))
        self.step(DOWNLOADING, 1010)
        self.assertEqual(self.client.limit, 0)
        self.assertEqual(self.events()[0]["ended_by"], "disabled")

    def test_a_new_limit_applies_while_throttled(self):
        self.enable(limit=MIB)
        self.step(DOWNLOADING, 1000)
        self.enable(limit=5 * MIB)
        self.step(DOWNLOADING, 1060)
        self.assertEqual(self.client.limit, 5 * MIB)
        self.step(DOWNLOADING, 1120)
        self.assertEqual(self.client.writes, [MIB, 5 * MIB], "not taken for a manual change")

    def test_release_now_and_status(self):
        self.enable()
        self.step(DOWNLOADING, 1000)
        status = throttle.status(self.conn, 1300)
        self.assertTrue(status["active"])
        self.assertEqual(status["week_seconds"], 300)
        throttle.release_now(self.conn, self.client, 1400)
        self.assertEqual(self.client.limit, 0)
        status = throttle.status(self.conn, 1500)
        self.assertFalse(status["active"])
        self.assertEqual(status["week_seconds"], 400)
        self.assertEqual(status["events"][0]["ended_by"], "stopped")

    def test_validation(self):
        valid = {"enabled": True, "limit": MIB, "delay": 120}
        self.assertEqual(throttle.validate(valid), throttle.ThrottleSettings(True, MIB, 120))
        for bad in ({**valid, "enabled": 1}, {**valid, "limit": 123}, {**valid, "delay": "120"}, {}):
            with self.assertRaises(throttle.InvalidThrottle):
                throttle.validate(bad)


class CollectorThrottleTest(unittest.TestCase):
    def test_a_qbittorrent_error_while_throttling_does_not_stop_the_statistics(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "c.db")
        conn = db.connect(path)
        self.addCleanup(conn.close)

        class Client(FakeLimits):
            def server_state(self):
                return {"alltime_ul": 1, "alltime_dl": 1}

            def torrents(self, active_only=False):
                return DOWNLOADING

        client = Client()
        client.fail = True
        throttle.save(conn, throttle.ThrottleSettings(True, MIB, 120))
        state = LiveState()
        collector = Collector(Config("http://q", "u", "p", db_path=path), client, conn, state)
        collector.tick(1000, include_torrents=False)
        self.assertEqual(state.last_success, 1000)
        self.assertIn("injoignable", state.snapshot()["throttle_error"])
        client.fail = False
        collector.tick(1060, include_torrents=False)
        self.assertEqual(client.limit, MIB)
        self.assertEqual(state.snapshot()["throttle_error"], "")


if __name__ == "__main__":
    unittest.main()
