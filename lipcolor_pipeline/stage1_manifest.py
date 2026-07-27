"""Stage 1 immutable source/content/occurrence manifest builder."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import traceback
import unicodedata
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from download_product_images import (
    IMAGE_FIELDS,
    output_filename,
    parse_image_urls,
    sanitize_component,
    set_large_csv_field_limit,
    url_extension,
)

from . import __version__


SCHEMA_VERSION = "stage1-1"
NAMING_RULE_VERSION = "download-product-images-sha1-url-12-v1"
ROOT_ALIAS = "raw_images"
ID_NAMESPACE = uuid.UUID("01b2a690-e5b7-4a3c-a5b1-3d57437da9ec")
IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".avif",
    ".heic",
    ".heif",
}
MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
    "BMP": "image/bmp",
    "AVIF": "image/avif",
    "HEIF": "image/heif",
    "UNKNOWN": "application/octet-stream",
}
FORMAT_EXTENSIONS = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "GIF": {".gif"},
    "WEBP": {".webp"},
    "TIFF": {".tif", ".tiff"},
    "BMP": {".bmp"},
    "AVIF": {".avif"},
    "HEIF": {".heic", ".heif"},
}
TABLE_EXPORT_ORDER = (
    "dataset_snapshots",
    "pipeline_runs",
    "folder_groups",
    "source_records",
    "source_image_refs",
    "image_contents",
    "image_occurrences",
    "source_ref_occurrences",
    "brand_alias_candidates",
    "legacy_id_mappings",
    "derived_assets",
    "pipeline_errors",
)


@dataclass(frozen=True)
class FileObservation:
    relative_path: str
    filename: str
    extension: str
    sha256: str
    byte_size: int
    mtime_ns: int
    detected_format: str
    mime_type: str
    extension_mismatch: int


@dataclass(frozen=True)
class LegacyMetadata:
    legacy_image_id: str
    sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    name = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{uuid.uuid5(ID_NAMESPACE, name).hex}"


def normalize_relative_path(path: str | Path) -> str:
    text = str(path).replace("\\", "/")
    return unicodedata.normalize("NFC", text).strip("/")


def detect_image_format(header: bytes) -> str:
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    if header.startswith(b"BM"):
        return "BMP"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "WEBP"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"avif", b"avis"}:
            return "AVIF"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "HEIF"
    return "UNKNOWN"


def observe_file(path: Path, root: Path) -> FileObservation:
    before = path.stat()
    digest = hashlib.sha256()
    header = b""
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            if not header:
                header = chunk[:32]
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed while hashing: {path.relative_to(root)}")

    detected_format = detect_image_format(header)
    extension = path.suffix.lower()
    expected_extensions = FORMAT_EXTENSIONS.get(detected_format, set())
    mismatch = int(
        detected_format != "UNKNOWN" and extension not in expected_extensions
    )
    return FileObservation(
        relative_path=normalize_relative_path(path.relative_to(root)),
        filename=path.name,
        extension=extension,
        sha256=digest.hexdigest(),
        byte_size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        detected_format=detected_format,
        mime_type=MIME_TYPES[detected_format],
        extension_mismatch=mismatch,
    )


def discover_source_files(root: Path) -> tuple[list[Path], list[str]]:
    images: list[Path] = []
    ignored: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = normalize_relative_path(path.relative_to(root))
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(path)
        else:
            ignored.append(relative)
    images.sort(key=lambda item: normalize_relative_path(item.relative_to(root)).casefold())
    ignored.sort(key=str.casefold)
    return images, ignored


def observe_source_files(
    paths: Sequence[Path],
    root: Path,
    *,
    workers: int,
    progress_every: int = 1000,
) -> list[FileObservation]:
    observations: list[FileObservation] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(observe_file, path, root): path for path in paths}
        for completed, future in enumerate(as_completed(futures), start=1):
            observations.append(future.result())
            if progress_every and (
                completed % progress_every == 0 or completed == len(paths)
            ):
                print(
                    f"hashed {completed}/{len(paths)} source images",
                    flush=True,
                )
    observations.sort(key=lambda item: item.relative_path.casefold())
    return observations


def load_legacy_metadata(path: Path | None) -> tuple[dict[str, LegacyMetadata], str]:
    if path is None or not path.exists():
        return {}, ""

    metadata_sha256 = sha256_file(path)
    mapping: dict[str, LegacyMetadata] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"relative_path", "image_id", "sha256"}
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"legacy metadata missing fields: {', '.join(sorted(missing))}"
            )
        for row in reader:
            relative_path = normalize_relative_path(row["relative_path"])
            value = LegacyMetadata(
                legacy_image_id=(row.get("image_id") or "").strip(),
                sha256=(row.get("sha256") or "").strip().lower(),
            )
            previous = mapping.get(relative_path)
            if previous is not None and previous != value:
                raise ValueError(f"conflicting legacy metadata: {relative_path}")
            mapping[relative_path] = value
    return mapping, metadata_sha256


def default_source_csv(repo_root: Path) -> Path:
    candidates = sorted((repo_root / "data").glob("*.csv"))
    if len(candidates) != 1:
        raise ValueError(
            f"expected exactly one source CSV in data/, found {len(candidates)}"
        )
    return candidates[0]


def repo_uri(path: Path, repo_root: Path) -> str:
    try:
        return f"repo://{normalize_relative_path(path.resolve().relative_to(repo_root.resolve()))}"
    except ValueError:
        return f"external://{path.name}"


def git_state(repo_root: Path) -> tuple[str, int]:
    def run(args: Sequence[str]) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return completed.stdout.strip()

    try:
        commit = run(["rev-parse", "HEAD"])
        dirty = int(bool(run(["status", "--porcelain"])))
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", 1


def code_fingerprint(repo_root: Path) -> str:
    paths = (
        repo_root / "lipcolor_pipeline" / "stage1_manifest.py",
        repo_root / "database" / "migrations" / "001_stage1.sql",
        repo_root / "download_product_images.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(repo_uri(path, repo_root).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def iter_sql_statements(sql: str) -> Iterator[str]:
    buffer = ""
    for character in sql:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete SQL statement")


def apply_sql_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
    name: str,
    sql: str,
    checksum: str,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    existing = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    if existing:
        if existing[0] != checksum:
            raise RuntimeError(f"migration checksum mismatch: {version}")
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in iter_sql_statements(sql):
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (version, name, checksum, utc_now()),
        )
        connection.execute(f"PRAGMA user_version = {int(version)}")
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def apply_migrations(connection: sqlite3.Connection, migrations_dir: Path) -> None:
    migrations = sorted(migrations_dir.glob("*.sql"))
    if not migrations:
        raise FileNotFoundError(f"no migrations found in {migrations_dir}")
    for path in migrations:
        prefix, _, _rest = path.name.partition("_")
        version = int(prefix)
        sql = path.read_text(encoding="utf-8")
        apply_sql_migration(
            connection,
            version=version,
            name=path.name,
            sql=sql,
            checksum=sha256_bytes(sql.encode("utf-8")),
        )


def _safe_row_value(row: Mapping[str, str | None], field: str) -> str:
    return (row.get(field) or "").strip()


def build_source_manifests(
    source_csv: Path,
    *,
    dataset_snapshot_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    set_large_csv_field_limit()
    records: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    folder_groups: dict[str, dict[str, Any]] = {}

    with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or ())
        required = set(IMAGE_FIELDS) | {
            "asset_id",
            "sku_id",
            "goods_id",
            "brand_id",
            "brand_name",
            "sku_name",
            "sku_concat_name",
            "sku_color_no",
        }
        missing = required - set(fieldnames)
        if missing:
            raise ValueError(f"source CSV missing fields: {', '.join(sorted(missing))}")

        for row_number, source_row in enumerate(reader, start=2):
            raw_row = {field: source_row.get(field) or "" for field in fieldnames}
            raw_record_json = canonical_json(raw_row)
            row_hash = sha256_bytes(raw_record_json.encode("utf-8"))

            sku_id = _safe_row_value(source_row, "sku_id")
            raw_brand = _safe_row_value(source_row, "brand_name")
            raw_product = _safe_row_value(
                source_row, "sku_concat_name"
            ) or _safe_row_value(source_row, "sku_name")
            brand_name = raw_brand or "未知品牌"
            product_name = raw_product or f"未命名商品_{sku_id or row_number}"
            brand_folder = sanitize_component(brand_name, "未知品牌", max_length=80)
            product_folder = sanitize_component(
                product_name,
                f"未命名商品_{sku_id or row_number}",
            )
            relative_folder = normalize_relative_path(
                Path(brand_folder) / product_folder
            )
            folder_group_id = stable_id(
                "fg", dataset_snapshot_id, relative_folder
            )
            source_record_id = stable_id(
                "src",
                dataset_snapshot_id,
                row_number,
                row_hash,
            )

            group = folder_groups.setdefault(
                folder_group_id,
                {
                    "folder_group_id": folder_group_id,
                    "dataset_snapshot_id": dataset_snapshot_id,
                    "brand_folder_raw": brand_folder,
                    "product_folder_raw": product_folder,
                    "relative_folder_path": relative_folder,
                    "source_record_ids": set(),
                    "image_occurrence_ids": set(),
                },
            )
            group["source_record_ids"].add(source_record_id)

            records.append(
                {
                    "source_record_id": source_record_id,
                    "dataset_snapshot_id": dataset_snapshot_id,
                    "folder_group_id": folder_group_id,
                    "row_number": row_number,
                    "row_hash": row_hash,
                    "asset_id_raw": _safe_row_value(source_row, "asset_id"),
                    "sku_id_raw": sku_id,
                    "goods_id_raw": _safe_row_value(source_row, "goods_id"),
                    "brand_id_raw": _safe_row_value(source_row, "brand_id"),
                    "brand_name_raw": raw_brand,
                    "sku_name_raw": _safe_row_value(source_row, "sku_name"),
                    "sku_concat_name_raw": _safe_row_value(
                        source_row, "sku_concat_name"
                    ),
                    "sku_color_no_raw": _safe_row_value(
                        source_row, "sku_color_no"
                    ),
                    "raw_record_json": raw_record_json,
                    "_brand_folder": brand_folder,
                }
            )

            for source_field in IMAGE_FIELDS:
                try:
                    urls = parse_image_urls(source_row.get(source_field))
                except ValueError as exc:
                    raise ValueError(
                        f"row {row_number} field {source_field}: {exc}"
                    ) from exc
                for image_index, url in enumerate(urls, start=1):
                    filename = output_filename(source_field, image_index, url)
                    expected_relative_path = normalize_relative_path(
                        Path(relative_folder) / filename
                    )
                    source_ref_id = stable_id(
                        "ref",
                        source_record_id,
                        source_field,
                        image_index,
                        url,
                    )
                    refs.append(
                        {
                            "source_ref_id": source_ref_id,
                            "source_record_id": source_record_id,
                            "source_field": source_field,
                            "image_index": image_index,
                            "source_url": url,
                            "source_url_hash": sha256_bytes(url.encode("utf-8")),
                            "declared_extension": url_extension(url),
                            "expected_relative_path": expected_relative_path,
                            "download_status": "pending",
                            "unmatched_reason": None,
                            "http_metadata_json": "{}",
                        }
                    )
    return records, refs, folder_groups


def build_brand_alias_candidates(
    records: Sequence[dict[str, Any]],
    dataset_snapshot_id: str,
) -> tuple[list[dict[str, Any]], int]:
    by_brand_id: dict[str, dict[str, Any]] = {}
    for record in records:
        brand_id = record["brand_id_raw"]
        if not brand_id:
            continue
        group = by_brand_id.setdefault(
            brand_id,
            {"folders": set(), "names": set(), "record_ids": []},
        )
        group["folders"].add(record["_brand_folder"])
        group["names"].add(record["brand_name_raw"])
        group["record_ids"].append(record["source_record_id"])

    candidates: list[dict[str, Any]] = []
    alias_group_count = 0
    for brand_id, evidence in sorted(by_brand_id.items()):
        folders = sorted(evidence["folders"], key=str.casefold)
        if len(folders) <= 1:
            continue
        alias_group_count += 1
        alias_group_id = stable_id(
            "brand_alias_group", dataset_snapshot_id, brand_id
        )
        evidence_json = canonical_json(
            {
                "brand_names_raw": sorted(evidence["names"], key=str.casefold),
                "brand_folders": folders,
                "source_record_count": len(evidence["record_ids"]),
            }
        )
        for folder in folders:
            candidates.append(
                {
                    "alias_candidate_id": stable_id(
                        "brand_alias", alias_group_id, folder
                    ),
                    "dataset_snapshot_id": dataset_snapshot_id,
                    "alias_group_id": alias_group_id,
                    "brand_id_raw": brand_id,
                    "brand_folder_raw": folder,
                    "canonical_brand_id": brand_id,
                    "evidence_json": evidence_json,
                    "status": "candidate_unreviewed",
                }
            )
    return candidates, alias_group_count


def map_occurrences(
    observations: Sequence[FileObservation],
    refs: list[dict[str, Any]],
    folder_groups: dict[str, dict[str, Any]],
    *,
    dataset_snapshot_id: str,
    legacy_metadata: Mapping[str, LegacyMetadata],
    metadata_source_path: str,
    metadata_sha256: str,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    image_contents_by_id: dict[str, dict[str, Any]] = {}
    occurrences: list[dict[str, Any]] = []
    occurrence_by_path: dict[str, dict[str, Any]] = {}
    occurrence_by_casefold: dict[str, list[dict[str, Any]]] = defaultdict(list)
    legacy_mappings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    now = utc_now()

    for observation in observations:
        parts = Path(observation.relative_path).parts
        if len(parts) != 3:
            raise ValueError(
                f"unexpected image directory depth: {observation.relative_path}"
            )
        brand_folder, product_folder, _filename = parts
        relative_folder = normalize_relative_path(
            Path(brand_folder) / product_folder
        )
        folder_group_id = stable_id(
            "fg", dataset_snapshot_id, relative_folder
        )
        group = folder_groups.setdefault(
            folder_group_id,
            {
                "folder_group_id": folder_group_id,
                "dataset_snapshot_id": dataset_snapshot_id,
                "brand_folder_raw": brand_folder,
                "product_folder_raw": product_folder,
                "relative_folder_path": relative_folder,
                "source_record_ids": set(),
                "image_occurrence_ids": set(),
            },
        )
        image_occurrence_id = stable_id(
            "occ", dataset_snapshot_id, ROOT_ALIAS, observation.relative_path
        )
        group["image_occurrence_ids"].add(image_occurrence_id)
        legacy = legacy_metadata.get(observation.relative_path)
        legacy_image_id = legacy.legacy_image_id if legacy else None

        image_contents_by_id.setdefault(
            observation.sha256,
            {
                "image_id": observation.sha256,
                "sha256": observation.sha256,
                "byte_size": observation.byte_size,
                "detected_format": observation.detected_format,
                "mime_type": observation.mime_type,
                "first_seen_at": now,
            },
        )
        occurrence = {
            "image_occurrence_id": image_occurrence_id,
            "image_id": observation.sha256,
            "folder_group_id": folder_group_id,
            "root_alias": ROOT_ALIAS,
            "relative_path": observation.relative_path,
            "filename": observation.filename,
            "extension": observation.extension,
            "extension_mismatch": observation.extension_mismatch,
            "brand_folder_raw": brand_folder,
            "product_folder_raw": product_folder,
            "legacy_image_id": legacy_image_id,
            "source_exists": 1,
            "source_mtime_ns": observation.mtime_ns,
        }
        occurrences.append(occurrence)
        occurrence_by_path[observation.relative_path] = occurrence
        occurrence_by_casefold[observation.relative_path.casefold()].append(occurrence)

        if legacy:
            if legacy.sha256 and legacy.sha256 != observation.sha256:
                errors.append(
                    {
                        "image_id": observation.sha256,
                        "image_occurrence_id": image_occurrence_id,
                        "error_code": "legacy_sha256_mismatch",
                        "error_type": "lineage_conflict",
                        "message": "legacy metadata SHA256 differs from source bytes",
                        "details": {
                            "relative_path": observation.relative_path,
                        },
                        "retryable": 0,
                    }
                )
            if legacy.legacy_image_id:
                legacy_mappings.append(
                    {
                        "legacy_image_id": legacy.legacy_image_id,
                        "image_id": observation.sha256,
                        "image_occurrence_id": image_occurrence_id,
                        "metadata_source_path": metadata_source_path,
                        "metadata_sha256": metadata_sha256,
                    }
                )

        if observation.detected_format == "UNKNOWN":
            errors.append(
                {
                    "image_id": observation.sha256,
                    "image_occurrence_id": image_occurrence_id,
                    "error_code": "unknown_image_magic",
                    "error_type": "format_observation",
                    "message": "image extension is supported but magic is unknown",
                    "details": {"relative_path": observation.relative_path},
                    "retryable": 0,
                }
            )

    ref_occurrences: list[dict[str, Any]] = []
    matched_occurrence_ids: set[str] = set()
    for ref in refs:
        expected = ref["expected_relative_path"]
        match = occurrence_by_path.get(expected)
        method = "downloader_exact_path"
        confidence = 1.0
        if match is None:
            candidates = occurrence_by_casefold.get(expected.casefold(), [])
            if len(candidates) == 1:
                match = candidates[0]
                method = "downloader_casefold_path"
                confidence = 0.99

        if match is None:
            ref["download_status"] = "unmatched"
            ref["unmatched_reason"] = "expected_path_missing"
            errors.append(
                {
                    "image_id": None,
                    "image_occurrence_id": None,
                    "error_code": "source_ref_unmatched",
                    "error_type": "lineage_conflict",
                    "message": "source image reference has no matching occurrence",
                    "details": {
                        "source_ref_id": ref["source_ref_id"],
                        "expected_relative_path": expected,
                    },
                    "retryable": 0,
                }
            )
            continue

        ref["download_status"] = "mapped"
        ref["unmatched_reason"] = None
        matched_occurrence_ids.add(match["image_occurrence_id"])
        ref_occurrences.append(
            {
                "source_ref_id": ref["source_ref_id"],
                "image_occurrence_id": match["image_occurrence_id"],
                "match_method": method,
                "match_confidence": confidence,
            }
        )

    for occurrence in occurrences:
        if occurrence["image_occurrence_id"] not in matched_occurrence_ids:
            errors.append(
                {
                    "image_id": occurrence["image_id"],
                    "image_occurrence_id": occurrence["image_occurrence_id"],
                    "error_code": "occurrence_without_source_ref",
                    "error_type": "lineage_conflict",
                    "message": "physical occurrence has no source image reference",
                    "details": {"relative_path": occurrence["relative_path"]},
                    "retryable": 0,
                }
            )

    return (
        sorted(image_contents_by_id.values(), key=lambda row: row["image_id"]),
        sorted(occurrences, key=lambda row: row["relative_path"].casefold()),
        sorted(
            ref_occurrences,
            key=lambda row: (row["source_ref_id"], row["image_occurrence_id"]),
        ),
        sorted(
            legacy_mappings,
            key=lambda row: (row["legacy_image_id"], row["image_occurrence_id"]),
        ),
        errors,
    )


def finalize_folder_groups(
    groups: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups.values():
        source_count = len(group["source_record_ids"])
        occurrence_count = len(group["image_occurrence_ids"])
        if source_count > 1:
            status = "multi_source_record"
        elif source_count == 1:
            status = "single_source_record"
        else:
            status = "orphan_physical_folder"
        rows.append(
            {
                "folder_group_id": group["folder_group_id"],
                "dataset_snapshot_id": group["dataset_snapshot_id"],
                "brand_folder_raw": group["brand_folder_raw"],
                "product_folder_raw": group["product_folder_raw"],
                "relative_folder_path": group["relative_folder_path"],
                "source_record_count": source_count,
                "image_occurrence_count": occurrence_count,
                "collision_status": status,
            }
        )
    return sorted(rows, key=lambda row: row["relative_folder_path"].casefold())


def insert_rows(
    connection: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    if not rows:
        return
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    connection.executemany(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
        [tuple(row[column] for column in columns) for row in rows],
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with path.open("wb") as handle:
        for row in rows:
            line = (
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            handle.write(line)
            digest.update(line)
            count += 1
    return count, digest.hexdigest()


def sqlite_rows(
    connection: sqlite3.Connection,
    table: str,
) -> Iterator[dict[str, Any]]:
    cursor = connection.execute(f"SELECT * FROM {table} ORDER BY 1")
    columns = [description[0] for description in cursor.description]
    for row in cursor:
        yield dict(zip(columns, row, strict=True))


def export_sqlite_jsonl(
    connection: sqlite3.Connection,
    output_dir: Path,
) -> dict[str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=False)
    catalog: dict[str, dict[str, Any]] = {}
    for table in TABLE_EXPORT_ORDER:
        path = output_dir / f"{table}.jsonl"
        count, digest = write_jsonl(path, sqlite_rows(connection, table))
        expected = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count != expected:
            raise RuntimeError(f"JSONL count mismatch for {table}: {count} != {expected}")
        catalog[table] = {
            "relative_path": f"jsonl/{path.name}",
            "row_count": count,
            "sha256": digest,
        }
    return catalog


def deterministic_sample(
    observations: Sequence[FileObservation],
    count: int,
) -> list[FileObservation]:
    if not observations or count <= 0:
        return []
    if len(observations) <= count:
        return list(observations)
    if count == 1:
        return [observations[0]]
    indices = {
        round(index * (len(observations) - 1) / (count - 1))
        for index in range(count)
    }
    return [observations[index] for index in sorted(indices)]


def verify_sources_unchanged(
    *,
    source_csv: Path,
    source_csv_sha256: str,
    raw_root: Path,
    observations: Sequence[FileObservation],
    sample_count: int = 100,
) -> dict[str, Any]:
    if sha256_file(source_csv) != source_csv_sha256:
        raise RuntimeError("source CSV changed during manifest build")

    current_paths, _ignored = discover_source_files(raw_root)
    current_relative = {
        normalize_relative_path(path.relative_to(raw_root)): path
        for path in current_paths
    }
    expected_relative = {item.relative_path for item in observations}
    if set(current_relative) != expected_relative:
        raise RuntimeError("source image file set changed during manifest build")

    for observation in observations:
        stat = current_relative[observation.relative_path].stat()
        if (stat.st_size, stat.st_mtime_ns) != (
            observation.byte_size,
            observation.mtime_ns,
        ):
            raise RuntimeError(
                f"source file stat changed: {observation.relative_path}"
            )

    sample = deterministic_sample(observations, sample_count)
    for observation in sample:
        if sha256_file(current_relative[observation.relative_path]) != observation.sha256:
            raise RuntimeError(
                f"source file hash changed: {observation.relative_path}"
            )
    return {
        "source_csv_sha256_recheck": "passed",
        "source_file_set_recheck": "passed",
        "all_file_stat_recheck": "passed",
        "sample_sha256_recheck": "passed",
        "sample_count": len(sample),
    }


def validate_database(
    connection: sqlite3.Connection,
    *,
    expected_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise RuntimeError(f"SQLite foreign key violations: {len(foreign_key_errors)}")

    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLE_EXPORT_ORDER
    }
    unmatched_refs = connection.execute(
        "SELECT COUNT(*) FROM source_image_refs WHERE download_status != 'mapped'"
    ).fetchone()[0]
    orphan_occurrences = connection.execute(
        """
        SELECT COUNT(*)
        FROM image_occurrences AS occurrence
        LEFT JOIN source_ref_occurrences AS link
          ON link.image_occurrence_id = occurrence.image_occurrence_id
        WHERE link.image_occurrence_id IS NULL
        """
    ).fetchone()[0]
    collision_groups = connection.execute(
        """
        SELECT COUNT(*) FROM folder_groups
        WHERE collision_status = 'multi_source_record'
        """
    ).fetchone()[0]
    alias_groups = connection.execute(
        "SELECT COUNT(DISTINCT alias_group_id) FROM brand_alias_candidates"
    ).fetchone()[0]
    legacy_sha_mismatches = connection.execute(
        """
        SELECT COUNT(*) FROM pipeline_errors
        WHERE error_code = 'legacy_sha256_mismatch'
        """
    ).fetchone()[0]

    if expected_counts:
        actual_values = {
            "dataset_snapshots": counts["dataset_snapshots"],
            "source_records": counts["source_records"],
            "source_image_refs": counts["source_image_refs"],
            "image_occurrences": counts["image_occurrences"],
            "image_contents": counts["image_contents"],
            "folder_collision_groups": collision_groups,
            "brand_alias_groups": alias_groups,
        }
        mismatches = {
            key: {"expected": expected, "actual": actual_values[key]}
            for key, expected in expected_counts.items()
            if actual_values.get(key) != expected
        }
        if mismatches:
            raise RuntimeError(
                f"audit acceptance count mismatch: {canonical_json(mismatches)}"
            )

    return {
        "table_counts": counts,
        "unmatched_source_refs": unmatched_refs,
        "orphan_image_occurrences": orphan_occurrences,
        "folder_collision_groups": collision_groups,
        "brand_alias_groups": alias_groups,
        "legacy_sha256_mismatches": legacy_sha_mismatches,
        "foreign_key_check": "passed",
    }


def read_security_gate(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    accepted = {"passed", "passed_with_owner_override"}
    status = report.get("stage_0_5_gate_status")
    if status not in accepted:
        raise RuntimeError(f"stage 0.5 security gate not passed: {status or 'missing'}")
    if report.get("tracked_tree_scan_status") != "passed":
        raise RuntimeError("stage 0.5 tracked tree scan is not passed")
    if report.get("reachable_history_scan_status") != "passed":
        raise RuntimeError("stage 0.5 reachable history scan is not passed")
    return report


def build_manifest(
    *,
    repo_root: Path,
    source_csv: Path,
    raw_root: Path,
    legacy_metadata_path: Path | None,
    security_report_path: Path,
    output_root: Path,
    workers: int,
    run_id: str | None = None,
    enforce_audit_counts: bool = False,
) -> Path:
    repo_root = repo_root.resolve()
    source_csv = source_csv.resolve()
    raw_root = raw_root.resolve()
    legacy_metadata_path = (
        legacy_metadata_path.resolve() if legacy_metadata_path else None
    )
    security_report_path = security_report_path.resolve()
    output_root = output_root.resolve()

    if not source_csv.is_file():
        raise FileNotFoundError(source_csv)
    if not raw_root.is_dir():
        raise NotADirectoryError(raw_root)
    security_report = read_security_gate(security_report_path)

    started_at = utc_now()
    run_id = run_id or (
        "stage1_"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    run_dir = output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"immutable run already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    source_csv_sha256 = sha256_file(source_csv)
    dataset_snapshot_id = f"ds_{source_csv_sha256}"
    git_commit, git_dirty = git_state(repo_root)
    fingerprint = code_fingerprint(repo_root)
    config = {
        "source_csv": repo_uri(source_csv, repo_root),
        "raw_root_alias": ROOT_ALIAS,
        "raw_root": repo_uri(raw_root, repo_root),
        "legacy_metadata": (
            repo_uri(legacy_metadata_path, repo_root)
            if legacy_metadata_path
            else None
        ),
        "security_evidence": repo_uri(security_report_path, repo_root),
        "naming_rule_version": NAMING_RULE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "workers": workers,
        "code_fingerprint": fingerprint,
    }
    config_json = canonical_json(config)
    dependency_snapshot = canonical_json(
        {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "third_party_runtime_dependencies": [],
        }
    )

    records, refs, folder_group_accumulators = build_source_manifests(
        source_csv,
        dataset_snapshot_id=dataset_snapshot_id,
    )
    alias_candidates, alias_group_count = build_brand_alias_candidates(
        records, dataset_snapshot_id
    )
    for record in records:
        record.pop("_brand_folder", None)

    image_paths, ignored_files = discover_source_files(raw_root)
    print(
        f"discovered {len(image_paths)} source images and {len(ignored_files)} non-image files",
        flush=True,
    )
    observations = observe_source_files(
        image_paths,
        raw_root,
        workers=workers,
    )
    legacy_metadata, legacy_metadata_sha256 = load_legacy_metadata(
        legacy_metadata_path
    )
    metadata_source_uri = (
        repo_uri(legacy_metadata_path, repo_root)
        if legacy_metadata_path
        else ""
    )
    (
        image_contents,
        occurrences,
        ref_occurrences,
        legacy_mappings,
        raw_errors,
    ) = map_occurrences(
        observations,
        refs,
        folder_group_accumulators,
        dataset_snapshot_id=dataset_snapshot_id,
        legacy_metadata=legacy_metadata,
        metadata_source_path=metadata_source_uri,
        metadata_sha256=legacy_metadata_sha256,
    )
    folder_groups = finalize_folder_groups(folder_group_accumulators)

    with source_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader)
    dataset_snapshot = {
        "dataset_snapshot_id": dataset_snapshot_id,
        "source_type": "csv",
        "source_path": repo_uri(source_csv, repo_root),
        "source_sha256": source_csv_sha256,
        "row_count": len(records),
        "column_schema_json": canonical_json(
            [{"name": name, "ordinal": index} for index, name in enumerate(headers)]
        ),
        "naming_rule_version": NAMING_RULE_VERSION,
        "root_aliases_json": canonical_json(
            {ROOT_ALIAS: repo_uri(raw_root, repo_root)}
        ),
        "created_at": started_at,
    }
    run_row = {
        "run_id": run_id,
        "dataset_snapshot_id": dataset_snapshot_id,
        "stage": "1",
        "pipeline_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "config_json": config_json,
        "config_hash": sha256_bytes(config_json.encode("utf-8")),
        "dependency_snapshot_json": dependency_snapshot,
        "started_at": started_at,
        "finished_at": None,
        "status": "running",
        "error_summary_json": "{}",
    }

    errors: list[dict[str, Any]] = []
    for index, error in enumerate(raw_errors, start=1):
        details_json = canonical_json(error.pop("details"))
        errors.append(
            {
                "error_id": stable_id(
                    "err",
                    run_id,
                    index,
                    error["error_code"],
                    details_json,
                ),
                "run_id": run_id,
                "image_id": error["image_id"],
                "image_occurrence_id": error["image_occurrence_id"],
                "stage": "1",
                "error_code": error["error_code"],
                "error_type": error["error_type"],
                "message": error["message"],
                "details_json": details_json,
                "retryable": error["retryable"],
                "created_at": utc_now(),
            }
        )

    sqlite_path = run_dir / "manifest.sqlite"
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    try:
        apply_migrations(connection, repo_root / "database" / "migrations")
        connection.execute("BEGIN IMMEDIATE")
        insert_rows(connection, "dataset_snapshots", [dataset_snapshot])
        insert_rows(connection, "pipeline_runs", [run_row])
        insert_rows(connection, "folder_groups", folder_groups)
        insert_rows(connection, "source_records", records)
        insert_rows(connection, "source_image_refs", refs)
        insert_rows(connection, "image_contents", image_contents)
        insert_rows(connection, "image_occurrences", occurrences)
        insert_rows(connection, "source_ref_occurrences", ref_occurrences)
        insert_rows(connection, "brand_alias_candidates", alias_candidates)
        insert_rows(connection, "legacy_id_mappings", legacy_mappings)
        insert_rows(connection, "pipeline_errors", errors)
        connection.commit()

        expected_counts = (
            {
                "dataset_snapshots": 1,
                "source_records": 2309,
                "source_image_refs": 31513,
                "image_occurrences": 31511,
                "image_contents": 12386,
                "folder_collision_groups": 16,
                "brand_alias_groups": 9,
            }
            if enforce_audit_counts
            else None
        )
        validation = validate_database(
            connection,
            expected_counts=expected_counts,
        )
        integrity = verify_sources_unchanged(
            source_csv=source_csv,
            source_csv_sha256=source_csv_sha256,
            raw_root=raw_root,
            observations=observations,
        )

        run_row["finished_at"] = utc_now()
        run_row["status"] = "completed"
        run_row["error_summary_json"] = canonical_json(
            {
                "pipeline_error_count": len(errors),
                "unmatched_source_refs": validation["unmatched_source_refs"],
                "orphan_image_occurrences": validation[
                    "orphan_image_occurrences"
                ],
            }
        )
        connection.execute(
            """
            UPDATE pipeline_runs
            SET finished_at = ?, status = ?, error_summary_json = ?
            WHERE run_id = ?
            """,
            (
                run_row["finished_at"],
                run_row["status"],
                run_row["error_summary_json"],
                run_id,
            ),
        )
        connection.commit()

        jsonl_catalog = export_sqlite_jsonl(connection, run_dir / "jsonl")
    finally:
        connection.close()

    baseline_count, baseline_sha = write_jsonl(
        run_dir / "integrity_baseline.jsonl",
        (
            {
                "image_occurrence_id": stable_id(
                    "occ",
                    dataset_snapshot_id,
                    ROOT_ALIAS,
                    observation.relative_path,
                ),
                "root_alias": ROOT_ALIAS,
                "relative_path": observation.relative_path,
                "sha256": observation.sha256,
                "byte_size": observation.byte_size,
                "mtime_ns": observation.mtime_ns,
            }
            for observation in observations
        ),
    )

    conflicts: list[dict[str, Any]] = []
    for group in folder_groups:
        if group["collision_status"] != "single_source_record":
            conflicts.append(
                {
                    "conflict_type": group["collision_status"],
                    "folder_group_id": group["folder_group_id"],
                    "relative_folder_path": group["relative_folder_path"],
                    "source_record_count": group["source_record_count"],
                    "image_occurrence_count": group["image_occurrence_count"],
                }
            )
    for candidate in alias_candidates:
        conflicts.append(
            {
                "conflict_type": "brand_alias_candidate",
                "alias_group_id": candidate["alias_group_id"],
                "brand_id_raw": candidate["brand_id_raw"],
                "brand_folder_raw": candidate["brand_folder_raw"],
            }
        )
    for ref in refs:
        if ref["download_status"] != "mapped":
            conflicts.append(
                {
                    "conflict_type": "unmatched_source_ref",
                    "source_ref_id": ref["source_ref_id"],
                    "expected_relative_path": ref["expected_relative_path"],
                    "reason": ref["unmatched_reason"],
                }
            )
    conflict_count, conflict_sha = write_jsonl(
        run_dir / "lineage_conflicts.jsonl", conflicts
    )

    security_evidence = dict(security_report)
    security_evidence["copied_at"] = utc_now()
    security_evidence_path = run_dir / "stage0_5_evidence.json"
    security_evidence_path.write_text(
        json.dumps(security_evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "completed",
        "dataset_snapshot_id": dataset_snapshot_id,
        "source_csv_sha256": source_csv_sha256,
        "git_commit": git_commit,
        "git_dirty": bool(git_dirty),
        "code_fingerprint": fingerprint,
        "naming_rule_version": NAMING_RULE_VERSION,
        "security_gate_status": security_report["stage_0_5_gate_status"],
        "validation": validation,
        "integrity": integrity,
        "brand_alias_group_count": alias_group_count,
        "ignored_non_image_files": ignored_files,
        "legacy_metadata_row_count": len(legacy_metadata),
        "legacy_metadata_sha256": legacy_metadata_sha256,
        "integrity_baseline": {
            "relative_path": "integrity_baseline.jsonl",
            "row_count": baseline_count,
            "sha256": baseline_sha,
        },
        "lineage_conflicts": {
            "relative_path": "lineage_conflicts.jsonl",
            "row_count": conflict_count,
            "sha256": conflict_sha,
        },
        "sqlite": {
            "relative_path": sqlite_path.name,
            "sha256": sha256_file(sqlite_path),
        },
        "jsonl": jsonl_catalog,
        "optional_exports": {
            "parquet": "not_generated",
            "full_csv_mirror": "not_generated",
        },
        "finished_at": run_row["finished_at"],
    }
    (run_dir / "stage1_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "table_counts": validation["table_counts"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=repo_root)
    parser.add_argument("--source-csv", type=Path)
    parser.add_argument(
        "--raw-root",
        type=Path,
        default=repo_root / "downloaded_images",
    )
    parser.add_argument(
        "--legacy-metadata",
        type=Path,
        default=repo_root
        / "image_preprocessing_output"
        / "metadata"
        / "image_preprocessing.csv",
    )
    parser.add_argument(
        "--security-report",
        type=Path,
        default=repo_root
        / "stage1_output"
        / "security"
        / "security_remediation.json",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repo_root / "stage1_output",
    )
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--run-id")
    parser.add_argument("--enforce-audit-counts", action="store_true")
    parser.add_argument(
        "--log-file",
        type=Path,
        help="write progress and tracebacks to this file (useful for background runs)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workers <= 0:
        raise SystemExit("--workers must be greater than zero")
    def execute() -> int:
        repo_root = args.repo_root.resolve()
        source_csv = args.source_csv or default_source_csv(repo_root)
        build_manifest(
            repo_root=repo_root,
            source_csv=source_csv,
            raw_root=args.raw_root,
            legacy_metadata_path=args.legacy_metadata,
            security_report_path=args.security_report,
            output_root=args.output_root,
            workers=args.workers,
            run_id=args.run_id,
            enforce_audit_counts=args.enforce_audit_counts,
        )
        return 0

    if not args.log_file:
        return execute()

    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("a", encoding="utf-8", buffering=1) as log_handle:
        with contextlib.redirect_stdout(log_handle), contextlib.redirect_stderr(
            log_handle
        ):
            try:
                return execute()
            except Exception:
                traceback.print_exc()
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
