"""Entry point: runs the collector thread and the web server."""

from __future__ import annotations

import logging
import signal
import sys
import threading

from . import __version__, db
from .collector import Collector, LiveState
from .config import Config, ConfigError
from .qbit import QbitClient
from .web import make_server


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        logging.error("%s", exc)
        return 2

    state = LiveState()
    conn = db.connect(config.db_path)
    client = QbitClient(config.qbt_url, config.qbt_username, config.qbt_password)
    collector = Collector(config, client, conn, state)
    server = make_server(config, state)

    stop = threading.Event()
    def shutdown(*_: object) -> None:
        stop.set()
        state.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    thread = threading.Thread(target=collector.run, args=(stop,), name="collector", daemon=True)
    thread.start()
    logging.info("SeedScope %s, port %d", __version__, config.port)
    server.serve_forever()
    thread.join(timeout=10)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
