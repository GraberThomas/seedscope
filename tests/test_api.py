import datetime as dt
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tests.helpers import GIB, HASH_A, HASH_B, HASH_C, PASSKEY, torrent

from seedscope import backfill, db, queries
from seedscope.collector import Collector, GlobalSample, LiveState, local_day, store_global_sample
from seedscope.config import Config
from seedscope.web import make_server

NOW = 1_800_000_000
DAY = 86400


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        path = str(Path(cls.tmp.name) / "api.db")
        cls.config = Config("http://qbit", "u", "p", db_path=path, host="127.0.0.1", port=0)
        cls.conn = db.connect(path)
        cls.addClassCleanup(cls.conn.close)
        collector = Collector(cls.config, None, cls.conn)

        previous = None
        for hours_ago in range(24 * 40, -1, -1):
            ts = NOW - hours_ago * 3600
            uploaded = (24 * 40 - hours_ago + 1) * GIB
            sample = GlobalSample(ts, 1000, 200, uploaded, 2000 * GIB)
            with cls.conn:
                store_global_sample(cls.conn, sample, previous)
            previous = (uploaded, 2000 * GIB)

        old = NOW - 400 * DAY
        first = [
            torrent(HASH_A, name="Film.A", uploaded=5 * GIB, added_on=old, last_activity=NOW - 100,
                    tags="mes-uploads, manuel"),
            torrent(HASH_B, name="Film.A.renamed", uploaded=GIB, added_on=old, last_activity=NOW - 60 * DAY,
                    tracker="https://other.example.net/x", category="cross-seed-link"),
            torrent(HASH_C, name="Vieux_100%", uploaded=0, size=20 * GIB, added_on=old,
                    last_activity=NOW - 60 * DAY, category=""),
        ]
        with cls.conn:
            collector.ingest_torrents(first, NOW - 3600)
        first[0]["uploaded"] = 8 * GIB
        with cls.conn:
            collector.ingest_torrents(first, NOW)

        cls.state = LiveState()
        cls.state.active = [{"hash": HASH_A, "name": "Film.A", "up_speed": 5}]
        cls.server = make_server(cls.config, cls.state, clock=lambda: NOW)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def status(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_summary_values(self):
        data = queries.summary(self.conn, NOW, self.state.snapshot())
        self.assertEqual(data["torrents"], 3)
        self.assertEqual(data["contents"], 2)
        self.assertEqual(data["size_total"], 40 * GIB)
        self.assertEqual(data["size_unique"], 30 * GIB)
        # Hourly totals: the imported counter, plus the 3 GiB sent by Film.A in the last poll.
        self.assertEqual(data["up_7d"], 7 * 24 * GIB + 3 * GIB)
        self.assertEqual(data["up_7d_previous"], 7 * 24 * GIB)
        self.assertIsNone(data["up_30d_previous"])  # only 40 days of history
        self.assertEqual(data["ratio"], round((24 * 40 + 1) / 2000, 3))

    def test_every_range_answers_with_series_buckets_and_stats(self):
        expected = {
            "1h": "min5", "6h": "min30", "24h": "hour", "3d": "hour6", "7d": "day",
            "30d": "day", "90d": "week", "180d": "week", "1y": "month", "all": "month",
        }
        self.assertEqual(set(expected), set(queries.RANGES))
        for range_key, kind in expected.items():
            data = self.get(f"/api/timeseries?range={range_key}")
            self.assertEqual(data["volume"]["kind"], kind, range_key)
            self.assertTrue(data["ts"], range_key)
            self.assertEqual(len(data["volume"]["keys"]), len(data["volume"]["up"]))
            self.assertLessEqual(len(data["ts"]), 1500, range_key)
            self.assertTrue(self.get(f"/api/breakdown?range={range_key}")["keys"], range_key)

    def test_period_stats(self):
        stats = self.get("/api/timeseries?range=7d")["stats"]
        self.assertEqual(stats["up"], 7 * 24 * GIB)
        self.assertEqual(stats["previous_up"], 7 * 24 * GIB)
        self.assertEqual(stats["peak_up"], 1000)
        self.assertIsNone(stats["ratio"])  # nothing downloaded over the period
        long_stats = self.get("/api/timeseries?range=30d")["stats"]
        self.assertGreaterEqual(long_stats["up"], 30 * 24 * GIB)
        self.assertIsNone(long_stats["previous_up"])  # only 40 days of history
        self.assertIsNone(self.get("/api/timeseries?range=all")["stats"]["previous_up"])

    def test_week_buckets_add_up_to_daily_totals(self):
        volume = self.get("/api/timeseries?range=90d")["volume"]
        self.assertEqual(sum(volume["up"]), 24 * 40 * GIB + 3 * GIB)
        self.assertEqual(dt.date.fromisoformat(volume["keys"][-1]).weekday(), 0)

    def test_popularity(self):
        data = self.get("/api/popularity")
        self.assertEqual(data["wanted"], 3)
        self.assertEqual(data["leechers"], 3)
        self.assertEqual(data["lonely"], 0)
        self.assertEqual({row["key"] for row in data["trackers"]}, {"tk.example.org", "other.example.net"})

    def test_timeseries_resolutions(self):
        self.assertEqual(self.get("/api/timeseries?range=30d")["resolution"], "hour")
        self.assertEqual(self.get("/api/timeseries?range=all")["resolution"], "day")
        data = self.get("/api/timeseries?range=7d")
        self.assertEqual(data["volume"]["kind"], "day")
        self.assertEqual(len(data["volume"]["keys"]), 7)
        self.assertEqual(self.get("/api/timeseries?range=24h")["volume"]["kind"], "hour")

    def test_data_dated_in_the_future_does_not_break_all_time_views(self):
        past = NOW - 500 * DAY
        self.assertTrue(queries.timeseries(self.conn, "all", past)["volume"]["keys"])
        self.assertTrue(queries.breakdown(self.conn, "tracker", "all", past)["keys"])

    def test_empty_database_is_served_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "empty.db"))
            self.addCleanup(conn.close)
            self.assertIsNone(queries.summary(conn, NOW, {})["ratio"])
            for range_key in queries.RANGES:
                self.assertEqual(queries.timeseries(conn, range_key, NOW)["ts"], [])
                self.assertEqual(queries.breakdown(conn, "tracker", range_key, NOW)["rows"], [])
            self.assertEqual(queries.list_contents(conn, NOW)["total"], 0)
            self.assertEqual(queries.filters(conn)["trackers"], [])

    def test_torrents_sorting_and_filters(self):
        rows = self.get("/api/torrents?sort=uploaded")["rows"]
        self.assertEqual(rows[0]["hash"], HASH_A)
        self.assertEqual(rows[0]["up_period"], 3 * GIB)
        self.assertEqual(rows[0]["spark"][-1], 3 * GIB)
        self.assertEqual(len(rows[0]["spark"]), 30)
        one_day = self.get("/api/torrents?sort=up_period&window=1")["rows"]
        self.assertEqual(one_day[0]["up_period"], 3 * GIB)
        self.assertEqual(self.get("/api/torrents?sort=popularity&known_swarm=1")["total"], 3)
        self.assertEqual(self.status("/api/torrents?window=2"), 400)
        self.assertEqual(self.get("/api/torrents?tag=manuel")["total"], 1)
        self.assertEqual(self.get("/api/torrents?tag=manu")["total"], 0)
        self.assertEqual(self.get("/api/torrents?tracker=other.example.net")["total"], 1)
        self.assertEqual(self.get("/api/torrents?category=")["total"], 1)

    def test_search_treats_wildcards_literally(self):
        self.assertEqual(self.get("/api/torrents?q=100%25")["total"], 1)
        self.assertEqual(self.get("/api/torrents?q=%25")["total"], 1)
        self.assertEqual(self.get("/api/torrents?q=_")["total"], 1)

    def test_invalid_sort_cannot_inject_sql(self):
        self.assertEqual(self.status("/api/torrents?sort=name;DROP%20TABLE%20torrents"), 400)
        self.assertEqual(self.status("/api/contents?sort=x"), 400)
        self.assertEqual(self.status("/api/timeseries?range=x"), 400)
        self.assertEqual(self.status("/api/breakdown?by=hash"), 400)

    def test_contents_group_cross_seeds(self):
        data = self.get("/api/contents?sort=size")
        self.assertEqual(data["total"], 2)
        grouped = next(row for row in data["rows"] if row["torrents"] == 2)
        self.assertEqual(grouped["uploaded"], 9 * GIB)
        self.assertAlmostEqual(grouped["efficiency"], 0.9)

    def test_contents_filters_keep_all_torrents_of_a_match_and_sparkline(self):
        data = self.get("/api/contents?tag=manuel&sort=size")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["rows"][0]["torrents"], 2)
        self.assertEqual(data["rows"][0]["uploaded"], 9 * GIB)
        self.assertEqual(data["rows"][0]["spark"][-1], 3 * GIB)
        self.assertEqual(self.get("/api/contents?q=renamed")["total"], 1)
        self.assertEqual(self.get("/api/contents?tracker=other.example.net")["total"], 1)
        self.assertEqual(self.get("/api/contents?category=")["total"], 1)
        self.assertEqual(self.get("/api/contents?sort=up_period&window=7")["rows"][0]["up_period"], 3 * GIB)
        self.assertEqual(self.status("/api/contents?sort=hash"), 400)

    def test_contents_summary_and_top(self):
        summary = self.get("/api/contents/summary")
        self.assertEqual(summary["contents"], 2)
        self.assertEqual(summary["cross_seeded"], 1)
        self.assertEqual(summary["never_uploaded"], 1)
        self.assertEqual(summary["never_uploaded_size"], 20 * GIB)
        top = self.get("/api/contents/top?range=24h")["rows"]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["up_period"], 3 * GIB)
        self.assertEqual(top[0]["torrents"], 2)
        self.assertEqual(self.get("/api/contents/top?range=all")["rows"][0]["up_period"], 3 * GIB)
        self.assertEqual(self.get("/api/contents/top?range=90d")["rows"][0]["up_period"], 3 * GIB)
        self.assertEqual(self.status("/api/contents/top?range=x"), 400)

    def test_content_detail_adds_up_its_torrents(self):
        key = self.get("/api/contents?tag=manuel")["rows"][0]["content_key"]
        data = self.get("/api/content?key=" + urllib.parse.quote(key))
        content = data["content"]
        self.assertEqual(content["torrents"], 2)
        self.assertEqual(content["uploaded"], 9 * GIB)
        self.assertEqual(content["efficiency"], 0.9)
        self.assertEqual(content["trackers"], ["other.example.net", "tk.example.org"])
        self.assertEqual(data["series"]["uploaded"], [6 * GIB, 9 * GIB])
        self.assertEqual(data["series"]["seeders"], [10, 10])
        self.assertEqual(data["series"]["leechers"], [2, 2])
        self.assertEqual(data["series"]["efficiency"], [0.6, 0.9])
        by_tracker = {item["key"]: item["values"] for item in data["series"]["by_tracker"]}
        self.assertEqual(by_tracker["tk.example.org"], [5 * GIB, 8 * GIB])
        self.assertEqual(by_tracker["other.example.net"], [GIB, GIB])
        self.assertEqual(data["up_period"], 3 * GIB)
        self.assertEqual([t["hash"] for t in data["torrents"]], [HASH_A, HASH_B])
        self.assertNotIn(PASSKEY, json.dumps(data))
        for range_key in queries.RANGES:
            self.assertTrue(
                self.get(f"/api/content?range={range_key}&key=" + urllib.parse.quote(key))["volume"]["keys"]
            )
        self.assertEqual(self.status("/api/content?key=nope"), 404)
        self.assertEqual(self.status("/api/content"), 400)
        self.assertEqual(self.status("/api/content?range=x&key=nope"), 400)

    def test_idle_content_gets_a_flat_line_on_short_ranges(self):
        key = self.get("/api/contents?sort=size")["rows"][0]["content_key"]
        series = queries.content_detail(self.conn, key, "1h", NOW + 2 * 3600)["series"]
        self.assertGreaterEqual(len(series["ts"]), 1)
        self.assertEqual(series["uploaded"][0], series["uploaded"][-1])

    def test_dead_content_needs_all_of_its_torrents_idle(self):
        data = self.get("/api/contents?dead_days=30")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["total_size"], 20 * GIB)
        self.assertEqual(data["rows"][0]["hash"], HASH_C)

    def test_torrent_detail(self):
        data = self.get(f"/api/torrent/{HASH_A}")
        self.assertEqual(data["torrent"]["hash"], HASH_A)
        self.assertEqual(data["series"]["uploaded"], [5 * GIB, 8 * GIB])
        self.assertEqual(data["series"]["seeders"], [5, 5])
        self.assertEqual(data["series"]["popularity"], [0.5, 0.5])
        self.assertEqual(sum(data["volume"]["up"]), 3 * GIB)
        self.assertEqual(data["up_period"], 3 * GIB)
        self.assertEqual(self.status(f"/api/torrent/{HASH_A}?range=x"), 400)
        for range_key in queries.RANGES:
            self.assertTrue(self.get(f"/api/torrent/{HASH_A}?range={range_key}")["volume"]["keys"])
        self.assertEqual([item["hash"] for item in data["siblings"]], [HASH_B])
        self.assertEqual(self.status("/api/torrent/" + "d" * 40), 404)
        self.assertEqual(self.status("/api/torrent/../../etc"), 400)

    def test_idle_torrent_gets_a_flat_line_on_short_ranges(self):
        # HASH_B did not change after its first sample, one hour before NOW.
        with self.conn:
            self.conn.execute(
                "UPDATE torrents SET last_seen = ? WHERE hash = ?", (NOW, HASH_B)
            )
            self.conn.execute(
                "DELETE FROM torrent_samples WHERE hash = ? AND ts = ?", (HASH_B, NOW)
            )
        series = queries.torrent_detail(self.conn, HASH_B, "1h", NOW)["series"]
        self.assertEqual(len(series["ts"]), 2)
        self.assertEqual(series["uploaded"], [GIB, GIB])
        self.assertEqual(series["ts"][-1], NOW)

    def test_breakdown(self):
        data = self.get("/api/breakdown?by=tracker&range=7d")
        rows = {row["key"]: row for row in data["rows"]}
        self.assertEqual(rows["tk.example.org"]["up_period"], 3 * GIB)
        self.assertEqual(rows["tk.example.org"]["size_unique"], 30 * GIB)
        slots = {series["key"]: series["slot"] for series in data["series"]}
        self.assertEqual(slots, {"other.example.net": 0, "tk.example.org": 1})
        self.assertEqual(self.get("/api/breakdown?by=category&range=all")["kind"], "month")

    def test_calendar_and_heatmap(self):
        calendar = self.get("/api/calendar")
        self.assertEqual(calendar["end"], local_day(NOW))
        heatmap = queries.heatmap(self.conn, NOW)
        self.assertEqual(len(heatmap["cells"]), 7 * 24)

    def test_filters_and_live(self):
        filters = self.get("/api/filters")
        self.assertEqual(filters["tags"], ["manuel", "mes-uploads"])
        self.assertEqual(self.get("/api/live")["rows"][0]["hash"], HASH_A)

    def test_api_is_read_only_and_never_exposes_a_passkey(self):
        self.assertEqual(self.status("/api/torrents", method="DELETE"), 501)
        self.assertEqual(self.status("/api/torrents", method="POST"), 405)
        with self.assertRaises(Exception):
            ro = db.connect_readonly(self.config.db_path)
            self.addCleanup(ro.close)
            ro.execute("DELETE FROM torrents")
        for path in ("/api/torrents", "/api/contents", f"/api/torrent/{HASH_A}", "/api/filters"):
            with urllib.request.urlopen(self.base + path) as response:
                self.assertNotIn(PASSKEY, response.read().decode())

    def test_static_files_and_traversal(self):
        for path in ("/", "/app.js", "/charts.js", "/table-sort.js", "/style.css"):
            self.assertEqual(self.status(path), 200, path)
        self.assertEqual(self.status("/../config.py"), 404)
        self.assertEqual(self.status("/static/app.js"), 404)
        with urllib.request.urlopen(self.base + "/") as response:
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])


class SettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = str(Path(self.tmp.name) / "s.db")
        self.path = path
        db.connect(path).close()
        config = Config("http://qbit", "u", "p", db_path=path, host="127.0.0.1", port=0)
        self.state = LiveState()
        self.server = make_server(config, self.state)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, payload, content_type="application/json", origin=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(self.base + "/api/settings", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def current(self):
        with urllib.request.urlopen(self.base + "/api/settings") as response:
            return json.load(response)

    def test_defaults_then_save_persists_and_wakes_the_collector(self):
        self.assertEqual(self.current()["global_interval"], 60)
        valid = {"global_interval": 120, "torrent_interval": 600}
        self.assertEqual(self.post(valid), 200)
        self.assertTrue(self.state.wake.is_set())
        self.assertEqual(self.current()["torrent_interval"], 600)

    def post_retention(self, payload, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(
            self.base + "/api/settings/retention", data=json.dumps(payload).encode(), headers=headers
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    UNLIMITED = {"minute": 0, "torrent_hourly": 0, "sample_fine": 0, "sample_hourly": 0}

    def test_retention_is_unlimited_by_default_and_reports_storage(self):
        current = self.current()
        self.assertEqual(current["retention"], self.UNLIMITED)
        self.assertIn(0, current["retention_options"])
        self.assertGreater(current["storage"]["bytes"], 0)
        self.assertEqual(current["storage"]["rows"]["minute"], 0)

    def test_shorter_retention_needs_a_literal_confirmation(self):
        with self.conn_write() as conn:
            conn.execute(
                "INSERT INTO global_minute (ts, up_speed, dl_speed, alltime_ul, alltime_dl) VALUES (1, 0, 0, 0, 0)"
            )
        shorter = {**self.UNLIMITED, "minute": 90}
        for attempt in (shorter, {**shorter, "confirm_delete": False}, {**shorter, "confirm_delete": "true"}, {**shorter, "confirm_delete": 1}):
            status, body = self.post_retention(attempt)
            self.assertEqual(status, 409, attempt)
            self.assertEqual(body["deletes"], {"minute": 1})
        self.assertEqual(self.current()["retention"], self.UNLIMITED)
        self.assertFalse(self.state.force_prune)
        status, _ = self.post_retention({**shorter, "confirm_delete": True})
        self.assertEqual(status, 200)
        self.assertEqual(self.current()["retention"]["minute"], 90)
        self.assertTrue(self.state.force_prune)

    def test_longer_retention_needs_no_confirmation(self):
        self.post_retention({**self.UNLIMITED, "minute": 90, "confirm_delete": True})
        self.state.force_prune = False
        self.assertEqual(self.post_retention({**self.UNLIMITED, "minute": 365})[0], 200)
        self.assertEqual(self.post_retention(self.UNLIMITED)[0], 200)
        self.assertFalse(self.state.force_prune)

    def test_retention_rejects_bad_values_and_other_sites(self):
        for value in (1, "90", True, None):
            status, _ = self.post_retention({**self.UNLIMITED, "minute": value, "confirm_delete": True})
            self.assertEqual(status, 400, value)
        status, _ = self.post_retention(
            {**self.UNLIMITED, "minute": 90, "confirm_delete": True}, origin="https://evil.example"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.current()["retention"], self.UNLIMITED)

    def conn_write(self):
        import contextlib

        @contextlib.contextmanager
        def opened():
            conn = db.connect(self.path)
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

        return opened()

    def collect(self, content_type="application/json"):
        request = urllib.request.Request(
            self.base + "/api/collect", data=b"{}", headers={"Content-Type": content_type}
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_manual_collection_wakes_the_collector_with_a_cooldown(self):
        self.assertEqual(self.collect(content_type="text/plain"), 415)
        self.assertFalse(self.state.force_full)
        self.assertEqual(self.collect(), 202)
        self.assertTrue(self.state.force_full)
        self.assertTrue(self.state.wake.is_set())
        self.assertEqual(self.collect(), 429)

    def test_rejects_values_outside_the_allowed_list(self):
        for payload in (
            {"global_interval": 1, "torrent_interval": 300},
            {"global_interval": 60, "torrent_interval": 2},
            {"global_interval": "60", "torrent_interval": 300},
            {"global_interval": True, "torrent_interval": 300},
            {"global_interval": 300, "torrent_interval": 60},
            {},
            [],
        ):
            self.assertEqual(self.post(payload), 400, payload)
        self.assertEqual(self.post(b"{not json"), 400)
        self.assertEqual(self.current()["global_interval"], 60)
        self.assertFalse(self.state.wake.is_set())

    def test_rejects_requests_from_another_site(self):
        valid = {"global_interval": 120, "torrent_interval": 600}
        self.assertEqual(self.post(valid, origin="https://evil.example"), 403)
        self.assertEqual(self.post(valid, content_type="text/plain"), 415)
        self.assertEqual(self.current()["global_interval"], 60)
        same_origin = self.base
        self.assertEqual(self.post(valid, origin=same_origin), 200)


class BackfillTest(unittest.TestCase):
    def test_import_builds_hourly_totals_from_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(str(Path(tmp) / "b.db"))
            self.addCleanup(conn.close)
            base = {
                backfill.METRICS["alltime_ul"]: {3600: 100, 3660: 160},
                backfill.METRICS["alltime_dl"]: {3600: 50, 3660: 50},
                backfill.METRICS["up_speed"]: {3600: 1, 3660: 2},
                backfill.METRICS["dl_speed"]: {},
            }
            count = backfill.import_history(conn, lambda m, s, e: base[m], 3600, 3700)
            self.assertEqual(count, 2)
            row = conn.execute("SELECT up_bytes, samples FROM global_hourly").fetchone()
            self.assertEqual(tuple(row), (60, 2))


if __name__ == "__main__":
    unittest.main()
