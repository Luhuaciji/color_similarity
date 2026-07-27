"""Independent validator for a completed Stage 1 SQLite/JSONL run."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .stage1_manifest import deterministic_sample, sha256_file


EXPECTED_AUDIT_COUNTS = {
    "dataset_snapshots": 1,
    "source_records": 2309,
    "source_image_refs": 31513,
    "image_contents": 12386,
    "image_occurrences": 31511,
    "source_ref_occurrences": 31513,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def jsonl_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path.name}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row is not an object: {path.name}:{line_number}")
            yield value


def table_primary_key_columns(
    connection: sqlite3.Connection, table: str
) -> list[str]:
    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        row[1]
        for row in sorted((row for row in columns if row[5]), key=lambda row: row[5])
    ]


def primary_key(row: Mapping[str, Any], columns: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


def validate_jsonl_mirrors(
    connection: sqlite3.Connection,
    run_dir: Path,
    catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for table, metadata in catalog.items():
        path = run_dir / str(metadata["relative_path"])
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = sha256_file(path)
        if digest != metadata["sha256"]:
            raise RuntimeError(f"JSONL SHA256 mismatch: {table}")

        pk_columns = table_primary_key_columns(connection, table)
        database_rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        database_count = len(database_rows)
        if database_count != metadata["row_count"]:
            raise RuntimeError(f"catalog/SQLite row count mismatch: {table}")

        json_rows = list(jsonl_rows(path))
        if len(json_rows) != database_count:
            raise RuntimeError(f"JSONL/SQLite row count mismatch: {table}")

        if pk_columns:
            database_columns = [
                description[0]
                for description in connection.execute(
                    f"SELECT * FROM {table} LIMIT 0"
                ).description
            ]
            database_keys = {
                primary_key(
                    dict(zip(database_columns, row, strict=True)),
                    pk_columns,
                )
                for row in database_rows
            }
            json_keys = {primary_key(row, pk_columns) for row in json_rows}
            if database_keys != json_keys:
                raise RuntimeError(f"JSONL/SQLite primary key mismatch: {table}")

        results[table] = {
            "row_count": database_count,
            "sha256": "passed",
            "primary_keys": "passed" if pk_columns else "not_applicable",
        }
    return results


def validate_lineage(connection: sqlite3.Connection) -> dict[str, Any]:
    queries = {
        "unmatched_source_refs": """
            SELECT COUNT(*) FROM source_image_refs
            WHERE download_status != 'mapped' OR unmatched_reason IS NOT NULL
        """,
        "source_refs_without_link": """
            SELECT COUNT(*)
            FROM source_image_refs AS ref
            LEFT JOIN source_ref_occurrences AS link
              ON link.source_ref_id = ref.source_ref_id
            WHERE link.source_ref_id IS NULL
        """,
        "occurrences_without_source_ref": """
            SELECT COUNT(*)
            FROM image_occurrences AS occurrence
            LEFT JOIN source_ref_occurrences AS link
              ON link.image_occurrence_id = occurrence.image_occurrence_id
            WHERE link.image_occurrence_id IS NULL
        """,
        "contents_without_source_context": """
            SELECT COUNT(*)
            FROM image_contents AS content
            LEFT JOIN image_occurrences AS occurrence
              ON occurrence.image_id = content.image_id
            LEFT JOIN source_ref_occurrences AS link
              ON link.image_occurrence_id = occurrence.image_occurrence_id
            WHERE link.image_occurrence_id IS NULL
        """,
        "content_id_sha_mismatch": """
            SELECT COUNT(*) FROM image_contents WHERE image_id != sha256
        """,
        "legacy_sha_mismatches": """
            SELECT COUNT(*) FROM pipeline_errors
            WHERE error_code = 'legacy_sha256_mismatch'
        """,
    }
    results = {
        name: connection.execute(sql).fetchone()[0] for name, sql in queries.items()
    }
    if any(results.values()):
        raise RuntimeError(f"lineage validation failed: {json.dumps(results)}")
    results["folder_collision_groups"] = connection.execute(
        """
        SELECT COUNT(*) FROM folder_groups
        WHERE collision_status = 'multi_source_record'
        """
    ).fetchone()[0]
    results["brand_alias_groups"] = connection.execute(
        "SELECT COUNT(DISTINCT alias_group_id) FROM brand_alias_candidates"
    ).fetchone()[0]
    return results


def validate_integrity_baseline(
    run_dir: Path,
    summary: Mapping[str, Any],
    raw_root: Path | None,
    *,
    sample_count: int,
) -> dict[str, Any]:
    metadata = summary["integrity_baseline"]
    path = run_dir / metadata["relative_path"]
    if sha256_file(path) != metadata["sha256"]:
        raise RuntimeError("integrity baseline SHA256 mismatch")
    rows = list(jsonl_rows(path))
    if len(rows) != metadata["row_count"]:
        raise RuntimeError("integrity baseline row count mismatch")

    result: dict[str, Any] = {
        "row_count": len(rows),
        "artifact_sha256": "passed",
    }
    if raw_root is None:
        result["raw_recheck"] = "not_requested"
        return result

    raw_root = raw_root.resolve()
    expected_paths = {row["relative_path"] for row in rows}
    actual_paths = {
        path.relative_to(raw_root).as_posix()
        for path in raw_root.rglob("*")
        if path.is_file() and path.suffix.lower()
        in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".avif", ".heic", ".heif"}
    }
    if actual_paths != expected_paths:
        raise RuntimeError("raw file set differs from integrity baseline")

    for row in rows:
        stat = (raw_root / Path(row["relative_path"])).stat()
        if (stat.st_size, stat.st_mtime_ns) != (row["byte_size"], row["mtime_ns"]):
            raise RuntimeError(f"raw file stat mismatch: {row['relative_path']}")

    sampled_rows = deterministic_sample(rows, sample_count)
    for row in sampled_rows:
        if sha256_file(raw_root / Path(row["relative_path"])) != row["sha256"]:
            raise RuntimeError(f"raw file SHA256 mismatch: {row['relative_path']}")
    result.update(
        {
            "raw_recheck": "passed",
            "all_file_stats": "passed",
            "sample_sha256": "passed",
            "sample_count": len(sampled_rows),
        }
    )
    return result


def validate_run(
    run_dir: Path,
    *,
    source_csv: Path | None,
    raw_root: Path | None,
    enforce_audit_counts: bool,
    sample_count: int,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    summary_path = run_dir / "stage1_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("status") != "completed":
        raise RuntimeError("Stage 1 run is not completed")

    sqlite_path = run_dir / summary["sqlite"]["relative_path"]
    if sha256_file(sqlite_path) != summary["sqlite"]["sha256"]:
        raise RuntimeError("SQLite SHA256 mismatch")

    connection = sqlite3.connect(f"file:{sqlite_path.as_posix()}?mode=ro", uri=True)
    try:
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity_check != "ok":
            raise RuntimeError(f"SQLite integrity_check failed: {integrity_check}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"SQLite foreign_key_check failed: {len(foreign_key_errors)}"
            )

        table_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in summary["jsonl"]
        }
        if enforce_audit_counts:
            mismatches = {
                table: {
                    "expected": expected,
                    "actual": table_counts.get(table),
                }
                for table, expected in EXPECTED_AUDIT_COUNTS.items()
                if table_counts.get(table) != expected
            }
            if mismatches:
                raise RuntimeError(
                    f"audit count mismatch: {json.dumps(mismatches)}"
                )
            collision_count = connection.execute(
                """
                SELECT COUNT(*) FROM folder_groups
                WHERE collision_status = 'multi_source_record'
                """
            ).fetchone()[0]
            alias_count = connection.execute(
                "SELECT COUNT(DISTINCT alias_group_id) FROM brand_alias_candidates"
            ).fetchone()[0]
            if collision_count != 16 or alias_count != 9:
                raise RuntimeError(
                    "audit collision/alias count mismatch: "
                    f"collisions={collision_count}, aliases={alias_count}"
                )

        jsonl_results = validate_jsonl_mirrors(
            connection, run_dir, summary["jsonl"]
        )
        lineage = validate_lineage(connection)
    finally:
        connection.close()

    if source_csv is not None:
        if sha256_file(source_csv) != summary["source_csv_sha256"]:
            raise RuntimeError("source CSV differs from dataset snapshot")
        source_csv_check = "passed"
    else:
        source_csv_check = "not_requested"

    baseline = validate_integrity_baseline(
        run_dir,
        summary,
        raw_root,
        sample_count=sample_count,
    )
    report = {
        "schema_version": "stage1-validation-1",
        "validated_at": utc_now(),
        "run_id": summary["run_id"],
        "status": "passed",
        "sqlite_sha256": "passed",
        "sqlite_integrity_check": "passed",
        "sqlite_foreign_key_check": "passed",
        "source_csv_sha256": source_csv_check,
        "table_counts": table_counts,
        "jsonl_mirrors": jsonl_results,
        "lineage": lineage,
        "integrity_baseline": baseline,
        "audit_counts_enforced": enforce_audit_counts,
        "parquet_required": False,
        "full_csv_mirror_required": False,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--source-csv", type=Path)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--sample-count", type=int, default=100)
    parser.add_argument("--enforce-audit-counts", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sample_count < 0:
        raise SystemExit("--sample-count must not be negative")
    report = validate_run(
        args.run_dir,
        source_csv=args.source_csv,
        raw_root=args.raw_root,
        enforce_audit_counts=args.enforce_audit_counts,
        sample_count=args.sample_count,
    )
    output = args.output or args.run_dir / "stage1_validation.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "run_id": report["run_id"],
                "table_counts": report["table_counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
