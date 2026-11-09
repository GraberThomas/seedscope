"""HTTP server: static interface and JSON API."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import __version__, db, queries, settings, throttle
from .collector import LiveState
from .config import Config

log = logging.getLogger("seedscope.web")

MAX_SETTINGS_BODY = 1024
MANUAL_COLLECT_COOLDOWN = 10
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/charts.js": ("charts.js", "text/javascript; charset=utf-8"),
    "/table-sort.js": ("table-sort.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class Params:
    def __init__(self, query: str) -> None:
        self._values = parse_qs(query, keep_blank_values=True)

    def text(self, name: str, default: str = "") -> str:
        return self._values.get(name, [default])[0]

    def optional(self, name: str) -> str | None:
        values = self._values.get(name)
        return values[0] if values else None

    def integer(self, name: str, default: int, low: int, high: int) -> int:
        raw = self.text(name, str(default))
        try:
            value = int(raw)
        except ValueError:
            raise queries.BadRequest(f"{name} doit etre un entier") from None
        if not low <= value <= high:
            raise queries.BadRequest(f"{name} hors limites")
        return value

    def descending(self) -> bool:
        return self.text("dir", "desc") != "asc"


def make_handler(
    config: Config, state: LiveState, clock: Callable[[], float]
) -> type[BaseHTTPRequestHandler]:
    def api_summary(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return {
            **queries.summary(conn, now, state.snapshot()),
            "global_interval": settings.load(conn, config).global_interval,
        }

    def api_timeseries(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.timeseries(conn, params.text("range", "7d"), now)

    def api_calendar(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.calendar(conn, now)

    def api_heatmap(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.heatmap(conn, now, params.integer("days", 90, 1, 3650))

    def api_live(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        snapshot = state.snapshot()
        return {"ts": snapshot["active_ts"], "rows": snapshot["active"][:500]}

    def api_torrents(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.list_torrents(
            conn,
            now,
            sort=params.text("sort", "uploaded"),
            descending=params.descending(),
            q=params.text("q")[:200],
            tracker=params.optional("tracker"),
            category=params.optional("category"),
            tag=params.optional("tag"),
            status=params.text("status", "active"),
            window=params.integer("window", 7, 1, 365),
            known_swarm=params.text("known_swarm") == "1",
            limit=params.integer("limit", 100, 1, 500),
            offset=params.integer("offset", 0, 0, 10_000_000),
        )

    def api_filters(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.filters(conn)

    def api_contents(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        dead = params.optional("dead_days")
        return queries.list_contents(
            conn,
            now,
            sort=params.text("sort", "efficiency"),
            descending=params.descending(),
            q=params.text("q")[:200],
            tracker=params.optional("tracker"),
            category=params.optional("category"),
            tag=params.optional("tag"),
            known_swarm=params.text("known_swarm") == "1",
            dead_days=params.integer("dead_days", 30, 1, 3650) if dead is not None else None,
            window=params.integer("window", 30, 1, 365),
            limit=params.integer("limit", 100, 1, 500),
            offset=params.integer("offset", 0, 0, 10_000_000),
        )

    def api_breakdown(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.breakdown(conn, params.text("by", "tracker"), params.text("range", "30d"), now)

    def api_contents_summary(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.contents_summary(conn)

    def api_contents_top(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.top_contents(conn, params.text("range", "7d"), now)

    def api_content(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.content_detail(conn, params.text("key"), params.text("range", "30d"), now)

    def api_popularity(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return queries.popularity(conn)

    def api_settings(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return settings_payload(conn)

    def settings_payload(conn: sqlite3.Connection) -> dict[str, Any]:
        current = settings.load(conn, config)
        return {
            "global_interval": current.global_interval,
            "torrent_interval": current.torrent_interval,
            "global_options": settings.GLOBAL_OPTIONS,
            "torrent_options": settings.TORRENT_OPTIONS,
            "retention": asdict(settings.load_retention(conn)),
            "retention_options": settings.RETENTION_OPTIONS,
            "storage": settings.storage(conn, config.db_path),
            "throttle": asdict(throttle.load(conn)),
            "throttle_limit_options": throttle.LIMIT_OPTIONS,
            "throttle_delay_options": throttle.DELAY_OPTIONS,
        }

    def api_throttle(conn: sqlite3.Connection, params: Params, now: int) -> Any:
        return {**throttle.status(conn, now), "error": state.snapshot()["throttle_error"]}

    routes: dict[str, Callable[[sqlite3.Connection, Params, int], Any]] = {
        "/api/summary": api_summary,
        "/api/timeseries": api_timeseries,
        "/api/calendar": api_calendar,
        "/api/heatmap": api_heatmap,
        "/api/live": api_live,
        "/api/torrents": api_torrents,
        "/api/filters": api_filters,
        "/api/contents": api_contents,
        "/api/contents/summary": api_contents_summary,
        "/api/contents/top": api_contents_top,
        "/api/breakdown": api_breakdown,
        "/api/popularity": api_popularity,
        "/api/settings": api_settings,
        "/api/throttle": api_throttle,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = f"SeedScope/{__version__}"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            path = parts.path
            try:
                if path == "/healthz":
                    self._json({"status": "ok", "version": __version__})
                elif path in STATIC_FILES:
                    filename, content_type = STATIC_FILES[path]
                    self._send(HTTPStatus.OK, (STATIC_DIR / filename).read_bytes(), content_type)
                elif path in routes:
                    self._api(lambda conn, now: routes[path](conn, Params(parts.query), now))
                elif path == "/api/content":
                    self._content(Params(parts.query))
                elif path.startswith("/api/torrent/"):
                    self._torrent(path.removeprefix("/api/torrent/"), Params(parts.query))
                else:
                    self._json({"error": "Introuvable"}, HTTPStatus.NOT_FOUND)
            except queries.BadRequest as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("Erreur sur %s", path)
                self._json({"error": "Erreur interne"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        do_HEAD = do_GET  # noqa: N815

        def _api(self, query: Callable[[sqlite3.Connection, int], Any]) -> None:
            conn = db.connect_readonly(config.db_path)
            try:
                self._json(query(conn, int(clock())))
            finally:
                conn.close()

        def _content(self, params: Params) -> None:
            conn = db.connect_readonly(config.db_path)
            try:
                detail = api_content(conn, params, int(clock()))
            finally:
                conn.close()
            if detail is None:
                self._json({"error": "Contenu introuvable"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(detail)

        def _torrent(self, torrent_hash: str, params: Params) -> None:
            conn = db.connect_readonly(config.db_path)
            try:
                detail = queries.torrent_detail(
                    conn, torrent_hash, params.text("range", "30d"), int(clock())
                )
            finally:
                conn.close()
            if detail is None:
                self._json({"error": "Torrent introuvable"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(detail)

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path not in ("/api/settings", "/api/settings/retention", "/api/settings/throttle", "/api/collect"):
                self._json({"error": "Methode non autorisee"}, HTTPStatus.METHOD_NOT_ALLOWED)
                return
            try:
                body = self._read_json_body()
                if body is None:
                    return
                if path == "/api/settings":
                    self._save_settings(body)
                elif path == "/api/settings/retention":
                    self._save_retention(body)
                elif path == "/api/settings/throttle":
                    self._save_throttle(body)
                else:
                    self._request_collection()
            except (settings.InvalidSetting, throttle.InvalidThrottle) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                log.exception("Erreur sur %s", path)
                self._json({"error": "Erreur interne"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def _read_json_body(self) -> dict[str, Any] | None:
            """Return the JSON object sent, or answer with an error and return None."""
            # Another website must not be able to act through a visitor's browser:
            # a JSON body forces a CORS preflight, which this server never accepts.
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
            if content_type != "application/json":
                self._json({"error": "JSON attendu"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return None
            origin = self.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc != self.headers.get("Host"):
                self._json({"error": "Origine refusee"}, HTTPStatus.FORBIDDEN)
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_SETTINGS_BODY:
                self._json({"error": "Taille invalide"}, HTTPStatus.BAD_REQUEST)
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except ValueError:
                self._json({"error": "JSON invalide"}, HTTPStatus.BAD_REQUEST)
                return None
            if not isinstance(body, dict):
                self._json({"error": "JSON invalide"}, HTTPStatus.BAD_REQUEST)
                return None
            return body

        def _save_settings(self, body: dict[str, Any]) -> None:
            intervals = settings.validate(body.get("global_interval"), body.get("torrent_interval"))
            conn = db.connect(config.db_path)
            try:
                settings.save(conn, intervals)
            finally:
                conn.close()
            state.wake.set()
            self._json(
                {
                    "global_interval": intervals.global_interval,
                    "torrent_interval": intervals.torrent_interval,
                }
            )

        def _save_retention(self, body: dict[str, Any]) -> None:
            new = settings.validate_retention(body)
            conn = db.connect(config.db_path)
            try:
                current = settings.load_retention(conn)
                names = settings.tightened(current, new)
                # Deleting history needs an explicit agreement: only a literal true counts.
                if names and body.get("confirm_delete") is not True:
                    self._json(
                        {
                            "requires_confirmation": True,
                            "deletes": settings.count_expired(conn, new, names, int(clock())),
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                settings.save_retention(conn, new)
            finally:
                conn.close()
            if names:
                state.request_prune()
            self._json({"retention": asdict(new)})

        def _save_throttle(self, body: dict[str, Any]) -> None:
            value = throttle.validate(body)
            conn = db.connect(config.db_path)
            try:
                throttle.save(conn, value)
            finally:
                conn.close()
            state.wake.set()  # applied, or lifted, at once
            self._json(asdict(value))

        def _request_collection(self) -> None:
            previous = state.snapshot()["last_full_success"]
            if not state.request_full_collection(clock(), MANUAL_COLLECT_COOLDOWN):
                self._json(
                    {"error": "Collecte deja demandee, patientez quelques secondes"},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            self._json({"previous_full_success": previous}, HTTPStatus.ACCEPTED)

    return Handler


def make_server(
    config: Config, state: LiveState, clock: Callable[[], float] = time.time
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config, state, clock))
    server.daemon_threads = True
    return server
