from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pipelines.day3 import DeliveryError, email_schedule


def test_schedule_invokes_email_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    received_args: list[list[str]] = []

    def _fake_run(argv=None):  # noqa: ANN001
        received_args.append(list(argv or []))
        output = tmp_path / "email_cron.md"
        output.write_text("ok", encoding="utf-8")
        return output

    monkeypatch.setattr(email_schedule.email_delivery, "run", _fake_run)
    caplog.set_level(logging.INFO, logger="pipelines.day3.email_schedule")
    args = [
        "--scoring-run",
        "demo-run",
        "--company-limit",
        "25",
        "--min-score",
        "80",
        "--output",
        str(tmp_path / "email_cron.md"),
        "--deliver",
        "--timezone",
        "UTC",
        "--now",
        "2025-01-06T09:00:00+00:00",
        "--enforce-window",
    ]

    result = email_schedule.run(args)

    assert result.name == "email_cron.md"
    assert received_args and "--deliver" in received_args[0]
    assert any(record.message == "delivery.email.schedule.start" for record in caplog.records)


def test_schedule_window_violation(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(DeliveryError):
        email_schedule.run(
            [
                "--scoring-run",
                "demo-run",
                "--enforce-window",
                "--now",
                "2025-01-06T08:59:00-08:00",
            ]
        )
