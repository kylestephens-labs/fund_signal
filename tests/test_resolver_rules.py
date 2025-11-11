import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tools import resolver_rules


def _expected_sha(sample: dict) -> str:
    canonical = json.dumps(sample, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_load_rules_returns_sha(tmp_path: Path):
    rules_path = tmp_path / "resolver_rules.yaml"
    sample = {
        "version": "v-test",
        "weights": {"token": 1},
        "tie_breakers": ["score_desc", "lexicographic_ci"],
        "slug_head_edit_distance_threshold": 1,
    }
    rules_path.write_text(yaml.safe_dump(sample), encoding="utf-8")

    rules = resolver_rules.load_rules(rules_path)

    assert rules.version == "v-test"
    assert rules.weights["token"] == 1
    assert rules.ruleset_sha256 == _expected_sha(sample)


def test_invalid_tie_breaker_raises(tmp_path: Path):
    rules_path = tmp_path / "resolver_rules.yaml"
    sample = {
        "version": "v-test",
        "weights": {"token": 1},
        "tie_breakers": ["unknown"],
        "slug_head_edit_distance_threshold": 1,
    }
    rules_path.write_text(yaml.safe_dump(sample), encoding="utf-8")

    with pytest.raises(resolver_rules.ResolverRulesError) as exc:
        resolver_rules.load_rules(rules_path)
    assert exc.value.code == "RULES_SCHEMA_INVALID"


def test_cli_print_sha(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rules_path = tmp_path / "resolver_rules.yaml"
    sample = {
        "version": "v-test",
        "weights": {"token": 1},
        "tie_breakers": ["score_desc"],
        "slug_head_edit_distance_threshold": 0,
    }
    rules_path.write_text(yaml.safe_dump(sample), encoding="utf-8")

    exit_code = resolver_rules.main(["--rules", str(rules_path), "--print-sha"])
    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    assert output == _expected_sha(sample)
