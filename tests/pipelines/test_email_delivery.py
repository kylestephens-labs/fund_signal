from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import settings
from app.models.company import BreakdownItem, CompanyScore
from app.models.signal_breakdown import SignalProof
from pipelines.day3 import DeliveryError
from pipelines.day3 import email_delivery as module


def _sample_score(score_value: int = 88) -> CompanyScore:
    proof = SignalProof(
        source_url="https://news.example.com/proof",
        verified_by=["Exa"],
        timestamp=datetime.now(UTC),
    )
    breakdown = [
        BreakdownItem(
            reason="Funding momentum",
            points=score_value,
            proof=proof,
            proofs=[proof],
        )
    ]
    return CompanyScore(
        company_id=uuid4(),
        score=score_value,
        breakdown=breakdown,
        recommended_approach="Email the VP of Sales.",
        pitch_angle="Help them convert capital into pipeline.",
        scoring_model="fixture",
        scoring_run_id="demo-run",
    )


def _stub_fetch_scores(monkeypatch: pytest.MonkeyPatch, scores: list[CompanyScore]) -> None:
    def _fake_fetch(scoring_run_id: str, *, limit: int | None = None, repository=None):  # noqa: ANN001
        return scores

    monkeypatch.setattr(module, "fetch_scores_for_delivery", _fake_fetch)


def test_run_generates_markdown_without_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    score = _sample_score()
    _stub_fetch_scores(monkeypatch, [score])
    output = tmp_path / "digest.md"

    result = module.run(
        [
            "--scoring-run",
            "demo-run",
            "--output",
            str(output),
        ]
    )

    assert result == output
    contents = output.read_text(encoding="utf-8")
    assert "Funding momentum" in contents


def test_deliver_requires_email_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    score = _sample_score()
    _stub_fetch_scores(monkeypatch, [score])
    monkeypatch.setattr(settings, "email_smtp_url", "smtp://localhost:1025", raising=False)
    monkeypatch.setattr(settings, "email_from", "alerts@fundsignal.dev", raising=False)
    monkeypatch.setattr(settings, "email_to", None, raising=False)

    output = tmp_path / "digest.md"
    with pytest.raises(DeliveryError):
        module.run(
            [
                "--scoring-run",
                "demo-run",
                "--output",
                str(output),
                "--deliver",
            ]
        )

    assert output.exists()


def test_deliver_sends_via_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    score = _sample_score()
    _stub_fetch_scores(monkeypatch, [score])
    monkeypatch.setattr(
        settings,
        "email_smtp_url",
        "smtp://user:secret@mailtrap.test:2525",
        raising=False,
    )
    monkeypatch.setattr(settings, "email_from", "alerts@fundsignal.dev", raising=False)
    monkeypatch.setattr(settings, "email_to", "ops@fundsignal.dev", raising=False)
    monkeypatch.setattr(settings, "email_subject", "Weekly FundSignal Drop", raising=False)
    sent_messages: list[dict[str, object]] = []

    class StubSMTP:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            self.host = host
            self.port = port
            self.timeout = timeout
            self.starttls_called = False
            self.logged_in: tuple[str, str] | None = None
            self.closed = False

        def ehlo(self) -> None:
            return None

        def starttls(self) -> None:
            self.starttls_called = True

        def login(self, username: str, password: str) -> None:
            self.logged_in = (username, password)

        def send_message(self, message, to_addrs=None) -> None:  # noqa: ANN001
            sent_messages.append(
                {
                    "subject": message["Subject"],
                    "recipients": list(to_addrs or []),
                    "body": message.get_body(preferencelist=("plain",)).get_content(),
                }
            )

        def quit(self) -> None:
            self.closed = True

    monkeypatch.setattr(module, "_create_smtp_client", lambda config: StubSMTP(config.host, config.port), raising=False)

    output = tmp_path / "digest.md"
    module.run(
        [
            "--scoring-run",
            "demo-run",
            "--output",
            str(output),
            "--deliver",
        ]
    )

    assert sent_messages, "SMTP stub was not invoked"
    assert sent_messages[0]["subject"] == "Weekly FundSignal Drop"
    assert sent_messages[0]["recipients"] == ["ops@fundsignal.dev"]
    assert "Funding momentum" in sent_messages[0]["body"]


def test_no_deliver_flag_overrides_env_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    score = _sample_score()
    _stub_fetch_scores(monkeypatch, [score])
    monkeypatch.setattr(settings, "delivery_email_force_run", True, raising=False)
    deliver_calls: list[tuple] = []

    def _stub(*args, **kwargs):  # noqa: ANN001
        deliver_calls.append(args)

    monkeypatch.setattr(module, "_deliver_via_smtp", _stub, raising=False)

    output = tmp_path / "digest.md"
    module.run(
        [
            "--scoring-run",
            "demo-run",
            "--output",
            str(output),
            "--no-deliver",
        ]
    )

    assert deliver_calls == []
