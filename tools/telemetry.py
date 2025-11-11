"""Lightweight telemetry helper for deterministic logging."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("telemetry")


@dataclass(frozen=True)
class TelemetryConfig:
    format: str = "text"
    path: Path | None = None


class Telemetry:
    """Emit structured telemetry to stdout and optional log file."""

    def __init__(self, config: TelemetryConfig) -> None:
        self._config = config
        if self._config.path:
            self._config.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, module: str, event: str, **fields: Any) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "module": module,
            "event": event,
            **fields,
        }
        if self._config.format == "json":
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            logger.info(serialized)
            self._write_to_file(serialized)
        else:
            logger.info("%s %s %s", payload["timestamp"], module, fields)
            self._write_to_file(json.dumps(payload))

    def _write_to_file(self, line: str) -> None:
        if not self._config.path:
            return
        try:
            with self._config.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:  # pragma: no cover
            logger.warning("TELEMETRY_WRITE_ERROR path=%s error=%s", self._config.path, exc)


_TELEMETRY: Telemetry | None = None


def _load_config() -> TelemetryConfig:
    fmt = (os.getenv("TELEMETRY_FORMAT") or "text").strip().lower()
    path_value = os.getenv("TELEMETRY_PATH")
    path = Path(path_value).expanduser() if path_value else None
    return TelemetryConfig(format=fmt if fmt in {"json", "text"} else "text", path=path)


def get_telemetry() -> Telemetry:
    global _TELEMETRY  # noqa: PLW0603
    if _TELEMETRY is None:
        _TELEMETRY = Telemetry(_load_config())
    return _TELEMETRY


def reset_telemetry_for_testing() -> None:  # pragma: no cover - test helper
    global _TELEMETRY  # noqa: PLW0603
    _TELEMETRY = None
