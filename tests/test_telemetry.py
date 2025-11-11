import json
from pathlib import Path

from tools import telemetry


def test_telemetry_writes_json_file(tmp_path: Path, monkeypatch):
    log_path = tmp_path / "telemetry.log"
    monkeypatch.setenv("TELEMETRY_FORMAT", "json")
    monkeypatch.setenv("TELEMETRY_PATH", str(log_path))
    telemetry.reset_telemetry_for_testing()
    sink = telemetry.get_telemetry()

    sink.emit(module="test", event="summary", value=1)

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])
    assert payload["module"] == "test"
    assert payload["event"] == "summary"
    assert payload["value"] == 1


def test_telemetry_defaults_to_text(monkeypatch):
    monkeypatch.delenv("TELEMETRY_FORMAT", raising=False)
    monkeypatch.delenv("TELEMETRY_PATH", raising=False)
    telemetry.reset_telemetry_for_testing()
    sink = telemetry.get_telemetry()
    # Should not raise when emitting text
    sink.emit(module="test", event="noop")
