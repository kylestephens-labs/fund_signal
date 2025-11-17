"""Shared helpers for Day-3 delivery pipelines."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.config import settings
from app.models.company import BreakdownItem, CompanyScore
from app.models.signal_breakdown import SignalProof
from app.observability.metrics import metrics
from app.services.scoring.repositories import ScoreRepository, build_score_repository

logger = logging.getLogger("pipelines.day3")

DEFAULT_COMPANY_LIMIT = 25


class DeliveryError(RuntimeError):
    """Domain exception for delivery pipelines."""

    def __init__(self, message: str, code: str = "DELIVERY_ERROR") -> None:
        super().__init__(message)
        self.code = code


def resolve_scoring_run(scoring_run_id: str | None) -> str:
    """Return the CLI-supplied scoring run or fall back to DELIVERY_SCORING_RUN."""
    run_id = scoring_run_id or settings.delivery_scoring_run
    if not run_id:
        raise DeliveryError(
            "scoring_run_id is required (pass --scoring-run or set DELIVERY_SCORING_RUN).",
            code="E_MISSING_RUN",
        )
    return run_id


def resolve_limit(limit: int | None, *, default: int | None = None) -> int | None:
    """Normalize the --limit/--company-limit flag."""
    if limit is None:
        return default
    return max(0, limit)


def fetch_scores_for_delivery(
    scoring_run_id: str,
    *,
    limit: int | None = None,
    repository: ScoreRepository | None = None,
) -> list[CompanyScore]:
    """Load persisted scores for a delivery run."""
    if not settings.database_url:
        raise DeliveryError(
            "DATABASE_URL is required to hydrate delivery payloads.",
            code="E_DATABASE_URL_MISSING",
        )
    repo = repository or build_score_repository()
    limit_value = resolve_limit(limit)
    scores = repo.list_run(scoring_run_id, limit=limit_value)
    if not scores:
        raise DeliveryError(
            f"No scores found for scoring_run_id '{scoring_run_id}'.",
            code="E_NO_SCORES",
        )
    logger.info(
        "delivery.supabase.query",
        extra={
            "scoring_run_id": scoring_run_id,
            "limit": limit_value,
            "count": len(scores),
        },
    )
    metrics.increment(
        "delivery.supabase.query",
        tags={"scoring_run": scoring_run_id, "count": len(scores)},
    )
    return scores


def compute_confidence(score: int) -> str:
    """Map numeric scores onto lightweight confidence bands."""
    if score >= 80:
        return "VERIFIED"
    if score >= 60:
        return "LIKELY"
    if score >= 45:
        return "WATCHLIST"
    return "NURTURE"


def flatten_proofs(item: BreakdownItem) -> list[SignalProof]:
    """Return the expanded proof list for a breakdown entry."""
    proofs = list(item.proofs) if item.proofs else []
    if not proofs and item.proof:
        proofs.append(item.proof)
    return proofs


def summarize_proofs(item: BreakdownItem) -> list[dict[str, str | list[str] | None]]:
    """Simplify proof metadata for downstream formatters."""
    summaries: list[dict[str, str | list[str] | None]] = []
    for proof in flatten_proofs(item):
        summaries.append(
            {
                "source_url": str(proof.source_url),
                "verified_by": proof.verified_by,
                "timestamp": proof.timestamp.isoformat() if proof.timestamp else None,
                "source_hint": proof.source_hint,
            }
        )
    return summaries


def serialize_score(score: CompanyScore) -> dict:
    """Convert a CompanyScore into a JSON-safe dictionary."""
    return {
        "id": str(score.id) if score.id else None,
        "company_id": str(score.company_id),
        "score": score.score,
        "confidence": compute_confidence(score.score),
        "recommended_approach": score.recommended_approach,
        "pitch_angle": score.pitch_angle,
        "scoring_model": score.scoring_model,
        "scoring_run_id": score.scoring_run_id,
        "created_at": score.created_at.isoformat(),
        "updated_at": (score.updated_at or score.created_at).isoformat(),
        "breakdown": [
            {
                "reason": item.reason,
                "points": item.points,
                "proofs": summarize_proofs(item),
            }
            for item in score.breakdown
        ],
    }


def record_delivery_event(
    channel: str,
    *,
    scoring_run_id: str,
    count: int,
    output_path: str | None = None,
) -> None:
    """Emit structured logs/metrics for downstream observability."""
    metrics.increment(
        f"delivery.{channel}.rendered",
        tags={"scoring_run": scoring_run_id, "count": count},
    )
    logger.info(
        f"delivery.{channel}.rendered",
        extra={"scoring_run_id": scoring_run_id, "count": count, "output": output_path},
    )


def utc_now() -> str:
    """Return an ISO8601 UTC timestamp for payload headers."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
