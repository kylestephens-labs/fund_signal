"""Secret rotation utility for capture-runner API keys."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("tools.rotate_keys")

MAX_AGE_DAYS = 90
DEFAULT_STATE_PATH = Path("security/rotation_state.json")


class SecretError(RuntimeError):
    """Raised when a secret is missing or expired."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderConfig:
    """Metadata describing a provider secret."""

    name: str
    env_var: str
    scope: str
    max_age_days: int = MAX_AGE_DAYS


PROVIDERS = {
    "exa": ProviderConfig(name="exa", env_var="EXA_API_KEY", scope="read_exa_api"),
    "youcom": ProviderConfig(name="youcom", env_var="YOUCOM_API_KEY", scope="read_youcom_api"),
    "tavily": ProviderConfig(name="tavily", env_var="TAVILY_API_KEY", scope="read_tavily_api"),
    "supabase": ProviderConfig(name="supabase", env_var="SUPABASE_SERVICE_KEY", scope="write_supabase_storage"),
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rotate or validate capture runner secrets.")
    parser.add_argument("--provider", action="append", default=["all"], help="Provider(s) to rotate (exa|youcom|tavily|supabase).")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH, help="Path to rotation metadata file.")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS, help="Maximum allowed secret age.")
    parser.add_argument("--check-only", action="store_true", help="Only verify rotation window; do not modify state.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate rotation without persisting state.")
    parser.add_argument("--force", action="store_true", help="Rotate even if within the max age window.")
    parser.add_argument("--output", type=Path, help="Optional path to write JSON results.")
    return parser.parse_args(argv)


def select_providers(requested: Iterable[str]) -> list[ProviderConfig]:
    names = []
    for item in requested:
        if item == "all":
            names.extend(PROVIDERS.keys())
        else:
            names.append(item.lower())
    seen = []
    for name in names:
        if name not in PROVIDERS:
            raise SecretError("E_SECRET_UNKNOWN", f"Unsupported provider: {name}")
        if name not in seen:
            seen.append(name)
    return [PROVIDERS[name] for name in seen]


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SecretError("E_SECRET_STATE_INVALID", f"{path} must contain an object.")
    return {str(k): str(v) for k, v in payload.items()}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_now(now: datetime | None = None) -> str:
    value = (now or datetime.now(UTC)).astimezone(UTC)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_env_present(provider: ProviderConfig) -> None:
    if not os.getenv(provider.env_var):
        raise SecretError("E_SECRET_MISSING", f"{provider.env_var} is not set.")


def _age_days(last_rotated: datetime, now: datetime) -> float:
    delta = now - last_rotated
    return delta.total_seconds() / 86400


def _rotation_threshold(provider: ProviderConfig, override: int) -> int:
    return min(provider.max_age_days, override)


def run_rotation(
    providers: Sequence[ProviderConfig],
    *,
    state_file: Path,
    max_age_days: int,
    check_only: bool,
    dry_run: bool,
    force: bool,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    now = now or datetime.now(UTC)
    state = load_state(state_file)
    results: list[dict[str, str]] = []

    for provider in providers:
        _ensure_env_present(provider)
        raw_last = state.get(provider.name)
        last_rotated = parse_timestamp(raw_last) if raw_last else None
        age = _age_days(last_rotated, now) if last_rotated else float("inf")
        threshold = _rotation_threshold(provider, max_age_days)
        expired = age > threshold

        if check_only:
            if expired:
                raise SecretError(
                    "E_SECRET_EXPIRED",
                    f"{provider.name} secret exceeded {threshold}d rotation window (age {age:.1f}d).",
                )
            results.append(_rotation_result(provider.name, "valid", raw_last))
            continue

        if not expired and not force:
            results.append(_rotation_result(provider.name, "skipped", raw_last))
            continue

        if dry_run:
            results.append(_rotation_result(provider.name, "pending", raw_last))
            continue

        rotated_at = iso_now(now)
        state[provider.name] = rotated_at
        results.append(_rotation_result(provider.name, "success", rotated_at))

    if not dry_run and not check_only:
        save_state(state_file, state)
    return results


def _rotation_result(provider: str, status: str, rotated_at: str | None) -> dict[str, str | None]:
    return {
        "provider": provider,
        "status": status,
        "rotated_at": rotated_at,
    }


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        providers = select_providers(args.provider)
        results = run_rotation(
            providers,
            state_file=args.state_file,
            max_age_days=args.max_age_days,
            check_only=args.check_only,
            dry_run=args.dry_run,
            force=args.force,
        )
    except SecretError as exc:
        logger.error("Secret rotation failed: %s (code=%s)", exc, exc.code)
        return 1

    output = json.dumps(results, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    logger.info("Rotation results:\n%s", output)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
