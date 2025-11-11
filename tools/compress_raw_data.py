"""Compress raw capture payloads into gzipped JSONL files."""

from __future__ import annotations

import argparse
import gzip
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from tools import capture_pipeline

logger = logging.getLogger("tools.compress_raw_data")

RAW_SUBDIR = "raw"
COMPRESSED_SUFFIX = ".jsonl.gz"
SUPPORTED_SUFFIXES = {".json", ".jsonl"}


class CompressionError(RuntimeError):
    """Raised when raw payload compression fails."""

    def __init__(self, message: str, code: str = "E_COMPRESSION_FAILED") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompressionResult:
    """Metadata about a single compression step."""

    source: Path
    output: Path
    records: int
    skipped: bool = False


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compress raw vendor payloads into .jsonl.gz files.")
    parser.add_argument("--input", type=Path, required=True, help="Path to capture bundle (expects raw/ subdir).")
    parser.add_argument("--dry-run", action="store_true", help="Report planned work without modifying files.")
    return parser.parse_args(argv)


def find_raw_files(bundle_path: Path) -> list[Path]:
    """Return a deterministic, sorted list of raw payload files within bundle/raw."""
    raw_dir = bundle_path / RAW_SUBDIR
    if not raw_dir.exists():
        logger.warning("Raw directory not found under %s; skipping compression.", bundle_path)
        return []
    targets: list[Path] = []
    for candidate in raw_dir.rglob("*"):
        if _is_compressible(candidate):
            targets.append(candidate)
    return sorted(targets)


def _is_compressible(path: Path) -> bool:
    return path.is_file() and path.suffix in SUPPORTED_SUFFIXES and not path.name.endswith(COMPRESSED_SUFFIX)


def _iter_records(path: Path) -> Iterator[dict]:
    if path.suffix == ".jsonl":
        yield from _iter_jsonl(path)
    else:
        yield from _iter_json(path)


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as infile:
        for line in infile:
            line = line.strip()
            if line:
                yield json.loads(line)


def _iter_json(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as infile:
        payload = json.load(infile)
    if isinstance(payload, list):
        yield from payload
    else:
        yield payload


def _target_output_path(source: Path) -> Path:
    if source.suffix == ".jsonl":
        return source.with_suffix(source.suffix + ".gz")
    return source.with_suffix(COMPRESSED_SUFFIX)


def _count_records(path: Path) -> int:
    return sum(1 for _ in _iter_records(path))


def compress_file(source: Path, *, dry_run: bool = False) -> CompressionResult:
    output = _target_output_path(source)
    if output.exists():
        logger.info("Skipping %s (already compressed).", source)
        return CompressionResult(source=source, output=output, records=0, skipped=True)

    if dry_run:
        count = _count_records(source)
        logger.info("[DRY-RUN] Would compress %s -> %s (%s records)", source, output, count)
        return CompressionResult(source=source, output=output, records=count, skipped=True)

    logger.info("Compressing %s -> %s", source, output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8") as outfile:
        count = 0
        for count, record in enumerate(_iter_records(source), start=1):
            json.dump(record, outfile, separators=(",", ":"))
            outfile.write("\n")

    source.unlink()
    return CompressionResult(source=source, output=output, records=count)


def compress_bundle(bundle_path: Path, *, dry_run: bool = False) -> list[CompressionResult]:
    results: list[CompressionResult] = []
    for path in find_raw_files(bundle_path):
        try:
            results.append(compress_file(path, dry_run=dry_run))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompressionError(f"Failed to compress {path}: {exc}") from exc
    if not dry_run:
        update_manifest(bundle_path, results)
    return results


def update_manifest(bundle_path: Path, results: list[CompressionResult]) -> None:
    manifest_path = bundle_path / "manifest.json"
    if not manifest_path.exists():
        logger.warning("Manifest not found at %s; skipping manifest update.", manifest_path)
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompressionError(f"Unable to parse manifest: {exc}", code="E_SCHEMA_INVALID") from exc

    files = manifest.get("files")
    if not isinstance(files, list):
        raise CompressionError("Manifest missing files array.", code="E_SCHEMA_INVALID")

    files_by_path: dict[str, dict] = {}
    for entry in files:
        if isinstance(entry, dict) and "path" in entry:
            files_by_path[entry["path"]] = entry

    updated = False
    for result in results:
        if result.skipped:
            continue
        try:
            source_rel = result.source.relative_to(bundle_path).as_posix()
            output_rel = result.output.relative_to(bundle_path).as_posix()
        except ValueError:
            # Should not happen, but guard against it.
            logger.warning("Skipping manifest update for %s (outside bundle).", result.source)
            continue

        entry = files_by_path.get(source_rel)
        if not entry:
            logger.warning("Manifest entry for %s not found; creating new entry.", source_rel)
            entry = {"path": output_rel}
            files.append(entry)
        entry["path"] = output_rel
        entry["size"] = result.output.stat().st_size
        entry["checksum"] = capture_pipeline.sha256_file(result.output)
        updated = True

    if not updated:
        return

    manifest_no_sig = dict(manifest)
    manifest_no_sig.pop("signature", None)
    manifest["signature"] = capture_pipeline.sign_manifest(manifest_no_sig)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    try:
        results = compress_bundle(args.input, dry_run=args.dry_run)
    except CompressionError as exc:
        logger.error("Compression failed: %s (code=%s)", exc, exc.code)
        return 1

    processed = [r for r in results if not r.skipped]
    logger.info("Compression complete. files=%s", len(processed))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
