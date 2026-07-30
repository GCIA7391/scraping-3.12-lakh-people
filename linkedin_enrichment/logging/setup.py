"""Logging configuration: structured JSON to file, readable text to console.

A 312k-row run over days produces a lot of log. The file handler emits one JSON
object per line so the run can be queried afterwards (which companies threw
errors, how often the limiter backed off) without regex archaeology, while the
console stays human-readable for someone watching it live.

Note: this module lives in a package called ``logging`` but ``import logging``
below resolves to the standard library, because Python 3 imports are absolute.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

_RESERVED = frozenset(vars(logging.makeLogRecord({})))


class JsonFormatter(logging.Formatter):
    """One JSON object per record, including any extra fields passed by callers."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


def configure(log_dir: str | Path, level: str = "INFO", *, quiet_console: bool = False) -> Path:
    """Install handlers and return the path of the JSON log file."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "enrichment.jsonl"

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Rotate so a multi-day run cannot fill the disk.
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    # When the live dashboard owns the terminal, keep the console to warnings so
    # routine progress lines do not fight the rendered panel.
    console.setLevel(logging.WARNING if quiet_console else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)-8s %(name)-38s %(message)s"))
    root.addHandler(console)

    # These libraries are chatty at DEBUG and say nothing useful here.
    for noisy in ("aiohttp", "asyncio", "urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_path
