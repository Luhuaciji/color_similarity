from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from download_product_images import (
    output_filename,
    parse_image_urls,
    sanitize_component,
    url_extension,
)
from lipcolor_pipeline.stage1_manifest import (
    apply_sql_migration,
    build_manifest,
    detect_image_format,
    sha256_bytes,
    stable_id,
)
from lipcolor_pipeline.stage1_validate import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]


class Stage1UnitTests(unittest.TestCase):
    def test_existing_downloader_parsing_and_naming_are_reused(self) -> None:
        self.assertEqual(
            ["https://example.test/a.jpg"],
            parse_image_urls('["https://example.test/a.jpg"]'),
        )
        self.assertEqual(
            ".jpg",
            url_extension("https://example.test/path/without-extension//"),
        )
        self.assertRegex(
            output_filename("pic_list", 1, "https://example.test/a.jpg"),
            r"^pic_list_001_[0-9a-f]{12}\.jpg$",
        )

    def test_unicode_nbsp_and_windows_names(self) -> None:
        self.assertEqual(
            "Brand Name",
            sanitize_component("Brand\u00a0Name", "fallback"),
        )
        self.assertEqual("_CON", sanitize_component("CON", "fallback"))

    def test_stable_ids_are_deterministic_and_snapshot_scoped(self) -> None:
        first = stable_id("src", "snapshot-a", 2, "row-hash")
        second = stable_id("src", "snapshot-a", 2, "row-hash")
        changed = stable_id("src", "snapshot-b", 2, "row-hash")
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_magic_detection_is_independent_of_extension(self) -> None:
        self.assertEqual("JPEG", detect_image_format(b"\xff\xd8\xffrest"))
        self.assertEqual("PNG", detect_image_format(b"\x89PNG\r\n\x1a\nrest"))
        self.assertEqual("UNKNOWN", detect_image_format(b"not-an-image"))

    def test_failed_migration_rolls_back_statements(self) -> None:
        connection = sqlite3.connect(":memory:")
        with self.assertRaises(sqlite3.OperationalError):
            apply_sql_migration(
                connection,
                version=99,
                name="bad.sql",
                sql="CREATE TABLE partial(value TEXT); INVALID SQL;",
                checksum="bad",
            )
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'partial'"
        ).fetchone()
        self.assertIsNone(table)
        migration = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 99"
        ).fetchone()
        self.assertIsNone(migration)
        connection.close()


class Stage1IntegrationTests(unittest.TestCase):
    def _write_fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        source_csv = root / "source.csv"
        raw_root = root / "raw"
        metadata = root / "legacy.csv"
        security = root / "security.json"
        raw_root.mkdir()

        fields = [
            "asset_id",
            "sku_id",
            "goods_id",
            "brand_id",
            "brand_name",
            "sku_name",
            "sku_concat_name",
            "sku_color_no",
            "pic_list",
            "show_pic",
        ]
        rows = [
            {
                "asset_id": "a1",
                "sku_id": "duplicate-sku",
                "goods_id": "g1",
                "brand_id": "brand-1",
                "brand_name": "Brand A",
                "sku_name": "Shared",
                "sku_concat_name": "Shared",
                "sku_color_no": "01",
                "pic_list": json.dumps(
                    [
                        "https://example.test/a.jpg",
                        "https://example.test/b.png",
                    ]
                ),
                "show_pic": "[]",
            },
            {
                "asset_id": "a2",
                "sku_id": "duplicate-sku",
                "goods_id": "g2",
                "brand_id": "brand-1",
                "brand_name": "Brand A",
                "sku_name": "Shared",
                "sku_concat_name": "Shared",
                "sku_color_no": "02",
                "pic_list": json.dumps(["https://example.test/c.jpg"]),
                "show_pic": "[]",
            },
            {
                "asset_id": "a3",
                "sku_id": "sku-3",
                "goods_id": "g3",
                "brand_id": "brand-1",
                "brand_name": "Brand A International",
                "sku_name": "Other",
                "sku_concat_name": "Other",
                "sku_color_no": "03",
                "pic_list": "[]",
                "show_pic": json.dumps(["https://example.test/d.jpg"]),
            },
            {
                "asset_id": "a4",
                "sku_id": "sku-4",
                "goods_id": "g4",
                "brand_id": "brand-2",
                "brand_name": "Brand B",
                "sku_name": "Solo",
                "sku_concat_name": "Solo",
                "sku_color_no": "04",
                "pic_list": json.dumps(["https://example.test/e.jpg"]),
                "show_pic": "[]",
            },
        ]
        with source_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        content_by_url = {
            "https://example.test/a.jpg": b"\xff\xd8\xffduplicate-content",
            "https://example.test/b.png": b"\x89PNG\r\n\x1a\npng-content",
            "https://example.test/c.jpg": b"\xff\xd8\xffthird-content",
            "https://example.test/d.jpg": b"\xff\xd8\xfffourth-content",
            "https://example.test/e.jpg": b"\xff\xd8\xffduplicate-content",
        }
        created: list[tuple[str, bytes]] = []
        for row in rows:
            brand = sanitize_component(row["brand_name"], "未知品牌", max_length=80)
            product = sanitize_component(row["sku_concat_name"], "未命名商品")
            for source_field in ("pic_list", "show_pic"):
                for image_index, url in enumerate(
                    parse_image_urls(row[source_field]), start=1
                ):
                    relative = (
                        Path(brand)
                        / product
                        / output_filename(source_field, image_index, url)
                    )
                    path = raw_root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content_by_url[url])
                    created.append((relative.as_posix(), content_by_url[url]))

        with metadata.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["relative_path", "image_id", "sha256"]
            )
            writer.writeheader()
            for index, (relative, content) in enumerate(created, start=1):
                writer.writerow(
                    {
                        "relative_path": relative,
                        "image_id": f"legacy-{index}",
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )

        security.write_text(
            json.dumps(
                {
                    "stage_0_5_gate_status": "passed_with_owner_override",
                    "tracked_tree_scan_status": "passed",
                    "reachable_history_scan_status": "passed",
                }
            ),
            encoding="utf-8",
        )
        return source_csv, raw_root, metadata, security

    def test_end_to_end_sqlite_jsonl_and_immutable_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_csv, raw_root, metadata, security = self._write_fixture(root)
            before = {
                path.relative_to(raw_root).as_posix(): sha256_bytes(path.read_bytes())
                for path in raw_root.rglob("*")
                if path.is_file()
            }

            run_dir = build_manifest(
                repo_root=REPO_ROOT,
                source_csv=source_csv,
                raw_root=raw_root,
                legacy_metadata_path=metadata,
                security_report_path=security,
                output_root=root / "output",
                workers=2,
                run_id="fixture-run-1",
            )

            after = {
                path.relative_to(raw_root).as_posix(): sha256_bytes(path.read_bytes())
                for path in raw_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

            database = sqlite3.connect(run_dir / "manifest.sqlite")
            counts = {
                table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "dataset_snapshots",
                    "source_records",
                    "source_image_refs",
                    "image_occurrences",
                    "image_contents",
                    "source_ref_occurrences",
                    "legacy_id_mappings",
                )
            }
            self.assertEqual(
                {
                    "dataset_snapshots": 1,
                    "source_records": 4,
                    "source_image_refs": 5,
                    "image_occurrences": 5,
                    "image_contents": 4,
                    "source_ref_occurrences": 5,
                    "legacy_id_mappings": 5,
                },
                counts,
            )
            self.assertEqual(
                1,
                database.execute(
                    """
                    SELECT COUNT(*) FROM folder_groups
                    WHERE collision_status = 'multi_source_record'
                    """
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                database.execute(
                    "SELECT COUNT(DISTINCT alias_group_id) FROM brand_alias_candidates"
                ).fetchone()[0],
            )
            self.assertEqual([], database.execute("PRAGMA foreign_key_check").fetchall())
            first_ids = {
                row[0]
                for row in database.execute(
                    "SELECT image_occurrence_id FROM image_occurrences"
                )
            }
            database.close()

            summary = json.loads(
                (run_dir / "stage1_summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual("completed", summary["status"])
            self.assertEqual("not_generated", summary["optional_exports"]["parquet"])
            self.assertFalse((run_dir / "manifest.parquet").exists())
            with (
                run_dir / "jsonl" / "image_occurrences.jsonl"
            ).open(encoding="utf-8") as handle:
                self.assertEqual(
                    counts["image_occurrences"],
                    sum(1 for _ in handle),
                )
            validation = validate_run(
                run_dir,
                source_csv=source_csv,
                raw_root=raw_root,
                enforce_audit_counts=False,
                sample_count=5,
            )
            self.assertEqual("passed", validation["status"])
            self.assertEqual("passed", validation["integrity_baseline"]["raw_recheck"])

            second_run = build_manifest(
                repo_root=REPO_ROOT,
                source_csv=source_csv,
                raw_root=raw_root,
                legacy_metadata_path=metadata,
                security_report_path=security,
                output_root=root / "output",
                workers=2,
                run_id="fixture-run-2",
            )
            second_db = sqlite3.connect(second_run / "manifest.sqlite")
            second_ids = {
                row[0]
                for row in second_db.execute(
                    "SELECT image_occurrence_id FROM image_occurrences"
                )
            }
            second_db.close()
            self.assertEqual(first_ids, second_ids)

            with self.assertRaises(FileExistsError):
                build_manifest(
                    repo_root=REPO_ROOT,
                    source_csv=source_csv,
                    raw_root=raw_root,
                    legacy_metadata_path=metadata,
                    security_report_path=security,
                    output_root=root / "output",
                    workers=1,
                    run_id="fixture-run-2",
                )


if __name__ == "__main__":
    unittest.main()
