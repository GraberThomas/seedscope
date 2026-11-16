import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.helpers import GIB

from seedscope import accounts, db
from seedscope.accounts import AccountError, AccountPoller, AccountsState
from seedscope.collector import LiveState
from seedscope.config import Config, ConfigError
from seedscope.web import make_server

SECRET = "0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000

GEMINI = {
    "username": "someone", "group": "Membre", "uploaded": "636.57 GiB", "downloaded": "2.09 GiB",
    "ratio": "304.45", "buffer": "846.66 GiB", "seeding": 89, "leeching": 0, "seedbonus": "140042.11",
    "hit_and_runs": 0,
}
TR4KER = {
    "id": 1, "username": "someone", "uploaded": 816325878692, "downloaded": 541206984431,
    "bonus_upload": 6758131040256, "bonus_download": 1073741824, "money": 7247,
    "api_key": SECRET, "passkey": SECRET,
}
V3X = {
    "username": "someone", "uploaded": 402156594754, "downloaded": 157497297792, "ratio": 2.553,
    "buffer": 244659296962, "downloadedReal": 582378645802, "seeding": 174, "leeching": 0,
    "bonusPoints": 85715, "pointsPerHour": 198, "hitAndRun": 0, "hitAndRunMax": 3, "level": 34,
    "ratioFloor": {"floor": 0.5, "warnAt": 0.7, "level": "ok"}, "freeleechTokens": 2,
}
C411 = {
    "username": "someone", "avatarUrl": f"/a/{SECRET}.png", "level": {"level": 14},
    "zen": {"balance": 259},
    "transfer": {
        "uploaded": {"actual": 312989992396, "credit": 155692564480, "total": 468682556876},
        "downloaded": {"actual": 180747831505, "credit": 0, "total": 180747831505},
    },
}


def extras(sample):
    return {e.label: e.value for e in sample.extras}


class ParseTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(accounts.parse_size("2 GiB"), 2 * GIB)
        self.assertEqual(accounts.parse_size("1.5 KB"), 1500)
        self.assertEqual(accounts.parse_size(42), 42)
        self.assertEqual(accounts.parse_size("12"), 12)
        self.assertIsNone(accounts.parse_size("beaucoup"))
        self.assertIsNone(accounts.parse_size(None))

    def test_gemini(self):
        sample = accounts.parse_gemini(GEMINI)
        self.assertEqual(sample.uploaded, round(636.57 * GIB))
        self.assertAlmostEqual(sample.ratio, 304.45)
        self.assertEqual(sample.seeding, 89)
        self.assertAlmostEqual(sample.bonus, 140042.11)
        self.assertEqual(extras(sample)["Hit and run"], 0)

    def test_tr4ker_computes_both_ratios(self):
        sample = accounts.parse_tr4ker(TR4KER)
        self.assertAlmostEqual(sample.ratio, 1.508, places=3)
        self.assertAlmostEqual(extras(sample)["Ratio avec les bonus"], 13.968, places=3)
        self.assertIsNone(sample.buffer)

    def test_v3x(self):
        sample = accounts.parse_v3x(V3X)
        self.assertAlmostEqual(sample.ratio, 2.553)
        self.assertEqual(extras(sample)["Ratio minimum"], 0.5)
        self.assertEqual(extras(sample)["Reçu réellement"], 582378645802)

    def test_c411_counts_credit(self):
        sample = accounts.parse_c411(C411)
        self.assertAlmostEqual(sample.ratio, 2.593, places=3)
        self.assertAlmostEqual(extras(sample)["Ratio sans crédit"], 1.732, places=3)
        self.assertEqual(sample.bonus, 259)

    def test_nothing_downloaded_gives_no_ratio(self):
        self.assertIsNone(accounts.parse_tr4ker({"uploaded": 10, "downloaded": 0}).ratio)


class FetchTest(unittest.TestCase):
    def setUp(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):  # noqa: N802
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/html"):
                    body, kind = b"<html>login</html>", "text/html"
                elif self.path.startswith("/denied"):
                    self.send_response(401)
                    self.end_headers()
                    return
                else:
                    body, kind = json.dumps(V3X).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def provider(self, path, auth="bearer"):
        return accounts.Provider("v3x", "V3X", self.base + path, auth, accounts.parse_v3x)

    def test_bearer_and_query(self):
        self.assertEqual(accounts.fetch(self.provider("/me"), SECRET, None)["ratio"], 2.553)
        accounts.fetch(self.provider("/me", "query"), SECRET, None)
        self.assertEqual(self.seen[0], ("/me", f"Bearer {SECRET}"))
        self.assertEqual(self.seen[1], (f"/me?apikey={SECRET}", None))

    def test_errors_never_quote_the_key(self):
        for path in ("/html", "/denied"):
            with self.assertRaises(AccountError) as caught:
                accounts.fetch(self.provider(path, "query"), SECRET, None)
            self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(AccountError) as caught:
            accounts.fetch(accounts.Provider("x", "X", f"http://127.0.0.1:1/{SECRET}", "query", accounts.parse_v3x), SECRET, None)
        self.assertNotIn(SECRET, str(caught.exception))


class ConfigTest(unittest.TestCase):
    BASE = {"QBT_URL": "http://q", "QBT_USERNAME": "u", "QBT_PASSWORD": "p"}

    def test_keys_and_defaults(self):
        config = Config.from_env({**self.BASE, "SEEDSCOPE_ACCOUNT_V3X_KEY": SECRET, "SEEDSCOPE_ACCOUNT_C411_KEY": " "})
        self.assertEqual(config.account_keys, {"v3x": SECRET})
        self.assertIsNone(config.account_proxy)
        self.assertEqual(config.account_interval, 3600)
        self.assertNotIn(SECRET, repr(config))

    def test_interval_floor(self):
        with self.assertRaises(ConfigError):
            Config.from_env({**self.BASE, "SEEDSCOPE_ACCOUNT_INTERVAL": "60"})


class PollerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "accounts.db")
        db.connect(self.path).close()
        self.state = AccountsState()
        self.calls = []

    def poller(self, answers, keys=None):
        def fetcher(provider, key, proxy):
            self.calls.append((provider.key, key, proxy))
            answer = answers[provider.key]
            if isinstance(answer, Exception):
                raise answer
            return answer
        keys = keys or {name: SECRET for name in answers}
        return AccountPoller(self.path, keys, "http://proxy:8888", 3600, self.state,
                             fetcher=fetcher, clock=lambda: NOW, pause=0)

    def test_stores_figures_and_never_secrets(self):
        self.poller({"gemini": GEMINI, "tr4ker": TR4KER, "v3x": V3X, "c411": C411}).poll_once()
        self.assertEqual(sorted(c[0] for c in self.calls), ["c411", "gemini", "tr4ker", "v3x"])
        self.assertTrue(all(c[2] == "http://proxy:8888" for c in self.calls))
        conn = db.connect(self.path)
        rows = conn.execute("SELECT * FROM account_samples").fetchall()
        conn.close()
        self.assertEqual(len(rows), 4)
        dump = json.dumps([dict(r) for r in rows])
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("someone", dump)
        self.assertEqual(self.state.snapshot()["v3x"]["last_success"], NOW)

    def test_error_is_recorded_without_the_key(self):
        self.poller({"v3x": AccountError(f"refus pour {SECRET}"), "c411": C411}).poll_once()
        status = self.state.snapshot()
        self.assertNotIn(SECRET, status["v3x"]["last_error"])
        self.assertEqual(status["v3x"]["last_success"], 0)
        self.assertEqual(status["c411"]["last_error"], "")

    def test_unknown_answer_is_an_error(self):
        self.poller({"v3x": {"message": "nope"}}).poll_once()
        self.assertIn("compteurs", self.state.snapshot()["v3x"]["last_error"])

    def test_unknown_tracker_is_ignored(self):
        poller = self.poller({"v3x": V3X}, keys={"v3x": SECRET, "other": SECRET})
        self.assertEqual(list(poller.keys), ["v3x"])


class OverviewApiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "api.db")
        conn = db.connect(path)
        with conn:
            for hours_ago, up in ((48, 100 * GIB), (2, 110 * GIB), (1, 112 * GIB)):
                sample = accounts.AccountSample(uploaded=up, downloaded=50 * GIB, ratio=up / (50 * GIB), bonus=10.0)
                accounts.store_sample(conn, "v3x", NOW - hours_ago * 3600, sample)
        conn.close()
        config = Config("http://q", "u", "p", db_path=path, host="127.0.0.1", port=0,
                        account_keys={"v3x": SECRET, "c411": SECRET})
        state = AccountsState()
        state.record("c411", NOW - 60, "HTTP 401 (clé refusée)")
        server = make_server(config, LiveState(), clock=lambda: NOW, accounts_state=state)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return json.load(response)

    def test_trackers_in_order_with_change_over_the_range(self):
        data = self.get("/api/accounts?range=24h")
        self.assertEqual([t["key"] for t in data["trackers"]], ["v3x", "c411"])
        v3x, c411 = data["trackers"]
        self.assertEqual(v3x["latest"]["uploaded"], 112 * GIB)
        self.assertEqual(v3x["change"]["uploaded"], 2 * GIB)
        self.assertEqual(len(v3x["series"]["ts"]), 2)
        self.assertIsNone(c411["latest"])
        self.assertIn("401", c411["last_error"])
        self.assertNotIn(SECRET, json.dumps(data))

    def test_all_time(self):
        v3x = self.get("/api/accounts?range=all")["trackers"][0]
        self.assertEqual(v3x["change"]["uploaded"], 12 * GIB)
        self.assertEqual(len(v3x["series"]["ts"]), 3)

    def test_bad_range(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/accounts?range=2d")
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
