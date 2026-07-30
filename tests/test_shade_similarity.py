from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from lipcolor_pipeline.color_extraction import srgb_to_lab
from lipcolor_pipeline.color_similarity import (
    classify_distance_band,
    delta_e_ciede2000,
    delta_e_ciede2000_array,
    delta_e_to_similarity,
    delta_hue_degrees,
    hex_to_rgb,
    normalize_hex,
)
from lipcolor_pipeline.settings import PipelineSettings, load_settings
from lipcolor_pipeline.shade_similarity import (
    _identity_for_code,
    export_shade_similarity,
    load_similarity_source_manifest,
    normalize_shade_code_tokens,
    plan_shade_similarity,
    run_shade_similarity,
)
from lipcolor_pipeline.stage1_manifest import (
    apply_migrations,
    sha256_file,
    stable_id,
)
from lipcolor_pipeline.workspace import Workspace


REPO_ROOT = Path(__file__).resolve().parents[1]


SHARMA_REFERENCE_DATA = (
    (50.0000, 2.6772, -79.7751, 50.0000, 0.0000, -82.7485, 2.0425),
    (50.0000, 3.1571, -77.2803, 50.0000, 0.0000, -82.7485, 2.8615),
    (50.0000, 2.8361, -74.0200, 50.0000, 0.0000, -82.7485, 3.4412),
    (50.0000, -1.3802, -84.2814, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, -1.1848, -84.8006, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, -0.9009, -85.5211, 50.0000, 0.0000, -82.7485, 1.0000),
    (50.0000, 0.0000, 0.0000, 50.0000, -1.0000, 2.0000, 2.3669),
    (50.0000, -1.0000, 2.0000, 50.0000, 0.0000, 0.0000, 2.3669),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0009, 7.1792),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0010, 7.1792),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0011, 7.2195),
    (50.0000, 2.4900, -0.0010, 50.0000, -2.4900, 0.0012, 7.2195),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0009, -2.4900, 4.8045),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0010, -2.4900, 4.8045),
    (50.0000, -0.0010, 2.4900, 50.0000, 0.0011, -2.4900, 4.7461),
    (50.0000, 2.5000, 0.0000, 50.0000, 0.0000, -2.5000, 4.3065),
    (50.0000, 2.5000, 0.0000, 73.0000, 25.0000, -18.0000, 27.1492),
    (50.0000, 2.5000, 0.0000, 61.0000, -5.0000, 29.0000, 22.8977),
    (50.0000, 2.5000, 0.0000, 56.0000, -27.0000, -3.0000, 31.9030),
    (50.0000, 2.5000, 0.0000, 58.0000, 24.0000, 15.0000, 19.4535),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.1736, 0.5854, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.2972, 0.0000, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 1.8634, 0.5757, 1.0000),
    (50.0000, 2.5000, 0.0000, 50.0000, 3.2592, 0.3350, 1.0000),
    (60.2574, -34.0099, 36.2677, 60.4626, -34.1751, 39.4387, 1.2644),
    (63.0109, -31.0961, -5.8663, 62.8187, -29.7946, -4.0864, 1.2630),
    (61.2901, 3.7196, -5.3901, 61.4292, 2.2480, -4.9620, 1.8731),
    (35.0831, -44.1164, 3.7933, 35.0232, -40.0716, 1.5901, 1.8645),
    (22.7233, 20.0904, -46.6940, 23.0331, 14.9730, -42.5619, 2.0373),
    (36.4612, 47.8580, 18.3852, 36.2715, 50.5065, 21.2231, 1.4146),
    (90.8027, -2.0831, 1.4410, 91.1528, -1.6435, 0.0447, 1.4441),
    (90.9257, -0.5406, -0.9208, 88.6381, -0.8985, -0.7239, 1.5381),
    (6.7747, -0.2908, -2.4247, 5.8714, -0.0985, -2.2286, 0.6377),
    (2.0776, 0.0795, -1.1350, 0.9033, -0.0636, -0.5514, 0.9082),
)


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    stage: str,
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id, dataset_snapshot_id, stage, pipeline_version,
            schema_version, git_commit, git_dirty, config_json, config_hash,
            dependency_snapshot_json, started_at, finished_at, status,
            error_summary_json
        ) VALUES (?, 'ds_similarity_test', ?, '0.4.0', 'test', 'test', 0,
                  '{}', 'test', '{}', '2026-01-01T00:00:00Z',
                  '2026-01-01T00:00:01Z', 'completed', '{}')
        """,
        (run_id, stage),
    )


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{value:02X}" for value in rgb)


def _insert_quick_image(
    connection: sqlite3.Connection,
    *,
    raw_root: Path,
    source_run_id: str,
    index: int,
    code: str,
    rgb_values: list[tuple[int, int, int]],
    qualities: list[str],
    source_code: str,
    source_sku: str,
    add_low_lip: bool = False,
) -> tuple[str, Path]:
    path = raw_root / f"source-{index}.png"
    Image.new("RGB", (20, 20), rgb_values[0]).save(path)
    image_id = sha256_file(path)
    folder_id = f"folder-{index}"
    occurrence_id = f"occurrence-{index}"
    source_record_id = f"source-record-{index}"
    source_ref_id = f"source-ref-{index}"
    connection.execute(
        """
        INSERT INTO folder_groups VALUES(
            ?, 'ds_similarity_test', 'Brand', 'Product',
            ?, 1, 1, 'none'
        )
        """,
        (folder_id, f"Brand/Product-{index}"),
    )
    connection.execute(
        """
        INSERT INTO image_contents VALUES(
            ?, ?, ?, 'PNG', 'image/png', '2026-01-01T00:00:00Z'
        )
        """,
        (image_id, image_id, path.stat().st_size),
    )
    connection.execute(
        """
        INSERT INTO image_occurrences VALUES(
            ?, ?, ?, 'raw_images', ?, ?, '.png', 0,
            'Brand', 'Product', NULL, 1, 1
        )
        """,
        (
            occurrence_id,
            image_id,
            folder_id,
            path.name,
            path.name,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_records(
            source_record_id, dataset_snapshot_id, folder_group_id,
            row_number, row_hash, asset_id_raw, sku_id_raw, goods_id_raw,
            brand_id_raw, brand_name_raw, sku_name_raw,
            sku_concat_name_raw, sku_color_no_raw, raw_record_json
        ) VALUES (?, 'ds_similarity_test', ?, ?, ?, '', ?, ?, 'brand-1',
                  'Brand', 'Product', 'Product', ?, '{}')
        """,
        (
            source_record_id,
            folder_id,
            index + 2,
            f"row-hash-{index}",
            source_sku,
            f"goods-{index}",
            source_code,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_image_refs(
            source_ref_id, source_record_id, source_field, image_index,
            source_url, source_url_hash, declared_extension,
            expected_relative_path, download_status,
            unmatched_reason, http_metadata_json
        ) VALUES (?, ?, 'pic_list', 1, ?, ?, 'png', ?, 'matched', NULL, '{}')
        """,
        (
            source_ref_id,
            source_record_id,
            f"https://example.invalid/{index}.png",
            f"url-hash-{index}",
            path.name,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_ref_occurrences VALUES(?, ?, 'exact', 1.0)
        """,
        (source_ref_id, occurrence_id),
    )
    quick_id = f"quick-image-{index}"
    connection.execute(
        """
        INSERT INTO quick_image_extractions(
            quick_image_extraction_id, run_id, image_id, output_semantics,
            status, primary_role, secondary_roles_json, role_confidence,
            layout_type, layout_summary, representative_color_eligible,
            eligibility_confidence, eligibility_reasons_json, summary,
            quality_risks_json, aggregation_method,
            successful_unit_ids_json, failed_unit_ids_json,
            skipped_unit_ids_json, evidence_json, schema_version,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'image_observed_color_candidate', 'success',
                  'single_swatch', '[]', 0.95, 'single_panel', 'test', 1,
                  0.95, '[]', 'test', '[]', 'ordinary_image',
                  '[]', '[]', '[]', '{}', 'quick-image-extraction-1.0',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (quick_id, source_run_id, image_id),
    )
    text_ids: list[tuple[str, str]] = []
    for color_index in range(len(rgb_values)):
        raw_code = code if color_index == 0 else f"# {code}"
        model_text_id = f"text-model-{index}-{color_index}"
        quick_text_id = f"quick-text-{index}-{color_index}"
        text_ids.append((model_text_id, quick_text_id))
        connection.execute(
            """
            INSERT INTO quick_text_items(
                quick_text_item_id, quick_image_extraction_id, run_id,
                image_id, text_item_id, raw_text, normalized_text,
                text_type, bbox_image_json, confidence,
                source_observations_json, deduplication_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'shade_code', '[0,0,5,5]',
                      0.99, '[]', '{}', '2026-01-01T00:00:00Z')
            """,
            (
                quick_text_id,
                quick_id,
                source_run_id,
                image_id,
                model_text_id,
                raw_code,
                raw_code.upper(),
            ),
        )
        rgb = rgb_values[color_index]
        lab = srgb_to_lab(np.asarray(rgb, dtype=np.uint8)).tolist()
        connection.execute(
            """
            INSERT INTO quick_color_regions(
                quick_color_region_id, quick_image_extraction_id, run_id,
                image_id, region_id, region_type, bbox_image_json,
                model_confidence, shade_code_text, shade_name_text,
                visual_color_name, linked_text_item_ids_json,
                association_confidence, extraction_eligible,
                extraction_status, output_semantics, color_hex, rgb_json,
                lab_json, valid_pixel_count, valid_pixel_ratio,
                cluster_proportion, dispersion, color_confidence,
                risks_json, source_observations_json, deduplication_json,
                algorithm_diagnostics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'swatch', '[0,0,20,20]', 0.98,
                      ?, NULL, NULL, ?, 0.98, 1, 'succeeded',
                      'image_observed_color_candidate', ?, ?, ?, 400, 1.0,
                      0.8, 4.0, ?, '[]', '[]', '{}', '{}',
                      '2026-01-01T00:00:00Z')
            """,
            (
                f"quick-region-{index}-{color_index}",
                quick_id,
                source_run_id,
                image_id,
                f"region-{index}-{color_index}",
                code,
                json.dumps([model_text_id]),
                _hex(rgb),
                json.dumps(list(rgb)),
                json.dumps(lab),
                qualities[color_index],
            ),
        )
    if add_low_lip:
        rgb = (80, 20, 40)
        lab = srgb_to_lab(np.asarray(rgb, dtype=np.uint8)).tolist()
        connection.execute(
            """
            INSERT INTO quick_color_regions(
                quick_color_region_id, quick_image_extraction_id, run_id,
                image_id, region_id, region_type, bbox_image_json,
                model_confidence, shade_code_text, shade_name_text,
                visual_color_name, linked_text_item_ids_json,
                association_confidence, extraction_eligible,
                extraction_status, output_semantics, color_hex, rgb_json,
                lab_json, valid_pixel_count, valid_pixel_ratio,
                cluster_proportion, dispersion, color_confidence,
                risks_json, source_observations_json, deduplication_json,
                algorithm_diagnostics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'lip', '[0,0,20,20]', 0.8,
                      ?, NULL, NULL, ?, 0.8, 1, 'succeeded',
                      'image_observed_color_candidate', ?, ?, ?, 400, 1.0,
                      0.4, 18.0, 'low', '[]', '[]', '{}', '{}',
                      '2026-01-01T00:00:00Z')
            """,
            (
                f"quick-region-{index}-lip",
                quick_id,
                source_run_id,
                image_id,
                f"region-{index}-lip",
                code,
                json.dumps([text_ids[0][0]]),
                _hex(rgb),
                json.dumps(list(rgb)),
                json.dumps(lab),
            ),
        )
    return image_id, path


@pytest.fixture()
def similarity_workspace(
    tmp_path: Path,
) -> tuple[Workspace, PipelineSettings, Path, list[Path]]:
    output_root = tmp_path / "output"
    (output_root / "runs").mkdir(parents=True)
    (output_root / "assets").mkdir()
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    database_path = tmp_path / "workspace.sqlite"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(
        connection,
        REPO_ROOT / "database/migrations",
        through_version=8,
    )
    connection.execute(
        """
        INSERT INTO dataset_snapshots VALUES(
            'ds_similarity_test', 'csv', 'repo://input.csv', 'csv-sha', 3,
            '{}', 'test', ?, '2026-01-01T00:00:00Z'
        )
        """,
        (json.dumps({"raw_images": f"external://{raw_root.as_posix()}"}),),
    )
    source_run = "quick_similarity_source"
    _insert_run(connection, source_run, "stage2.6")
    sources = [
        _insert_quick_image(
            connection,
            raw_root=raw_root,
            source_run_id=source_run,
            index=1,
            code="A12",
            rgb_values=[(180, 60, 75)],
            qualities=["medium"],
            source_code="A12 Rose",
            source_sku="sku-a12",
            add_low_lip=True,
        ),
        _insert_quick_image(
            connection,
            raw_root=raw_root,
            source_run_id=source_run,
            index=2,
            code="B20",
            rgb_values=[(190, 75, 70)],
            qualities=["high"],
            source_code="B20",
            source_sku="sku-b20",
        ),
        _insert_quick_image(
            connection,
            raw_root=raw_root,
            source_run_id=source_run,
            index=3,
            code="C30",
            rgb_values=[(125, 50, 90), (130, 52, 92)],
            qualities=["medium", "high"],
            source_code="Z99",
            source_sku="sku-z99",
        ),
    ]
    connection.commit()
    connection.close()
    manifest = tmp_path / "sources.jsonl"
    lines = []
    for sequence, (image_id, _path) in enumerate(sources, start=1):
        lines.append(
            json.dumps(
                {
                    "schema_version": "observed-similarity-source-1.0",
                    "manifest_version": "1.0.0",
                    "sequence": sequence,
                    "image_id": image_id,
                    "sha256": image_id,
                    "source_quick_run_id": source_run,
                    "selection_reason": "test_canonical_success",
                },
                separators=(",", ":"),
            )
        )
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    workspace = Workspace(
        repo_root=REPO_ROOT,
        output_root=output_root,
        database_path=database_path,
        dataset_snapshot_id="ds_similarity_test",
        stage1_database_path=database_path,
    )
    loaded = load_settings(REPO_ROOT, load_dotenv=False)
    settings = PipelineSettings(
        repo_root=loaded.repo_root,
        config_path=loaded.config_path,
        values=copy.deepcopy(loaded.values),
        config_hash=loaded.config_hash,
    )
    return workspace, settings, manifest, [path for _, path in sources]


def test_migration_008_adds_similarity_tables() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(
        connection,
        REPO_ROOT / "database/migrations",
        through_version=8,
    )
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "shade_similarity_inputs",
        "shade_color_observations",
        "shade_color_profiles",
        "shade_similarity_pairs",
        "shade_similarity_topk",
    } <= tables
    assert connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == 8


def test_ciede2000_matches_all_sharma_reference_pairs() -> None:
    first = np.asarray([row[:3] for row in SHARMA_REFERENCE_DATA])
    second = np.asarray([row[3:6] for row in SHARMA_REFERENCE_DATA])
    expected = np.asarray([row[6] for row in SHARMA_REFERENCE_DATA])
    actual = delta_e_ciede2000_array(first, second)
    reverse = delta_e_ciede2000_array(second, first)
    assert np.allclose(actual, expected, atol=1.0e-4)
    assert np.all(actual >= 0.0)
    assert np.allclose(actual, reverse, atol=1.0e-12)
    assert all(
        abs(delta_e_ciede2000(row[:3], row[3:6]) - row[6]) < 1.0e-4
        for row in SHARMA_REFERENCE_DATA
    )


def test_colour_and_shade_normalization_properties() -> None:
    assert {
        normalize_hex("#a45e5b"),
        normalize_hex("A45E5B"),
        normalize_hex("  #A45E5B "),
    } == {"#A45E5B"}
    assert hex_to_rgb("#A45E5B") == (164, 94, 91)
    for invalid in ("", "#FFF", "#GG0000", "#11223344"):
        with pytest.raises(ValueError):
            normalize_hex(invalid)
    assert normalize_shade_code_tokens("# V01") == ("V01",)
    assert normalize_shade_code_tokens("#602 CHILI") == ("602",)
    assert normalize_shade_code_tokens("模特所用色号为#400") == ("400",)
    assert normalize_shade_code_tokens("White Peach") == ()
    assert normalize_shade_code_tokens("A12 / B20") == ("A12", "B20")
    lab = (50.0, 20.0, -10.0)
    assert delta_e_ciede2000(lab, lab) == pytest.approx(0.0, abs=1.0e-12)
    assert delta_hue_degrees(359.0, 1.0) == 2.0
    assert 0.0 < delta_e_to_similarity(200.0) < 100.0
    assert delta_e_to_similarity(2.0) > delta_e_to_similarity(5.0)
    assert classify_distance_band(2.0) == "de00_le_2"
    assert classify_distance_band(20.1) == "de00_gt_20"


def test_business_and_image_local_identity_rules_are_stable() -> None:
    records = [
        {
            "source_record_id": "record-a",
            "sku_id_raw": "sku-a",
            "sku_color_no_raw": "#602 CHILI",
            "brand_id_raw": "brand-a",
            "brand_name_raw": "Brand A",
            "sku_name_raw": "Product A",
        },
        {
            "source_record_id": "record-b",
            "sku_id_raw": "sku-b",
            "sku_color_no_raw": "602",
            "brand_id_raw": "brand-b",
            "brand_name_raw": "Brand B",
            "sku_name_raw": "Product B",
        },
    ]
    unique = _identity_for_code(
        dataset_snapshot_id="dataset",
        image_id="image-a",
        normalized_code="602",
        source_records=records[:1],
    )
    assert unique["identity_status"] == "business_resolved"
    assert unique["source_sku_id_raw"] == "sku-a"
    ambiguous = _identity_for_code(
        dataset_snapshot_id="dataset",
        image_id="image-a",
        normalized_code="602",
        source_records=records,
    )
    assert ambiguous["identity_status"] == "image_local_ambiguous"
    assert ambiguous["candidate_sku_ids"] == ["sku-a", "sku-b"]
    unmatched = _identity_for_code(
        dataset_snapshot_id="dataset",
        image_id="image-a",
        normalized_code="707",
        source_records=records,
    )
    assert unmatched["identity_status"] == "image_local_unmatched"
    assert unmatched["shade_id"] == _identity_for_code(
        dataset_snapshot_id="dataset",
        image_id="image-a",
        normalized_code="707",
        source_records=[],
    )["shade_id"]
    other_image = _identity_for_code(
        dataset_snapshot_id="dataset",
        image_id="image-b",
        normalized_code="707",
        source_records=[],
    )
    assert other_image["shade_id"] != unmatched["shade_id"]


def test_versioned_real_source_manifest_has_canonical_39_images() -> None:
    manifest = (
        REPO_ROOT
        / "configs"
        / "samples"
        / "observed_similarity_mvp_sources_v1.jsonl"
    )
    items = load_similarity_source_manifest(manifest)
    assert len(items) == 39
    assert len({item.image_id for item in items}) == 39
    run_counts: dict[str, int] = {}
    for item in items:
        run_counts[item.source_quick_run_id] = (
            run_counts.get(item.source_quick_run_id, 0) + 1
        )
    assert run_counts == {
        "stage2_6_e3_20260729": 26,
        "stage2_6_e3_recovery_20260729": 2,
        "stage2_6_e4_20260729": 11,
    }


def test_similarity_plan_run_resume_and_export(
    similarity_workspace: tuple[
        Workspace,
        PipelineSettings,
        Path,
        list[Path],
    ],
    tmp_path: Path,
) -> None:
    workspace, settings, manifest, source_paths = similarity_workspace
    settings.values["shade_similarity"]["pair_block_size"] = 1
    loaded_manifest = load_similarity_source_manifest(manifest)
    assert len(loaded_manifest) == 3
    database_hash_before = sha256_file(workspace.database_path)
    source_hashes_before = [sha256_file(path) for path in source_paths]
    planned = plan_shade_similarity(
        workspace,
        settings,
        run_id="similarity_test_run",
        source_manifest=manifest,
    )
    assert planned["status"] == "planned_no_writes"
    assert planned["counts"] == {
        "selected_images": 3,
        "source_regions": 5,
        "successful_colors": 5,
        "formal_observations": 4,
        "profiles": 3,
        "business_resolved_profiles": 2,
        "image_local_profiles": 1,
        "pairs": 3,
        "topk_rows": 6,
        "model_api_calls": 0,
    }
    assert sha256_file(workspace.database_path) == database_hash_before

    result = run_shade_similarity(
        workspace,
        settings,
        run_id="similarity_test_run",
        source_manifest=manifest,
        resume=False,
    )
    assert result["status"] == "completed"
    assert result["counts"] == planned["counts"]
    assert [sha256_file(path) for path in source_paths] == source_hashes_before
    with sqlite3.connect(workspace.database_path) as connection:
        connection.row_factory = sqlite3.Row
        local = connection.execute(
            """
            SELECT * FROM shade_color_profiles
            WHERE run_id='similarity_test_run'
              AND identity_status='image_local_unmatched'
            """
        ).fetchone()
        assert local["normalized_shade_code"] == "C30"
        assert local["accepted_observation_count"] == 2
        assert local["profile_status"] == "multi_observation_provisional"
        assert local["representative_hex"] == "#82345C"
        excluded = connection.execute(
            """
            SELECT exclusion_reasons_json
            FROM shade_color_observations
            WHERE run_id='similarity_test_run' AND region_type='lip'
            """
        ).fetchone()[0]
        assert {
            "region_type_not_formal",
            "color_confidence_not_accepted",
        } <= set(json.loads(excluded))
        assert connection.execute(
            """
            SELECT COUNT(*) FROM shade_similarity_topk
            WHERE run_id='similarity_test_run'
            """
        ).fetchone()[0] == 6

    resumed = run_shade_similarity(
        workspace,
        settings,
        run_id="similarity_test_run",
        source_manifest=manifest,
        resume=True,
    )
    assert resumed["resume"] == "completed_run_reused"
    assert resumed["counts"] == planned["counts"]
    exported = export_shade_similarity(
        workspace,
        run_id="similarity_test_run",
        output_dir=tmp_path / "exports",
    )
    assert exported["counts"] == {
        "observations": 5,
        "profiles": 3,
        "pairs": 3,
        "topk_rows": 6,
    }
    assert all(Path(path).is_file() for path in exported["files"].values())
    assert len(exported["file_sha256"]) == 5
