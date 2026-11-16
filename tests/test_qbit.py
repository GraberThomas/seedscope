import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from seedscope.qbit import QbitAuthError, QbitClient


class FakeQbit(BaseHTTPRequestHandler):
    logins = 0
    post_paths: list[str] = []
    accept = True
    sync_calls: list[str] = []
    bodies: list[bytes] = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).post_paths.append(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path != "/api/v2/auth/login":
            type(self).bodies.append(body)
            self._reply(200, b"")
            return
        type(self).logins += 1
        if not type(self).accept:
            self._reply(200, b"Fails.")
            return
        self.send_response(200)
        self.send_header("Set-Cookie", f"QBT_SID_8080=session{type(self).logins}; path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if "QBT_SID_8080=session" not in self.headers.get("Cookie", ""):
            self._reply(403, b"Forbidden")
        elif self.path.startswith("/api/v2/sync/maindata"):
            type(self).sync_calls.append(self.path)
            if self.path.endswith("rid=0"):
                payload = {"rid": 1, "full_update": True, "server_state": {"alltime_ul": 10, "alltime_dl": 5}}
            else:
                payload = {"rid": 2, "server_state": {"alltime_ul": 20}}
            self._reply(200, json.dumps(payload).encode())
        elif self.path == "/api/v2/app/preferences":
            self._reply(200, json.dumps({"up_limit": 2048, "alt_up_limit": 10240}).encode())
        else:
            self._reply(200, b"[]")


class QbitClientTest(unittest.TestCase):
    def setUp(self):
        FakeQbit.logins = 0
        FakeQbit.post_paths = []
        FakeQbit.accept = True
        FakeQbit.sync_calls = []
        FakeQbit.bodies = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeQbit)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = QbitClient(f"http://127.0.0.1:{self.server.server_port}", "u", "p")

    def test_logs_in_again_when_the_session_expired(self):
        self.assertEqual(self.client.torrents(), [])
        self.assertEqual(FakeQbit.logins, 1)

    def test_refused_credentials_raise_and_are_not_retried(self):
        FakeQbit.accept = False
        with self.assertRaises(QbitAuthError):
            self.client.torrents()
        self.assertEqual(FakeQbit.logins, 1)

    def test_server_state_merges_partial_updates(self):
        self.client.login()
        self.assertEqual(self.client.server_state()["alltime_ul"], 10)
        state = self.client.server_state()
        self.assertEqual(state, {"alltime_ul": 20, "alltime_dl": 5})
        self.assertEqual(FakeQbit.sync_calls[1], "/api/v2/sync/maindata?rid=1")

    def test_upload_limit_writes_the_normal_limit_only(self):
        self.client.login()
        self.assertEqual(self.client.upload_limit(), 2048)
        self.client.set_upload_limit(1048576)
        self.assertEqual(FakeQbit.post_paths[-1], "/api/v2/app/setPreferences")
        sent = urllib.parse.parse_qs(FakeQbit.bodies[-1].decode())
        self.assertEqual(json.loads(sent["json"][0]), {"up_limit": 1048576})

    def test_reading_never_posts_anything_but_the_login(self):
        self.client.login()
        self.client.server_state()
        self.client.torrents(active_only=True)
        self.assertEqual(FakeQbit.post_paths, ["/api/v2/auth/login"])


if __name__ == "__main__":
    unittest.main()
