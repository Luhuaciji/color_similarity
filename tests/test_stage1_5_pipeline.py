from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from lipcolor_pipeline.annotations import (
    append_annotation_event,
    approve_pilot_item,
    create_pilot_review_set,
)
from lipcolor_pipeline.image_assets import (
    ImagePolicyRejected,
    ensure_vlm_compatible_asset,
    load_oriented_working_image,
    prepare_analysis_assets,
)
from lipcolor_pipeline.pilot import (
    create_context_review_sample,
    fuse_pilot_context,
)
from lipcolor_pipeline.review_app import create_app
from lipcolor_pipeline.settings import load_settings
from lipcolor_pipeline.stage1_manifest import (
    apply_migrations,
    sha256_file,
    stable_id,
)
from lipcolor_pipeline.vlm_client import (
    AnalysisTask,
    _request_manifest,
    audit_image_only_manifest,
    run_pilot_vlm,
)
from lipcolor_pipeline.vlm_schemas import (
    parse_content_visual_analysis,
    parse_with_deterministic_repair,
)
from lipcolor_pipeline.workspace import (
    Workspace,
    ensure_run_directory,
    open_database,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
ROLE_CODES = (
    "single_bullet",
    "single_swatch",
    "lip_effect",
    "multi_shade_comparison",
    "color_card",
    "packaging",
    "text_promo",
    "invalid",
)


def _valid_analysis(
    *,
    role: str = "single_swatch",
    scope: str = "image",
) -> dict:
    return {
        "schema_version": "content_visual_analysis-1.0",
        "analysis_scope": scope,
        "input_context_policy": "image_only",
        "primary_role": role,
        "secondary_roles": [],
        "layout_type": "single_panel",
        "global_layout": {},
        "role_confidence": 0.91,
        "contains_text": False,
        "contains_multiple_shades": False,
        "contains_lips": False,
        "contains_skin_swatch": True,
        "contains_product_bullet": False,
        "contains_packaging": False,
        "depicted_shades": ["12"],
        "representative_color_eligible": True,
        "eligibility_score": 0.88,
        "recommended_strategy": "single_swatch_segmentation",
        "rejection_reasons": [],
        "candidate_color_regions": [
            {
                "region_type": "swatch",
                "bbox_norm": [0.1, 0.2, 0.8, 0.9],
                "confidence": 0.9,
                "risks": [],
            }
        ],
        "observed_objects": ["swatch"],
        "quality_risks": [],
        "reason": "Visible single color swatch.",
    }


@pytest.mark.parametrize("role", ROLE_CODES)
def test_content_schema_accepts_all_eight_roles(role: str) -> None:
    payload = _valid_analysis(role=role)
    parsed = parse_content_visual_analysis(
        f"```json\n{json.dumps(payload)}\n```"
    )
    assert parsed.primary_role == role


def test_content_schema_rejects_bad_enum_bbox_and_missing_field() -> None:
    bad_enum = _valid_analysis(role="not_a_role")
    with pytest.raises(ValidationError):
        parse_content_visual_analysis(json.dumps(bad_enum))

    bad_bbox = _valid_analysis()
    bad_bbox["candidate_color_regions"][0]["bbox_norm"] = [0.8, 0.2, 0.1, 0.9]
    with pytest.raises(ValidationError):
        parse_content_visual_analysis(json.dumps(bad_bbox))

    missing = _valid_analysis()
    del missing["reason"]
    with pytest.raises(ValidationError):
        parse_content_visual_analysis(json.dumps(missing))


def test_deterministic_schema_repair_is_narrow_and_auditable() -> None:
    payload = _valid_analysis(role="single_bullet")
    payload["recommended_strategy"] = "single_region_matching"
    payload["candidate_color_regions"][0]["bbox_norm"] = [10, 20, 80, 90]
    parsed, actions = parse_with_deterministic_repair(
        json.dumps(payload),
        image_width=100,
        image_height=100,
    )
    assert parsed.recommended_strategy == "single_bullet_segmentation"
    assert parsed.candidate_color_regions[0].bbox_norm == (
        0.1,
        0.2,
        0.8,
        0.9,
    )
    assert set(actions) == {
        "mapped_single_region_matching_strategy",
        "normalized_pixel_bbox_to_unit_interval:0",
    }


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    stage: str = "1.5",
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id, dataset_snapshot_id, stage, pipeline_version,
            schema_version, git_commit, git_dirty, config_json, config_hash,
            dependency_snapshot_json, started_at, finished_at, status,
            error_summary_json
        ) VALUES (?, 'ds_test', ?, '0.2.0', 'test', 'test', 0,
                  '{}', 'test', '{}', '2026-01-01T00:00:00Z',
                  NULL, 'running', '{}')
        """,
        (run_id, stage),
    )


@pytest.fixture()
def mini_workspace(tmp_path: Path) -> tuple[Workspace, str, str]:
    database_path = tmp_path / "lipcolor.sqlite"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(
        connection,
        REPO_ROOT / "database" / "migrations",
        through_version=6,
    )
    connection.execute(
        """
        INSERT INTO dataset_snapshots VALUES(
            'ds_test', 'csv', 'repo://input.csv', 'csv_sha', 1, '{}',
            'test', '{}', '2026-01-01T00:00:00Z'
        )
        """
    )
    _insert_run(connection, "pilot_test")
    connection.execute(
        """
        INSERT INTO folder_groups VALUES(
            'folder_test', 'ds_test', 'BrandSecret', 'ProductSecret',
            'BrandSecret/ProductSecret', 2, 1, 'multi_source_record'
        )
        """
    )

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source = raw_dir / "observed.jpg"
    Image.new("RGBA", (80, 40), (180, 30, 80, 180)).save(
        source, format="PNG"
    )
    image_id = sha256_file(source)
    connection.execute(
        """
        INSERT INTO image_contents VALUES(
            ?, ?, ?, 'PNG', 'image/png', '2026-01-01T00:00:00Z'
        )
        """,
        (image_id, image_id, source.stat().st_size),
    )
    occurrence_id = stable_id("occurrence", image_id, "observed.jpg")
    connection.execute(
        """
        INSERT INTO image_occurrences VALUES(
            ?, ?, 'folder_test', 'raw_test', 'BrandSecret/ProductSecret/observed.jpg',
            'observed.jpg', '.jpg', 1, 'BrandSecret', 'ProductSecret',
            'legacy-path-id', 1, 1
        )
        """,
        (occurrence_id, image_id),
    )
    source_record_ids: list[str] = []
    source_ref_ids: list[str] = []
    for index in (1, 2):
        record_id = f"record_{index}"
        ref_id = f"ref_{index}"
        source_record_ids.append(record_id)
        source_ref_ids.append(ref_id)
        connection.execute(
            """
            INSERT INTO source_records VALUES(
                ?, 'ds_test', 'folder_test', ?, ?, '', ?, '', '',
                'BrandSecret', 'SKU Secret', 'SKU Secret 12', '12', '{}'
            )
            """,
            (record_id, index + 1, f"row_hash_{index}", f"SKU-SECRET-{index}"),
        )
        connection.execute(
            """
            INSERT INTO source_image_refs VALUES(
                ?, ?, 'sku_image_urls', ?, 'https://example.invalid/image',
                ?, '.jpg', 'BrandSecret/ProductSecret/observed.jpg',
                'success', NULL, '{}'
            )
            """,
            (ref_id, record_id, index, f"url_hash_{index}"),
        )
        connection.execute(
            """
            INSERT INTO source_ref_occurrences VALUES(?, ?, 'exact_path', 1.0)
            """,
            (ref_id, occurrence_id),
        )
    connection.execute(
        """
        INSERT INTO pilot_samples VALUES(
            'sample_test', 'pilot_test', ?, ?, ?, '["format_mismatch"]',
            'test', 'pending', '2026-01-01T00:00:00Z', 'human'
        )
        """,
        (
            image_id,
            json.dumps([occurrence_id]),
            json.dumps(source_record_ids),
        ),
    )
    connection.commit()
    connection.close()

    # Keep the output root short enough to exercise full SHA-based asset names
    # on Windows installations where legacy MAX_PATH remains enabled.
    output_root = (
        tmp_path.parents[2] / f"lc_{stable_id('test_output', str(tmp_path))[:16]}"
    )
    workspace = Workspace(
        repo_root=REPO_ROOT,
        output_root=output_root,
        database_path=database_path,
        dataset_snapshot_id="ds_test",
        stage1_database_path=database_path,
    )
    for child in ("runs", "assets"):
        (output_root / child).mkdir(parents=True)
    ensure_run_directory(workspace, "pilot_test", resume=False)
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    with open_database(database_path) as db:
        prepared = prepare_analysis_assets(
            db,
            workspace,
            run_id="pilot_test",
            image_id=image_id,
            source_path=source,
            config=settings.section("preprocessing"),
        )
        asset = prepared.analysis_assets[0]
        db.execute(
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
                'observation_test', ?, 'pilot_test', 'ok', 0, 'PNG', 'RGBA',
                80, 40, 1, 0, 1, 0, 'none', 'sRGB', 0, 1, 0.25,
                ?, NULL, '{}', 'test', '2026-01-01T00:00:00Z'
            )
            """,
            (image_id, asset.derived_asset_id),
        )
    try:
        yield workspace, image_id, occurrence_id
    finally:
        shutil.rmtree(output_root, ignore_errors=True)


def test_a_layer_manifest_has_strict_whitelist() -> None:
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    task = AnalysisTask(
        image_id="a" * 64,
        scope="image",
        unit_type="derived_image_asset",
        unit_id="asset-safe-id",
        asset_id="asset-safe-id",
        asset_path=Path("not-serialized.jpg"),
        asset_sha256="b" * 64,
        asset_type="analysis_preview",
        asset_format="JPEG",
        width=100,
        height=50,
        transform_fingerprint="c" * 64,
        tile_index=None,
        tile_bbox_image_json="[]",
    )
    manifest = _request_manifest(settings, task, "qwen3.6-plus")
    assert "not-serialized.jpg" not in json.dumps(manifest)
    assert "relative_path" not in manifest["image_asset"]
    with pytest.raises(ValueError, match="non-whitelisted"):
        audit_image_only_manifest({**manifest, "sku_id": "forbidden"})


def test_owner_delegated_review_requires_immutable_scoped_authorization(
    mini_workspace: tuple[Workspace, str, str],
) -> None:
    workspace, image_id, _occurrence_id = mini_workspace
    review = create_pilot_review_set(
        workspace,
        pilot_run_id="pilot_test",
    )
    with open_database(workspace.database_path, readonly=True) as connection:
        item_id = connection.execute(
            """
            SELECT annotation_item_id
            FROM annotation_items
            WHERE annotation_set_id = ?
              AND content_context_visibility = 'image_only'
            """,
            (review["annotation_set_id"],),
        ).fetchone()[0]

    with pytest.raises(ValueError, match="owner_review_delegation_id"):
        append_annotation_event(
            workspace,
            annotation_item_id=item_id,
            annotator_id="agent-test",
            annotation_type="role",
            payload={"role_code": "color_card"},
            review_provenance="owner_delegated_agent",
        )

    delegation_id = stable_id(
        "owner_review_delegation",
        "pilot_test",
        image_id,
    )
    evidence = {
        "authorized_image_ids": [image_id],
        "authorized_annotation_types": ["role", "eligibility"],
        "authorized_role_codes": ["color_card"],
    }
    with open_database(workspace.database_path) as connection:
        connection.execute(
            """
            INSERT INTO owner_review_delegations(
                owner_review_delegation_id, pilot_run_id, scope,
                instruction_text, delegated_agent, evidence_json,
                created_at
            ) VALUES (?, 'pilot_test', 'stage1_5_color_card_topup',
                      'owner-authorized test', 'agent-test', ?,
                      '2026-01-01T00:00:00Z')
            """,
            (delegation_id, json.dumps(evidence)),
        )

    role = append_annotation_event(
        workspace,
        annotation_item_id=item_id,
        annotator_id="agent-test",
        annotation_type="role",
        payload={"role_code": "color_card"},
        review_provenance="owner_delegated_agent",
        owner_review_delegation_id=delegation_id,
    )
    eligibility = append_annotation_event(
        workspace,
        annotation_item_id=item_id,
        annotator_id="agent-test",
        annotation_type="eligibility",
        payload={
            "eligibility_label": True,
            "eligibility_reason_codes": ["visible_labeled_color_blocks"],
        },
        review_provenance="owner_delegated_agent",
        owner_review_delegation_id=delegation_id,
    )
    approve_pilot_item(
        workspace,
        annotation_item_id=item_id,
        annotator_id="agent-test",
    )

    with open_database(workspace.database_path) as connection:
        events = list(
            connection.execute(
                """
                SELECT review_provenance, owner_review_delegation_id
                FROM annotation_events
                WHERE annotation_event_id IN (?, ?)
                """,
                (
                    role["annotation_event_id"],
                    eligibility["annotation_event_id"],
                ),
            )
        )
        assert {
            (row["review_provenance"], row["owner_review_delegation_id"])
            for row in events
        } == {("owner_delegated_agent", delegation_id)}
        sample = connection.execute(
            """
            SELECT human_review_status, review_provenance
            FROM pilot_samples
            WHERE pilot_run_id = 'pilot_test' AND image_id = ?
            """,
            (image_id,),
        ).fetchone()
        assert tuple(sample) == ("approved", "owner_delegated_agent")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                """
                UPDATE owner_review_delegations
                SET instruction_text = 'changed'
                WHERE owner_review_delegation_id = ?
                """,
                (delegation_id,),
            )


class _FakeUsage:
    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


class _FakeResponse:
    model = "qwen3.6-plus-provider-revision"
    usage = _FakeUsage()

    def __init__(self, content: str) -> None:
        self.choices = [
            SimpleNamespace(message=SimpleNamespace(content=content))
        ]
        self._content = content

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return {
            "model": self.model,
            "choices": [{"message": {"content": self._content}}],
            "usage": self.usage.model_dump(mode="json"),
        }


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> _FakeResponse:
        self.calls += 1
        assert kwargs["extra_body"] == {"enable_thinking": False}
        assert kwargs["response_format"] == {"type": "json_object"}
        return _FakeResponse(json.dumps(_valid_analysis()))


def test_mock_vlm_cache_context_fusion_and_safe_review_api(
    mini_workspace: tuple[Workspace, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, image_id, _occurrence_id = mini_workspace
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    completions = _FakeCompletions()
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: fake_client)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3.6-plus")

    first = run_pilot_vlm(
        workspace,
        settings,
        run_id="pilot_test",
        execute_online=True,
        max_calls=1,
    )
    assert first["online_calls_made"] == 1
    assert completions.calls == 1

    fusion = fuse_pilot_context(workspace, run_id="pilot_test")
    assert fusion["inserted"] == 2
    with open_database(workspace.database_path, readonly=True) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM occurrence_context_fusions
            WHERE run_id = 'pilot_test'
            """
        ).fetchone()[0] == 2
        request_path = connection.execute(
            """
            SELECT request_path FROM model_runs
            WHERE run_id = 'pilot_test' AND status = 'succeeded'
            """
        ).fetchone()[0]
    request_payload = json.loads(
        (workspace.run_dir("pilot_test") / request_path).read_text(
            encoding="utf-8"
        )
    )
    serialized_request = json.dumps(request_payload)
    assert "BrandSecret" not in serialized_request
    assert "SKU-SECRET" not in serialized_request

    review = create_pilot_review_set(
        workspace,
        pilot_run_id="pilot_test",
    )
    client = TestClient(create_app(workspace))
    review_html = client.get("/").text
    assert "单色膏体图" in review_html
    assert "主角色 <span class=\"required\">必选并保存" in review_html
    assert "来源上下文" in review_html
    assert "已审核项（可修订）" in review_html
    assert "保存修订（保留旧审核记录）" in review_html
    assert "function loadDirection(direction=1,reset=false)" in review_html
    assert "value=\"${x}\"" in review_html
    items = client.get(
        f"/api/sets/{review['annotation_set_id']}/items",
        params={"visibility": "image_only"},
    ).json()
    item_id = items[0]["annotation_item_id"]
    payload = client.get(f"/api/items/{item_id}").json()
    serialized_payload = json.dumps(payload)
    assert image_id not in serialized_payload
    assert "BrandSecret" not in serialized_payload
    assert "SKU-SECRET" not in serialized_payload
    assert client.get(
        f"/api/items/{item_id}/occurrence-context"
    ).status_code == 403

    role = client.post(
        f"/api/items/{item_id}/events",
        json={
            "annotator_id": "reviewer-a",
            "annotation_type": "role",
            "payload": {"role_code": "single_swatch"},
        },
    )
    assert role.status_code == 200
    eligibility = client.post(
        f"/api/items/{item_id}/events",
        json={
            "annotator_id": "reviewer-a",
            "annotation_type": "eligibility",
            "payload": {
                "eligibility_label": True,
                "eligibility_reason_codes": [],
            },
        },
    )
    assert eligibility.status_code == 200
    mask = client.post(
        f"/api/items/{item_id}/events",
        json={
            "annotator_id": "reviewer-a",
            "annotation_type": "mask",
            "payload": {
                "region_type": "swatch",
                "polygon_image": [[5, 5], [50, 5], [50, 30], [5, 30]],
            },
        },
    )
    assert mask.status_code == 200
    assert mask.json()["mask_asset_id"]

    event_id = role.json()["annotation_event_id"]
    with open_database(workspace.database_path) as connection:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute(
                """
                UPDATE annotation_events SET annotator_id = 'changed'
                WHERE annotation_event_id = ?
                """,
                (event_id,),
            )

    with open_database(workspace.database_path) as connection:
        _insert_run(connection, "pilot_cached")
        source_asset = connection.execute(
            """
            SELECT * FROM derived_assets
            WHERE run_id = 'pilot_test' AND asset_type = 'analysis_preview'
            """
        ).fetchone()
        cached_asset_id = "cached_asset"
        connection.execute(
            """
            INSERT INTO derived_assets(
                derived_asset_id, image_id, image_occurrence_id, run_id,
                asset_type, relative_path, sha256, width, height, format,
                transform_name, transform_version, transform_fingerprint,
                root_alias, created_at, metadata_json
            ) VALUES (?, ?, NULL, 'pilot_cached', 'analysis_preview',
                      ?, ?, ?, ?, ?, ?, ?, ?, 'pipeline_output',
                      '2026-01-01T00:00:00Z', ?)
            """,
            (
                cached_asset_id,
                image_id,
                source_asset["relative_path"],
                source_asset["sha256"],
                source_asset["width"],
                source_asset["height"],
                source_asset["format"],
                source_asset["transform_name"],
                source_asset["transform_version"],
                source_asset["transform_fingerprint"],
                source_asset["metadata_json"],
            ),
        )
        connection.execute(
            """
            INSERT INTO pilot_samples VALUES(
                'sample_cached', 'pilot_cached', ?, '[]', '[]', '[]',
                'cache test', 'pending', '2026-01-01T00:00:00Z', 'human'
            )
            """,
            (image_id,),
        )
    ensure_run_directory(workspace, "pilot_cached", resume=False)
    cached = run_pilot_vlm(
        workspace,
        settings,
        run_id="pilot_cached",
        execute_online=True,
        max_calls=1,
    )
    assert cached["cache_hits"] == 1
    assert cached["online_calls_made"] == 0
    assert completions.calls == 1


def test_context_review_sampling_is_stratified_and_keeps_machine_labels_distinct(
    mini_workspace: tuple[Workspace, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, image_id, _original_occurrence_id = mini_workspace
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    with open_database(workspace.database_path) as connection:
        for index in range(2, 184):
            occurrence_id = f"occurrence_sampling_{index:03d}"
            connection.execute(
                """
                INSERT INTO image_occurrences VALUES(
                    ?, ?, 'folder_test', 'raw_test', ?,
                    ?, '.jpg', 1, 'BrandSecret', 'ProductSecret',
                    ?, 1, ?
                )
                """,
                (
                    occurrence_id,
                    image_id,
                    f"BrandSecret/ProductSecret/sampling_{index:03d}.jpg",
                    f"sampling_{index:03d}.jpg",
                    f"legacy-sampling-{index:03d}",
                    index,
                ),
            )
            connection.execute(
                """
                INSERT INTO source_ref_occurrences
                VALUES('ref_1', ?, 'exact_path', 1.0)
                """,
                (occurrence_id,),
            )

    completions = _FakeCompletions()
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **_kwargs: fake_client)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DASHSCOPE_MODEL", "qwen3.6-plus")
    run_pilot_vlm(
        workspace,
        settings,
        run_id="pilot_test",
        execute_online=True,
        max_calls=1,
    )
    fuse_pilot_context(workspace, run_id="pilot_test")

    with open_database(workspace.database_path) as connection:
        occurrence_ids = [
            row[0]
            for row in connection.execute(
                """
                SELECT image_occurrence_id FROM image_occurrences
                WHERE image_id = ? ORDER BY image_occurrence_id
                """,
                (image_id,),
            )
        ]
        assignments = (
            ["unrelated"] * 7
            + ["contains_context_shade"] * 23
            + ["same_product_unspecified_shade"] * 38
            + ["shade_conflict"] * 115
        )
        assert len(occurrence_ids) == len(assignments) == 183
        confidence = {
            "unrelated": 0.75,
            "contains_context_shade": 0.85,
            "same_product_unspecified_shade": 0.55,
            "shade_conflict": 0.65,
        }
        for occurrence_id, relationship in zip(
            occurrence_ids,
            assignments,
            strict=True,
        ):
            connection.execute(
                """
                UPDATE occurrence_context_fusions
                SET relationship_to_context = ?, confidence = ?
                WHERE run_id = 'pilot_test'
                  AND image_occurrence_id = ?
                """,
                (relationship, confidence[relationship], occurrence_id),
            )
    review = create_pilot_review_set(
        workspace,
        pilot_run_id="pilot_test",
    )
    with open_database(workspace.database_path, readonly=True) as connection:
        reviewed_item_id = connection.execute(
            """
            SELECT DISTINCT item.annotation_item_id
            FROM annotation_items AS item
            JOIN occurrence_context_fusions AS fusion
              ON fusion.run_id = 'pilot_test'
             AND fusion.image_occurrence_id =
                 item.image_occurrence_id
            WHERE item.annotation_set_id = ?
              AND fusion.relationship_to_context = 'shade_conflict'
            ORDER BY item.annotation_item_id
            LIMIT 1
            """,
            (review["annotation_set_id"],),
        ).fetchone()[0]
    original_relation_event = append_annotation_event(
        workspace,
        annotation_item_id=reviewed_item_id,
        annotator_id="human-a",
        annotation_type="occurrence_relation",
        payload={"relationship_to_context": "unrelated"},
    )
    approve_pilot_item(
        workspace,
        annotation_item_id=reviewed_item_id,
        annotator_id="human-a",
    )

    summary = create_context_review_sample(
        workspace,
        run_id="pilot_test",
    )
    assert summary["target_count"] == 40
    assert summary["sample_counts"] == {
        "contains_context_shade": 8,
        "same_product_unspecified_shade": 10,
        "shade_conflict": 15,
        "unrelated": 7,
    }
    assert summary["sample_status_counts"]["approved"] == 1

    with open_database(workspace.database_path) as connection:
        policy_id = summary["context_review_sampling_policy_id"]
        assert connection.execute(
            """
            SELECT COUNT(*) FROM context_review_sample_items
            WHERE context_review_sampling_policy_id = ?
            """,
            (policy_id,),
        ).fetchone()[0] == 40
        assert connection.execute(
            """
            SELECT COUNT(*) FROM occurrence_context_fusions
            WHERE run_id = 'pilot_test'
              AND review_status = 'machine_prelabel_unreviewed'
            """
        ).fetchone()[0] > 0
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            connection.execute(
                """
                UPDATE context_review_sample_items
                SET selected_reason = 'changed'
                WHERE context_review_sampling_policy_id = ?
                """,
                (policy_id,),
            )
        non_sample_item = connection.execute(
            """
            SELECT item.annotation_item_id
            FROM annotation_items AS item
            WHERE item.annotation_set_id = ?
              AND item.content_context_visibility = 'occurrence_context'
              AND NOT EXISTS (
                  SELECT 1 FROM context_review_sample_items AS sample
                  WHERE sample.context_review_sampling_policy_id = ?
                    AND sample.annotation_item_id =
                        item.annotation_item_id
              )
            LIMIT 1
            """,
            (review["annotation_set_id"], policy_id),
        ).fetchone()[0]
    with pytest.raises(ValueError, match="machine-prelabel-only"):
        append_annotation_event(
            workspace,
            annotation_item_id=non_sample_item,
            annotator_id="human-a",
            annotation_type="occurrence_relation",
            payload={"relationship_to_context": "unrelated"},
        )

    client = TestClient(create_app(workspace))
    sampled_items = client.get(
        f"/api/sets/{review['annotation_set_id']}/items",
        params={
            "visibility": "occurrence_context",
            "sample_only": True,
            "limit": 500,
        },
    )
    assert sampled_items.status_code == 200
    assert len(sampled_items.json()) == 40
    reviewed_payload = client.get(
        f"/api/items/{reviewed_item_id}",
        params={"annotator_id": "human-a"},
    ).json()
    assert reviewed_payload["status"] == "approved"
    assert reviewed_payload["sample_model_relationship"] == "shade_conflict"
    assert reviewed_payload["events"][-1]["after"][
        "relationship_to_context"
    ] == "unrelated"
    revision = client.post(
        f"/api/items/{reviewed_item_id}/events",
        json={
            "annotator_id": "human-a",
            "annotation_type": "occurrence_relation",
            "payload": {
                "relationship_to_context": "contains_context_shade",
                "review_notes": "corrected after reviewing the source context",
            },
            "supersedes_event_id": original_relation_event[
                "annotation_event_id"
            ],
        },
    )
    assert revision.status_code == 200
    revised_payload = client.get(
        f"/api/items/{reviewed_item_id}",
        params={"annotator_id": "human-a"},
    ).json()
    relation_events = [
        event
        for event in revised_payload["events"]
        if event["annotation_type"] == "occurrence_relation"
    ]
    assert len(relation_events) == 2
    assert relation_events[-1]["supersedes_event_id"] == (
        original_relation_event["annotation_event_id"]
    )
    assert relation_events[-1]["after"]["relationship_to_context"] == (
        "contains_context_shade"
    )
    assert revised_payload["sample_model_relationship"] == "shade_conflict"
    assert revised_payload["status"] == "completed"
    reapproval = client.post(
        f"/api/items/{reviewed_item_id}/approve-pilot",
        json={"annotator_id": "human-a"},
    )
    assert reapproval.status_code == 200
    assert client.get(f"/api/items/{reviewed_item_id}").json()["status"] == (
        "approved"
    )


def _png_header(width: int, height: int) -> bytes:
    def chunk(name: bytes, value: bytes) -> bytes:
        return (
            struct.pack(">I", len(value))
            + name
            + value
            + struct.pack(">I", zlib.crc32(name + value) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def test_decode_format_alpha_gif_orientation_truncation_and_100mp(
    tmp_path: Path,
) -> None:
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    config = settings.section("preprocessing")

    mismatch = tmp_path / "mismatch.jpg"
    Image.new("RGBA", (20, 10), (1, 2, 3, 100)).save(
        mismatch, format="PNG"
    )
    _working, alpha, inspection = load_oriented_working_image(
        mismatch, config
    )
    assert inspection.source_format == "PNG"
    assert inspection.format_mismatch
    assert inspection.has_alpha
    assert alpha is not None

    gif = tmp_path / "animated.gif"
    Image.new("RGB", (12, 8), "red").save(
        gif,
        save_all=True,
        append_images=[Image.new("RGB", (12, 8), "blue")],
        duration=10,
        loop=0,
    )
    _working, _alpha, gif_inspection = load_oriented_working_image(gif, config)
    assert gif_inspection.frame_count == 2
    assert gif_inspection.selected_frame == 0

    for orientation in range(1, 9):
        oriented_path = tmp_path / f"orientation_{orientation}.jpg"
        exif = Image.Exif()
        exif[274] = orientation
        Image.new("RGB", (30, 20), "red").save(
            oriented_path, exif=exif
        )
        _working, _alpha, oriented = load_oriented_working_image(
            oriented_path, config
        )
        expected = (20, 30) if orientation in {5, 6, 7, 8} else (30, 20)
        assert (oriented.oriented_width, oriented.oriented_height) == expected

    valid_jpeg = tmp_path / "valid.jpg"
    Image.new("RGB", (40, 40), "red").save(valid_jpeg)
    truncated = tmp_path / "truncated.jpg"
    truncated.write_bytes(valid_jpeg.read_bytes()[:-30])
    with pytest.raises(OSError):
        load_oriented_working_image(truncated, config)

    huge = tmp_path / "huge.png"
    huge.write_bytes(_png_header(10_000, 10_000))
    with pytest.raises(ImagePolicyRejected):
        load_oriented_working_image(huge, config)


def test_long_image_global_thumbnail_tiles_and_coordinates(
    mini_workspace: tuple[Workspace, str, str],
    tmp_path: Path,
) -> None:
    workspace, _existing_image_id, _occurrence_id = mini_workspace
    source = tmp_path / "long.png"
    Image.new("RGB", (64, 320), "purple").save(source)
    image_id = sha256_file(source)
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    config = {
        **settings.section("preprocessing"),
        "long_aspect_ratio": 4.0,
        "long_min_edge": 300,
        "long_min_short_edge": 64,
        "global_thumbnail_max_long_edge": 256,
        "tile_long_axis": 128,
        "tile_overlap": 32,
    }
    with open_database(workspace.database_path) as connection:
        connection.execute(
            """
            INSERT INTO image_contents VALUES(
                ?, ?, ?, 'PNG', 'image/png', '2026-01-01T00:00:00Z'
            )
            """,
            (image_id, image_id, source.stat().st_size),
        )
        prepared = prepare_analysis_assets(
            connection,
            workspace,
            run_id="pilot_test",
            image_id=image_id,
            source_path=source,
            config=config,
        )
        assert prepared.inspection.is_long
        assert [asset.asset_type for asset in prepared.analysis_assets] == [
            "global_thumbnail",
            "image_tile",
            "image_tile",
            "image_tile",
        ]
        layout = connection.execute(
            """
            SELECT * FROM long_image_layouts
            WHERE long_image_layout_id = ?
            """,
            (prepared.long_image_layout_id,),
        ).fetchone()
        assert layout["reading_axis"] == "vertical"
        assert (layout["original_width"], layout["original_height"]) == (64, 320)
        tiles = list(
            connection.execute(
                """
                SELECT bbox_image_json, image_to_tile_transform_json,
                       tile_to_image_transform_json
                FROM image_tiles
                WHERE long_image_layout_id = ?
                ORDER BY tile_index
                """,
                (prepared.long_image_layout_id,),
            )
        )
        assert [json.loads(row["bbox_image_json"]) for row in tiles] == [
            [0, 0, 64, 128],
            [0, 96, 64, 224],
            [0, 192, 64, 320],
        ]
        assert all(row["image_to_tile_transform_json"] for row in tiles)
        assert all(row["tile_to_image_transform_json"] for row in tiles)


def test_vlm_transport_padding_preserves_source_asset(
    mini_workspace: tuple[Workspace, str, str],
    tmp_path: Path,
) -> None:
    workspace, _existing_image_id, _occurrence_id = mini_workspace
    source = tmp_path / "strip.png"
    Image.new("RGB", (60, 1), "red").save(source)
    image_id = sha256_file(source)
    settings = load_settings(REPO_ROOT, load_dotenv=False)
    with open_database(workspace.database_path) as connection:
        connection.execute(
            """
            INSERT INTO image_contents VALUES(
                ?, ?, ?, 'PNG', 'image/png', '2026-01-01T00:00:00Z'
            )
            """,
            (image_id, image_id, source.stat().st_size),
        )
        prepared = prepare_analysis_assets(
            connection,
            workspace,
            run_id="pilot_test",
            image_id=image_id,
            source_path=source,
            config=settings.section("preprocessing"),
        )
        original = prepared.analysis_assets[0]
        padded = ensure_vlm_compatible_asset(
            connection,
            workspace,
            run_id="pilot_test",
            source_asset=original,
        )
        assert padded is not None
        assert (original.width, original.height) == (60, 1)
        assert (padded.width, padded.height) == (60, 11)
        assert original.path.is_file()
        assert padded.path.is_file()
        assert padded.metadata["source_asset_id"] == original.derived_asset_id
        assert padded.metadata["transport_compatibility"]["padding_only"]


def test_annotation_revision_requires_latest_event(
    mini_workspace: tuple[Workspace, str, str],
) -> None:
    workspace, _image_id, _occurrence_id = mini_workspace
    review = create_pilot_review_set(workspace, pilot_run_id="pilot_test")
    with open_database(workspace.database_path, readonly=True) as connection:
        item_id = connection.execute(
            """
            SELECT annotation_item_id FROM annotation_items
            WHERE annotation_set_id = ?
              AND content_context_visibility = 'image_only'
            """,
            (review["annotation_set_id"],),
        ).fetchone()[0]
    first = append_annotation_event(
        workspace,
        annotation_item_id=item_id,
        annotator_id="reviewer",
        annotation_type="role",
        payload={"role_code": "single_swatch"},
    )
    with pytest.raises(ValueError, match="explicitly supersede"):
        append_annotation_event(
            workspace,
            annotation_item_id=item_id,
            annotator_id="reviewer",
            annotation_type="role",
            payload={"role_code": "color_card"},
        )
    revised = append_annotation_event(
        workspace,
        annotation_item_id=item_id,
        annotator_id="reviewer",
        annotation_type="role",
        payload={"role_code": "color_card"},
        supersedes_event_id=first["annotation_event_id"],
    )
    assert revised["annotation_event_id"] != first["annotation_event_id"]
