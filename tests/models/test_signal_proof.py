from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.signal_breakdown import SignalProof, SignalProofValidationError


def _proof_payload(**overrides):
    payload = {
        "source_url": "https://news.example.com/acme",
        "verified_by": ["Exa"],
        "timestamp": datetime.now(UTC),
    }
    payload.update(overrides)
    return payload


def test_signal_proof_requires_timestamp():
    with pytest.raises(ValidationError):
        SignalProof(source_url="https://news.example.com/acme", verified_by=["Exa"])


def test_signal_proof_raises_when_stale():
    stale_timestamp = datetime.now(UTC) - timedelta(days=120)
    proof = SignalProof(**_proof_payload(timestamp=stale_timestamp))

    with pytest.raises(SignalProofValidationError) as excinfo:
        proof.ensure_fresh(max_age_days=90)

    assert excinfo.value.code == "422_PROOF_STALE"


def test_signal_proof_passes_when_fresh():
    recent = datetime.now(UTC) - timedelta(days=10)
    proof = SignalProof(**_proof_payload(timestamp=recent))

    assert proof.ensure_fresh(max_age_days=90) is proof
