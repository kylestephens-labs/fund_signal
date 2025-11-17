"""API endpoints for running the ChatGPT scoring engine."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field

from app.models.company import CompanyProfile, CompanyScore
from app.services.scoring.chatgpt_engine import (
    ChatGPTScoringEngine,
    ScoringEngineError,
    get_scoring_engine,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class ScoreCompanyRequest(CompanyProfile):
    """Request payload for scoring a single verified company."""

    scoring_run_id: str | None = Field(
        default=None,
        description="Optional identifier for idempotent scoring runs.",
    )


@router.post("/scores", response_model=CompanyScore, status_code=status.HTTP_201_CREATED)
async def create_score(
    payload: ScoreCompanyRequest,
    *,
    force: bool = Query(False, description="Force re-scoring even if cached."),
    engine: ChatGPTScoringEngine = Depends(get_scoring_engine),
) -> CompanyScore:
    """Trigger a scoring run for a company."""
    default_run_id = datetime.now(timezone.utc).date().isoformat()
    scoring_run_id = payload.scoring_run_id or f"adhoc-{default_run_id}"
    company = CompanyProfile(**payload.model_dump(exclude={"scoring_run_id"}))
    try:
        return engine.score_company(company, scoring_run_id=scoring_run_id, force=force)
    except ScoringEngineError as exc:
        logger.error(
            "scoring.api_error",
            extra={"company_id": str(company.company_id), "code": exc.code},
        )
        raise HTTPException(status_code=_map_error_code(exc.code), detail=str(exc)) from exc


@router.get("/scores/{company_id}", response_model=list[CompanyScore])
async def list_scores(
    company_id: UUID,
    scoring_run_id: str | None = Query(None, description="Filter to a specific scoring run."),
    engine: ChatGPTScoringEngine = Depends(get_scoring_engine),
) -> list[CompanyScore]:
    """Fetch cached scoring results."""
    result = engine.fetch_scores(str(company_id), scoring_run_id=scoring_run_id)
    if scoring_run_id and not result:
        raise HTTPException(status_code=404, detail="Score not found for requested run.")
    return result


def _map_error_code(code: str) -> int:
    if code == "409_SCORE_ALREADY_EXISTS":
        return status.HTTP_409_CONFLICT
    if code == "429_RATE_LIMIT":
        return status.HTTP_429_TOO_MANY_REQUESTS
    if code == "422_INVALID_COMPANY_DATA":
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if code == "502_OPENAI_UPSTREAM":
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_500_INTERNAL_SERVER_ERROR
