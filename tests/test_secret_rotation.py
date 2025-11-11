from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools import rotate_keys


def _write_state(path: Path, **entries: str) -> None:
    path.write_text(
        '{' + ", ".join(f'"{k}": "{v}"' for k, v in entries.items()) + '}',
        encoding="utf-8",
    )


def test_check_only_flags_expired_secret(monkeypatch, tmp_path: Path):
    state = tmp_path / "state.json"
    expired_date = (datetime.now(UTC) - timedelta(days=200)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _write_state(state, youcom=expired_date)
    monkeypatch.setenv("YOUCOM_API_KEY", "dummy")

    with pytest.raises(rotate_keys.SecretError) as excinfo:
        rotate_keys.run_rotation(
            [rotate_keys.PROVIDERS["youcom"]],
            state_file=state,
            max_age_days=90,
            check_only=True,
            dry_run=False,
            force=False,
        )
    assert excinfo.value.code == "E_SECRET_EXPIRED"


def test_rotation_updates_state(monkeypatch, tmp_path: Path):
    state = tmp_path / "state.json"
    old_date = (datetime.now(UTC) - timedelta(days=200)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _write_state(state, youcom=old_date)
    monkeypatch.setenv("YOUCOM_API_KEY", "dummy")

    results = rotate_keys.run_rotation(
        [rotate_keys.PROVIDERS["youcom"]],
        state_file=state,
        max_age_days=90,
        check_only=False,
        dry_run=False,
        force=False,
        now=datetime.now(UTC),
    )

    assert results[0]["status"] == "success"
    updated = rotate_keys.load_state(state)["youcom"]
    assert updated != old_date


def test_rotation_requires_env(monkeypatch, tmp_path: Path):
    state = tmp_path / "state.json"
    _write_state(state, exa="2025-10-01T00:00:00Z")
    monkeypatch.delenv("EXA_API_KEY", raising=False)

    with pytest.raises(rotate_keys.SecretError) as excinfo:
        rotate_keys.run_rotation(
            [rotate_keys.PROVIDERS["exa"]],
            state_file=state,
            max_age_days=90,
            check_only=False,
            dry_run=True,
            force=True,
        )
    assert excinfo.value.code == "E_SECRET_MISSING"
