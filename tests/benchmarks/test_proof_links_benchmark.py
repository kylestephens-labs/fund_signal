from __future__ import annotations

import pytest

from tools import proof_links_benchmark

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


@pytest.fixture(autouse=True)
def _fixture_mode(monkeypatch):
    monkeypatch.setenv("FUND_SIGNAL_MODE", "fixture")
    monkeypatch.setenv("SCORING_MODEL", "fixture-rubric")
    yield


def _config_from_env() -> proof_links_benchmark.ProofLinksBenchmarkConfig:
    args = proof_links_benchmark.parse_args([])
    return proof_links_benchmark.ProofLinksBenchmarkConfig(
        fixture_path=args.input,
        runs=args.runs,
        cold_runs=args.cold_runs,
        concurrency=args.concurrency,
        sample_size=args.sample_size,
        skip_cold=args.skip_cold,
        report_path=None,
        force_report=False,
        scoring_run_id="proof-links-pytest",
        p95_threshold_ms=args.threshold_ms,
    )


def test_proof_link_cache_benchmark_stays_under_threshold(benchmark):
    config = _config_from_env()

    def _run() -> dict:
        return proof_links_benchmark.run_benchmark(config)

    metrics = benchmark.pedantic(_run, iterations=1, rounds=1)
    proof_links_benchmark.verify_thresholds(metrics, p95_threshold_ms=config.p95_threshold_ms)

    warm = metrics["warm"]
    assert warm["latency_ms"]["p95"] <= config.p95_threshold_ms
    assert warm["cache_hit_ratio"] >= proof_links_benchmark.DEFAULT_MIN_HIT_RATIO
    assert warm["cache_hits"] >= warm["cache_misses"]
    assert metrics["statsd_payload"]["proof_links.latency_p95"] == warm["latency_ms"]["p95"]

    if not config.skip_cold:
        assert metrics["cold"] is not None


def test_verify_thresholds_detects_regressions():
    metrics = {
        "warm": {
            "latency_ms": {"p95": 999.0},
            "cache_hit_ratio": 0.5,
        }
    }
    with pytest.raises(proof_links_benchmark.BenchmarkFailure) as excinfo:
        proof_links_benchmark.verify_thresholds(metrics, p95_threshold_ms=300.0)
    assert excinfo.value.code == "BENCH_THRESHOLD_FAILED"
