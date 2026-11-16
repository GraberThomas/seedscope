"""Account statistics read from the trackers' own APIs (ratio, upload, bonus...).

qBittorrent only knows what this client sent: the trackers also count freeleech,
bonuses and other clients. Each tracker has its own API and answer format, so every
supported tracker has a small parser that keeps a fixed list of figures: the answers
also hold private data (passkey, API key, e-mail) which must never be stored.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import db

log = logging.getLogger("seedscope.accounts")

MAX_BODY = 1024 * 1024
TIMEOUT = 30
PAUSE_BETWEEN_TRACKERS = 5
FIRST_POLL_DELAY = 20
USER_AGENT = "SeedScope"

# Units used by UNIT3D sites, such as "636.57 GiB".
_UNITS = {
    "b": 1, "o": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4, "pb": 1000**5,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4, "pib": 1024**5,
}
_SIZE = re.compile(r"^\s*(-?[\d\s.,]+)\s*([a-zA-Z]*)\s*$")


class AccountError(Exception):
    """The tracker answered nothing usable. The message never contains the key."""


def parse_size(value: Any) -> int | None:
    """Bytes from a number, or from a text such as ``636.57 GiB``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _SIZE.match(str(value))
    if not match:
        return None
    number_text, unit = match.groups()
    unit_factor = _UNITS.get(unit.lower() or "b")
    if unit_factor is None:
        return None
    try:
        number = float(number_text.replace(" ", "").replace(",", ""))
    except ValueError:
        return None
    return int(round(number * unit_factor))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _ratio(uploaded: int | None, downloaded: int | None) -> float | None:
    if uploaded is None or not downloaded:
        return None
    return uploaded / downloaded


def _buffer(uploaded: int | None, downloaded: int | None, min_ratio: float) -> int | None:
    """What can still be downloaded before the ratio falls under the site's minimum."""
    if uploaded is None or downloaded is None:
        return None
    return int(uploaded / min_ratio) - downloaded


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, Mapping):
            return None
        data = data.get(key)
    return data


@dataclass(frozen=True)
class Extra:
    """A tracker-specific figure. kind: bytes, ratio, count or number."""

    label: str
    kind: str
    value: float | int | None


@dataclass(frozen=True)
class AccountSample:
    uploaded: int | None
    downloaded: int | None
    ratio: float | None
    buffer: int | None = None
    seeding: int | None = None
    leeching: int | None = None
    bonus: float | None = None
    extras: tuple[Extra, ...] = ()


def parse_gemini(data: Mapping[str, Any]) -> AccountSample:
    """UNIT3D ``/api/user``: sizes come as text."""
    uploaded = parse_size(data.get("uploaded"))
    downloaded = parse_size(data.get("downloaded"))
    ratio = _number(data.get("ratio"))
    return AccountSample(
        uploaded=uploaded,
        downloaded=downloaded,
        ratio=ratio if ratio is not None else _ratio(uploaded, downloaded),
        buffer=parse_size(data.get("buffer")),
        seeding=_integer(data.get("seeding")),
        leeching=_integer(data.get("leeching")),
        bonus=_number(data.get("seedbonus")),
        extras=(Extra("Hit and run", "count", _integer(data.get("hit_and_runs"))),),
    )


def parse_tr4ker(data: Mapping[str, Any]) -> AccountSample:
    """TR4KER ``/api/me``: raw bytes and bonus counters apart, no ratio.

    The site adds the bonus to the transfer it shows (6.89 TiB = real upload + bonus),
    so the official figures are the sums; the real ones are kept as extras.
    """
    real_up = _integer(data.get("uploaded"))
    real_down = _integer(data.get("downloaded"))
    bonus_up = _integer(data.get("bonus_upload"))
    bonus_down = _integer(data.get("bonus_download"))
    uploaded = None if real_up is None else real_up + (bonus_up or 0)
    downloaded = None if real_down is None else real_down + (bonus_down or 0)
    buffer = None if uploaded is None or downloaded is None else uploaded - downloaded
    return AccountSample(
        uploaded=uploaded,
        downloaded=downloaded,
        ratio=_ratio(uploaded, downloaded),
        buffer=buffer,
        bonus=_number(data.get("money")),
        extras=(
            Extra("Ratio sans les bonus", "ratio", _ratio(real_up, real_down)),
            Extra("Envoyé réellement", "bytes", real_up),
            Extra("Bonus d’envoi", "bytes", bonus_up),
            Extra("Bonus de réception", "bytes", bonus_down),
        ),
    )


def parse_v3x(data: Mapping[str, Any]) -> AccountSample:
    """V3X ``/api/me``: raw bytes and a ratio computed by the site."""
    return AccountSample(
        uploaded=_integer(data.get("uploaded")),
        downloaded=_integer(data.get("downloaded")),
        ratio=_number(data.get("ratio")),
        buffer=_integer(data.get("buffer")),
        seeding=_integer(data.get("seeding")),
        leeching=_integer(data.get("leeching")),
        bonus=_number(data.get("bonusPoints")),
        extras=(
            Extra("Reçu réellement", "bytes", _integer(data.get("downloadedReal"))),
            Extra("Ratio minimum", "ratio", _number(_dig(data, "ratioFloor", "floor"))),
            Extra("Hit and run", "count", _integer(data.get("hitAndRun"))),
            Extra("Hit and run tolérés", "count", _integer(data.get("hitAndRunMax"))),
            Extra("Points par heure", "number", _number(data.get("pointsPerHour"))),
            Extra("Niveau", "count", _integer(data.get("level"))),
        ),
    )


# Not in the API: deduced from the buffer the site shows (377 GiB = 436.49 / 0.8 - 168.33).
C411_MIN_RATIO = 0.8


def parse_c411(data: Mapping[str, Any]) -> AccountSample:
    """C411 ``/api/user/profile``: real transfer plus credit, no ratio nor buffer."""
    uploaded = _integer(_dig(data, "transfer", "uploaded", "total"))
    downloaded = _integer(_dig(data, "transfer", "downloaded", "total"))
    up_actual = _integer(_dig(data, "transfer", "uploaded", "actual"))
    down_actual = _integer(_dig(data, "transfer", "downloaded", "actual"))
    return AccountSample(
        uploaded=uploaded,
        downloaded=downloaded,
        ratio=_ratio(uploaded, downloaded),
        buffer=_buffer(uploaded, downloaded, C411_MIN_RATIO),
        bonus=_number(_dig(data, "zen", "balance")),
        extras=(
            Extra("Ratio sans crédit", "ratio", _ratio(up_actual, down_actual)),
            Extra("Envoyé réellement", "bytes", up_actual),
            Extra("Crédit d’envoi", "bytes", _integer(_dig(data, "transfer", "uploaded", "credit"))),
            Extra("Ratio minimum", "ratio", C411_MIN_RATIO),
            Extra("Niveau", "count", _integer(_dig(data, "level", "level"))),
        ),
    )


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    url: str
    auth: str  # "bearer" (Authorization header) or "query" (?apikey=)
    parse: Callable[[Mapping[str, Any]], AccountSample]
    bonus_label: str = "Points bonus"


PROVIDERS: dict[str, Provider] = {
    provider.key: provider
    for provider in (
        Provider("gemini", "Gemini", "https://gemini-tracker.org/api/user", "bearer", parse_gemini),
        Provider("tr4ker", "TR4KER", "https://tr4ker.net/api/me", "query", parse_tr4ker, "Monnaie"),
        Provider("v3x", "V3X", "https://api.v3x.club/api/me", "bearer", parse_v3x),
        Provider("c411", "C411", "https://c411.org/api/user/profile", "bearer", parse_c411, "Zen"),
    )
}


def fetch(provider: Provider, key: str, proxy: str | None) -> Mapping[str, Any]:
    """Ask one tracker for the account figures. Errors never quote the key."""
    url = provider.url
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if provider.auth == "query":
        url += "?" + urllib.parse.urlencode({"apikey": key})
    else:
        headers["Authorization"] = f"Bearer {key}"
    # Without a proxy, ignore any proxy from the environment: the choice must be explicit.
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
            body = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        reason = "clé refusée" if exc.code in (401, 403) else "réponse inattendue"
        raise AccountError(f"HTTP {exc.code} ({reason})") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AccountError(f"Tracker injoignable : {str(reason).replace(key, '***')}") from None
    if len(body) > MAX_BODY:
        raise AccountError("Réponse trop longue")
    try:
        data = json.loads(body)
    except ValueError:
        # A login page instead of JSON: the key does not open this API.
        raise AccountError("Réponse qui n’est pas du JSON (clé sans accès au profil ?)") from None
    if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping) and "uploaded" not in data:
        data = data["data"]
    if not isinstance(data, Mapping):
        raise AccountError("Réponse JSON inattendue")
    return data


def store_sample(conn: sqlite3.Connection, tracker: str, ts: int, sample: AccountSample) -> None:
    extras = [{"label": e.label, "kind": e.kind, "value": e.value} for e in sample.extras]
    conn.execute(
        "INSERT OR REPLACE INTO account_samples (tracker, ts, uploaded, downloaded, ratio, buffer, "
        "seeding, leeching, bonus, extras) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tracker, ts, sample.uploaded, sample.downloaded, sample.ratio, sample.buffer,
            sample.seeding, sample.leeching, sample.bonus,
            json.dumps(extras, ensure_ascii=False, separators=(",", ":")),
        ),
    )


@dataclass
class AccountStatus:
    last_success: int = 0
    last_attempt: int = 0
    last_error: str = ""


@dataclass
class AccountsState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    statuses: dict[str, AccountStatus] = field(default_factory=dict)
    wake: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {
                key: {"last_success": s.last_success, "last_attempt": s.last_attempt, "last_error": s.last_error}
                for key, s in self.statuses.items()
            }

    def record(self, tracker: str, now: int, error: str = "") -> None:
        with self.lock:
            status = self.statuses.setdefault(tracker, AccountStatus())
            status.last_attempt = now
            status.last_error = error
            if not error:
                status.last_success = now


Fetcher = Callable[[Provider, str, "str | None"], Mapping[str, Any]]


class AccountPoller:
    """Polls each configured tracker in turn, once per interval."""

    def __init__(
        self,
        db_path: str,
        keys: Mapping[str, str],
        proxy: str | None,
        interval: int,
        state: AccountsState,
        fetcher: Fetcher = fetch,
        clock: Callable[[], float] = time.time,
        pause: float = PAUSE_BETWEEN_TRACKERS,
    ) -> None:
        self.db_path = db_path
        self.keys = {k: v for k, v in keys.items() if k in PROVIDERS and v}
        self.proxy = proxy
        self.interval = interval
        self.state = state
        self.fetcher = fetcher
        self.clock = clock
        self.pause = pause

    def poll_once(self, stop: threading.Event | None = None) -> None:
        conn = db.connect(self.db_path)
        try:
            for index, (tracker, key) in enumerate(sorted(self.keys.items())):
                if stop is not None and stop.is_set():
                    return
                if index and stop is not None and stop.wait(self.pause):
                    return
                self._poll(conn, tracker, key)
        finally:
            conn.close()

    def _poll(self, conn: sqlite3.Connection, tracker: str, key: str) -> None:
        provider = PROVIDERS[tracker]
        now = int(self.clock())
        try:
            sample = provider.parse(self.fetcher(provider, key, self.proxy))
            if sample.uploaded is None and sample.downloaded is None:
                raise AccountError("Réponse sans compteurs d’envoi ni de réception")
            with conn:
                store_sample(conn, tracker, now, sample)
        except AccountError as exc:
            message = str(exc).replace(key, "***")
            log.warning("Compte %s : %s", provider.label, message)
            self.state.record(tracker, now, message)
            return
        except sqlite3.Error as exc:
            log.warning("Compte %s : base de données : %s", provider.label, exc)
            self.state.record(tracker, now, "Erreur de base de données")
            return
        except Exception:  # a parser surprised by a new answer format
            log.exception("Compte %s : réponse non comprise", provider.label)
            self.state.record(tracker, now, "Réponse non comprise")
            return
        self.state.record(tracker, now)

    def run(self, stop: threading.Event) -> None:
        if not self.keys:
            return
        if stop.wait(FIRST_POLL_DELAY):
            return
        while not stop.is_set():
            started = time.time()
            self.poll_once(stop)
            self.state.wake.wait(max(1.0, self.interval - (time.time() - started)))
            self.state.wake.clear()


# ---- Queries for the web layer ----

MAX_POINTS = 1500


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        extras = json.loads(row["extras"] or "[]")
    except ValueError:
        extras = []
    return {
        "ts": row["ts"], "uploaded": row["uploaded"], "downloaded": row["downloaded"],
        "ratio": row["ratio"], "buffer": row["buffer"], "seeding": row["seeding"],
        "leeching": row["leeching"], "bonus": row["bonus"], "extras": extras,
    }


def overview(
    conn: sqlite3.Connection,
    configured: Mapping[str, str],
    statuses: Mapping[str, Mapping[str, Any]],
    since: int | None,
    interval: int,
) -> dict[str, Any]:
    """Latest figures, change since ``since`` and history of every tracker, in order."""
    known = {r[0] for r in conn.execute("SELECT DISTINCT tracker FROM account_samples")}
    trackers = []
    for key, provider in PROVIDERS.items():
        if key not in configured and key not in known:
            continue
        latest = _row(conn.execute(
            "SELECT * FROM account_samples WHERE tracker = ? ORDER BY ts DESC LIMIT 1", (key,)
        ).fetchone())
        start_clause, args = ("AND ts >= ?", (key, since)) if since is not None else ("", (key,))
        first = _row(conn.execute(
            f"SELECT * FROM account_samples WHERE tracker = ? {start_clause} ORDER BY ts LIMIT 1", args
        ).fetchone())
        rows = conn.execute(
            f"SELECT ts, uploaded, downloaded, ratio, buffer FROM account_samples "
            f"WHERE tracker = ? {start_clause} ORDER BY ts",
            args,
        ).fetchall()
        step = max(1, -(-len(rows) // MAX_POINTS))
        kept = rows[::step]
        if rows and kept[-1] is not rows[-1]:
            kept.append(rows[-1])
        change = None
        if latest and first and first["ts"] < latest["ts"]:
            change = {
                "since": first["ts"],
                "uploaded": _diff(latest["uploaded"], first["uploaded"]),
                "downloaded": _diff(latest["downloaded"], first["downloaded"]),
                "ratio": _diff(latest["ratio"], first["ratio"]),
                "bonus": _diff(latest["bonus"], first["bonus"]),
            }
        status = statuses.get(key, {})
        trackers.append({
            "key": key,
            "label": provider.label,
            "bonus_label": provider.bonus_label,
            "configured": key in configured,
            "last_success": status.get("last_success", 0) or (latest["ts"] if latest else 0),
            "last_attempt": status.get("last_attempt", 0),
            "last_error": status.get("last_error", ""),
            "latest": latest,
            "change": change,
            "series": {
                "ts": [r["ts"] for r in kept],
                "ratio": [r["ratio"] for r in kept],
                "uploaded": [r["uploaded"] for r in kept],
                "downloaded": [r["downloaded"] for r in kept],
                "buffer": [r["buffer"] for r in kept],
            },
        })
    return {"interval": interval, "trackers": trackers}


def _diff(current: Any, previous: Any) -> Any:
    if current is None or previous is None:
        return None
    return current - previous
