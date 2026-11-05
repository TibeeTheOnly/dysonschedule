"""Logging: human-readable lines on stdout (captured by journald) plus a
rotating JSON-lines file."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import LoggingConfig


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("event_kind", "event_data"):
            if hasattr(record, key):
                payload[key.removeprefix("event_")] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(cfg: LoggingConfig, log_dir: Path | None, under_systemd: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(cfg.level)

    stream = logging.StreamHandler(sys.stdout)
    # journald adds its own timestamps
    fmt = "%(levelname)s %(name)s: %(message)s" if under_systemd else "%(asctime)s %(levelname)s %(name)s: %(message)s"
    stream.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "dyson-pi.log", maxBytes=int(cfg.file_max_mb * 1024 * 1024),
                backupCount=cfg.file_backups, encoding="utf-8",
            )
            fh.setFormatter(JsonFormatter())
            root.addHandler(fh)
        except OSError as exc:
            root.warning("file logging disabled (%s)", exc)

    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
