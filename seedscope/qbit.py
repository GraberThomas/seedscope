"""Read-only client for the qBittorrent WebUI API.

The only POST this client ever sends is the login. It exposes no method able to
change a torrent or a setting.
"""

from __future__ import annotations

import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class QbitError(Exception):
    """qBittorrent could not be reached or answered unexpectedly."""


class QbitAuthError(QbitError):
    """qBittorrent refused the credentials."""


class QbitClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 30) -> None:
        self._base = base_url.rstrip("/")
        self._credentials = urllib.parse.urlencode(
            {"username": username, "password": password}
        ).encode()
        self._timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self._rid = 0
        self._server_state: dict[str, Any] = {}

    def login(self) -> None:
        request = urllib.request.Request(
            f"{self._base}/api/v2/auth/login",
            data=self._credentials,
            headers={"Referer": self._base},
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(64)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise QbitAuthError(f"connexion refusee (HTTP {exc.code})") from None
            raise QbitError(f"connexion impossible (HTTP {exc.code})") from None
        except OSError as exc:
            raise QbitError(f"qBittorrent injoignable : {exc}") from None
        if body.strip() == b"Fails.":
            raise QbitAuthError("identifiants refuses")
        self._rid = 0
        self._server_state = {}

    def _get(self, path: str, retry_login: bool = True) -> Any:
        request = urllib.request.Request(
            f"{self._base}{path}", headers={"Referer": self._base}
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and retry_login:
                self.login()
                return self._get(path, retry_login=False)
            if exc.code in (401, 403):
                raise QbitAuthError(f"acces refuse (HTTP {exc.code})") from None
            raise QbitError(f"{path.split('?')[0]} : HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise QbitError(f"{path.split('?')[0]} : {exc}") from None

    def server_state(self) -> dict[str, Any]:
        """Return global counters, using incremental sync to avoid large payloads.

        ``sync/maindata`` is the only endpoint exposing all-time totals. Its first
        answer lists every torrent; later ones only hold what changed, so fields are
        merged into a persistent state.
        """
        data = self._get(f"/api/v2/sync/maindata?rid={self._rid}")
        if data.get("full_update"):
            self._server_state = {}
        self._server_state.update(data.get("server_state") or {})
        self._rid = int(data.get("rid", 0))
        return dict(self._server_state)

    def torrents(self, active_only: bool = False) -> list[dict[str, Any]]:
        suffix = "?filter=active" if active_only else ""
        return self._get(f"/api/v2/torrents/info{suffix}")
