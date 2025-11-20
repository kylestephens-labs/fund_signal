from __future__ import annotations

from typing import Any


class StubMetrics:
    """Test double that captures emitted metrics for assertions."""

    def __init__(self) -> None:
        self.timing_calls: list[dict[str, Any]] = []
        self.increment_calls: list[dict[str, Any]] = []
        self.gauge_calls: list[dict[str, Any]] = []
        self.alert_calls: list[dict[str, Any]] = []

    def timing(self, metric: str, value: float, *, tags: dict[str, Any] | None = None) -> None:
        self.timing_calls.append({"metric": metric, "value": value, "tags": tags or {}})

    def increment(
        self, metric: str, value: float = 1.0, *, tags: dict[str, Any] | None = None
    ) -> None:
        self.increment_calls.append({"metric": metric, "value": value, "tags": tags or {}})

    def gauge(self, metric: str, value: float, *, tags: dict[str, Any] | None = None) -> None:
        self.gauge_calls.append({"metric": metric, "value": value, "tags": tags or {}})

    def alert(
        self,
        metric: str,
        *,
        value: float,
        threshold: float,
        severity: str,
        tags: dict[str, Any] | None = None,
    ) -> None:
        self.alert_calls.append(
            {
                "metric": metric,
                "value": value,
                "threshold": threshold,
                "severity": severity,
                "tags": tags or {},
            }
        )
