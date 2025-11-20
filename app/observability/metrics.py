from __future__ import annotations

import logging
import secrets
from typing import Any

from app.config import settings

try:  # pragma: no cover - optional dependency
    from statsd import StatsClient
except Exception:  # pragma: no cover - optional dependency guard
    StatsClient = None  # type: ignore[assignment]

logger = logging.getLogger("app.metrics")


class MetricsReporter:
    """Lightweight metrics emitter supporting stdout and StatsD backends."""

    def __init__(self) -> None:
        self._disabled = settings.metrics_disable
        self._namespace = settings.metrics_namespace or "proof_links"
        self._backend = (settings.metrics_backend or "stdout").lower()
        self._sample_rate = max(0.0, min(settings.metrics_sample_rate, 1.0))
        self._schema_version = settings.metrics_schema_version
        self._statsd: StatsClient | None = None
        if self._backend == "statsd" and not self._disabled:
            if StatsClient is None:
                logger.warning("statsd backend requested but statsd package is not installed.")
            else:
                try:
                    self._statsd = StatsClient(
                        host=settings.metrics_statsd_host,
                        port=settings.metrics_statsd_port,
                        prefix="",
                    )
                except Exception as exc:  # pragma: no cover - defensive guard
                    self._log_backend_error("statsd.init", exc)

    def timing(self, metric: str, value_ms: float, *, tags: dict[str, Any] | None = None) -> None:
        self._emit("timing", metric, value_ms, tags=tags)

    def gauge(self, metric: str, value: float, *, tags: dict[str, Any] | None = None) -> None:
        self._emit("gauge", metric, value, tags=tags)

    def increment(
        self, metric: str, value: float = 1.0, *, tags: dict[str, Any] | None = None
    ) -> None:
        self._emit("counter", metric, value, tags=tags)

    def alert(
        self,
        metric: str,
        *,
        value: float,
        threshold: float,
        severity: str,
        tags: dict[str, Any] | None = None,
    ) -> None:
        """Emit a structured alert payload for Render/Supabase ingestion."""
        if self._disabled:
            return
        name = self._normalize_metric(metric)
        payload = {
            "metric": name,
            "value": round(float(value), 4),
            "threshold": round(float(threshold), 4),
            "severity": severity,
            "schema_version": self._schema_version,
            "tags": tags or {},
        }
        self._log_event("proof_links.alert", payload)

    def _emit(
        self, metric_type: str, metric: str, value: float, *, tags: dict[str, Any] | None
    ) -> None:
        if self._disabled or value is None:
            return
        sampled = metric_type != "gauge" and self._sample_rate < 1.0
        sample_rate = self._sample_rate if sampled else 1.0
        if sampled:
            roll = secrets.randbelow(1_000_000) / 1_000_000
            if roll > sample_rate:
                return
        name = self._normalize_metric(metric)
        payload = {
            "metric": name,
            "value": round(float(value), 4),
            "type": metric_type,
            "tags": tags or {},
        }
        if sampled:
            payload["sample_rate"] = round(sample_rate, 4)
        self._log_event("proof_links.metric", payload)
        if self._backend == "statsd" and self._statsd is not None:
            try:
                if metric_type == "timing":
                    self._statsd.timing(name, value, rate=sample_rate)
                elif metric_type == "gauge":
                    self._statsd.gauge(name, value)
                else:
                    self._statsd.incr(name, value, rate=sample_rate)
            except Exception as exc:  # pragma: no cover - defensive guard
                self._log_backend_error(name, exc)

    def _normalize_metric(self, metric: str) -> str:
        trimmed = (metric or "").strip()
        if trimmed.startswith(f"{self._namespace}.") or trimmed.startswith("proof_links."):
            return trimmed
        return f"{self._namespace}.{trimmed}" if trimmed else self._namespace

    def _log_event(self, event: str, payload: dict[str, Any]) -> None:
        try:
            logger.info(event, extra={"metrics": payload})
        except Exception:  # pragma: no cover - defensive guard
            logger.debug("Unable to log metrics payload", exc_info=True)

    def _log_backend_error(self, metric: str, exc: Exception) -> None:
        logger.warning(
            "metrics.backend_error",
            extra={"metric": metric, "backend": self._backend, "error": type(exc).__name__},
        )


metrics = MetricsReporter()
