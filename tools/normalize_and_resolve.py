"""Run candidate generation + resolver in a single deterministic pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from tools import candidate_generator, resolve_company_name
from tools.manifest_utils import compute_sha256, update_manifest
from tools.resolver_rules import load_rules as load_resolver_rules
from tools.telemetry import get_telemetry

logger = logging.getLogger("tools.normalize_and_resolve")


def _atomic_write(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _relative_path(path: Path, bundle_root: Path) -> str | None:
    try:
        return str(path.relative_to(bundle_root))
    except ValueError:
        logger.warning("Path %s is outside bundle root %s; skipping manifest update.", path, bundle_root)
        return None


def run_pipeline(
    *,
    input_path: Path,
    candidates_out: Path,
    normalized_out: Path,
    normalizer_rules: Path,
    resolver_rules: Path,
    manifest_path: Path | None = None,
    ):
    start = time.perf_counter()
    tmp_dir = candidates_out.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_candidates = candidates_out.with_suffix(".candidates.tmp.json")
    tmp_normalized = normalized_out.with_suffix(".normalized.tmp.json")

    logger.info("Generating candidates from %s", input_path)
    generator_payload = candidate_generator.generate_candidates(
        input_path=input_path,
        output_path=tmp_candidates,
        rules_path=normalizer_rules,
    )

    resolver_ruleset = load_resolver_rules(resolver_rules)
    logger.info("Resolving candidates using ruleset=%s sha=%s", resolver_ruleset.version, resolver_ruleset.ruleset_sha256)
    resolver_payload = resolve_company_name.resolve_company_name(
        input_path=tmp_candidates,
        output_path=tmp_normalized,
        rules=resolver_ruleset,
    )

    _atomic_write(tmp_candidates, candidates_out)
    _atomic_write(tmp_normalized, normalized_out)

    if manifest_path:
        bundle_root = manifest_path.parent
        candidates_rel = _relative_path(candidates_out, bundle_root)
        normalized_rel = _relative_path(normalized_out, bundle_root)
        updates: dict[str, str] = {}
        if candidates_rel:
            updates[candidates_rel] = compute_sha256(candidates_out)
        if normalized_rel:
            updates[normalized_rel] = compute_sha256(normalized_out)
        if updates:
            update_manifest(manifest_path, updates)

    summary = {
        "generator": {
            "ruleset_version": generator_payload.get("ruleset_version"),
            "ruleset_sha256": generator_payload.get("ruleset_sha256"),
            "metrics": generator_payload.get("metrics"),
        },
        "resolver": {
            "ruleset_version": resolver_payload.get("resolver_ruleset_version"),
            "ruleset_sha256": resolver_payload.get("resolver_ruleset_sha256"),
            "metrics": {
                "items_total": resolver_payload.get("items_total"),
                "items_resolved": resolver_payload.get("items_resolved"),
                "items_skipped": resolver_payload.get("items_skipped"),
            },
        },
    }
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info(
        "normalize_and_resolve complete elapsed_ms=%s summary=%s",
        elapsed_ms,
        json.dumps(summary, sort_keys=True),
    )
    telemetry = get_telemetry()
    telemetry.emit(
        module="normalize_and_resolve",
        event="summary",
        elapsed_ms=elapsed_ms,
        generator=summary["generator"],
        resolver=summary["resolver"],
    )
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end deterministic normalization (generator + resolver).")
    parser.add_argument("--input", type=Path, required=True, help="Raw Exa seed JSON/JSONL input.")
    parser.add_argument("--candidates-out", type=Path, required=True, help="Path to write candidates JSON.")
    parser.add_argument("--normalized-out", type=Path, required=True, help="Path to write normalized JSON.")
    parser.add_argument("--normalizer-rules", type=Path, default=Path("configs/normalizer_rules.v1.yaml"))
    parser.add_argument("--resolver-rules", type=Path, default=Path("configs/resolver_rules.v1.yaml"))
    parser.add_argument(
        "--update-manifest",
        type=Path,
        default=None,
        help="Optional manifest.json to update with candidate/normalized SHA entries.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        run_pipeline(
            input_path=args.input,
            candidates_out=args.candidates_out,
            normalized_out=args.normalized_out,
            normalizer_rules=args.normalizer_rules,
            resolver_rules=args.resolver_rules,
            manifest_path=args.update_manifest,
        )
    except FileNotFoundError as exc:
        logger.error("INPUT_READ_ERROR: %s", exc)
        return 1
    except Exception as exc:  # pragma: no cover
        logger.exception("normalize_and_resolve failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
