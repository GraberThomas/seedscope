import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from seedscope.qbit import QbitAuthError, QbitClient


class FakeQbit(BaseHTTPRequestHandler):
    logins = 0
    post_paths: list[str] = []
    accept = True
    sync_calls: list[str] = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).post_paths.append(self.path)
        type(self).logins += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
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
        else:
            self._reply(200, b"[]")


class QbitClientTest(unittest.TestCase):
    def setUp(self):
        FakeQbit.logins = 0
        FakeQbit.post_paths = []
        FakeQbit.accept = True
        FakeQbit.sync_calls = []
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

    def test_only_the_login_is_ever_posted(self):
        self.client.login()
        self.client.server_state()
        self.client.torrents(active_only=True)
        self.assertEqual(FakeQbit.post_paths, ["/api/v2/auth/login"])


if __name__ == "__main__":
    unittest.main()
