from __future__ import annotations

import pytest

from tools import check_egress


class StubConnector:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def __call__(self, host: str, port: int, timeout: float) -> tuple[str, float]:
        if host not in self.allowed:
            raise OSError("blocked")
        return "203.0.113.10", 5.0


def test_enforce_allowlist_passes_with_expected_hosts():
    connector = StubConnector({"api.ydc-index.io", "api.tavily.com", "api.exa.ai"})
    results = check_egress.enforce_allowlist(
        allow_hosts=["api.ydc-index.io", "api.tavily.com", "api.exa.ai"],
        deny_hosts=[],
        port=443,
        timeout=1.0,
        connector=connector,
    )

    assert len(results) == 3
    assert all(result.status == "allowed" for result in results)


def test_enforce_allowlist_blocks_unexpected_host():
    connector = StubConnector({"api.ydc-index.io"})
    results = check_egress.enforce_allowlist(
        allow_hosts=["api.ydc-index.io"],
        deny_hosts=["malicious.example.com"],
        port=443,
        timeout=1.0,
        connector=connector,
    )
    denied = [result for result in results if result.host == "malicious.example.com"]
    assert denied and denied[0].status == "denied"


def test_enforce_allowlist_errors_when_allowlisted_host_unreachable():
    connector = StubConnector(set())
    with pytest.raises(check_egress.EgressCheckError) as excinfo:
        check_egress.enforce_allowlist(
            allow_hosts=["api.ydc-index.io"],
            deny_hosts=[],
            port=443,
            timeout=1.0,
            connector=connector,
        )
    assert excinfo.value.code == "E_ALLOWED_HOST_UNREACHABLE"
