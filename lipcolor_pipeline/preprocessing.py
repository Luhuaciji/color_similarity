"""Stage 2 legacy migration, content-level preprocessing, and validation."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image

from .image_assets import (
    ImagePolicyRejected,
    load_oriented_working_image,
    prepare_analysis_assets,
    register_existing_asset,
)
from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import sha256_file, stable_id
from .workspace import (
    Workspace,
    atomic_link_or_copy,
    begin_pipeline_run,
    ensure_run_directory,
    export_tables_jsonl,
    finish_pipeline_run,
    open_database,
    utc_now,
    write_json_snapshot,
)


STAGE2_TABLES = (
    "image_preprocessing_observations",
    "preprocessing_occurrence_links",
    "derived_assets",
    "quality_flags",
    "duplicate_edges",
    "long_image_layouts",
    "image_tiles",
    "pipeline_errors",
)


def _legacy_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _legacy_metadata_path(settings: PipelineSettings) -> Path:
    return (
        settings.project_path("legacy_output_root")
        / "metadata"
        / "image_preprocessing.jsonl"
    )


def assert_stage1_5_gate(
    connection: sqlite3.Connection,
    pilot_run_id: str,
) -> None:
    run = connection.execute(
        "SELECT status FROM pipeline_runs WHERE run_id = ? AND stage = '1.5'",
        (pilot_run_id,),
    ).fetchone()
    if run is None or run[0] != "completed":
        raise RuntimeError(
            "Stage 2 full execution is blocked until Stage 1.5 is completed"
        )
    sample_count = connection.execute(
        """
        SELECT COUNT(*),
               SUM(CASE human_review_status WHEN 'approved' THEN 1 ELSE 0 END)
        FROM pilot_samples WHERE pilot_run_id = ?
        """,
        (pilot_run_id,),
    ).fetchone()
    review_set = connection.execute(
        """
        SELECT annotation_set_id
        FROM annotation_sets
        WHERE run_id = ? AND purpose = 'pilot_review'
        ORDER BY created_at DESC LIMIT 1
        """,
        (pilot_run_id,),
    ).fetchone()
    reviewed_roles: set[str] = set()
    approved_content = 0
    invalid_review_provenance = 0
    context_sample_target = 0
    context_sample_approved = 0
    latest_gate_decision: str | None = None
    if review_set is not None:
        annotation_set_id = review_set["annotation_set_id"]
        for item in connection.execute(
            """
            SELECT annotation_item_id, image_id, status
            FROM annotation_items
            WHERE annotation_set_id = ?
              AND content_context_visibility = 'image_only'
            """,
            (annotation_set_id,),
        ):
            if item["status"] != "approved":
                continue
            role_event = connection.execute(
                """
                SELECT event.role_code, event.annotator_id,
                       event.review_provenance,
                       event.owner_review_delegation_id
                FROM annotation_events AS event
                WHERE event.annotation_item_id = ?
                  AND event.annotation_type = 'role'
                  AND NOT EXISTS (
                      SELECT 1 FROM annotation_events AS revocation
                      WHERE revocation.annotation_type = 'revoke'
                        AND revocation.supersedes_event_id =
                            event.annotation_event_id
                  )
                ORDER BY event.created_at DESC,
                         event.annotation_event_id DESC
                LIMIT 1
                """,
                (item["annotation_item_id"],),
            ).fetchone()
            eligibility_event = connection.execute(
                """
                SELECT event.annotator_id, event.review_provenance,
                       event.owner_review_delegation_id
                FROM annotation_events AS event
                WHERE event.annotation_item_id = ?
                  AND event.annotation_type = 'eligibility'
                  AND NOT EXISTS (
                      SELECT 1 FROM annotation_events AS revocation
                      WHERE revocation.annotation_type = 'revoke'
                        AND revocation.supersedes_event_id =
                            event.annotation_event_id
                  )
                ORDER BY event.created_at DESC,
                         event.annotation_event_id DESC
                LIMIT 1
                """,
                (item["annotation_item_id"],),
            ).fetchone()
            if (
                role_event is None
                or eligibility_event is None
                or not role_event["role_code"]
            ):
                continue
            provenance = str(role_event["review_provenance"])
            valid_provenance = (
                provenance == eligibility_event["review_provenance"]
                and role_event["annotator_id"]
                == eligibility_event["annotator_id"]
                and role_event["owner_review_delegation_id"]
                == eligibility_event["owner_review_delegation_id"]
            )
            if provenance == "human":
                valid_provenance = (
                    valid_provenance
                    and role_event["owner_review_delegation_id"] is None
                )
            elif provenance == "owner_delegated_agent":
                delegation_id = role_event[
                    "owner_review_delegation_id"
                ]
                delegation = (
                    connection.execute(
                        """
                        SELECT * FROM owner_review_delegations
                        WHERE owner_review_delegation_id = ?
                          AND pilot_run_id = ?
                        """,
                        (delegation_id, pilot_run_id),
                    ).fetchone()
                    if delegation_id
                    else None
                )
                if delegation is None:
                    valid_provenance = False
                else:
                    evidence = json.loads(delegation["evidence_json"])
                    valid_provenance = (
                        valid_provenance
                        and delegation["scope"]
                        == "stage1_5_color_card_topup"
                        and delegation["delegated_agent"]
                        == role_event["annotator_id"]
                        and item["image_id"]
                        in set(evidence.get("authorized_image_ids", []))
                        and {"role", "eligibility"}.issubset(
                            set(
                                evidence.get(
                                    "authorized_annotation_types",
                                    [],
                                )
                            )
                        )
                        and role_event["role_code"]
                        in set(
                            evidence.get(
                                "authorized_role_codes",
                                [],
                            )
                        )
                    )
            else:
                valid_provenance = False
            if not valid_provenance:
                invalid_review_provenance += 1
                continue
            approved_content += 1
            reviewed_roles.add(str(role_event["role_code"]))
        policy = connection.execute(
            """
            SELECT context_review_sampling_policy_id, target_count
            FROM context_review_sampling_policies
            WHERE annotation_set_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (annotation_set_id,),
        ).fetchone()
        if policy is not None:
            context_sample_target = int(policy["target_count"])
            context_sample_approved = connection.execute(
                """
                SELECT COUNT(*)
                FROM context_review_sample_items AS sample
                JOIN annotation_items AS item
                  ON item.annotation_item_id =
                     sample.annotation_item_id
                WHERE sample.context_review_sampling_policy_id = ?
                  AND item.status = 'approved'
                  AND EXISTS (
                      SELECT 1 FROM annotation_events AS event
                      WHERE event.annotation_item_id =
                            item.annotation_item_id
                        AND event.annotation_type =
                            'occurrence_relation'
                        AND event.review_provenance = 'human'
                        AND event.owner_review_delegation_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1
                            FROM annotation_events AS revocation
                            WHERE revocation.annotation_type = 'revoke'
                              AND revocation.supersedes_event_id =
                                  event.annotation_event_id
                        )
                  )
                """,
                (policy["context_review_sampling_policy_id"],),
            ).fetchone()[0]
            gate = connection.execute(
                """
                SELECT decision
                FROM pilot_gate_decisions
                WHERE pilot_run_id = ?
                  AND context_review_sampling_policy_id = ?
                ORDER BY created_at DESC,
                         pilot_gate_decision_id DESC
                LIMIT 1
                """,
                (
                    pilot_run_id,
                    policy["context_review_sampling_policy_id"],
                ),
            ).fetchone()
            latest_gate_decision = gate["decision"] if gate else None
    if (
        sample_count[0] < 50
        or sample_count[0] > 100
        or sample_count[0] != sample_count[1]
        or approved_content != sample_count[0]
        or len(reviewed_roles) != 8
        or invalid_review_provenance
        or context_sample_target != 40
        or context_sample_approved != context_sample_target
        or latest_gate_decision != "go"
    ):
        raise RuntimeError(
            "Stage 2 full execution is blocked by incomplete Pilot review"
        )


def _asset_path(
    workspace: Workspace,
    asset_type: str,
    image_id: str,
    fingerprint: str,
    extension: str,
) -> Path:
    return (
        workspace.assets_root
        / asset_type
        / image_id[:2]
        / image_id
        / f"{fingerprint}{extension}"
    )


def migrate_legacy_preprocessing(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    pilot_run_id: str,
    resume: bool = False,
    enforce_pilot_gate: bool = True,
) -> dict[str, Any]:
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    legacy_root = settings.project_path("legacy_output_root")
    metadata_path = _legacy_metadata_path(settings)
    if not metadata_path.is_file():
        raise FileNotFoundError(f"legacy metadata missing: {metadata_path}")
    preprocessing = settings.section("preprocessing")
    transform_payload = {
        "name": "legacy_working_image_registration",
        "legacy_processing_version": "1.1.0",
        "implementation_version": preprocessing["implementation_version"],
        "legacy_config_sha256": sha256_file(
            settings.repo_root / "image_preprocessing_pipeline" / "config.yaml"
        ),
    }
    working_fingerprint = sha256_json(transform_payload)
    alpha_fingerprint = sha256_json(
        {**transform_payload, "name": "legacy_alpha_mask_registration"}
    )

    with open_database(workspace.database_path) as connection:
        if enforce_pilot_gate:
            assert_stage1_5_gate(connection, pilot_run_id)
        begin_pipeline_run(
            connection,
            workspace,
            settings,
            run_id=run_id,
            stage="2",
            resume=resume,
            extra_config={
                "stage2_operation": "full",
                "pilot_run_id": pilot_run_id,
            },
        )
        path_to_occurrence = {
            row["relative_path"]: (row["image_occurrence_id"], row["image_id"])
            for row in connection.execute(
                """
                SELECT relative_path, image_occurrence_id, image_id
                FROM image_occurrences
                """
            )
        }
        existing_assets = {
            (row["image_id"], row["asset_type"]): row["derived_asset_id"]
            for row in connection.execute(
                """
                SELECT image_id, asset_type, derived_asset_id
                FROM derived_assets
                WHERE run_id = ? AND asset_type IN (
                    'working_image_legacy', 'alpha_mask_legacy'
                )
                """,
                (run_id,),
            )
        }

        rows_by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
        total_rows = 0
        mapped_rows = 0
        source_mapping_errors = 0
        working_verified = 0
        working_hash_errors = 0
        alpha_verified = 0
        alpha_hashes_by_sha: dict[str, set[str]] = defaultdict(set)
        working_hashes_by_sha: dict[str, set[str]] = defaultdict(set)
        row_hashes_by_path: dict[str, str] = {}
        for row in _legacy_rows(metadata_path):
            total_rows += 1
            relative_path = str(row["relative_path"])
            row_hashes_by_path[relative_path] = sha256_json(row)
            mapping = path_to_occurrence.get(relative_path)
            if mapping is None or mapping[1] != row.get("sha256"):
                source_mapping_errors += 1
                continue
            mapped_rows += 1
            image_id = str(row["sha256"])
            rows_by_sha[image_id].append(row)
            if row.get("working_image_path"):
                working_path = legacy_root / str(row["working_image_path"])
                expected = str(row.get("working_image_sha256") or "")
                actual = sha256_file(working_path)
                if expected and actual == expected:
                    working_verified += 1
                    working_hashes_by_sha[image_id].add(actual)
                else:
                    working_hash_errors += 1
            if row.get("alpha_mask_path"):
                alpha_path = legacy_root / str(row["alpha_mask_path"])
                alpha_hashes_by_sha[image_id].add(sha256_file(alpha_path))
                alpha_verified += 1

        inconsistent_working_contents = sum(
            len(values) > 1 for values in working_hashes_by_sha.values()
        )
        inconsistent_alpha_contents = sum(
            len(values) > 1 for values in alpha_hashes_by_sha.values()
        )
        if source_mapping_errors or working_hash_errors or inconsistent_working_contents:
            raise RuntimeError(
                "legacy migration verification failed: "
                f"mapping={source_mapping_errors}, "
                f"working_hash={working_hash_errors}, "
                f"inconsistent_working={inconsistent_working_contents}"
            )

        canonical_assets: dict[str, dict[str, str | None]] = {}
        link_methods: Counter[str] = Counter()
        for image_id, rows in sorted(rows_by_sha.items()):
            canonical_assets[image_id] = {
                "working_asset_id": existing_assets.get(
                    (image_id, "working_image_legacy")
                ),
                "alpha_asset_id": existing_assets.get(
                    (image_id, "alpha_mask_legacy")
                ),
            }
            successful = next(
                (
                    row
                    for row in rows
                    if row.get("decode_success") and row.get("working_image_path")
                ),
                None,
            )
            if successful and not canonical_assets[image_id]["working_asset_id"]:
                source = legacy_root / str(successful["working_image_path"])
                destination = _asset_path(
                    workspace,
                    "working_image_legacy",
                    image_id,
                    working_fingerprint,
                    ".png",
                )
                method = atomic_link_or_copy(source, destination)
                link_methods[method] += 1
                asset = register_existing_asset(
                    connection,
                    workspace,
                    run_id,
                    image_id=image_id,
                    asset_type="working_image_legacy",
                    path=destination,
                    width=int(successful.get("working_width") or 0),
                    height=int(successful.get("working_height") or 0),
                    image_format="PNG",
                    transform_name="legacy_working_image_registration",
                    transform_version="1.1.0",
                    transform_fingerprint=working_fingerprint,
                    metadata={
                        "migration_schema_version": "legacy-preprocessing-1",
                        "legacy_working_sha256": successful[
                            "working_image_sha256"
                        ],
                        "link_method": method,
                        "source_root_alias": "legacy_preprocessing_output",
                    },
                )
                canonical_assets[image_id][
                    "working_asset_id"
                ] = asset.derived_asset_id
            alpha_row = next(
                (row for row in rows if row.get("alpha_mask_path")), None
            )
            if alpha_row and not canonical_assets[image_id]["alpha_asset_id"]:
                source = legacy_root / str(alpha_row["alpha_mask_path"])
                destination = _asset_path(
                    workspace,
                    "alpha_mask_legacy",
                    image_id,
                    alpha_fingerprint,
                    ".png",
                )
                method = atomic_link_or_copy(source, destination)
                link_methods[method] += 1
                with Image.open(destination) as alpha_image:
                    alpha_width, alpha_height = alpha_image.size
                asset = register_existing_asset(
                    connection,
                    workspace,
                    run_id,
                    image_id=image_id,
                    asset_type="alpha_mask_legacy",
                    path=destination,
                    width=alpha_width,
                    height=alpha_height,
                    image_format="PNG",
                    transform_name="legacy_alpha_mask_registration",
                    transform_version="1.1.0",
                    transform_fingerprint=alpha_fingerprint,
                    metadata={
                        "migration_schema_version": "legacy-preprocessing-1",
                        "link_method": method,
                        "source_root_alias": "legacy_preprocessing_output",
                    },
                )
                canonical_assets[image_id]["alpha_asset_id"] = asset.derived_asset_id
        connection.commit()

        manifest_lines: list[str] = []
        for relative_path, (occurrence_id, image_id) in sorted(
            path_to_occurrence.items()
        ):
            assets = canonical_assets.get(
                image_id, {"working_asset_id": None, "alpha_asset_id": None}
            )
            manifest_lines.append(
                canonical_json(
                    {
                        "schema_version": "legacy-migration-manifest-1",
                        "image_id": image_id,
                        "image_occurrence_id": occurrence_id,
                        "legacy_metadata_row_hash": row_hashes_by_path.get(
                            relative_path
                        ),
                        "working_asset_id": assets["working_asset_id"],
                        "alpha_asset_id": assets["alpha_asset_id"],
                        "migration_status": (
                            "verified_and_registered"
                            if relative_path in row_hashes_by_path
                            else "legacy_metadata_missing"
                        ),
                    }
                )
                + "\n"
            )
        manifest_text = "".join(manifest_lines)
        manifest_digest = hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        manifest_path = (
            run_dir
            / f"legacy_migration_manifest.{manifest_digest[:16]}.jsonl"
        )
        if not manifest_path.exists():
            manifest_path.write_text(
                manifest_text,
                encoding="utf-8",
                newline="\n",
            )
        summary = {
            "schema_version": "legacy-migration-summary-1",
            "run_id": run_id,
            "metadata_rows": total_rows,
            "mapped_rows": mapped_rows,
            "source_mapping_errors": source_mapping_errors,
            "working_assets_verified": working_verified,
            "working_hash_errors": working_hash_errors,
            "alpha_assets_verified": alpha_verified,
            "unique_content_rows": len(rows_by_sha),
            "canonical_working_assets": sum(
                bool(value["working_asset_id"])
                for value in canonical_assets.values()
            ),
            "canonical_alpha_assets": sum(
                bool(value["alpha_asset_id"])
                for value in canonical_assets.values()
            ),
            "inconsistent_working_contents": inconsistent_working_contents,
            "inconsistent_alpha_contents": inconsistent_alpha_contents,
            "link_methods": dict(link_methods),
            "manifest_sha256": sha256_file(manifest_path),
        }
        write_json_snapshot(
            run_dir / "reports",
            "legacy_migration_summary",
            summary,
        )
        return summary


def _save_new_working_image(
    workspace: Workspace,
    connection: sqlite3.Connection,
    run_id: str,
    image_id: str,
    working: Image.Image,
    config: Mapping[str, Any],
) -> str:
    payload = {
        "name": "strict_oriented_srgb_working_png",
        "implementation_version": config["implementation_version"],
        "alpha_display_background": "white",
    }
    fingerprint = sha256_json(payload)
    destination = _asset_path(
        workspace, "working_image", image_id, fingerprint, ".png"
    )
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp")
        working.save(temporary, format="PNG", compress_level=6, optimize=False)
        temporary.replace(destination)
    asset = register_existing_asset(
        connection,
        workspace,
        run_id,
        image_id=image_id,
        asset_type="working_image",
        path=destination,
        width=working.width,
        height=working.height,
        image_format="PNG",
        transform_name="strict_oriented_srgb_working_png",
        transform_version=str(config["implementation_version"]),
        transform_fingerprint=fingerprint,
        metadata={"color_semantics": "image_observation_input"},
    )
    return asset.derived_asset_id


def _quality_payload(
    legacy: Mapping[str, Any] | None,
    inspection: Any | None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "preprocessing-quality-1",
        "threshold_status": "provisional_target",
        "source": "legacy_1.1.0_verified_metrics",
    }
    if legacy:
        for key in (
            "aspect_ratio",
            "megapixels",
            "blur_score",
            "mean_brightness",
            "dark_pixel_ratio",
            "bright_pixel_ratio",
            "quality_warning",
        ):
            payload[key] = legacy.get(key)
    if inspection:
        payload.update(
            {
                "is_long": inspection.is_long,
                "is_extreme_aspect_ratio": inspection.is_extreme_aspect_ratio,
                "semantic_invalid_candidate": (
                    inspection.is_semantic_invalid_candidate
                ),
                "format_mismatch": inspection.format_mismatch,
            }
        )
    return payload


def _insert_quality_flags(
    connection: sqlite3.Connection,
    run_id: str,
    image_id: str,
    quality: Mapping[str, Any],
    threshold_version: str,
) -> None:
    flags: dict[str, float | None] = {}
    for value in str(quality.get("quality_warning") or "").split(";"):
        if value:
            flags[value] = None
    for boolean_flag in (
        "is_long",
        "is_extreme_aspect_ratio",
        "semantic_invalid_candidate",
        "format_mismatch",
    ):
        if quality.get(boolean_flag):
            flags[boolean_flag] = 1.0
    for code, metric in flags.items():
        connection.execute(
            """
            INSERT OR IGNORE INTO quality_flags(
                quality_flag_id, image_id, run_id, flag_code,
                metric_value, threshold_version, severity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id("quality", run_id, image_id, code, threshold_version),
                image_id,
                run_id,
                code,
                metric,
                threshold_version,
                "review",
                utc_now(),
            ),
        )


def _insert_duplicate_edges(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    run_id: str,
) -> int:
    path = settings.project_path("legacy_output_root") / "duplicate_pairs.csv"
    threshold = settings.section("thresholds")
    version = str(threshold["threshold_version"])
    high = int(threshold["phash_high_confidence_distance"])
    possible = int(threshold["phash_possible_duplicate_distance"])
    inserted = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            image_a = str(row["sha256_a"])
            image_b = str(row["sha256_b"])
            if image_a == image_b or row["pair_type"] == "exact_sha256":
                continue
            image_a, image_b = sorted((image_a, image_b))
            distance = int(row["phash_distance"])
            confidence = (
                "high"
                if distance <= high
                else "possible"
                if distance <= possible
                else "outside_current_threshold"
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO duplicate_edges(
                    duplicate_edge_id, image_id_a, image_id_b, method,
                    distance, threshold_version, confidence_class,
                    run_id, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id(
                        "duplicate_edge",
                        run_id,
                        image_a,
                        image_b,
                        "phash64",
                        version,
                    ),
                    image_a,
                    image_b,
                    "phash64",
                    distance,
                    version,
                    confidence,
                    run_id,
                    "unreviewed",
                    utc_now(),
                ),
            )
            inserted += max(0, cursor.rowcount)
    return inserted


def run_stage2_preprocessing(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    pilot_run_id: str,
    resume: bool = False,
    enforce_pilot_gate: bool = True,
    limit: int | None = None,
    image_id: str | None = None,
) -> dict[str, Any]:
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    raw_root = settings.project_path("raw_root")
    preprocessing = settings.section("preprocessing")
    thresholds = settings.section("thresholds")
    metadata_rows = list(_legacy_rows(_legacy_metadata_path(settings)))
    legacy_by_sha: dict[str, dict[str, Any]] = {}
    legacy_by_path: dict[str, dict[str, Any]] = {}
    for row in metadata_rows:
        legacy_by_path[str(row["relative_path"])] = row
        legacy_by_sha.setdefault(str(row["sha256"]), row)

    counts: Counter[str] = Counter()
    with open_database(workspace.database_path) as connection:
        if enforce_pilot_gate:
            assert_stage1_5_gate(connection, pilot_run_id)
        begin_pipeline_run(
            connection,
            workspace,
            settings,
            run_id=run_id,
            stage="2",
            resume=resume,
            extra_config={
                "stage2_operation": "full",
                "pilot_run_id": pilot_run_id,
            },
        )
        contents = list(
            connection.execute(
                """
                SELECT content.image_id, content.detected_format,
                       MIN(occurrence.relative_path) AS relative_path
                FROM image_contents AS content
                JOIN image_occurrences AS occurrence
                  ON occurrence.image_id = content.image_id
                GROUP BY content.image_id
                ORDER BY content.image_id
                """
            )
        )
        if image_id is not None:
            contents = [row for row in contents if row["image_id"] == image_id]
            if not contents:
                raise KeyError("requested image_id is not in the dataset")
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            contents = contents[:limit]
        existing_assets = {
            (row["image_id"], row["asset_type"]): row["derived_asset_id"]
            for row in connection.execute(
                """
                SELECT image_id, asset_type, derived_asset_id
                FROM derived_assets WHERE run_id = ?
                """,
                (run_id,),
            )
        }
        for index, content in enumerate(contents, start=1):
            image_id = str(content["image_id"])
            if connection.execute(
                """
                SELECT 1 FROM image_preprocessing_observations
                WHERE run_id = ? AND image_id = ?
                """,
                (run_id, image_id),
            ).fetchone():
                counts["resumed"] += 1
                continue
            source_path = raw_root / Path(str(content["relative_path"]))
            legacy = legacy_by_sha.get(image_id)
            inspection = None
            working_asset_id = existing_assets.get(
                (image_id, "working_image_legacy")
            )
            alpha_asset_id = existing_assets.get(
                (image_id, "alpha_mask_legacy")
            )
            error: Exception | None = None
            try:
                if sha256_file(source_path) != image_id:
                    raise RuntimeError("source SHA256 no longer matches image_id")
                working, _alpha, inspection = load_oriented_working_image(
                    source_path, preprocessing
                )
                if not working_asset_id:
                    working_asset_id = _save_new_working_image(
                        workspace,
                        connection,
                        run_id,
                        image_id,
                        working,
                        preprocessing,
                    )
                decode_status = "ok"
                counts["ok"] += 1
                if inspection.is_long:
                    prepare_analysis_assets(
                        connection,
                        workspace,
                        run_id=run_id,
                        image_id=image_id,
                        source_path=source_path,
                        config=preprocessing,
                        decoded_working=working,
                        decoded_inspection=inspection,
                    )
                    counts["long"] += 1
            except ImagePolicyRejected as exc:
                decode_status = "policy_rejected"
                error = exc
                counts["policy_rejected"] += 1
            except Exception as exc:
                decode_status = "corrupt"
                error = exc
                counts["corrupt"] += 1
            quality = _quality_payload(legacy, inspection)
            if decode_status != "ok":
                quality["decode_error_type"] = type(error).__name__
                quality["decode_error_message"] = str(error)
            width = (
                inspection.oriented_width
                if inspection
                else int((legacy or {}).get("oriented_width") or 0) or None
            )
            height = (
                inspection.oriented_height
                if inspection
                else int((legacy or {}).get("oriented_height") or 0) or None
            )
            observation_id = stable_id(
                "preprocess_observation",
                run_id,
                image_id,
                preprocessing["implementation_version"],
            )
            transform_fingerprint = sha256_json(
                {
                    "implementation_version": preprocessing[
                        "implementation_version"
                    ],
                    "config": preprocessing,
                    "working_asset_id": working_asset_id,
                }
            )
            connection.execute(
                """
                INSERT INTO image_preprocessing_observations(
                    preprocess_observation_id, image_id, run_id,
                    decode_status, decode_recovered, source_format,
                    source_mode, width, height, frame_count, selected_frame,
                    exif_orientation, orientation_corrected, icc_status,
                    working_color_space, converted_to_srgb, has_alpha,
                    transparent_pixel_ratio, working_asset_id, alpha_asset_id,
                    quality_json, transform_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    image_id,
                    run_id,
                    decode_status,
                    0,
                    (
                        inspection.source_format
                        if inspection
                        else str(content["detected_format"])
                    ),
                    (
                        inspection.source_mode
                        if inspection
                        else str((legacy or {}).get("source_mode") or "")
                    ),
                    width,
                    height,
                    (
                        inspection.frame_count
                        if inspection
                        else int((legacy or {}).get("frame_count") or 1)
                    ),
                    (
                        inspection.selected_frame
                        if inspection
                        else int(preprocessing["selected_frame"])
                    ),
                    (
                        inspection.exif_orientation
                        if inspection
                        else int(
                            (legacy or {}).get("exif_orientation_value") or 1
                        )
                    ),
                    int(inspection.orientation_corrected) if inspection else 0,
                    (
                        inspection.icc_status
                        if inspection
                        else str(
                            (legacy or {}).get("color_profile_status") or ""
                        )
                    ),
                    (
                        inspection.working_color_space
                        if inspection
                        else str(
                            (legacy or {}).get("working_color_space") or ""
                        )
                    ),
                    int(inspection.converted_to_srgb) if inspection else 0,
                    int(inspection.has_alpha) if inspection else 0,
                    (
                        inspection.transparent_pixel_ratio
                        if inspection
                        else float(
                            (legacy or {}).get("transparent_pixel_ratio") or 0
                        )
                    ),
                    working_asset_id,
                    alpha_asset_id,
                    canonical_json(quality),
                    transform_fingerprint,
                    utc_now(),
                ),
            )
            _insert_quality_flags(
                connection,
                run_id,
                image_id,
                quality,
                str(thresholds["threshold_version"]),
            )
            if error is not None:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO pipeline_errors(
                        error_id, run_id, image_id, image_occurrence_id,
                        stage, error_code, error_type, message,
                        details_json, retryable, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id("err", run_id, image_id, decode_status),
                        run_id,
                        image_id,
                        "2",
                        decode_status,
                        type(error).__name__,
                        str(error),
                        "{}",
                        int(decode_status == "corrupt"),
                        utc_now(),
                    ),
                )
            if index % 100 == 0:
                connection.commit()
        connection.commit()

        observation_by_image = {
            row["image_id"]: row
            for row in connection.execute(
                """
                SELECT preprocess_observation_id, image_id
                FROM image_preprocessing_observations WHERE run_id = ?
                """,
                (run_id,),
            )
        }
        legacy_asset_by_image = defaultdict(dict)
        for row in connection.execute(
            """
            SELECT image_id, asset_type, derived_asset_id
            FROM derived_assets
            WHERE run_id = ? AND asset_type IN (
                'working_image_legacy', 'alpha_mask_legacy'
            )
            """,
            (run_id,),
        ):
            legacy_asset_by_image[row["image_id"]][
                row["asset_type"]
            ] = row["derived_asset_id"]
        occurrences = connection.execute(
            """
            SELECT occurrence.image_occurrence_id, occurrence.image_id,
                   occurrence.relative_path, occurrence.legacy_image_id
            FROM image_occurrences AS occurrence
            ORDER BY occurrence.image_occurrence_id
            """
        )
        links_inserted = 0
        for occurrence in occurrences:
            observation = observation_by_image.get(occurrence["image_id"])
            if not observation:
                continue
            legacy = legacy_by_path.get(occurrence["relative_path"])
            assets = legacy_asset_by_image[occurrence["image_id"]]
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO preprocessing_occurrence_links(
                    preprocess_occurrence_link_id, preprocess_observation_id,
                    image_occurrence_id, legacy_image_id,
                    legacy_metadata_row_hash, legacy_working_asset_id,
                    legacy_alpha_asset_id, migration_status, conflict_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id(
                        "preprocess_occurrence_link",
                        run_id,
                        occurrence["image_occurrence_id"],
                    ),
                    observation["preprocess_observation_id"],
                    occurrence["image_occurrence_id"],
                    occurrence["legacy_image_id"],
                    sha256_json(legacy) if legacy else None,
                    assets.get("working_image_legacy"),
                    assets.get("alpha_mask_legacy"),
                    "verified" if legacy else "legacy_metadata_missing",
                    "{}" if legacy else '{"missing_legacy_metadata":true}',
                    utc_now(),
                ),
            )
            links_inserted += max(0, cursor.rowcount)
        duplicate_edges = _insert_duplicate_edges(connection, settings, run_id)
        connection.commit()
        counts["occurrence_links_inserted"] = links_inserted
        counts["duplicate_edges_inserted"] = duplicate_edges

        summary = {
            "schema_version": "stage2-execution-summary-1",
            "run_id": run_id,
            "content_limit": limit,
            "counts": dict(sorted(counts.items())),
        }
        write_json_snapshot(
            run_dir / "reports",
            "stage2_execution_summary",
            summary,
        )
        return summary


def validate_stage2(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    full_source_hash_check: bool = True,
    finalize: bool = False,
) -> dict[str, Any]:
    run_dir = workspace.run_dir(run_id)
    raw_root = settings.project_path("raw_root")
    with open_database(workspace.database_path) as connection:
        content_status = {
            row["decode_status"]: row["count"]
            for row in connection.execute(
                """
                SELECT decode_status, COUNT(*) AS count
                FROM image_preprocessing_observations
                WHERE run_id = ? GROUP BY decode_status
                """,
                (run_id,),
            )
        }
        occurrence_status = {
            row["decode_status"]: row["count"]
            for row in connection.execute(
                """
                SELECT observation.decode_status, COUNT(*) AS count
                FROM preprocessing_occurrence_links AS link
                JOIN image_preprocessing_observations AS observation
                  ON observation.preprocess_observation_id =
                     link.preprocess_observation_id
                WHERE observation.run_id = ?
                GROUP BY observation.decode_status
                """,
                (run_id,),
            )
        }
        observation_count = sum(content_status.values())
        occurrence_count = sum(occurrence_status.values())
        mismatch_count = connection.execute(
            "SELECT COUNT(*) FROM image_occurrences WHERE extension_mismatch = 1"
        ).fetchone()[0]
        long_count = connection.execute(
            "SELECT COUNT(*) FROM long_image_layouts WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        tile_count = connection.execute(
            """
            SELECT COUNT(*) FROM image_tiles AS tile
            JOIN long_image_layouts AS layout
              ON layout.long_image_layout_id = tile.long_image_layout_id
            WHERE layout.run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        long_missing = connection.execute(
            """
            SELECT COUNT(*) FROM long_image_layouts AS layout
            WHERE layout.run_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM image_tiles AS tile
                  WHERE tile.long_image_layout_id = layout.long_image_layout_id
              )
            """,
            (run_id,),
        ).fetchone()[0]
        asset_missing = 0
        asset_hash_mismatch = 0
        for row in connection.execute(
            """
            SELECT relative_path, sha256 FROM derived_assets WHERE run_id = ?
            """,
            (run_id,),
        ):
            path = workspace.output_root / row["relative_path"]
            if not path.is_file():
                asset_missing += 1
            elif sha256_file(path) != row["sha256"]:
                asset_hash_mismatch += 1

        source_checked = 0
        source_hash_mismatch = 0
        if full_source_hash_check:
            for row in connection.execute(
                """
                SELECT image_id, relative_path
                FROM image_occurrences ORDER BY image_occurrence_id
                """
            ):
                source_checked += 1
                if sha256_file(raw_root / Path(row["relative_path"])) != row["image_id"]:
                    source_hash_mismatch += 1

        expected = {
            "content": {"ok": 12383, "corrupt": 2, "policy_rejected": 1},
            "occurrence": {
                "ok": 31508,
                "corrupt": 2,
                "policy_rejected": 1,
            },
        }
        hard_gates = {
            "content_count": observation_count == 12386,
            "occurrence_count": occurrence_count == 31511,
            "content_status": all(
                content_status.get(key, 0) == value
                for key, value in expected["content"].items()
            ),
            "occurrence_status": all(
                occurrence_status.get(key, 0) == value
                for key, value in expected["occurrence"].items()
            ),
            "format_mismatch": mismatch_count == 229,
            "source_sha": (
                source_checked == 31511 and source_hash_mismatch == 0
                if full_source_hash_check
                else True
            ),
            "assets": asset_missing == 0 and asset_hash_mismatch == 0,
            "long_assets": long_count > 0 and tile_count > 0 and long_missing == 0,
        }
        status = "passed" if all(hard_gates.values()) else "failed"
        summary = {
            "schema_version": "stage2-validation-1",
            "run_id": run_id,
            "status": status,
            "content_status": content_status,
            "occurrence_status": occurrence_status,
            "format_mismatch_occurrences": mismatch_count,
            "long_image_count": long_count,
            "tile_count": tile_count,
            "long_layouts_without_tiles": long_missing,
            "asset_missing": asset_missing,
            "asset_hash_mismatch": asset_hash_mismatch,
            "source_files_checked": source_checked,
            "source_hash_mismatch": source_hash_mismatch,
            "hard_gates": hard_gates,
        }
        write_json_snapshot(
            run_dir / "reports",
            "stage2_validation",
            summary,
        )
        export_tables_jsonl(connection, run_dir / "jsonl", STAGE2_TABLES)
        if finalize:
            finish_pipeline_run(
                connection,
                run_id,
                status="completed" if status == "passed" else "failed",
                error_summary={"validation_status": status},
            )
        return summary
