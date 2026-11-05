"""Configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping


class ConfigError(ValueError):
    """Raised when a required setting is missing or invalid."""


def parse_aliases(raw: str) -> dict[str, str]:
    """Parse ``host=Label,host2=Label`` into a lowercase-host mapping."""
    aliases: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, label = item.partition("=")
        if not sep or not host.strip() or not label.strip():
            raise ConfigError(f"Alias de tracker invalide : {item!r} (attendu hote=Nom)")
        aliases[host.strip().lower()] = label.strip()
    return aliases


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} doit etre un entier") from exc
    if value <= 0:
        raise ConfigError(f"{name} doit etre strictement positif")
    return value


@dataclass(frozen=True)
class Config:
    qbt_url: str
    qbt_username: str
    qbt_password: str = field(repr=False)
    db_path: str = "/data/seedscope.db"
    host: str = "0.0.0.0"
    port: int = 8080
    global_interval: int = 60
    torrent_interval: int = 300
    minute_retention_days: int = 30
    hourly_retention_days: int = 90
    tracker_aliases: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        missing = [
            name
            for name in ("QBT_URL", "QBT_USERNAME", "QBT_PASSWORD")
            if not env.get(name)
        ]
        if missing:
            raise ConfigError("Variables manquantes : " + ", ".join(missing))
        return cls(
            qbt_url=env["QBT_URL"].rstrip("/"),
            qbt_username=env["QBT_USERNAME"],
            qbt_password=env["QBT_PASSWORD"],
            db_path=env.get("SEEDSCOPE_DB", "/data/seedscope.db"),
            host=env.get("SEEDSCOPE_HOST", "0.0.0.0"),
            port=_positive_int(env, "SEEDSCOPE_PORT", 8080),
            global_interval=_positive_int(env, "SEEDSCOPE_GLOBAL_INTERVAL", 60),
            torrent_interval=_positive_int(env, "SEEDSCOPE_TORRENT_INTERVAL", 300),
            minute_retention_days=_positive_int(env, "SEEDSCOPE_MINUTE_RETENTION_DAYS", 30),
            hourly_retention_days=_positive_int(env, "SEEDSCOPE_HOURLY_RETENTION_DAYS", 90),
            tracker_aliases=parse_aliases(env.get("SEEDSCOPE_TRACKER_ALIASES", "")),
        )
