"""Loader/validator for resolver rulesets."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("tools.resolver_rules")

DEFAULT_RULES_PATH = Path("configs/resolver_rules.v1.1.yaml")
ALLOWED_TIE_BREAKERS = {"score_desc", "token_count_asc", "appears_in_title_first", "lexicographic_ci"}


class ResolverRulesError(RuntimeError):
    """Raised when resolver rules cannot be loaded or validated."""

    def __init__(self, message: str, code: str = "RULES_LOAD_ERROR") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolverRules:
    version: str
    weights: Mapping[str, float]
    tie_breakers: tuple[str, ...]
    slug_head_edit_distance_threshold: int
    token_limits: Mapping[str, int] | None
    ruleset_sha256: str


def load_rules(path: Path | None = None) -> ResolverRules:
    target = (path or DEFAULT_RULES_PATH).expanduser()
    if not target.exists():
        raise ResolverRulesError(f"Ruleset not found at {target}", code="RULES_LOAD_ERROR")
    try:
        blob = target.read_bytes()
        parsed = yaml.safe_load(blob.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise ResolverRulesError(f"Unable to parse YAML: {exc}", code="RULES_SCHEMA_INVALID") from exc
    if not isinstance(parsed, Mapping):
        raise ResolverRulesError("Ruleset must be a mapping.", code="RULES_SCHEMA_INVALID")

    version = str(parsed.get("version") or "").strip()
    if not version:
        raise ResolverRulesError("version is required.", code="RULES_SCHEMA_INVALID")

    weights = parsed.get("weights")
    if not isinstance(weights, Mapping) or not weights:
        raise ResolverRulesError("weights must be a non-empty mapping.", code="RULES_SCHEMA_INVALID")
    normalized_weights: dict[str, float] = {}
    for key, value in weights.items():
        if not isinstance(key, str):
            raise ResolverRulesError(f"weights contains non-string key: {key}", code="RULES_SCHEMA_INVALID")
        if not isinstance(value, int | float):
            raise ResolverRulesError(f"weights[{key}] must be numeric.", code="RULES_SCHEMA_INVALID")
        normalized_weights[key] = float(value)

    tie_breakers = parsed.get("tie_breakers")
    if not isinstance(tie_breakers, Sequence) or not tie_breakers:
        raise ResolverRulesError("tie_breakers must be a non-empty list.", code="RULES_SCHEMA_INVALID")
    normalized_tie_breakers: list[str] = []
    for breaker in tie_breakers:
        if breaker not in ALLOWED_TIE_BREAKERS:
            raise ResolverRulesError(f"Unsupported tie breaker: {breaker}", code="RULES_SCHEMA_INVALID")
        normalized_tie_breakers.append(str(breaker))

    threshold = parsed.get("slug_head_edit_distance_threshold")
    if not isinstance(threshold, int) or threshold < 0:
        raise ResolverRulesError("slug_head_edit_distance_threshold must be a non-negative integer.", code="RULES_SCHEMA_INVALID")

    token_limits = parsed.get("token_limits")
    normalized_token_limits = None
    if token_limits is not None:
        if not isinstance(token_limits, Mapping):
            raise ResolverRulesError("token_limits must be a mapping when provided.", code="RULES_SCHEMA_INVALID")
        normalized_token_limits = {}
        for key, value in token_limits.items():
            if not isinstance(value, int) or value < 0:
                raise ResolverRulesError(f"token_limits[{key}] must be a non-negative integer.", code="RULES_SCHEMA_INVALID")
            normalized_token_limits[str(key)] = value

    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    ruleset_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    logger.info("Loaded resolver rules version=%s sha=%s", version, ruleset_sha256)

    return ResolverRules(
        version=version,
        weights=normalized_weights,
        tie_breakers=tuple(normalized_tie_breakers),
        slug_head_edit_distance_threshold=threshold,
        token_limits=normalized_token_limits,
        ruleset_sha256=ruleset_sha256,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolver rules loader/validator.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH, help="Path to resolver rules YAML.")
    parser.add_argument("--print-sha", action="store_true", help="Print the ruleset sha256 and exit.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv or [])
    try:
        rules = load_rules(args.rules)
    except ResolverRulesError as exc:
        logger.error("%s (code=%s)", exc, exc.code)
        return 1
    if args.print_sha:
        print(rules.ruleset_sha256)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
