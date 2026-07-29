from __future__ import annotations

import copy
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from lipcolor_pipeline.color_extraction import extract_observed_color
from lipcolor_pipeline.quick_extract import (
    QuickUnit,
    _request_manifest,
    _units_from_database,
    bbox_iou,
    export_quick_extraction,
    merge_region_observations,
    merge_text_observations,
    normalize_visible_text,
    plan_quick_extraction,
    quick_cache_key,
    recover_quick_run_artifacts,
    run_quick_extraction,
)
from lipcolor_pipeline.quick_extract_schemas import (
    QuickImageExtraction,
    parse_quick_image_extraction,
)
from lipcolor_pipeline.settings import PipelineSettings, load_settings
from lipcolor_pipeline.stage1_manifest import (
    apply_migrations,
    sha256_file,
    stable_id,
)
from lipcolor_pipeline.workspace import Workspace, ensure_run_directory


REPO_ROOT = Path(__file__).resolve().parents[1]
ANCHOR = REPO_ROOT / "configs/samples/stage2_6_mvp_anchor_v1.jsonl"


def _valid_quick_payload(scope: str = "image") -> dict:
    return {
        "schema_version": "quick-image-extraction-1.0",
        "scope": scope,
        "input_context_policy": "image_only",
        "primary_role": "single_swatch",
        "secondary_roles": [],
        "role_confidence": 0.94,
        "layout_type": "single_panel",
        "layout_summary": "One visible swatch.",
        "representative_color_eligible": True,
        "eligibility_confidence": 0.91,
        "eligibility_reasons": ["visible_uniform_swatch"],
        "summary": "A swatch labelled A12.",
        "quality_risks": [],
        "text_items": (
            []
            if scope == "global_thumbnail"
            else [
                {
                    "text_item_id": "txt-1",
                    "text": " A  12 ",
                    "text_type": "shade_code",
                    "bbox_norm": [0.1, 0.1, 0.4, 0.2],
                    "confidence": 0.9,
                }
            ]
        ),
        "color_regions": (
            []
            if scope == "global_thumbnail"
            else [
                {
                    "region_id": "region-1",
                    "region_type": "swatch",
                    "bbox_norm": [0.1, 0.2, 0.9, 0.9],
                    "shade_code_text": "A12",
                    "shade_name_text": None,
                    "visual_color_name": "red",
                    "confidence": 0.93,
                    "risks": [],
                    "linked_text_item_ids": ["txt-1"],
                    "association_confidence": 0.89,
                }
            ]
        ),
    }


def test_migration_007_supports_failure_units_without_model_result() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(
        connection,
        REPO_ROOT / "database/migrations",
        through_version=7,
    )
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "quick_extraction_units",
        "quick_image_extractions",
        "quick_text_items",
        "quick_color_regions",
    } <= tables
    unit_columns = {
        row["name"]: row
        for row in connection.execute(
            "PRAGMA table_info(quick_extraction_units)"
        )
    }
    assert unit_columns["model_run_id"]["notnull"] == 0
    assert unit_columns["source_asset_id"]["notnull"] == 0
    assert connection.execute(
        "SELECT MAX(version) FROM schema_migrations"
    ).fetchone()[0] == 7


def test_quick_schema_enforces_scope_links_limits_and_extra_fields() -> None:
    payload = _valid_quick_payload()
    parsed, actions = parse_quick_image_extraction(
        f"```json\n{json.dumps(payload)}\n```",
        expected_scope="image",
        image_width=100,
        image_height=100,
    )
    assert parsed.color_regions[0].linked_text_item_ids == ["txt-1"]
    assert actions == ()

    bad_link = copy.deepcopy(payload)
    bad_link["color_regions"][0]["linked_text_item_ids"] = ["missing"]
    with pytest.raises(ValidationError):
        QuickImageExtraction.model_validate(bad_link)

    wrong_scope = _valid_quick_payload("global_thumbnail")
    wrong_scope["text_items"] = payload["text_items"]
    with pytest.raises(ValidationError):
        QuickImageExtraction.model_validate(wrong_scope)

    extra = copy.deepcopy(payload)
    extra["final_hex"] = "#AA0000"
    with pytest.raises(ValidationError):
        QuickImageExtraction.model_validate(extra)

    too_many = copy.deepcopy(payload)
    too_many["text_items"] = [
        {
            "text_item_id": f"t-{index}",
            "text": str(index),
            "text_type": "other",
            "bbox_norm": None,
            "confidence": 0.5,
        }
        for index in range(21)
    ]
    too_many["color_regions"] = []
    with pytest.raises(ValidationError):
        QuickImageExtraction.model_validate(too_many)


def test_quick_schema_only_repairs_unambiguous_bboxes() -> None:
    payload = _valid_quick_payload()
    payload["text_items"][0]["bbox_norm"] = [10, 10, 40, 20]
    payload["color_regions"][0]["bbox_norm"] = [10, 20, 90, 90]
    parsed, actions = parse_quick_image_extraction(
        json.dumps(payload),
        expected_scope="image",
        image_width=100,
        image_height=100,
    )
    assert parsed.text_items[0].bbox_norm == (0.1, 0.1, 0.4, 0.2)
    assert len(actions) == 2

    ambiguous = _valid_quick_payload()
    ambiguous["color_regions"][0]["bbox_norm"] = [10, 20, 9000, 9000]
    with pytest.raises(ValidationError):
        parse_quick_image_extraction(
            json.dumps(ambiguous),
            expected_scope="image",
            image_width=100,
            image_height=100,
        )


def test_text_normalization_and_cross_tile_deduplication() -> None:
    assert normalize_visible_text("  ａ\t 12  ") == "A 12"
    observations = [
        {
            "unit_id": "tile-0",
            "model_text_item_id": "a",
            "raw_text": " a  12 ",
            "text_type": "shade_code",
            "bbox_image": [10, 10, 50, 30],
            "confidence": 0.8,
        },
        {
            "unit_id": "tile-1",
            "model_text_item_id": "b",
            "raw_text": "Ａ 12",
            "text_type": "shade_code",
            "bbox_image": [12, 10, 52, 30],
            "confidence": 0.9,
        },
        {
            "unit_id": "tile-1",
            "model_text_item_id": "c",
            "raw_text": "A 12",
            "text_type": "shade_code",
            "bbox_image": [100, 100, 140, 120],
            "confidence": 0.9,
        },
    ]
    merged, mapping = merge_text_observations(
        observations,
        image_id="image",
        iou_threshold=0.5,
    )
    assert len(merged) == 2
    assert mapping[("tile-0", "a")] == mapping[("tile-1", "b")]
    assert mapping[("tile-1", "c")] != mapping[("tile-1", "b")]
    assert bbox_iou([10, 10, 50, 30], [12, 10, 52, 30]) > 0.5


def test_region_dedup_rejects_conflicting_shade_codes_and_keeps_sources() -> None:
    text_map = {("tile-0", "a"): "canonical-text"}
    base = {
        "scope": "tile",
        "source_asset_id": "asset",
        "region_type": "swatch",
        "bbox_norm": [0.1, 0.1, 0.5, 0.5],
        "shade_name_text": None,
        "visual_color_name": "red",
        "confidence": 0.8,
        "risks": [],
        "linked_model_text_item_ids": ["a"],
        "association_confidence": 0.7,
        "source_extraction_eligible": True,
    }
    observations = [
        {
            **base,
            "unit_id": "tile-0",
            "tile_index": 0,
            "model_region_id": "r0",
            "bbox_image": [10, 10, 50, 50],
            "shade_code_text": "A12",
        },
        {
            **base,
            "unit_id": "tile-1",
            "tile_index": 1,
            "model_region_id": "r1",
            "bbox_image": [11, 10, 51, 50],
            "shade_code_text": "A12",
            "linked_model_text_item_ids": [],
        },
        {
            **base,
            "unit_id": "tile-2",
            "tile_index": 2,
            "model_region_id": "r2",
            "bbox_image": [11, 10, 51, 50],
            "shade_code_text": "B13",
            "linked_model_text_item_ids": [],
        },
    ]
    merged = merge_region_observations(
        observations,
        image_id="image",
        iou_threshold=0.6,
        text_id_map=text_map,
    )
    assert len(merged) == 2
    assert len(merged[0]["sources"]) == 2
    assert merged[0]["linked_text_item_ids"] == ["canonical-text"]


def test_uniform_color_is_successful_and_deterministic(tmp_path: Path) -> None:
    image_path = tmp_path / "uniform.png"
    Image.new("RGB", (80, 80), (200, 30, 70)).save(image_path)
    first = extract_observed_color(
        image_path,
        bbox_image=[0, 0, 80, 80],
        region_type="swatch",
    )
    second = extract_observed_color(
        image_path,
        bbox_image=[0, 0, 80, 80],
        region_type="swatch",
    )
    assert first["status"] == "succeeded"
    assert first["hex"] == "#C81E46"
    assert first["rgb"] == [200, 30, 70]
    assert first == second


def test_alpha_background_filters_and_lip_chroma_rule(tmp_path: Path) -> None:
    rgb = np.full((100, 100, 3), 128, dtype=np.uint8)
    rgb[:, 80:, :] = [210, 25, 60]
    mixed_path = tmp_path / "mixed.png"
    Image.fromarray(rgb, mode="RGB").save(mixed_path)
    lip = extract_observed_color(
        mixed_path,
        bbox_image=[0, 0, 100, 100],
        region_type="lip",
    )
    other = extract_observed_color(
        mixed_path,
        bbox_image=[0, 0, 100, 100],
        region_type="swatch",
    )
    assert lip["rgb"] == [210, 25, 60]
    assert other["rgb"] == [128, 128, 128]

    alpha_rgb = np.full((100, 100, 3), [30, 30, 220], dtype=np.uint8)
    alpha_rgb[25:75, 25:75] = [180, 20, 80]
    alpha = np.zeros((100, 100), dtype=np.uint8)
    alpha[25:75, 25:75] = 255
    working_path = tmp_path / "alpha-working.png"
    alpha_path = tmp_path / "alpha-mask.png"
    Image.fromarray(alpha_rgb, mode="RGB").save(working_path)
    Image.fromarray(alpha, mode="L").save(alpha_path)
    result = extract_observed_color(
        working_path,
        bbox_image=[0, 0, 100, 100],
        region_type="swatch",
        alpha_path=alpha_path,
    )
    assert result["rgb"] == [180, 20, 80]
    assert 0.20 < result["valid_pixel_ratio"] < 0.30


def test_anchor_manifest_quota_order_and_challenge_coverage() -> None:
    rows = [
        json.loads(line)
        for line in ANCHOR.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 28
    assert [row["sequence"] for row in rows] == list(range(1, 29))
    assert all(row["image_id"] == row["sha256"] for row in rows)
    assert Counter(row["expected_role"] for row in rows) == {
        "single_bullet": 4,
        "single_swatch": 4,
        "lip_effect": 4,
        "multi_shade_comparison": 4,
        "color_card": 1,
        "packaging": 4,
        "text_promo": 3,
        "invalid": 4,
    }
    assert {row["expected_role"] for row in rows[:10]} == {
        "single_bullet",
        "single_swatch",
        "lip_effect",
        "multi_shade_comparison",
        "color_card",
        "packaging",
        "text_promo",
        "invalid",
    }
    assert any(row["tile_count"] == 4 for row in rows)
    assert any(row["tile_count"] == 6 for row in rows)
    color_card = [
        row for row in rows if row["expected_role"] == "color_card"
    ]
    assert color_card[0]["label_source"] == "owner_delegated_agent"
    all_tags = {
        tag for row in rows for tag in row["challenge_tags"]
    }
    assert {
        "format_mismatch",
        "gif",
        "transparent",
        "duplicate_multi_occurrence",
        "folder_collision",
        "business_invalid_slices",
    } <= all_tags


def _insert_pipeline_run(
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
        ) VALUES (?, 'ds_quick_test', ?, '0.3.0', 'test', 'test', 0,
                  '{}', 'test', '{}', '2026-01-01T00:00:00Z',
                  NULL, 'completed', '{}')
        """,
        (run_id, stage),
    )


@pytest.fixture()
def quick_workspace(
    tmp_path: Path,
) -> tuple[Workspace, PipelineSettings, str, Path]:
    output_root = (
        tmp_path.parents[2]
        / f"qe_{stable_id('output', str(tmp_path))[-16:]}"
    )
    output_root.mkdir()
    (output_root / "assets").mkdir()
    (output_root / "runs").mkdir()
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    source_path = raw_root / "source.png"
    Image.new("RGB", (100, 100), (200, 30, 70)).save(source_path)
    image_id = sha256_file(source_path)
    database_path = tmp_path / "workspace.sqlite"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(
        connection,
        REPO_ROOT / "database/migrations",
        through_version=7,
    )
    root_uri = f"external://{raw_root.as_posix()}"
    connection.execute(
        """
        INSERT INTO dataset_snapshots VALUES(
            'ds_quick_test', 'csv', 'repo://input.csv', 'csv-sha', 1,
            '{}', 'test', ?, '2026-01-01T00:00:00Z'
        )
        """,
        (json.dumps({"raw_images": root_uri}),),
    )
    _insert_pipeline_run(
        connection,
        "stage2_full_20260728",
        "stage2",
    )
    connection.execute(
        """
        INSERT INTO folder_groups VALUES(
            'folder_quick', 'ds_quick_test', 'Brand', 'Product',
            'Brand/Product', 1, 1, 'none'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO image_contents VALUES(
            ?, ?, ?, 'PNG', 'image/png', '2026-01-01T00:00:00Z'
        )
        """,
        (image_id, image_id, source_path.stat().st_size),
    )
    occurrence_id = stable_id("occurrence", image_id, "source.png")
    connection.execute(
        """
        INSERT INTO image_occurrences VALUES(
            ?, ?, 'folder_quick', 'raw_images', 'source.png', 'source.png',
            '.png', 0, 'Brand', 'Product', NULL, 1, 1
        )
        """,
        (occurrence_id, image_id),
    )
    working_path = output_root / "assets/working/test.png"
    working_path.parent.mkdir(parents=True)
    Image.open(source_path).convert("RGB").save(working_path)
    working_sha = sha256_file(working_path)
    working_id = stable_id("asset", image_id, "working")
    connection.execute(
        """
        INSERT INTO derived_assets(
            derived_asset_id, image_id, image_occurrence_id, run_id,
            asset_type, relative_path, sha256, width, height, format,
            transform_name, transform_version, transform_fingerprint,
            root_alias, created_at, metadata_json
        ) VALUES (?, ?, NULL, 'stage2_full_20260728',
                  'working_image_legacy', ?, ?, 100, 100, 'PNG',
                  'test_working', '1', 'working-fp',
                  'pipeline_output', '2026-01-01T00:00:00Z', '{}')
        """,
        (
            working_id,
            image_id,
            working_path.relative_to(output_root).as_posix(),
            working_sha,
        ),
    )
    connection.execute(
        """
        INSERT INTO image_preprocessing_observations(
            preprocess_observation_id, image_id, run_id, decode_status,
            decode_recovered, source_format, source_mode, width, height,
            frame_count, selected_frame, exif_orientation,
            orientation_corrected, icc_status, working_color_space,
            converted_to_srgb, has_alpha, transparent_pixel_ratio,
            working_asset_id, alpha_asset_id, quality_json,
            transform_fingerprint, created_at
        ) VALUES(
            'prep-quick', ?, 'stage2_full_20260728', 'ok', 0,
            'PNG', 'RGB', 100, 100, 1, 0, 1, 0, 'none', 'sRGB',
            0, 0, 0.0, ?, NULL, '{}', 'prep-fp',
            '2026-01-01T00:00:00Z'
        )
        """,
        (image_id, working_id),
    )
    connection.commit()
    connection.close()
    workspace = Workspace(
        repo_root=REPO_ROOT,
        output_root=output_root,
        database_path=database_path,
        dataset_snapshot_id="ds_quick_test",
        stage1_database_path=database_path,
    )
    loaded = load_settings(REPO_ROOT, load_dotenv=False)
    values = copy.deepcopy(loaded.values)
    values["vlm"]["concurrency"] = 1
    settings = PipelineSettings(
        repo_root=loaded.repo_root,
        config_path=loaded.config_path,
        values=values,
        config_hash=loaded.config_hash,
    )
    return workspace, settings, image_id, source_path


def test_mock_online_ordinary_cache_database_and_exports(
    quick_workspace: tuple[Workspace, PipelineSettings, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace, settings, image_id, source_path = quick_workspace
    payload = _valid_quick_payload()
    calls: list[dict] = []

    class FakeResponse:
        model = "qwen-test"
        usage = SimpleNamespace(
            model_dump=lambda mode="json": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            }
        )
        choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload))
            )
        ]

        def model_dump(self, mode: str = "json") -> dict:
            return {
                "model": self.model,
                "choices": [
                    {"message": {"content": json.dumps(payload)}}
                ],
                "usage": self.usage.model_dump(),
            }

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            calls.append(dict(kwargs))
            return FakeResponse()

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI),
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-secret")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen-test")
    source_sha_before = sha256_file(source_path)
    first = run_quick_extraction(
        workspace,
        settings,
        run_id="quick_online_1",
        execute_online=True,
        max_calls=2,
        resume=False,
        image_id=image_id,
    )
    assert first["online_calls_made"] == 1
    assert first["image_statuses"] == {"success": 1}
    assert first["color_statuses"] == {"succeeded": 1}
    assert len(calls) == 1
    assert sha256_file(source_path) == source_sha_before
    with sqlite3.connect(workspace.database_path) as connection:
        connection.row_factory = sqlite3.Row
        model = connection.execute(
            "SELECT * FROM model_runs WHERE run_id='quick_online_1'"
        ).fetchone()
        assert model["request_path"]
        assert model["raw_response_path"]
        assert model["parsed_response_path"]
        assert connection.execute(
            """
            SELECT color_hex FROM quick_color_regions
            WHERE run_id='quick_online_1'
            """
        ).fetchone()[0] == "#C81E46"
    for relative in (
        model["request_path"],
        model["raw_response_path"],
        model["parsed_response_path"],
    ):
        assert (workspace.run_dir("quick_online_1") / relative).is_file()

    exported = export_quick_extraction(
        workspace,
        run_id="quick_online_1",
        output_dir=tmp_path / "exports",
    )
    assert exported["counts"] == {
        "images": 1,
        "text_items": 1,
        "color_regions": 1,
        "occurrences": 1,
    }
    assert all(Path(path).is_file() for path in exported["files"].values())

    second = run_quick_extraction(
        workspace,
        settings,
        run_id="quick_cached_2",
        execute_online=False,
        max_calls=None,
        resume=False,
        image_id=image_id,
    )
    assert second["online_calls_made"] == 0
    assert second["cache_hits_materialized"] == 1
    assert second["image_statuses"] == {"success": 1}
    assert len(calls) == 1
    with sqlite3.connect(workspace.database_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM model_runs
            WHERE run_id='quick_cached_2' AND status='cache_hit'
            """
        ).fetchone()[0] == 1


def test_mock_long_image_merge_mapping_dedup_and_partial_failure(
    quick_workspace: tuple[Workspace, PipelineSettings, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, settings, image_id, source_path = quick_workspace
    asset_dir = workspace.output_root / "assets/long-test"
    asset_dir.mkdir(parents=True)
    global_path = asset_dir / "global.jpg"
    tile_paths = [
        asset_dir / "tile-0.jpg",
        asset_dir / "tile-1.jpg",
        asset_dir / "tile-2.jpg",
    ]
    Image.new("RGB", (100, 100), (200, 30, 70)).save(
        global_path,
        format="JPEG",
        quality=92,
        subsampling=0,
    )
    for path in tile_paths:
        Image.new("RGB", (60, 100), (200, 30, 70)).save(
            path,
            format="JPEG",
            quality=92,
            subsampling=0,
        )
    with sqlite3.connect(workspace.database_path) as connection:
        connection.row_factory = sqlite3.Row
        global_id = stable_id("asset", image_id, "global")
        connection.execute(
            """
            INSERT INTO derived_assets(
                derived_asset_id, image_id, image_occurrence_id, run_id,
                asset_type, relative_path, sha256, width, height, format,
                transform_name, transform_version, transform_fingerprint,
                root_alias, created_at, metadata_json
            ) VALUES (?, ?, NULL, 'stage2_full_20260728',
                      'global_thumbnail', ?, ?, 100, 100, 'JPEG',
                      'global', '1', 'global-fp', 'pipeline_output',
                      '2026-01-01T00:00:00Z', '{}')
            """,
            (
                global_id,
                image_id,
                global_path.relative_to(workspace.output_root).as_posix(),
                sha256_file(global_path),
            ),
        )
        layout_id = stable_id("layout", image_id)
        connection.execute(
            """
            INSERT INTO long_image_layouts(
                long_image_layout_id, image_id, run_id,
                global_thumbnail_asset_id, original_width, original_height,
                global_thumbnail_width, global_thumbnail_height,
                reading_axis, layout_type, global_layout_json,
                image_to_thumbnail_transform_json,
                tiling_strategy_version, created_at
            ) VALUES (?, ?, 'stage2_full_20260728', ?, 100, 100,
                      100, 100, 'horizontal', 'long_detail_strip', '{}',
                      '{"scale_x":1,"scale_y":1,"translate_x":0,"translate_y":0}',
                      'test-tiling', '2026-01-01T00:00:00Z')
            """,
            (layout_id, image_id, global_id),
        )
        starts = [0, 40, 20]
        for index, (path, start) in enumerate(zip(tile_paths, starts)):
            asset_id = stable_id("asset", image_id, "tile", index)
            connection.execute(
                """
                INSERT INTO derived_assets(
                    derived_asset_id, image_id, image_occurrence_id, run_id,
                    asset_type, relative_path, sha256, width, height, format,
                    transform_name, transform_version,
                    transform_fingerprint, root_alias, created_at,
                    metadata_json
                ) VALUES (?, ?, NULL, 'stage2_full_20260728',
                          'image_tile', ?, ?, 60, 100, 'JPEG',
                          'tile', '1', ?, 'pipeline_output',
                          '2026-01-01T00:00:00Z', '{}')
                """,
                (
                    asset_id,
                    image_id,
                    path.relative_to(workspace.output_root).as_posix(),
                    sha256_file(path),
                    f"tile-fp-{index}",
                ),
            )
            connection.execute(
                """
                INSERT INTO image_tiles(
                    image_tile_id, long_image_layout_id, image_id,
                    tile_asset_id, tile_index, bbox_image_json,
                    overlap_before_px, overlap_after_px, tile_width,
                    tile_height, image_to_tile_transform_json,
                    tile_to_image_transform_json, transform_fingerprint,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 20, 20, 60, 100, ?, ?, ?,
                          '2026-01-01T00:00:00Z')
                """,
                (
                    stable_id("tile", layout_id, index),
                    layout_id,
                    image_id,
                    asset_id,
                    index,
                    json.dumps([start, 0, start + 60, 100]),
                    json.dumps(
                        {
                            "scale_x": 1,
                            "scale_y": 1,
                            "translate_x": -start,
                            "translate_y": 0,
                        }
                    ),
                    json.dumps(
                        {
                            "scale_x": 1,
                            "scale_y": 1,
                            "translate_x": start,
                            "translate_y": 0,
                        }
                    ),
                    f"tile-fp-{index}",
                ),
            )
        connection.commit()

    tile_call_count = 0
    calls: list[str] = []

    class FakeResponse:
        model = "qwen-test"
        usage = SimpleNamespace(
            model_dump=lambda mode="json": {"total_tokens": 5}
        )

        def __init__(self, payload: dict) -> None:
            self.payload = payload
            self.choices = [
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(payload))
                )
            ]

        def model_dump(self, mode: str = "json") -> dict:
            return {
                "model": self.model,
                "choices": [
                    {"message": {"content": json.dumps(self.payload)}}
                ],
                "usage": {"total_tokens": 5},
            }

    class FakeCompletions:
        def create(self, **kwargs: object) -> FakeResponse:
            nonlocal tile_call_count
            content = kwargs["messages"][0]["content"]  # type: ignore[index]
            prompt = content[-1]["text"]
            if "scope=global_thumbnail" in prompt:
                calls.append("global")
                global_payload = _valid_quick_payload("global_thumbnail")
                global_payload["primary_role"] = "multi_shade_comparison"
                global_payload["layout_type"] = "long_detail_strip"
                return FakeResponse(global_payload)
            calls.append("tile")
            tile_call_count += 1
            if tile_call_count > 2:
                return FakeResponse({})
            tile_payload = _valid_quick_payload("tile")
            if tile_call_count == 1:
                tile_payload["text_items"][0]["bbox_norm"] = [
                    0.75,
                    0.1,
                    0.95,
                    0.2,
                ]
                tile_payload["color_regions"][0]["bbox_norm"] = [
                    0.70,
                    0.2,
                    0.98,
                    0.9,
                ]
            else:
                tile_payload["text_items"][0]["bbox_norm"] = [
                    5 / 60,
                    0.1,
                    17 / 60,
                    0.2,
                ]
                tile_payload["color_regions"][0]["bbox_norm"] = [
                    2 / 60,
                    0.2,
                    18.8 / 60,
                    0.9,
                ]
            return FakeResponse(tile_payload)

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=FakeOpenAI),
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key-not-secret")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen-test")
    source_sha = sha256_file(source_path)
    result = run_quick_extraction(
        workspace,
        settings,
        run_id="quick_long_partial",
        execute_online=True,
        max_calls=7,
        resume=False,
        image_id=image_id,
    )
    assert result["online_calls_made"] == 7
    assert result["image_statuses"] == {"partial": 1}
    assert result["unit_statuses"] == {"failed": 1, "succeeded": 3}
    assert result["image_summaries"][0]["text_items"] == 1
    assert result["image_summaries"][0]["color_regions"] == 1
    assert result["color_statuses"] == {"succeeded": 1}
    assert calls.count("global") == 1
    assert calls.count("tile") == 6
    assert sha256_file(source_path) == source_sha
    with sqlite3.connect(workspace.database_path) as connection:
        connection.row_factory = sqlite3.Row
        text = connection.execute(
            """
            SELECT source_observations_json FROM quick_text_items
            WHERE run_id='quick_long_partial'
            """
        ).fetchone()
        region = connection.execute(
            """
            SELECT source_observations_json, linked_text_item_ids_json
            FROM quick_color_regions
            WHERE run_id='quick_long_partial'
            """
        ).fetchone()
        assert len(json.loads(text["source_observations_json"])) == 2
        assert len(json.loads(region["source_observations_json"])) == 2
        assert len(json.loads(region["linked_text_item_ids_json"])) == 1


def test_interrupted_artifacts_are_recovered_and_finalized(
    quick_workspace: tuple[Workspace, PipelineSettings, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, settings, image_id, _source_path = quick_workspace
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen-test")
    run_id = "quick_interrupted"
    prepared = run_quick_extraction(
        workspace,
        settings,
        run_id=run_id,
        execute_online=False,
        max_calls=None,
        resume=False,
        image_id=image_id,
    )
    assert prepared["status"] == "prepared_cache_only"
    with sqlite3.connect(workspace.database_path) as connection:
        connection.row_factory = sqlite3.Row
        units = _units_from_database(
            connection,
            workspace,
            run_id=run_id,
        )
    unit = units[0]
    manifest = _request_manifest(settings, unit, "qwen-test")
    cache_key = quick_cache_key(manifest)
    model_run_id = stable_id(
        "model_run",
        run_id,
        unit.unit_id,
        cache_key,
        1,
    )
    run_dir = workspace.run_dir(run_id)
    request_path = run_dir / f"model/requests/{model_run_id}.json"
    raw_path = run_dir / f"model/raw/{model_run_id}.json"
    parsed_path = run_dir / f"model/parsed/{model_run_id}.json"
    request_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    payload = _valid_quick_payload()
    raw_path.write_text(
        json.dumps(
            {
                "model": "qwen-test",
                "choices": [
                    {"message": {"content": json.dumps(payload)}}
                ],
                "usage": {"total_tokens": 12},
            }
        ),
        encoding="utf-8",
    )
    parsed_path.write_text(json.dumps(payload), encoding="utf-8")
    recovered = recover_quick_run_artifacts(
        workspace,
        settings,
        run_id=run_id,
        finalize=True,
    )
    assert recovered["succeeded"] == 1
    assert recovered["provider_attempts_recovered"] == 1
    assert recovered["pipeline_status"] == "completed"
    assert recovered["image_statuses"] == {"success": 1}
    with sqlite3.connect(workspace.database_path) as connection:
        assert connection.execute(
            """
            SELECT unit_status FROM quick_extraction_units
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()[0] == "succeeded"


def test_cache_key_excludes_run_identity_but_includes_schema_and_prompt() -> None:
    base = {
        "scope": "image",
        "model": "qwen",
        "prompt_name": "quick",
        "prompt_version": "1",
        "prompt_sha256": "prompt",
        "response_schema_version": "schema-1",
        "generation_parameters": {"temperature": 0},
        "image_asset": {"sha256": "asset"},
    }
    first = quick_cache_key(base)
    with_run_identity = {
        **base,
        "analysis_unit_id": "another-run-unit",
        "image_id": "another-image-id",
    }
    assert quick_cache_key(with_run_identity) == first
    changed = {
        **base,
        "response_schema_version": "schema-2",
    }
    assert quick_cache_key(changed) != first


def test_real_stage2_asset_selection_when_workspace_is_available() -> None:
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    stage1_db = settings.project_path("stage1_database")
    if not stage1_db.is_file():
        pytest.skip("repository Stage 1/2 workspace is not present")
    output_root = settings.project_path("output_root")
    workspace_db_candidates = list(
        (output_root / "workspaces").glob("*/lipcolor.sqlite")
    )
    if not workspace_db_candidates:
        pytest.skip("repository Stage 2 workspace is not present")
    workspace = Workspace(
        repo_root=REPO_ROOT,
        output_root=output_root,
        database_path=workspace_db_candidates[0],
        dataset_snapshot_id=workspace_db_candidates[0].parent.name,
        stage1_database_path=stage1_db,
    )
    plan = plan_quick_extraction(
        workspace,
        settings,
        run_id="test_read_only_asset_plan",
        image_id=(
            "4edb5dac6d80b2c16f2dd18c941755b3"
            "ae1a5c09f00dfabbad448c1ad702bc96"
        ),
    )
    assert plan["unit_count"] == 7
    assert Counter(item["scope"] for item in plan["units"]) == {
        "global_thumbnail": 1,
        "tile": 6,
    }
    assert all(
        item["asset_type"] in {"global_thumbnail", "image_tile"}
        for item in plan["units"]
    )
