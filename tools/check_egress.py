"""Network egress guard for the capture runner."""

from __future__ import annotations

import argparse
import json
import logging
import socket
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("tools.check_egress")

DEFAULT_ALLOWLIST = (
    "api.ydc-index.io",
    "api.tavily.com",
    "api.exa.ai",
)
DEFAULT_TIMEOUT = 3.0


class EgressCheckError(RuntimeError):
    """Raised when egress policy checks fail."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeResult:
    """Result of an egress probe attempt."""

    host: str
    status: str
    ip: str | None = None
    latency_ms: float | None = None
    error: str | None = None


Connector = Callable[[str, int, float], tuple[str, float]]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate capture-runner egress allowlists.")
    parser.add_argument("--allow", action="append", help="Host that must remain reachable.")
    parser.add_argument("--deny", action="append", help="Host that must remain blocked.")
    parser.add_argument("--port", type=int, default=443, help="Port to probe.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="TCP timeout per host.")
    parser.add_argument("--output", type=Path, help="Optional path to write JSON results.")
    args = parser.parse_args(argv)
    if args.allow is None:
        args.allow = list(DEFAULT_ALLOWLIST)
    if args.deny is None:
        args.deny = []
    return args


def enforce_allowlist(
    *,
    allow_hosts: Iterable[str],
    deny_hosts: Iterable[str],
    port: int,
    timeout: float,
    connector: Connector | None = None,
) -> list[ProbeResult]:
    """Ensure only allowlisted hosts are reachable."""
    probe = connector or _connect
    results: list[ProbeResult] = []

    for host in _unique_hosts(allow_hosts):
        results.append(_probe_allowed_host(host, port=port, timeout=timeout, connector=probe))

    for host in _unique_hosts(deny_hosts):
        results.append(_probe_denied_host(host, port=port, timeout=timeout, connector=probe))

    return results


def _connect(host: str, port: int, timeout: float) -> tuple[str, float]:
    resolved = socket.gethostbyname(host)
    start = time.perf_counter()
    with socket.create_connection((resolved, port), timeout=timeout):
        pass
    elapsed_ms = (time.perf_counter() - start) * 1000
    return resolved, elapsed_ms


def _write_results(results: Sequence[ProbeResult], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "host": result.host,
            "status": result.status,
            "ip": result.ip,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }
        for result in results
    ]
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _unique_hosts(hosts: Iterable[str]) -> list[str]:
    normalized = {host.strip() for host in hosts if host and host.strip()}
    return sorted(normalized)


def _probe_allowed_host(
    host: str,
    *,
    port: int,
    timeout: float,
    connector: Connector,
) -> ProbeResult:
    try:
        ip, latency = connector(host, port, timeout)
        return ProbeResult(host=host, status="allowed", ip=ip, latency_ms=latency)
    except OSError as exc:
        raise EgressCheckError("E_ALLOWED_HOST_UNREACHABLE", f"{host} unreachable: {exc}") from exc


def _probe_denied_host(
    host: str,
    *,
    port: int,
    timeout: float,
    connector: Connector,
) -> ProbeResult:
    try:
        connector(host, port, timeout)
    except OSError as exc:
        return ProbeResult(host=host, status="denied", error=str(exc))
    raise EgressCheckError("E_EGRESS_DENIED", f"{host} is reachable but should be blocked.")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        results = enforce_allowlist(
            allow_hosts=args.allow,
            deny_hosts=args.deny,
            port=args.port,
            timeout=args.timeout,
        )
    except EgressCheckError as exc:
        logger.error("Egress validation failed: %s (code=%s)", exc, exc.code)
        return 1

    for result in results:
        latency = f"{result.latency_ms:.1f}ms" if result.latency_ms is not None else "-"
        logger.info("Host %s status=%s ip=%s latency=%s", result.host, result.status, result.ip or "-", latency)

    if args.output:
        _write_results(results, args.output)

    logger.info("Egress allowlist validation complete. hosts_checked=%s", len(results))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
