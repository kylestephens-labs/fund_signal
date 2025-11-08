"""Retention enforcement for raw vs canonical capture artifacts."""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

logger = logging.getLogger("tools.enforce_retention")

DEFAULT_RAW_RETENTION_DAYS = 30
DEFAULT_CANONICAL_RETENTION_DAYS = 90
CANONICAL_DIRS = ("leads", "fixtures")


class RetentionError(RuntimeError):
    """Raised when retention enforcement fails."""

    def __init__(self, message: str, code: str = "E_RETENTION_FAILED") -> None:
        super().__init__(message)
        self.code = code


def _env_or_fallback(env_key: str, fallback: int) -> int:
    value = os.getenv(env_key)
    if value is None or not value.strip():
        return fallback
    try:
        days = int(value)
    except ValueError as exc:
        raise ValueError(f"{env_key} must be an integer: {exc}") from exc
    if days <= 0:
        raise ValueError(f"{env_key} must be greater than zero.")
    return days


def _default_raw_days() -> int:
    return _env_or_fallback("RETENTION_RAW_DAYS", DEFAULT_RAW_RETENTION_DAYS)


def _default_canonical_days() -> int:
    return _env_or_fallback("RETENTION_CANONICAL_DAYS", DEFAULT_CANONICAL_RETENTION_DAYS)


@dataclass
class RetentionResult:
    raw_deleted: list[str] = field(default_factory=list)
    canonical_deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bytes_deleted: int = 0


@dataclass(frozen=True)
class BundleTargets:
    manifest_path: Path
    age_days: float
    raw_files: list[Path]
    canonical_files: list[Path]


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer day count.") from exc
    if days <= 0:
        raise argparse.ArgumentTypeError("Retention windows must be greater than zero days.")
    return days


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete expired raw/canonical artifacts according to policy.")
    parser.add_argument("--path", type=Path, required=True, help="Root directory containing capture bundles.")
    parser.add_argument("--delete", action="store_true", help="Actually delete files (default dry-run).")
    parser.add_argument(
        "--raw-days",
        type=_positive_days,
        default=None,
        help=f"Retention window for raw payloads (env RETENTION_RAW_DAYS or default {DEFAULT_RAW_RETENTION_DAYS}).",
    )
    parser.add_argument(
        "--canonical-days",
        type=_positive_days,
        default=None,
        help=(
            "Retention window for canonical data "
            f"(env RETENTION_CANONICAL_DAYS or default {DEFAULT_CANONICAL_RETENTION_DAYS})."
        ),
    )
    parser.add_argument("--report", type=Path, default=Path("retention-report.json"), help="Where to write the JSON summary.")
    args = parser.parse_args(argv)
    try:
        args.raw_days = args.raw_days or _default_raw_days()
        args.canonical_days = args.canonical_days or _default_canonical_days()
    except ValueError as exc:
        parser.error(str(exc))
    return args


def parse_timestamp(value: str | None, *, fallback: datetime) -> datetime:
    if not value:
        return fallback
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _bundle_age_days(bundle_dir: Path, manifest: dict, now: datetime) -> float:
    captured_at_str = manifest.get("captured_at")
    fallback = datetime.fromtimestamp(bundle_dir.stat().st_mtime, tz=timezone.utc)
    captured_at = parse_timestamp(captured_at_str, fallback=fallback)
    return (now - captured_at).total_seconds() / 86400


def _collect_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [path for path in directory.rglob("*") if path.is_file()]


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        ordered.append(path)
        seen.add(path)
    return ordered


def _build_bundle_targets(manifest_path: Path, age_days: float) -> BundleTargets:
    bundle_dir = manifest_path.parent
    raw_files = _collect_files(bundle_dir / "raw")
    canonical_files = []
    for directory in CANONICAL_DIRS:
        canonical_files.extend(_collect_files(bundle_dir / directory))
    canonical_files.append(manifest_path)
    return BundleTargets(
        manifest_path=manifest_path,
        age_days=age_days,
        raw_files=_dedupe_paths(raw_files),
        canonical_files=_dedupe_paths(canonical_files),
    )


def _delete_paths(paths: list[Path], *, delete: bool) -> int:
    bytes_removed = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RetentionError(f"Unable to stat {path}: {exc}", code="E_RETENTION_PERMISSION") from exc
        if delete:
            try:
                path.unlink()
            except PermissionError as exc:
                raise RetentionError(f"Permission denied deleting {path}: {exc}", code="E_RETENTION_PERMISSION") from exc
            logger.info("Deleted %s", path)
        else:
            logger.info("[DRY-RUN] Would delete %s", path)
        bytes_removed += size
    return bytes_removed


def _apply_window(
    candidates: list[Path],
    *,
    threshold_days: int,
    age_days: float,
    delete: bool,
) -> tuple[list[str], int]:
    if not candidates or age_days < threshold_days:
        return [], 0
    bytes_removed = _delete_paths(candidates, delete=delete)
    return [str(path) for path in candidates], bytes_removed


def _load_manifest(manifest_path: Path) -> dict | None:
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Unable to parse manifest %s: %s", manifest_path, exc)
        return None


def enforce_retention(
    root: Path,
    *,
    delete: bool,
    raw_days: int,
    canonical_days: int,
    now: datetime | None = None,
) -> RetentionResult:
    if not root.exists():
        raise RetentionError(f"Path not found: {root}", code="E_RETENTION_PATH")

    now = now or datetime.now(timezone.utc)
    manifests = sorted(root.rglob("manifest.json"))
    result = RetentionResult()

    for manifest_path in manifests:
        bundle_dir = manifest_path.parent
        manifest = _load_manifest(manifest_path)
        if manifest is None:
            result.errors.append(f"{manifest_path}:E_MANIFEST_INVALID")
            continue

        try:
            age_days = _bundle_age_days(bundle_dir, manifest, now)
        except OSError as exc:
            logger.error("Failed to stat bundle %s: %s", bundle_dir, exc)
            result.errors.append(f"{bundle_dir}:E_RETENTION_PERMISSION")
            continue

        bundle = _build_bundle_targets(manifest_path, age_days)

        try:
            deleted_raw, bytes_removed = _apply_window(
                bundle.raw_files, threshold_days=raw_days, age_days=bundle.age_days, delete=delete
            )
        except RetentionError as exc:
            logger.error("%s", exc)
            result.errors.append(f"{bundle_dir}:{exc.code}")
        else:
            result.raw_deleted.extend(deleted_raw)
            result.bytes_deleted += bytes_removed

        try:
            deleted_canonical, bytes_removed = _apply_window(
                bundle.canonical_files, threshold_days=canonical_days, age_days=bundle.age_days, delete=delete
            )
        except RetentionError as exc:
            logger.error("%s", exc)
            result.errors.append(f"{bundle_dir}:{exc.code}")
        else:
            result.canonical_deleted.extend(deleted_canonical)
            result.bytes_deleted += bytes_removed

    return result


def write_report(report_path: Path, *, payload: dict) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        outcome = enforce_retention(
            args.path,
            delete=args.delete,
            raw_days=args.raw_days,
            canonical_days=args.canonical_days,
        )
    except RetentionError as exc:
        logger.error("Retention enforcement failed: %s (code=%s)", exc, exc.code)
        return 1

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "raw_deleted": outcome.raw_deleted,
        "canonical_deleted": outcome.canonical_deleted,
        "errors": outcome.errors,
        "storage_saved_mb": round(outcome.bytes_deleted / (1024 * 1024), 2),
    }
    write_report(args.report, payload=summary)
    logger.info(
        "Retention summary raw=%s canonical=%s bytes=%.2fMB delete=%s",
        len(outcome.raw_deleted),
        len(outcome.canonical_deleted),
        outcome.bytes_deleted / (1024 * 1024),
        args.delete,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
