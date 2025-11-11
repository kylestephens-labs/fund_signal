from pathlib import Path

from tools import verify_feedback_resolver

FIXTURE_DIR = Path("tests/fixtures/bundles/feedback_case/leads")


def test_feedback_cli_is_deterministic(tmp_path: Path, monkeypatch):
    normalized = tmp_path / "exa_seed.normalized.json"
    normalized.write_text((FIXTURE_DIR / "exa_seed.normalized.json").read_text(), encoding="utf-8")
    youcom = FIXTURE_DIR / "youcom_verified.json"
    tavily = FIXTURE_DIR / "tavily_verified.json"
    output = tmp_path / "exa_seed.feedback_resolved.json"

    argv = [
        "--input",
        str(normalized),
        "--youcom",
        str(youcom),
        "--tavily",
        str(tavily),
        "--out",
        str(output),
    ]
    assert verify_feedback_resolver.main(argv) == 0
    first = output.read_bytes()
    assert verify_feedback_resolver.main(argv) == 0
    assert first == output.read_bytes()
