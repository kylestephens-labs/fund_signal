"""Seed a deterministic CompanyScore row for drawer smoke tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.engine.url import make_url

from app.config import Settings
from app.models.company import CompanyProfile
from app.models.score_record import ScoreRecord
from app.services.scoring.chatgpt_engine import ChatGPTScoringEngine, ScoringContext
from app.services.scoring.repositories import InMemoryScoreRepository

logger = logging.getLogger("scripts.seed_scores")


def _render_database_url(url: str) -> str:
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<invalid DATABASE_URL>"


def _load_company_profiles(fixture_path: Path, company_id: UUID | None) -> list[CompanyProfile]:
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    profiles: list[CompanyProfile] = []
    for entry in payload.get("companies", []):
        profile = entry.get("profile") or {}
        if company_id and profile.get("company_id") != str(company_id):
            continue
        profiles.append(CompanyProfile(**profile))
    if company_id and not profiles:
        raise ValueError(f"Company {company_id} not found in {fixture_path}")
    if not profiles:
        raise ValueError(f"No companies found in {fixture_path}")
    return profiles


def _build_context(settings: Settings) -> ScoringContext:
    prompt_path = Path(settings.scoring_system_prompt_path).expanduser()
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    return ScoringContext(
        mode="fixture",
        system_prompt=system_prompt,
        model=settings.scoring_model,
        temperature=settings.scoring_temperature,
    )


async def _persist_score(database_url: str, record: ScoreRecord, *, force: bool) -> None:
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            if force:
                await session.execute(
                    delete(ScoreRecord).where(
                        ScoreRecord.company_id == record.company_id,
                        ScoreRecord.scoring_run_id == record.scoring_run_id,
                    )
                )
                await session.flush()
            session.add(record)
            await session.commit()
    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed FundSignal score rows.")
    parser.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="Path to regression_companies.json.",
    )
    parser.add_argument(
        "--company-id",
        type=UUID,
        default=UUID("11111111-0000-0000-0000-000000000001"),
        help="Company ID to seed. Defaults to the UI smoke persona.",
    )
    parser.add_argument(
        "--seed-all",
        action="store_true",
        help="Seed every company in the fixture instead of a single ID.",
    )
    parser.add_argument(
        "--scoring-run",
        "--scoring-run-id",
        type=str,
        dest="scoring_run_id",
        default="ui-smoke",
        help="Scoring run identifier stored alongside the score.",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="Override DATABASE_URL (falls back to .env).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing (company_id, scoring_run_id) row.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    local_settings = Settings()
    database_url = args.database_url or local_settings.database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL is required to seed scores.")
    logger.info("Using DATABASE_URL=%s", _render_database_url(database_url))

    target_company = None if args.seed_all else args.company_id
    profiles = _load_company_profiles(args.fixture, target_company)
    context = _build_context(local_settings)
    engine = ChatGPTScoringEngine(repository=InMemoryScoreRepository(), context=context)
    seeded = 0
    for profile in profiles:
        score = engine.score_company(profile, scoring_run_id=args.scoring_run_id, force=True)
        record = ScoreRecord.from_company_score(score)
        await _persist_score(database_url, record, force=args.force)
        seeded += 1
        logger.info(
            "scoring.persistence.persisted",
            extra={
                "company_id": str(record.company_id),
                "scoring_run_id": record.scoring_run_id,
                "score": record.score,
            },
        )
    logger.info(
        "seed_scores.complete",
        extra={"count": seeded, "scoring_run_id": args.scoring_run_id, "fixture": str(args.fixture)},
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
