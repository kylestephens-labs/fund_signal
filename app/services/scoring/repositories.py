"""Persistence backends for ChatGPT scoring results."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import settings
from app.models.company import CompanyScore
from app.models.score_record import ScoreRecord
from app.observability.metrics import metrics
from app.services.scoring.errors import ScorePersistenceError

logger = logging.getLogger(__name__)


class ScoreRepository(Protocol):
    """Persistence contract for scoring results."""

    def get(self, company_id: str, scoring_run_id: str) -> CompanyScore | None:
        ...

    def save(self, result: CompanyScore) -> CompanyScore:
        ...

    def list(self, company_id: str) -> list[CompanyScore]:
        ...

    def list_run(self, scoring_run_id: str, *, limit: int | None = None) -> list[CompanyScore]:
        ...


class InMemoryScoreRepository(ScoreRepository):
    """Thread-safe repository used for API/local development."""

    def __init__(self) -> None:
        self._scores: dict[tuple[str, str], CompanyScore] = {}
        self._company_index: dict[str, list[str]] = {}
        self._lock = Lock()

    def get(self, company_id: str, scoring_run_id: str) -> CompanyScore | None:
        key = (company_id, scoring_run_id)
        with self._lock:
            score = self._scores.get(key)
        if score:
            metrics.increment("scoring.persistence.hit", tags={"repository": "memory"})
            logger.info(
                "scoring.persistence.hit",
                extra={
                    "company_id": company_id,
                    "scoring_run_id": scoring_run_id,
                    "backend": "memory",
                },
            )
        return score

    def save(self, result: CompanyScore) -> CompanyScore:
        key = (str(result.company_id), result.scoring_run_id)
        with self._lock:
            self._scores[key] = result
            self._company_index.setdefault(str(result.company_id), [])
            if result.scoring_run_id not in self._company_index[str(result.company_id)]:
                self._company_index[str(result.company_id)].append(result.scoring_run_id)
        metrics.increment("scoring.persistence.persisted", tags={"repository": "memory"})
        logger.info(
            "scoring.persistence.persisted",
            extra={
                "company_id": str(result.company_id),
                "scoring_run_id": result.scoring_run_id,
                "score": result.score,
                "backend": "memory",
            },
        )
        return result

    def list(self, company_id: str) -> list[CompanyScore]:
        with self._lock:
            scoring_run_ids = self._company_index.get(company_id, [])
            return [self._scores[(company_id, run_id)] for run_id in scoring_run_ids]

    def list_run(self, scoring_run_id: str, *, limit: int | None = None) -> list[CompanyScore]:
        with self._lock:
            matches = [
                score for (_, run_id), score in self._scores.items() if run_id == scoring_run_id
            ]
        ordered = sorted(
            matches,
            key=lambda entry: (entry.score, entry.updated_at or entry.created_at),
            reverse=True,
        )
        if limit is not None:
            return ordered[: max(0, limit)]
        return ordered


class SupabaseScoreRepository(ScoreRepository):
    """SQLModel-backed repository that persists scores to Postgres/Supabase."""

    def __init__(
        self,
        database_url: str,
        *,
        pool_min_size: int | None = None,
        pool_max_size: int | None = None,
        auto_create_schema: bool = False,
    ) -> None:
        if not database_url:
            raise ValueError("DATABASE_URL is required for SupabaseScoreRepository.")

        parsed_url = make_url(database_url)
        sync_url, connect_args, drivername = _coerce_sync_database_url(parsed_url)
        pool_min = max(pool_min_size or settings.db_pool_min_size, 1)
        pool_max = max(pool_max_size or settings.db_pool_max_size, pool_min)
        max_overflow = max(pool_max - pool_min, 0)
        is_sqlite = drivername.startswith("sqlite")
        engine_kwargs: dict[str, Any] = {
            "echo": settings.debug,
            "connect_args": connect_args,
            "pool_pre_ping": not is_sqlite,
        }
        if not is_sqlite:
            engine_kwargs["pool_size"] = pool_min
            engine_kwargs["max_overflow"] = max_overflow

        self._engine: Engine = create_engine(
            sync_url,
            **engine_kwargs,
        )
        if auto_create_schema:
            SQLModel.metadata.create_all(self._engine)
        self._metrics_tags = {"repository": _resolve_metrics_tag(parsed_url, drivername)}

    def dispose(self) -> None:
        """Close the underlying SQLAlchemy engine."""
        self._engine.dispose()

    def get(self, company_id: str, scoring_run_id: str) -> CompanyScore | None:
        normalized_id = _parse_company_id(company_id)
        try:
            with self._session() as session:
                statement = select(ScoreRecord).where(
                    ScoreRecord.company_id == normalized_id,
                    ScoreRecord.scoring_run_id == scoring_run_id,
                )
                record = session.exec(statement).first()
                if record:
                    metrics.increment("scoring.persistence.hit", tags=self._metrics_tags)
                    logger.info(
                        "scoring.persistence.hit",
                        extra={
                            "company_id": company_id,
                            "scoring_run_id": scoring_run_id,
                            "backend": self._metrics_tags["repository"],
                        },
                    )
                    return record.to_company_score()
                return None
        except SQLAlchemyError as exc:  # pragma: no cover - defensive guard
            logger.exception(
                "scoring.persistence.error",
                extra={
                    "company_id": company_id,
                    "scoring_run_id": scoring_run_id,
                    "backend": "database",
                },
            )
            raise ScorePersistenceError(
                "Failed to load persisted score.", code="500_INTERNAL"
            ) from exc

    def list(self, company_id: str) -> list[CompanyScore]:
        normalized_id = _parse_company_id(company_id)
        try:
            with self._session() as session:
                statement = (
                    select(ScoreRecord)
                    .where(ScoreRecord.company_id == normalized_id)
                    .order_by(ScoreRecord.created_at.desc())
                )
                records = session.exec(statement).all()
                return [record.to_company_score() for record in records]
        except SQLAlchemyError as exc:  # pragma: no cover - defensive guard
            logger.exception(
                "scoring.persistence.error", extra={"company_id": company_id, "backend": "database"}
            )
            raise ScorePersistenceError(
                "Failed to list persisted scores.", code="500_INTERNAL"
            ) from exc

    def list_run(self, scoring_run_id: str, *, limit: int | None = None) -> list[CompanyScore]:
        try:
            with self._session() as session:
                statement = (
                    select(ScoreRecord)
                    .where(ScoreRecord.scoring_run_id == scoring_run_id)
                    .order_by(ScoreRecord.score.desc(), ScoreRecord.created_at.desc())
                )
                if limit is not None and limit >= 0:
                    statement = statement.limit(limit)
                records = session.exec(statement).all()
                return [record.to_company_score() for record in records]
        except SQLAlchemyError as exc:  # pragma: no cover - defensive guard
            logger.exception(
                "scoring.persistence.error",
                extra={"scoring_run_id": scoring_run_id, "backend": "database"},
            )
            raise ScorePersistenceError(
                "Failed to list persisted scores.", code="500_INTERNAL"
            ) from exc

    def save(self, result: CompanyScore) -> CompanyScore:
        record = ScoreRecord.from_company_score(result)
        try:
            with self._session() as session:
                statement = select(ScoreRecord).where(
                    ScoreRecord.company_id == record.company_id,
                    ScoreRecord.scoring_run_id == record.scoring_run_id,
                )
                existing = session.exec(statement).first()
                if existing:
                    existing.score = record.score
                    existing.breakdown = record.breakdown
                    existing.recommended_approach = record.recommended_approach
                    existing.pitch_angle = record.pitch_angle
                    existing.scoring_model = record.scoring_model
                    existing.updated_at = record.updated_at or record.created_at
                    persisted = existing
                else:
                    session.add(record)
                    persisted = record
                session.commit()
                session.refresh(persisted)
                metrics.increment("scoring.persistence.persisted", tags=self._metrics_tags)
                logger.info(
                    "scoring.persistence.persisted",
                    extra={
                        "company_id": str(persisted.company_id),
                        "scoring_run_id": persisted.scoring_run_id,
                        "score": persisted.score,
                        "backend": self._metrics_tags["repository"],
                    },
                )
                return persisted.to_company_score()
        except IntegrityError as exc:
            logger.warning(
                "scoring.persistence.conflict",
                extra={
                    "company_id": str(record.company_id),
                    "scoring_run_id": record.scoring_run_id,
                    "backend": self._metrics_tags["repository"],
                },
            )
            raise ScorePersistenceError(
                "Score already exists for this company and scoring_run_id.",
                code="409_SCORE_ALREADY_EXISTS",
            ) from exc
        except SQLAlchemyError as exc:
            logger.exception(
                "scoring.persistence.error",
                extra={
                    "company_id": str(record.company_id),
                    "scoring_run_id": record.scoring_run_id,
                    "backend": self._metrics_tags["repository"],
                },
            )
            raise ScorePersistenceError(
                "Failed to persist scoring result.", code="500_INTERNAL"
            ) from exc

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with Session(self._engine) as session:
            yield session


def _parse_company_id(company_id: str) -> UUID:
    try:
        return UUID(company_id)
    except ValueError as exc:  # pragma: no cover - defensive guard
        raise ScorePersistenceError(
            "company_id must be a valid UUID.", code="422_INVALID_COMPANY_DATA"
        ) from exc


def _coerce_sync_database_url(url: URL) -> tuple[str, dict[str, Any], str]:
    """Convert async connection strings into sync SQLAlchemy URLs."""
    drivername = url.drivername
    connect_args: dict[str, Any] = {}
    if drivername.endswith("+asyncpg"):
        drivername = drivername.replace("+asyncpg", "+psycopg2")
    elif drivername.endswith("+psycopg"):
        drivername = drivername.replace("+psycopg", "+psycopg2")
    sync_url = url.set(drivername=drivername)
    query = dict(sync_url.query) if sync_url.query else {}
    removed_ssl = False
    if "ssl" in query:
        query.pop("ssl", None)
        removed_ssl = True
    if query:
        sync_url = sync_url.set(query=query)
    else:
        sync_url = sync_url.set(query=None)

    host = (url.host or "").lower()
    if drivername.startswith("postgresql"):
        query = dict(sync_url.query) if sync_url.query else {}
        if "sslmode" not in query and (removed_ssl or "supabase.co" in host):
            connect_args["sslmode"] = "require"
    if drivername.startswith("sqlite"):
        connect_args.setdefault("check_same_thread", False)
    return sync_url.render_as_string(hide_password=False), connect_args, drivername


def _resolve_metrics_tag(url: URL, drivername: str) -> str:
    host = (url.host or "").lower()
    if "supabase.co" in host:
        return "supabase"
    if drivername.startswith("sqlite"):
        return "sqlite"
    return "postgres"


def build_score_repository(database_url: str | None = None) -> ScoreRepository:
    """Instantiate a ScoreRepository using DATABASE_URL when available."""
    resolved_url = database_url or settings.database_url
    if not resolved_url:
        logger.info("scoring.repository.initialized", extra={"backend": "memory"})
        return InMemoryScoreRepository()
    try:
        repository = SupabaseScoreRepository(
            resolved_url,
            pool_min_size=settings.db_pool_min_size,
            pool_max_size=settings.db_pool_max_size,
        )
        logger.info("scoring.repository.initialized", extra={"backend": "database"})
        return repository
    except Exception:
        logger.exception("scoring.repository.init_failed", extra={"backend": "database"})
        raise
