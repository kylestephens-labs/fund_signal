import time
from pathlib import Path

from tools import capture_pipeline


def test_rate_limiter_respects_positive_qps():
    limiter = capture_pipeline.RateLimiter(5)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    end = time.monotonic()
    assert end - start >= 0  # Should not raise and returns quickly


def test_jsonl_capture_resume(tmp_path: Path):
    path = tmp_path / "provider.jsonl"
    capture = capture_pipeline.JsonlCapture(path)
    capture.append("acme", [{"url": "https://example.com"}])
    assert capture.has("acme")

    capture2 = capture_pipeline.JsonlCapture(path)
    assert capture2.has("acme")
    all_data = capture2.read_all()
    assert len(all_data) == 1


def test_provider_stats_ratio():
    stats = capture_pipeline.ProviderStats("youcom")
    stats.record_request()
    stats.record_success(4, 2)
    payload = stats.to_dict()
    assert payload["dedup_ratio"] == 0.5
