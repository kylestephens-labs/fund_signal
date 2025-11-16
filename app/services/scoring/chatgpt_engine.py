"""ChatGPT-powered scoring engine with deterministic offline fallback."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Final, Protocol, TypeVar

from app.config import settings
from app.models.company import BreakdownItem, CompanyProfile, CompanyScore
from app.models.signal_breakdown import SignalProof
from app.services.scoring.proof_links import ProofLinkError, ProofLinkHydrator
from app.observability.metrics import metrics

try:  # pragma: no cover - import guard for optional dependency
    from openai import APIError as OpenAIAPIError
    from openai import OpenAI
    from openai import OpenAIError as OpenAIBaseError
except Exception:  # pragma: no cover - openai not installed in some environments
    OpenAI = None  # type: ignore[assignment]
    OpenAIAPIError = Exception  # type: ignore[assignment]
    OpenAIBaseError = Exception  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

DEFAULT_SOURCE_MAP: Final[dict[str, str]] = {
    "funding": "https://fundsignal.local/proof/funding",
    "hiring": "https://fundsignal.local/proof/hiring",
    "tech": "https://fundsignal.local/proof/tech-stack",
    "team": "https://fundsignal.local/proof/team-size",
    "signals": "https://fundsignal.local/proof/buying-signal",
}


class ScoringEngineError(RuntimeError):
    """Base exception raised by the ChatGPT scoring engine."""

    def __init__(self, message: str, code: str = "SCORING_ENGINE_ERROR") -> None:
        super().__init__(message)
        self.code = code


class ScoringProviderError(ScoringEngineError):
    """Raised when an upstream AI provider fails."""


class ScoringValidationError(ScoringEngineError):
    """Raised when a model response cannot be parsed safely."""


class ScoreRepository(Protocol):
    """Persistence contract for scoring results."""

    def get(self, company_id: str, scoring_run_id: str) -> CompanyScore | None:
        ...

    def save(self, result: CompanyScore) -> CompanyScore:
        ...

    def list(self, company_id: str) -> list[CompanyScore]:
        ...


class OpenAIChatClient(Protocol):
    """Minimal contract for OpenAI chat completions."""

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> str:
        ...


class OpenAIResponseClient(OpenAIChatClient):
    """Thin wrapper around the official OpenAI Responses API."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required to score in online mode.")
        if OpenAI is None:  # pragma: no cover - import guard
            raise ImportError("openai package is not installed.")
        self._client = OpenAI(api_key=api_key)

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> str:
        response = self._client.responses.create(
            model=model,
            temperature=temperature,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return _extract_response_text(response)


def _extract_response_text(response: Any) -> str:
    """Normalize OpenAI responses across SDK versions."""
    if hasattr(response, "output"):
        text_chunks: list[str] = []
        for item in getattr(response, "output", []):
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "output_text":
                    text_chunks.append(getattr(content, "text", ""))
        if text_chunks:
            return "".join(text_chunks).strip()

    if hasattr(response, "choices"):  # ChatCompletions fallback
        choices = getattr(response, "choices", [])
        if choices:
            message = choices[0].message
            content = getattr(message, "content", "")
            if isinstance(content, list):  # new SDK streaming chunks
                return "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
            if isinstance(content, str):
                return content.strip()

    try:
        dumped = json.dumps(getattr(response, "model_dump", lambda: response)())
    except TypeError:
        dumped = str(response)
    raise ScoringProviderError(
        "OpenAI response did not include text output.",
        code="502_OPENAI_UPSTREAM",
    )


class InMemoryScoreRepository:
    """Thread-safe repository used for API/local development."""

    def __init__(self) -> None:
        self._scores: dict[tuple[str, str], CompanyScore] = {}
        self._company_index: dict[str, list[str]] = {}
        self._lock = Lock()

    def get(self, company_id: str, scoring_run_id: str) -> CompanyScore | None:
        key = (company_id, scoring_run_id)
        with self._lock:
            return self._scores.get(key)

    def save(self, result: CompanyScore) -> CompanyScore:
        key = (str(result.company_id), result.scoring_run_id)
        with self._lock:
            self._scores[key] = result
            self._company_index.setdefault(str(result.company_id), [])
            if result.scoring_run_id not in self._company_index[str(result.company_id)]:
                self._company_index[str(result.company_id)].append(result.scoring_run_id)
        return result

    def list(self, company_id: str) -> list[CompanyScore]:
        with self._lock:
            scoring_run_ids = self._company_index.get(company_id, [])
            return [self._scores[(company_id, run_id)] for run_id in scoring_run_ids]


@dataclass(frozen=True)
class ScoringContext:
    """Configuration bundle for the scoring engine."""

    mode: str
    system_prompt: str
    model: str
    temperature: float


class ChatGPTScoringEngine:
    """Scores verified companies using OpenAI when online, deterministic rubric otherwise."""

    def __init__(
        self,
        *,
        repository: ScoreRepository | None = None,
        client: OpenAIChatClient | None = None,
        context: ScoringContext | None = None,
        proof_hydrator: ProofLinkHydrator | None = None,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.2,
    ) -> None:
        resolved_context = context or _build_context()
        self._repository = repository or InMemoryScoreRepository()
        self._client = client
        self._context = resolved_context
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._proof_links = proof_hydrator or ProofLinkHydrator(
            default_sources=DEFAULT_SOURCE_MAP,
            cache_ttl_seconds=float(settings.proof_cache_ttl_seconds),
        )

    def score_company(
        self,
        company: CompanyProfile,
        *,
        scoring_run_id: str,
        force: bool = False,
    ) -> CompanyScore:
        """Score a single company and persist the result."""
        if not scoring_run_id:
            raise ScoringValidationError("scoring_run_id is required.", code="422_INVALID_COMPANY_DATA")

        company_key = str(company.company_id)
        cache_state = "miss"
        metrics_tags = {"mode": self._context.mode, "company_id": company_key}
        start = time.perf_counter()
        try:
            cached_score = self._repository.get(company_key, scoring_run_id)
            if cached_score and not force:
                cache_state = "hit"
                metrics.increment("scoring.cache_hit", tags=metrics_tags)
                logger.info(
                    "scoring.cache.hit",
                    extra={
                        "company_id": company_key,
                        "scoring_run_id": scoring_run_id,
                        "mode": self._context.mode,
                    },
                )
                return cached_score

            metrics.increment("scoring.cache_miss", tags=metrics_tags)
            if self._context.mode == "online":
                result = self._score_with_openai(company, scoring_run_id=scoring_run_id)
            else:
                result = self._score_with_rubric(company, scoring_run_id=scoring_run_id)

            persisted = self._repository.save(result)
            metrics.increment("scoring.success", tags=metrics_tags)
            logger.info(
                "scoring.persisted",
                extra={
                    "company_id": company_key,
                    "score": persisted.score,
                    "scoring_run_id": scoring_run_id,
                    "mode": self._context.mode,
                },
            )
            return persisted
        except ScoringEngineError as exc:
            metrics.increment(
                "scoring.errors",
                tags={**metrics_tags, "code": exc.code},
            )
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            metrics.increment(
                "scoring.errors",
                tags={**metrics_tags, "code": getattr(exc, "code", "SCORING_ENGINE_ERROR")},
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            metrics.timing(
                "scoring.latency_ms",
                elapsed_ms,
                tags={**metrics_tags, "cache": cache_state},
            )

    def score_companies(
        self,
        companies: list[CompanyProfile],
        *,
        scoring_run_id: str,
        force: bool = False,
    ) -> list[CompanyScore]:
        """Score an entire batch of verified companies."""
        return [
            self.score_company(company, scoring_run_id=scoring_run_id, force=force)
            for company in companies
        ]

    def fetch_scores(self, company_id: str, scoring_run_id: str | None = None) -> list[CompanyScore]:
        """Retrieve cached scores for API consumption."""
        if scoring_run_id:
            result = self._repository.get(company_id, scoring_run_id)
            return [result] if result else []
        return self._repository.list(company_id)

    def _score_with_openai(self, company: CompanyProfile, *, scoring_run_id: str) -> CompanyScore:
        client = self._ensure_client()
        user_prompt = _render_user_prompt(company)

        def _invoke() -> CompanyScore:
            try:
                response_text = client.generate(
                    system_prompt=self._context.system_prompt,
                    user_prompt=user_prompt,
                    model=self._context.model,
                    temperature=self._context.temperature,
                )
            except OpenAIAPIError as exc:  # pragma: no cover - depends on SDK
                code = "429_RATE_LIMIT" if getattr(exc, "status_code", 500) == 429 else "502_OPENAI_UPSTREAM"
                message = getattr(exc, "message", str(exc))
                raise ScoringProviderError(f"OpenAI request failed: {message}", code=code) from exc
            except OpenAIBaseError as exc:  # pragma: no cover - depends on SDK
                code = "502_OPENAI_UPSTREAM"
                message = getattr(exc, "message", str(exc))
                raise ScoringProviderError(f"OpenAI request failed: {message}", code=code) from exc
            except Exception as exc:  # pragma: no cover - defensive
                raise ScoringProviderError(f"Unexpected OpenAI failure: {exc}", code="502_OPENAI_UPSTREAM") from exc

            try:
                payload = _parse_json_payload(response_text)
            except ValueError as exc:
                logger.error(
                    "scoring.parse_error",
                    extra={"company_id": str(company.company_id), "mode": self._context.mode},
                )
                raise ScoringValidationError("Model response was not valid JSON.", code="502_OPENAI_UPSTREAM") from exc

            return _convert_payload_to_score(
                payload,
                company=company,
                scoring_run_id=scoring_run_id,
                scoring_model=self._context.model,
            )

        return self._execute_with_retry(_invoke)

    def _execute_with_retry(self, func: Callable[[], _T]) -> _T:
        """Retry helper with exponential backoff."""
        delay = self._retry_backoff_seconds
        for attempt in range(1, self._retry_attempts + 1):
            try:
                return func()
            except ScoringProviderError as exc:
                logger.warning(
                    "scoring.retry",
                    extra={"attempt": attempt, "code": exc.code},
                )
                if attempt == self._retry_attempts or exc.code != "429_RATE_LIMIT":
                    raise
                time.sleep(delay)
                delay *= 2

    def _score_with_rubric(self, company: CompanyProfile, *, scoring_run_id: str) -> CompanyScore:
        """Deterministic fallback for fixture/offline mode."""
        breakdown: list[BreakdownItem] = []
        rubric_components: tuple[
            tuple[str, Callable[[CompanyProfile], tuple[str, int]]],
            ...
        ] = (
            ("funding", self._funding_component),
            ("hiring", self._hiring_component),
            ("tech", self._tech_component),
            ("team", self._team_component),
            ("signals", self._signals_component),
        )

        for slug, builder in rubric_components:
            reason, points = builder(company)
            try:
                proofs = self._proof_links.hydrate_many(company, slug)
            except ProofLinkError as exc:
                raise ScoringEngineError(str(exc), code=exc.code) from exc
            breakdown.append(
                _build_breakdown_item(
                    reason=reason,
                    points=points,
                    proofs=proofs,
                )
            )

        total = _align_breakdown_with_score(
            breakdown,
            sum(item.points for item in breakdown),
            company_id=str(company.company_id),
            log_adjustment=False,
        )

        recommended_approach = (
            f"Reach out to {company.name}'s GTM lead via LinkedIn referencing their {company.funding_stage} raise."
        )
        pitch_angle = f"Help {company.name} convert {company.funding_stage} capital into outbound pipeline."

        return CompanyScore(
            company_id=company.company_id,
            score=total,
            breakdown=breakdown,
            recommended_approach=recommended_approach,
            pitch_angle=pitch_angle,
            scoring_model="fixture-rubric",
            scoring_run_id=scoring_run_id,
        )

    def _ensure_client(self) -> OpenAIChatClient:
        if self._client:
            return self._client
        if not settings.openai_api_key:
            raise ScoringProviderError(
                "OPENAI_API_KEY is required for online scoring.",
                code="502_OPENAI_UPSTREAM",
            )
        self._client = OpenAIResponseClient(settings.openai_api_key)
        return self._client

    @staticmethod
    def _funding_component(company: CompanyProfile) -> tuple[str, int]:
        if company.days_since_funding <= 90:
            points = 30
        else:
            decay_periods = math.floor((company.days_since_funding - 90) / 15)
            points = max(0, 30 - decay_periods * 5)
        reason = f"Funding {company.days_since_funding} days ago ({company.funding_amount} {company.funding_stage})"
        return reason, points

    @staticmethod
    def _hiring_component(company: CompanyProfile) -> tuple[str, int]:
        roles = company.job_postings
        if roles >= 5:
            points = 25
        elif roles >= 3:
            points = 18
        elif roles >= 1:
            points = 10
        else:
            points = 0
        return f"{roles} open sales roles", points

    @staticmethod
    def _tech_component(company: CompanyProfile) -> tuple[str, int]:
        stack = {tool.lower() for tool in company.tech_stack}
        if {"salesforce", "hubspot"}.issubset(stack):
            points = 20
        elif "salesforce" in stack or "hubspot" in stack:
            points = 12
        elif {"outreach", "apollo", "salesloft"} & stack:
            points = 8
        else:
            points = 0
        stack_display = ", ".join(company.tech_stack) or "no CRM fit yet"
        return f"Tech stack includes {stack_display}", points

    @staticmethod
    def _team_component(company: CompanyProfile) -> tuple[str, int]:
        employees = company.employee_count
        if 25 <= employees <= 50:
            points = 15
        elif 15 <= employees <= 80:
            points = 10
        elif employees > 80:
            points = 8
        else:
            points = 5
        return f"Team size at {employees} employees", points

    @staticmethod
    def _signals_component(company: CompanyProfile) -> tuple[str, int]:
        has_signals = bool(company.buying_signals)
        points = 10 if has_signals else 2
        reason = "Recent buying signals present" if has_signals else "Limited external signals"
        return reason, points


def _build_context() -> ScoringContext:
    prompt_path = Path(settings.scoring_system_prompt_path).expanduser()
    if not prompt_path.exists():
        raise FileNotFoundError(f"Scoring prompt not found at {prompt_path}")
    system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    return ScoringContext(
        mode=settings.fund_signal_mode.lower(),
        system_prompt=system_prompt,
        model=settings.scoring_model,
        temperature=settings.scoring_temperature,
    )


def _render_user_prompt(company: CompanyProfile) -> str:
    tech_stack = ", ".join(company.tech_stack) if company.tech_stack else "Unknown"
    signals = "\n".join(str(url) for url in company.buying_signals) if company.buying_signals else "None provided"
    return (
        "Score this company using the rubric and return JSON only.\n"
        f"Company: {company.name}\n"
        f"Funding: {company.funding_amount} {company.funding_stage}\n"
        f"Days since funding: {company.days_since_funding}\n"
        f"Employees: {company.employee_count}\n"
        f"Sales roles open: {company.job_postings}\n"
        f"Tech stack: {tech_stack}\n"
        f"Buying signals:\n{signals}\n"
        f"Verified sources: {', '.join(company.verified_sources) or 'None'}\n"
        f"Company ID: {company.company_id}\n"
    )


def _convert_payload_to_score(
    payload: dict[str, Any],
    *,
    company: CompanyProfile,
    scoring_run_id: str,
    scoring_model: str,
) -> CompanyScore:
    try:
        breakdown_payload = payload.get("breakdown", [])
        breakdown = [BreakdownItem(**item) for item in breakdown_payload]
        if not breakdown:
            raise ValueError("Breakdown is required.")

        score_value = int(payload["score"])
        recommended = payload.get("recommended_approach", "").strip()
        pitch = payload.get("pitch_angle", "").strip()
        if not recommended or not pitch:
            raise ValueError("Missing recommendations.")
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoringValidationError(
            "Model response missing required fields.",
            code="422_INVALID_COMPANY_DATA",
        ) from exc

    adjusted_score = _align_breakdown_with_score(
        breakdown,
        score_value,
        company_id=str(company.company_id),
        log_adjustment=True,
    )
    return CompanyScore(
        company_id=company.company_id,
        score=adjusted_score,
        breakdown=breakdown,
        recommended_approach=recommended,
        pitch_angle=pitch,
        scoring_model=scoring_model,
        scoring_run_id=scoring_run_id,
    )


def _parse_json_payload(raw_text: str) -> dict[str, Any]:
    """Best-effort JSON decoding that tolerates code fences or prose."""
    candidate = raw_text.strip()
    if candidate.startswith("```"):
        candidate = "\n".join(line for line in candidate.splitlines() if not line.strip().startswith("```")).strip()
    if candidate.startswith("{") and candidate.endswith("}"):
        return json.loads(candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(candidate[start : end + 1])
    raise ValueError("Response did not contain JSON object.")


def _build_breakdown_item(
    *,
    reason: str,
    points: int,
    proofs: list[SignalProof],
) -> BreakdownItem:
    if not proofs:
        raise ValueError("At least one proof is required.")
    return BreakdownItem(
        reason=reason,
        points=_clamp(points, -100, 100),
        proof=proofs[0],
        proofs=proofs,
    )


def _align_breakdown_with_score(
    breakdown: list[BreakdownItem],
    target_score: int,
    *,
    company_id: str | None,
    log_adjustment: bool,
) -> int:
    score_value = _clamp(target_score, 0, 100)
    delta = score_value - sum(item.points for item in breakdown)
    if delta and breakdown:
        breakdown[-1].points += delta
        if log_adjustment:
            logger.warning(
                "scoring.adjusted_breakdown",
                extra={
                    "company_id": company_id,
                    "expected": score_value,
                    "delta": delta,
                },
            )
    return score_value


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


_ENGINE_INSTANCE: ChatGPTScoringEngine | None = None


def get_scoring_engine() -> ChatGPTScoringEngine:
    """Singleton accessor used by API routes."""
    global _ENGINE_INSTANCE  # noqa: PLW0603
    if _ENGINE_INSTANCE is None:
        _ENGINE_INSTANCE = ChatGPTScoringEngine()
    return _ENGINE_INSTANCE
