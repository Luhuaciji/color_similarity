"""Stage 1.5 deterministic sampling, asset preparation, fusion, and validation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .image_assets import (
    ImagePolicyRejected,
    ensure_vlm_compatible_asset,
    prepare_analysis_assets,
)
from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import sha256_file, stable_id
from .workspace import (
    Workspace,
    begin_pipeline_run,
    ensure_run_directory,
    export_tables_jsonl,
    finish_pipeline_run,
    open_database,
    source_fingerprint,
    utc_now,
    write_json_snapshot,
)


PILOT_TABLES = (
    "pilot_samples",
    "derived_assets",
    "long_image_layouts",
    "image_tiles",
    "model_runs",
    "content_visual_analyses",
    "occurrence_context_fusions",
    "context_review_sampling_policies",
    "context_review_sample_items",
    "pilot_gate_decisions",
    "owner_review_delegations",
    "pilot_sample_additions",
)

SELECTION_QUOTAS = (
    ("long_image", 8),
    ("format_mismatch", 8),
    ("duplicate_multi_occurrence", 12),
    ("folder_collision", 8),
    ("transparent", 6),
    ("gif", 6),
    ("semantic_invalid_candidate", 4),
)

CONTEXT_REVIEW_QUOTAS = (
    ("shade_conflict", 15),
    ("same_product_unspecified_shade", 10),
    ("contains_context_shade", 8),
    ("unrelated", 7),
)
CONTEXT_REVIEW_TARGET = sum(value for _, value in CONTEXT_REVIEW_QUOTAS)
CONTEXT_REVIEW_SEED = "stage1-5-context-review-v1"
CONTEXT_REVIEW_POLICY_VERSION = "stage1-5-stratified-context-review-1.0"


def _stable_rank(seed: str, label: str, image_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{label}\0{image_id}".encode()).hexdigest()


def _load_legacy_features(path: Path, config: Mapping[str, Any]) -> dict[str, dict]:
    result: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            image_id = str(row.get("sha256") or "")
            if not image_id:
                continue
            width = int(row.get("source_width") or 0)
            height = int(row.get("source_height") or 0)
            short_edge = min(width, height) if width and height else 0
            long_edge = max(width, height)
            aspect = long_edge / max(short_edge, 1)
            features = result.setdefault(
                image_id,
                {
                    "decode_success": bool(row.get("decode_success")),
                    "source_format": str(row.get("source_format") or ""),
                    "has_alpha": bool(row.get("has_alpha")),
                    "width": width,
                    "height": height,
                    "is_long": False,
                    "semantic_invalid_candidate": False,
                },
            )
            features["decode_success"] = (
                features["decode_success"] or bool(row.get("decode_success"))
            )
            features["has_alpha"] = features["has_alpha"] or bool(
                row.get("has_alpha")
            )
            features["is_long"] = features["is_long"] or (
                short_edge >= int(config["long_min_short_edge"])
                and long_edge >= int(config["long_min_edge"])
                and aspect >= float(config["long_aspect_ratio"])
            )
            features["semantic_invalid_candidate"] = features[
                "semantic_invalid_candidate"
            ] or (
                short_edge < int(config["semantic_invalid_short_edge"])
                or width * height < int(config["semantic_invalid_min_pixels"])
            )
    return result


def _content_candidates(
    connection: sqlite3.Connection,
    legacy_features: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            content.image_id,
            MIN(occurrence.relative_path) AS representative_relative_path,
            COUNT(DISTINCT occurrence.image_occurrence_id) AS occurrence_count,
            MAX(occurrence.extension_mismatch) AS extension_mismatch,
            MAX(CASE WHEN folder.collision_status = 'multi_source_record'
                THEN 1 ELSE 0 END) AS folder_collision
        FROM image_contents AS content
        JOIN image_occurrences AS occurrence
          ON occurrence.image_id = content.image_id
        JOIN folder_groups AS folder
          ON folder.folder_group_id = occurrence.folder_group_id
        GROUP BY content.image_id
        """
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        image_id = str(row["image_id"])
        features = legacy_features.get(image_id, {})
        if not features.get("decode_success"):
            continue
        tags: list[str] = []
        if features.get("is_long"):
            tags.append("long_image")
        if int(row["extension_mismatch"]):
            tags.append("format_mismatch")
        if int(row["occurrence_count"]) > 1:
            tags.append("duplicate_multi_occurrence")
        if int(row["folder_collision"]):
            tags.append("folder_collision")
        if features.get("has_alpha"):
            tags.append("transparent")
        if str(features.get("source_format")).upper() == "GIF":
            tags.append("gif")
        if features.get("semantic_invalid_candidate"):
            tags.append("semantic_invalid_candidate")
        candidates.append(
            {
                "image_id": image_id,
                "relative_path": row["representative_relative_path"],
                "occurrence_count": int(row["occurrence_count"]),
                "coverage_tags": tags,
            }
        )
    return candidates


def _sample_context(
    connection: sqlite3.Connection, image_id: str
) -> tuple[list[str], list[str]]:
    occurrence_ids = [
        row[0]
        for row in connection.execute(
            """
            SELECT image_occurrence_id
            FROM image_occurrences
            WHERE image_id = ?
            ORDER BY image_occurrence_id
            """,
            (image_id,),
        )
    ]
    source_record_ids = [
        row[0]
        for row in connection.execute(
            """
            SELECT DISTINCT record.source_record_id
            FROM image_occurrences AS occurrence
            JOIN source_ref_occurrences AS link
              ON link.image_occurrence_id = occurrence.image_occurrence_id
            JOIN source_image_refs AS ref
              ON ref.source_ref_id = link.source_ref_id
            JOIN source_records AS record
              ON record.source_record_id = ref.source_record_id
            WHERE occurrence.image_id = ?
            ORDER BY record.source_record_id
            """,
            (image_id,),
        )
    ]
    return occurrence_ids, source_record_ids


def select_pilot_samples(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    resume: bool = False,
    count: int | None = None,
) -> dict[str, Any]:
    pilot_config = settings.section("pilot")
    preprocessing = settings.section("preprocessing")
    target = int(count or pilot_config["initial_unique_images"])
    maximum = int(pilot_config["maximum_unique_images"])
    if not 50 <= target <= maximum <= 100:
        raise ValueError("Pilot selection must contain between 50 and 100 images")

    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    legacy_jsonl = (
        settings.project_path("legacy_output_root")
        / "metadata"
        / "image_preprocessing.jsonl"
    )
    if not legacy_jsonl.is_file():
        raise FileNotFoundError(f"legacy metadata not found: {legacy_jsonl}")
    features = _load_legacy_features(legacy_jsonl, preprocessing)
    seed = str(pilot_config["selection_seed"])

    with open_database(workspace.database_path) as connection:
        begin_pipeline_run(
            connection,
            workspace,
            settings,
            run_id=run_id,
            stage="1.5",
            resume=resume,
            extra_config={"pilot_count": target},
        )
        existing_count = connection.execute(
            "SELECT COUNT(*) FROM pilot_samples WHERE pilot_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        if existing_count:
            if existing_count != target:
                raise RuntimeError(
                    "resume rejected: frozen Pilot selection count changed"
                )
        else:
            candidates = _content_candidates(connection, features)
            selected: dict[str, dict[str, Any]] = {}
            for tag, quota in SELECTION_QUOTAS:
                tagged = [
                    candidate
                    for candidate in candidates
                    if tag in candidate["coverage_tags"]
                    and candidate["image_id"] not in selected
                ]
                tagged.sort(
                    key=lambda row: _stable_rank(seed, tag, row["image_id"])
                )
                for candidate in tagged[:quota]:
                    selected[candidate["image_id"]] = candidate
            remaining = [
                candidate
                for candidate in candidates
                if candidate["image_id"] not in selected
            ]
            remaining.sort(
                key=lambda row: _stable_rank(seed, "fill", row["image_id"])
            )
            for candidate in remaining:
                if len(selected) >= target:
                    break
                selected[candidate["image_id"]] = candidate
            if len(selected) != target:
                raise RuntimeError(
                    f"could only select {len(selected)} valid Pilot images"
                )

            for image_id, candidate in sorted(selected.items()):
                occurrence_ids, source_record_ids = _sample_context(
                    connection, image_id
                )
                tags = sorted(candidate["coverage_tags"])
                sample_id = stable_id("pilot_sample", run_id, image_id)
                connection.execute(
                    """
                    INSERT INTO pilot_samples(
                        pilot_sample_id, pilot_run_id, image_id,
                        selected_occurrence_ids_json,
                        selected_source_record_ids_json, coverage_tags_json,
                        selection_reason, human_review_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample_id,
                        run_id,
                        image_id,
                        canonical_json(occurrence_ids),
                        canonical_json(source_record_ids),
                        canonical_json(tags),
                        "deterministic_stratified_content_sample_v1",
                        "pending",
                        utc_now(),
                    ),
                )
            connection.commit()

        selection_text = "".join(
            canonical_json(dict(row)) + "\n"
            for row in connection.execute(
                """
                SELECT * FROM pilot_samples
                WHERE pilot_run_id = ?
                ORDER BY image_id
                """,
                (run_id,),
            )
        )
        selection_digest = hashlib.sha256(
            selection_text.encode("utf-8")
        ).hexdigest()
        selection_path = (
            run_dir / f"pilot_selection.{selection_digest[:16]}.jsonl"
        )
        if not selection_path.exists():
            selection_path.write_text(
                selection_text,
                encoding="utf-8",
                newline="\n",
            )
        tag_counts = Counter()
        for row in connection.execute(
            """
            SELECT coverage_tags_json FROM pilot_samples
            WHERE pilot_run_id = ?
            """,
            (run_id,),
        ):
            tag_counts.update(json.loads(row[0]))
        summary = {
            "schema_version": "pilot-selection-summary-1",
            "run_id": run_id,
            "unique_image_count": existing_count or target,
            "coverage_tag_counts": dict(sorted(tag_counts.items())),
            "selection_sha256": sha256_file(selection_path),
            "status": "frozen_before_online_calls",
        }
        write_json_snapshot(
            run_dir / "reports",
            "selection_summary",
            summary,
        )
        return summary


def add_pilot_topup_sample(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    image_id: str,
    delegation_scope: str,
    delegated_agent: str,
    owner_instruction: str,
    selection_method: str,
    selection_version: str,
    selection_reason: str,
    candidate_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one explicitly authorized, image-only Pilot top-up sample.

    The initial deterministic selection manifest remains immutable.  This
    operation writes a separate addition ledger and owner-delegation record,
    and then prepares only the newly selected content.
    """

    image_id = image_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", image_id):
        raise ValueError("image_id must be a lowercase SHA256")
    required_text = {
        "delegation_scope": delegation_scope,
        "delegated_agent": delegated_agent,
        "owner_instruction": owner_instruction,
        "selection_method": selection_method,
        "selection_version": selection_version,
        "selection_reason": selection_reason,
    }
    if any(not value.strip() for value in required_text.values()):
        raise ValueError("top-up audit text fields must not be empty")
    allowed_evidence_keys = {
        "retrieval_method",
        "retrieval_version",
        "score",
        "gallery_index",
        "visual_basis",
        "source_sha256",
    }
    unexpected_keys = sorted(set(candidate_evidence) - allowed_evidence_keys)
    if unexpected_keys:
        raise ValueError(
            "candidate evidence is image-only and may not contain keys: "
            + ", ".join(unexpected_keys)
        )
    if candidate_evidence.get("source_sha256") not in {None, image_id}:
        raise ValueError("candidate evidence source_sha256 does not match image_id")

    legacy_jsonl = (
        settings.project_path("legacy_output_root")
        / "metadata"
        / "image_preprocessing.jsonl"
    )
    if not legacy_jsonl.is_file():
        raise FileNotFoundError(f"legacy metadata not found: {legacy_jsonl}")
    features = _load_legacy_features(
        legacy_jsonl,
        settings.section("preprocessing"),
    )
    run_dir = ensure_run_directory(workspace, run_id, resume=True)
    created_at = utc_now()

    with open_database(workspace.database_path) as connection:
        run = connection.execute(
            """
            SELECT stage, status, config_hash
            FROM pipeline_runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run is None or run["stage"] != "1.5":
            raise ValueError("top-up target must be an existing Stage 1.5 run")
        if run["status"] not in {
            "running",
            "awaiting_human_review",
            "awaiting_human_go",
        }:
            raise ValueError("top-up is forbidden after the Pilot is finalized")
        if connection.execute(
            "SELECT 1 FROM pilot_gate_decisions WHERE pilot_run_id = ?",
            (run_id,),
        ).fetchone():
            raise ValueError("top-up is forbidden after a Pilot gate decision")

        candidates = {
            candidate["image_id"]: candidate
            for candidate in _content_candidates(connection, features)
        }
        candidate = candidates.get(image_id)
        if candidate is None:
            raise ValueError("top-up image is not a strictly decodable content candidate")
        existing = connection.execute(
            """
            SELECT addition.*, delegation.scope, delegation.delegated_agent
            FROM pilot_sample_additions AS addition
            JOIN owner_review_delegations AS delegation
              ON delegation.owner_review_delegation_id =
                 addition.owner_review_delegation_id
            WHERE addition.pilot_run_id = ? AND addition.image_id = ?
            """,
            (run_id, image_id),
        ).fetchone()
        current_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM pilot_samples WHERE pilot_run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        count_before = current_count - (1 if existing is not None else 0)
        maximum = int(settings.section("pilot")["maximum_unique_images"])
        if existing is None and current_count >= maximum:
            raise ValueError("Pilot top-up would exceed the configured 100-image cap")

        delegation_evidence = {
            "schema_version": "owner-review-delegation-evidence-1",
            "authorized_image_ids": [image_id],
            "authorized_annotation_types": ["role", "eligibility"],
            "authorized_role_codes": ["color_card"],
            "candidate_evidence": dict(candidate_evidence),
            "context_fields_used_for_role_decision": [],
        }
        delegation_id = stable_id(
            "owner_review_delegation",
            run_id,
            delegation_scope,
            delegated_agent,
            owner_instruction,
            sha256_json(delegation_evidence),
        )
        addition_id = stable_id("pilot_sample_addition", run_id, image_id)
        if existing is not None:
            if (
                existing["owner_review_delegation_id"] != delegation_id
                or existing["selection_method"] != selection_method
                or existing["selection_version"] != selection_version
                or existing["selection_reason"] != selection_reason
                or json.loads(existing["candidate_evidence_json"])
                != dict(candidate_evidence)
            ):
                raise RuntimeError(
                    "an immutable top-up already exists with different evidence"
                )
            reused = True
        else:
            occurrence_ids, source_record_ids = _sample_context(
                connection,
                image_id,
            )
            connection.execute(
                """
                INSERT INTO owner_review_delegations(
                    owner_review_delegation_id, pilot_run_id, scope,
                    instruction_text, delegated_agent, evidence_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delegation_id,
                    run_id,
                    delegation_scope,
                    owner_instruction,
                    delegated_agent,
                    canonical_json(delegation_evidence),
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO pilot_samples(
                    pilot_sample_id, pilot_run_id, image_id,
                    selected_occurrence_ids_json,
                    selected_source_record_ids_json, coverage_tags_json,
                    selection_reason, human_review_status, created_at,
                    review_provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id("pilot_sample", run_id, image_id),
                    run_id,
                    image_id,
                    canonical_json(occurrence_ids),
                    canonical_json(source_record_ids),
                    canonical_json(sorted(candidate["coverage_tags"])),
                    selection_reason,
                    "pending",
                    created_at,
                    "owner_delegated_agent",
                ),
            )
            connection.execute(
                """
                INSERT INTO pilot_sample_additions(
                    pilot_sample_addition_id, pilot_run_id, image_id,
                    selection_method, selection_version, selection_reason,
                    candidate_evidence_json, owner_review_delegation_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    addition_id,
                    run_id,
                    image_id,
                    selection_method,
                    selection_version,
                    selection_reason,
                    canonical_json(dict(candidate_evidence)),
                    delegation_id,
                    created_at,
                ),
            )
            connection.commit()
            reused = False
        count_after = int(
            connection.execute(
                "SELECT COUNT(*) FROM pilot_samples WHERE pilot_run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        original_config_hash = str(run["config_hash"])

    topup_summary = {
        "schema_version": "pilot-topup-summary-1",
        "run_id": run_id,
        "image_id": image_id,
        "count_before": count_before,
        "count_after": count_after,
        "selection_method": selection_method,
        "selection_version": selection_version,
        "selection_reason": selection_reason,
        "candidate_evidence": dict(candidate_evidence),
        "owner_review_delegation_id": delegation_id,
        "delegation_scope": delegation_scope,
        "delegated_agent": delegated_agent,
        "review_provenance": "owner_delegated_agent",
        "context_fields_used_for_role_decision": [],
        "initial_run_config_hash": original_config_hash,
        "topup_code_fingerprint": source_fingerprint(workspace.repo_root),
        "initial_selection_manifest_modified": False,
        "reused": reused,
    }
    write_json_snapshot(run_dir / "reports", "pilot_topup", topup_summary)
    preparation = prepare_pilot_assets(
        workspace,
        settings,
        run_id=run_id,
        resume=True,
        image_id=image_id,
        verify_run_fingerprint=False,
    )
    return {**topup_summary, "asset_preparation": preparation}


def prepare_pilot_assets(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    resume: bool = True,
    image_id: str | None = None,
    limit: int | None = None,
    verify_run_fingerprint: bool = True,
) -> dict[str, Any]:
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    raw_root = settings.project_path("raw_root")
    preprocessing = settings.section("preprocessing")
    prepared = 0
    failed = 0
    source_hash_mismatch = 0
    vlm_compatibility_assets = 0
    manifest_rows: list[dict[str, Any]] = []

    with open_database(workspace.database_path) as connection:
        if verify_run_fingerprint:
            begin_pipeline_run(
                connection,
                workspace,
                settings,
                run_id=run_id,
                stage="1.5",
                resume=True,
                extra_config={
                    "pilot_count": connection.execute(
                        """
                        SELECT COUNT(*) FROM pilot_samples
                        WHERE pilot_run_id = ?
                        """,
                        (run_id,),
                    ).fetchone()[0]
                },
            )
        samples = list(
            connection.execute(
                """
                SELECT sample.image_id, MIN(occurrence.relative_path) AS relative_path
                FROM pilot_samples AS sample
                JOIN image_occurrences AS occurrence
                  ON occurrence.image_id = sample.image_id
                WHERE sample.pilot_run_id = ?
                GROUP BY sample.image_id
                ORDER BY sample.image_id
                """,
                (run_id,),
            )
        )
        if image_id is not None:
            samples = [row for row in samples if row["image_id"] == image_id]
            if not samples:
                raise KeyError("requested image_id is not in this Pilot")
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            samples = samples[:limit]
        for row in samples:
            image_id = str(row["image_id"])
            source_path = raw_root / Path(str(row["relative_path"]))
            try:
                if sha256_file(source_path) != image_id:
                    source_hash_mismatch += 1
                    raise RuntimeError("source SHA256 no longer matches image_id")
                content = prepare_analysis_assets(
                    connection,
                    workspace,
                    run_id=run_id,
                    image_id=image_id,
                    source_path=source_path,
                    config=preprocessing,
                )
                analysis_assets = list(content.analysis_assets)
                for source_asset in tuple(analysis_assets):
                    if source_asset.asset_type != "analysis_preview":
                        continue
                    compatible = ensure_vlm_compatible_asset(
                        connection,
                        workspace,
                        run_id=run_id,
                        source_asset=source_asset,
                    )
                    if compatible is not None:
                        analysis_assets.append(compatible)
                        vlm_compatibility_assets += 1
                prepared += 1
                manifest_rows.append(
                    {
                        "schema_version": "pilot-asset-manifest-1",
                        "image_id": image_id,
                        "is_long": content.inspection.is_long,
                        "source_format": content.inspection.source_format,
                        "format_mismatch": content.inspection.format_mismatch,
                        "semantic_invalid_candidate": (
                            content.inspection.is_semantic_invalid_candidate
                        ),
                        "long_image_layout_id": content.long_image_layout_id,
                        "assets": [
                            {
                                "derived_asset_id": asset.derived_asset_id,
                                "asset_type": asset.asset_type,
                                "relative_path": asset.relative_path,
                                "sha256": asset.sha256,
                                "width": asset.width,
                                "height": asset.height,
                                "metadata": asset.metadata,
                            }
                            for asset in analysis_assets
                        ],
                    }
                )
            except Exception as exc:
                failed += 1
                error_id = stable_id(
                    "err", run_id, image_id, "pilot_asset_preparation"
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO pipeline_errors(
                        error_id, run_id, image_id, image_occurrence_id,
                        stage, error_code, error_type, message, details_json,
                        retryable, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        error_id,
                        run_id,
                        image_id,
                        "1.5",
                        "pilot_asset_preparation_failed",
                        type(exc).__name__,
                        str(exc),
                        "{}",
                        0,
                        utc_now(),
                    ),
                )
                connection.commit()
        manifest_text = "".join(
            canonical_json(row) + "\n"
            for row in sorted(manifest_rows, key=lambda item: item["image_id"])
        )
        manifest_digest = hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        manifest_path = (
            run_dir / f"pilot_asset_manifest.{manifest_digest[:16]}.jsonl"
        )
        if manifest_path.exists():
            if manifest_path.read_text(encoding="utf-8") != manifest_text:
                raise FileExistsError("immutable Pilot asset manifest changed")
        else:
            manifest_path.write_text(
                manifest_text,
                encoding="utf-8",
                newline="\n",
            )
        summary = {
            "schema_version": "pilot-asset-summary-1",
            "run_id": run_id,
            "selected": len(samples),
            "prepared": prepared,
            "failed": failed,
            "source_hash_mismatch": source_hash_mismatch,
            "vlm_compatibility_assets": vlm_compatibility_assets,
            "asset_manifest_sha256": sha256_file(manifest_path),
        }
        summary_path = (
            run_dir
            / "reports"
            / f"asset_preparation_summary.{sha256_json(summary)[:16]}.json"
        )
        if not summary_path.exists():
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return summary


_SHADE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{0,3}[- ]?\d{1,4}[A-Za-z]?)(?![A-Za-z0-9])"
)
_CAPACITY = re.compile(r"^\d+(?:\.\d+)?(?:g|ml)$", re.IGNORECASE)


def _normalize_shade(value: str) -> str:
    return re.sub(r"[\s\-_]+", "", value).casefold()


def _context_shades(*values: str) -> list[str]:
    candidates: list[str] = []
    for value in values:
        if value and not _CAPACITY.fullmatch(value.strip()):
            if len(value.strip()) <= 20:
                candidates.append(value.strip())
        candidates.extend(match.group(1) for match in _SHADE_TOKEN.finditer(value or ""))
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_shade(candidate)
        if not normalized or _CAPACITY.fullmatch(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        result.append(candidate)
    return result


def fuse_pilot_context(
    workspace: Workspace,
    *,
    run_id: str,
    fusion_version: str = "deterministic-context-fusion-1.0",
    image_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    inserted = 0
    relationships: Counter[str] = Counter()
    with open_database(workspace.database_path) as connection:
        analyses = {
            row["image_id"]: row
            for row in connection.execute(
                """
                SELECT analysis.*
                FROM content_visual_analyses AS analysis
                JOIN (
                    SELECT image_id,
                           MAX(CASE analysis_scope
                               WHEN 'merged_content_summary' THEN 3
                               WHEN 'image' THEN 2
                               WHEN 'global_thumbnail' THEN 1
                               ELSE 0 END) AS preference
                    FROM content_visual_analyses
                    WHERE run_id = ?
                    GROUP BY image_id
                ) AS chosen ON chosen.image_id = analysis.image_id
                WHERE analysis.run_id = ?
                  AND CASE analysis.analysis_scope
                      WHEN 'merged_content_summary' THEN 3
                      WHEN 'image' THEN 2
                      WHEN 'global_thumbnail' THEN 1
                      ELSE 0 END = chosen.preference
                """,
                (run_id, run_id),
            )
        }
        context_rows = list(
            connection.execute(
            """
            SELECT
                occurrence.image_id,
                occurrence.image_occurrence_id,
                occurrence.folder_group_id,
                occurrence.brand_folder_raw,
                occurrence.product_folder_raw,
                ref.source_ref_id,
                ref.source_field,
                ref.image_index,
                record.source_record_id,
                record.sku_id_raw,
                record.sku_name_raw,
                record.sku_concat_name_raw,
                record.sku_color_no_raw,
                record.raw_record_json
            FROM pilot_samples AS sample
            JOIN image_occurrences AS occurrence
              ON occurrence.image_id = sample.image_id
            JOIN source_ref_occurrences AS link
              ON link.image_occurrence_id = occurrence.image_occurrence_id
            JOIN source_image_refs AS ref
              ON ref.source_ref_id = link.source_ref_id
            JOIN source_records AS record
              ON record.source_record_id = ref.source_record_id
            WHERE sample.pilot_run_id = ?
            ORDER BY occurrence.image_occurrence_id, ref.source_ref_id
            """,
            (run_id,),
            )
        )
        if image_id is not None:
            context_rows = [
                row for row in context_rows if row["image_id"] == image_id
            ]
            if not context_rows:
                raise KeyError("requested image_id has no Pilot context")
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be positive")
            selected_images = {
                row["image_id"]
                for row in context_rows
            }
            selected_images = set(sorted(selected_images)[:limit])
            context_rows = [
                row
                for row in context_rows
                if row["image_id"] in selected_images
            ]
        for context in context_rows:
            analysis = analyses.get(context["image_id"])
            if analysis is None:
                continue
            depicted = json.loads(analysis["depicted_shades_json"])
            depicted_strings = [
                str(value.get("shade", ""))
                if isinstance(value, dict)
                else str(value)
                for value in depicted
            ]
            context_values = _context_shades(
                str(context["sku_color_no_raw"]),
                str(context["sku_name_raw"]),
                str(context["sku_concat_name_raw"]),
                str(context["product_folder_raw"]),
            )
            depicted_norm = {_normalize_shade(value) for value in depicted_strings if value}
            context_norm = {_normalize_shade(value) for value in context_values if value}
            overlap = depicted_norm & context_norm
            conflicts: list[str] = []
            if analysis["primary_role"] == "invalid":
                relationship = "unrelated"
                confidence = 0.75
            elif not context_norm:
                relationship = "insufficient_evidence"
                confidence = 0.35
                conflicts.append("no_reliable_context_shade")
            elif not depicted_norm:
                relationship = "same_product_unspecified_shade"
                confidence = 0.55
            elif overlap and (
                bool(analysis["contains_multiple_shades"]) or len(depicted_norm) > 1
            ):
                relationship = "contains_context_shade"
                confidence = 0.85
            elif overlap:
                relationship = "exact_shade_match"
                confidence = 0.9
            else:
                relationship = "shade_conflict"
                confidence = 0.65
                conflicts.append("explicit_depicted_shade_not_in_context")
            fusion_id = stable_id(
                "context_fusion",
                run_id,
                context["image_occurrence_id"],
                context["source_record_id"],
                context["source_ref_id"],
                analysis["content_visual_analysis_id"],
                fusion_version,
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO occurrence_context_fusions(
                    occurrence_context_fusion_id, run_id,
                    image_occurrence_id, source_record_id, source_ref_id,
                    folder_group_id, content_visual_analysis_id,
                    source_sku_id_raw, folder_context_json, csv_context_json,
                    context_shade_json, depicted_shades_json,
                    relationship_to_context, context_conflicts_json,
                    fusion_method, fusion_version, confidence,
                    review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fusion_id,
                    run_id,
                    context["image_occurrence_id"],
                    context["source_record_id"],
                    context["source_ref_id"],
                    context["folder_group_id"],
                    analysis["content_visual_analysis_id"],
                    context["sku_id_raw"],
                    canonical_json(
                        {
                            "brand_folder_raw": context["brand_folder_raw"],
                            "product_folder_raw": context["product_folder_raw"],
                        }
                    ),
                    canonical_json(
                        {
                            "sku_name_raw": context["sku_name_raw"],
                            "sku_concat_name_raw": context["sku_concat_name_raw"],
                            "source_field": context["source_field"],
                            "image_index": context["image_index"],
                        }
                    ),
                    canonical_json(
                        {
                            "raw_sku_color_no": context["sku_color_no_raw"],
                            "candidates": context_values,
                        }
                    ),
                    canonical_json(depicted_strings),
                    relationship,
                    canonical_json(conflicts),
                    "deterministic_normalized_token_overlap",
                    fusion_version,
                    confidence,
                    "pending" if conflicts else "unreviewed",
                    utc_now(),
                ),
            )
            if cursor.rowcount:
                inserted += 1
                relationships[relationship] += 1
        connection.commit()
    return {
        "schema_version": "pilot-context-fusion-summary-1",
        "run_id": run_id,
        "inserted": inserted,
        "relationships": dict(sorted(relationships.items())),
        "fusion_version": fusion_version,
    }


def create_context_review_sample(
    workspace: Workspace,
    *,
    run_id: str,
) -> dict[str, Any]:
    """Freeze a deterministic 40-item, relation-stratified B-layer audit."""

    run_dir = workspace.run_dir(run_id)
    with open_database(workspace.database_path) as connection:
        annotation_set = connection.execute(
            """
            SELECT annotation_set_id
            FROM annotation_sets
            WHERE run_id = ? AND purpose = 'pilot_review'
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if annotation_set is None:
            raise RuntimeError("Pilot review set must be created before sampling")
        annotation_set_id = annotation_set["annotation_set_id"]
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in connection.execute(
            """
            SELECT item.annotation_item_id, item.status,
                   fusion.relationship_to_context,
                   fusion.confidence
            FROM annotation_items AS item
            JOIN occurrence_context_fusions AS fusion
              ON fusion.run_id = ?
             AND fusion.image_occurrence_id =
                 item.image_occurrence_id
            WHERE item.annotation_set_id = ?
              AND item.content_context_visibility = 'occurrence_context'
            ORDER BY item.annotation_item_id,
                     fusion.occurrence_context_fusion_id
            """,
            (run_id, annotation_set_id),
        ):
            grouped[row["annotation_item_id"]].append(row)
        if not grouped:
            raise RuntimeError("Pilot has no occurrence-context items to sample")

        population: list[dict[str, Any]] = []
        for annotation_item_id, rows in sorted(grouped.items()):
            relationships = {
                str(row["relationship_to_context"]) for row in rows
            }
            if len(relationships) != 1:
                raise RuntimeError(
                    "one occurrence has conflicting machine relationships; "
                    "a multi-context sampling policy is required"
                )
            population.append(
                {
                    "annotation_item_id": annotation_item_id,
                    "status": rows[0]["status"],
                    "model_relationship": next(iter(relationships)),
                    "model_confidence": min(
                        float(row["confidence"]) for row in rows
                    ),
                }
            )
        population_text = "".join(
            canonical_json(
                {
                    "annotation_item_id": row["annotation_item_id"],
                    "model_relationship": row["model_relationship"],
                    "model_confidence": row["model_confidence"],
                }
            )
            + "\n"
            for row in population
        )
        population_sha = hashlib.sha256(
            population_text.encode("utf-8")
        ).hexdigest()
        relation_counts = Counter(
            row["model_relationship"] for row in population
        )
        policy_version = (
            f"{CONTEXT_REVIEW_POLICY_VERSION}+{population_sha[:12]}"
        )
        policy_id = stable_id(
            "context_review_policy",
            run_id,
            policy_version,
            population_sha,
        )
        existing = connection.execute(
            """
            SELECT * FROM context_review_sampling_policies
            WHERE context_review_sampling_policy_id = ?
            """,
            (policy_id,),
        ).fetchone()
        if existing is not None:
            selected_rows = list(
                connection.execute(
                    """
                    SELECT sample.annotation_item_id,
                           sample.model_relationship,
                           sample.model_confidence,
                           sample.selected_reason,
                           item.status
                    FROM context_review_sample_items AS sample
                    JOIN annotation_items AS item
                      ON item.annotation_item_id =
                         sample.annotation_item_id
                    WHERE sample.context_review_sampling_policy_id = ?
                    ORDER BY sample.annotation_item_id
                    """,
                    (policy_id,),
                )
            )
            return _context_review_sample_summary(
                run_id=run_id,
                policy_id=policy_id,
                policy_version=policy_version,
                population_sha=population_sha,
                relation_counts=relation_counts,
                selected_rows=selected_rows,
                reused=True,
            )

        selected: dict[str, dict[str, Any]] = {}
        selected_counts: Counter[str] = Counter()
        for row in population:
            if row["status"] != "approved":
                continue
            selected[row["annotation_item_id"]] = {
                **row,
                "selected_reason": "preexisting_human_review",
                "deterministic_rank": _stable_rank(
                    CONTEXT_REVIEW_SEED,
                    row["model_relationship"],
                    row["annotation_item_id"],
                ),
            }
            selected_counts[row["model_relationship"]] += 1
        if len(selected) > CONTEXT_REVIEW_TARGET:
            raise RuntimeError(
                "preexisting context reviews exceed the sampling target"
            )

        for relationship, quota in CONTEXT_REVIEW_QUOTAS:
            candidates = [
                row
                for row in population
                if row["model_relationship"] == relationship
                and row["annotation_item_id"] not in selected
            ]
            candidates.sort(
                key=lambda row: _stable_rank(
                    CONTEXT_REVIEW_SEED,
                    relationship,
                    row["annotation_item_id"],
                )
            )
            needed = max(0, quota - selected_counts[relationship])
            if len(candidates) < needed:
                raise RuntimeError(
                    f"sampling stratum {relationship} has only "
                    f"{len(candidates)} remaining candidates; needs {needed}"
                )
            for row in candidates[:needed]:
                selected[row["annotation_item_id"]] = {
                    **row,
                    "selected_reason": "deterministic_stratified_sample",
                    "deterministic_rank": _stable_rank(
                        CONTEXT_REVIEW_SEED,
                        relationship,
                        row["annotation_item_id"],
                    ),
                }
                selected_counts[relationship] += 1
        if len(selected) != CONTEXT_REVIEW_TARGET:
            raise RuntimeError(
                "context review sampling did not produce exactly "
                f"{CONTEXT_REVIEW_TARGET} items"
            )
        selection_text = "".join(
            canonical_json(
                {
                    key: row[key]
                    for key in (
                        "annotation_item_id",
                        "model_relationship",
                        "model_confidence",
                        "selected_reason",
                        "deterministic_rank",
                    )
                }
            )
            + "\n"
            for row in sorted(
                selected.values(),
                key=lambda value: value["annotation_item_id"],
            )
        )
        selection_sha = hashlib.sha256(
            selection_text.encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            INSERT INTO context_review_sampling_policies(
                context_review_sampling_policy_id, pilot_run_id,
                annotation_set_id, policy_version, selection_seed,
                target_count, quotas_json, source_relation_counts_json,
                source_population_sha256, selection_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                run_id,
                annotation_set_id,
                policy_version,
                CONTEXT_REVIEW_SEED,
                CONTEXT_REVIEW_TARGET,
                canonical_json(dict(CONTEXT_REVIEW_QUOTAS)),
                canonical_json(dict(sorted(relation_counts.items()))),
                population_sha,
                selection_sha,
                utc_now(),
            ),
        )
        for row in sorted(
            selected.values(),
            key=lambda value: value["annotation_item_id"],
        ):
            connection.execute(
                """
                INSERT INTO context_review_sample_items(
                    context_review_sampling_policy_id,
                    annotation_item_id, model_relationship,
                    model_confidence, selected_reason,
                    deterministic_rank, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    row["annotation_item_id"],
                    row["model_relationship"],
                    row["model_confidence"],
                    row["selected_reason"],
                    row["deterministic_rank"],
                    utc_now(),
                ),
            )
        connection.execute(
            """
            UPDATE occurrence_context_fusions
            SET review_status = 'machine_prelabel_unreviewed'
            WHERE run_id = ?
            """,
            (run_id,),
        )
        selected_item_ids = sorted(selected)
        placeholders = ",".join("?" for _ in selected_item_ids)
        connection.execute(
            f"""
            UPDATE occurrence_context_fusions
            SET review_status = 'pending_human_sample'
            WHERE run_id = ?
              AND image_occurrence_id IN (
                  SELECT image_occurrence_id
                  FROM annotation_items
                  WHERE annotation_item_id IN ({placeholders})
                    AND status <> 'approved'
              )
            """,
            (run_id, *selected_item_ids),
        )
        connection.execute(
            f"""
            UPDATE occurrence_context_fusions
            SET review_status = 'approved'
            WHERE run_id = ?
              AND image_occurrence_id IN (
                  SELECT image_occurrence_id
                  FROM annotation_items
                  WHERE annotation_item_id IN ({placeholders})
                    AND status = 'approved'
              )
            """,
            (run_id, *selected_item_ids),
        )
        connection.commit()
        selected_rows = list(
            connection.execute(
                """
                SELECT sample.annotation_item_id,
                       sample.model_relationship,
                       sample.model_confidence,
                       sample.selected_reason,
                       item.status
                FROM context_review_sample_items AS sample
                JOIN annotation_items AS item
                  ON item.annotation_item_id =
                     sample.annotation_item_id
                WHERE sample.context_review_sampling_policy_id = ?
                ORDER BY sample.annotation_item_id
                """,
                (policy_id,),
            )
        )
        summary = _context_review_sample_summary(
            run_id=run_id,
            policy_id=policy_id,
            policy_version=policy_version,
            population_sha=population_sha,
            relation_counts=relation_counts,
            selected_rows=selected_rows,
            reused=False,
        )
        write_json_snapshot(
            run_dir / "reports",
            "context_review_sampling",
            summary,
        )
        export_tables_jsonl(
            connection,
            run_dir / "jsonl",
            (
                "context_review_sampling_policies",
                "context_review_sample_items",
                "pilot_gate_decisions",
            ),
        )
        return summary


def _context_review_sample_summary(
    *,
    run_id: str,
    policy_id: str,
    policy_version: str,
    population_sha: str,
    relation_counts: Mapping[str, int],
    selected_rows: Iterable[Mapping[str, Any]],
    reused: bool,
) -> dict[str, Any]:
    selected = list(selected_rows)
    sample_counts = Counter(
        row["model_relationship"] for row in selected
    )
    status_counts = Counter(row["status"] for row in selected)
    return {
        "schema_version": "context-review-sampling-1",
        "run_id": run_id,
        "context_review_sampling_policy_id": policy_id,
        "policy_version": policy_version,
        "selection_seed": CONTEXT_REVIEW_SEED,
        "population_count": sum(relation_counts.values()),
        "source_relation_counts": dict(sorted(relation_counts.items())),
        "source_population_sha256": population_sha,
        "target_count": CONTEXT_REVIEW_TARGET,
        "quotas": dict(CONTEXT_REVIEW_QUOTAS),
        "sample_counts": dict(sorted(sample_counts.items())),
        "sample_status_counts": dict(sorted(status_counts.items())),
        "reused": reused,
        "unselected_semantics": "machine_prelabel_unreviewed",
        "status": (
            "awaiting_human_sample_review"
            if status_counts.get("approved", 0) < CONTEXT_REVIEW_TARGET
            else "sample_review_complete"
        ),
    }


def validate_pilot(
    workspace: Workspace,
    *,
    run_id: str,
    finalize: bool = False,
    approved_by: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    if decision is not None and decision not in {"go", "no_go"}:
        raise ValueError("decision must be go or no_go")
    if (decision is None) != (approved_by is None):
        raise ValueError("decision and approved_by must be provided together")
    if decision is not None and not finalize:
        raise ValueError("an explicit gate decision requires finalize=True")
    run_dir = workspace.run_dir(run_id)
    with open_database(workspace.database_path) as connection:
        sample_count = connection.execute(
            "SELECT COUNT(*) FROM pilot_samples WHERE pilot_run_id = ?",
            (run_id,),
        ).fetchone()[0]
        tags: Counter[str] = Counter()
        human_status: Counter[str] = Counter()
        for row in connection.execute(
            """
            SELECT coverage_tags_json, human_review_status
            FROM pilot_samples WHERE pilot_run_id = ?
            """,
            (run_id,),
        ):
            tags.update(json.loads(row["coverage_tags_json"]))
            human_status[row["human_review_status"]] += 1
        model_count = connection.execute(
            "SELECT COUNT(*) FROM model_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        model_artifact_missing = 0
        for row in connection.execute(
            """
            SELECT request_path, raw_response_path, parsed_response_path, status
            FROM model_runs WHERE run_id = ?
            """,
            (run_id,),
        ):
            required = [row["request_path"]]
            if row["status"] in {
                "succeeded",
                "cache_hit",
                "local_repair_succeeded",
                "schema_failed",
            }:
                required.append(row["raw_response_path"])
            if row["status"] in {
                "succeeded",
                "cache_hit",
                "local_repair_succeeded",
            }:
                required.append(row["parsed_response_path"])
            for relative in required:
                if not relative or not (run_dir / relative).is_file():
                    model_artifact_missing += 1
        duplicate_paid_success = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT cache_key
                FROM model_runs
                WHERE status = 'succeeded'
                GROUP BY cache_key
                HAVING COUNT(*) > 1
            )
            """,
        ).fetchone()[0]
        analysis_count = connection.execute(
            """
            SELECT COUNT(DISTINCT image_id)
            FROM content_visual_analyses WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        model_role_counts = {
            row["primary_role"]: row["count"]
            for row in connection.execute(
                """
                SELECT primary_role, COUNT(*) AS count
                FROM content_visual_analyses
                WHERE run_id = ? AND analysis_scope IN (
                    'image', 'merged_content_summary'
                )
                GROUP BY primary_role
                """,
                (run_id,),
            )
        }
        reviewed_role_counts: Counter[str] = Counter()
        human_role_counts: Counter[str] = Counter()
        owner_delegated_role_counts: Counter[str] = Counter()
        owner_review_delegation_ids: set[str] = set()
        invalid_review_provenance_count = 0
        pilot_review_set = connection.execute(
            """
            SELECT annotation_set_id
            FROM annotation_sets
            WHERE run_id = ? AND purpose = 'pilot_review'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        reviewed_content_items = 0
        occurrence_population_total = 0
        occurrence_review_total = 0
        occurrence_review_approved = 0
        human_occurrence_relations: Counter[str] = Counter()
        context_policy_id: str | None = None
        context_policy_version: str | None = None
        context_sample_confusion: dict[str, Counter[str]] = defaultdict(Counter)
        context_sample_agreements = 0
        if pilot_review_set is not None:
            annotation_set_id = pilot_review_set["annotation_set_id"]
            content_items = list(
                connection.execute(
                    """
                    SELECT annotation_item_id, image_id, status
                    FROM annotation_items
                    WHERE annotation_set_id = ?
                      AND content_context_visibility = 'image_only'
                    """,
                    (annotation_set_id,),
                )
            )
            for item in content_items:
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
                valid_role_codes = {
                    "single_bullet",
                    "single_swatch",
                    "lip_effect",
                    "multi_shade_comparison",
                    "color_card",
                    "packaging",
                    "text_promo",
                    "invalid",
                }
                if (
                    role_event is None
                    or eligibility_event is None
                    or role_event["role_code"] not in valid_role_codes
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
                            (delegation_id, run_id),
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
                        if valid_provenance:
                            owner_review_delegation_ids.add(
                                str(delegation_id)
                            )
                else:
                    valid_provenance = False
                if not valid_provenance:
                    invalid_review_provenance_count += 1
                    continue
                reviewed_content_items += 1
                reviewed_role_counts[role_event["role_code"]] += 1
                if provenance == "human":
                    human_role_counts[role_event["role_code"]] += 1
                else:
                    owner_delegated_role_counts[
                        role_event["role_code"]
                    ] += 1
            occurrence_population_total = connection.execute(
                """
                SELECT COUNT(*) FROM annotation_items
                WHERE annotation_set_id = ?
                  AND content_context_visibility = 'occurrence_context'
                """,
                (annotation_set_id,),
            ).fetchone()[0]
            context_policy = connection.execute(
                """
                SELECT * FROM context_review_sampling_policies
                WHERE annotation_set_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (annotation_set_id,),
            ).fetchone()
            occurrence_items: list[sqlite3.Row] = []
            if context_policy is not None:
                context_policy_id = context_policy[
                    "context_review_sampling_policy_id"
                ]
                context_policy_version = context_policy["policy_version"]
                occurrence_review_total = int(context_policy["target_count"])
                occurrence_items = list(
                    connection.execute(
                        """
                        SELECT item.annotation_item_id, item.status,
                               sample.model_relationship
                        FROM context_review_sample_items AS sample
                        JOIN annotation_items AS item
                          ON item.annotation_item_id =
                             sample.annotation_item_id
                        WHERE sample.context_review_sampling_policy_id = ?
                        ORDER BY item.annotation_item_id
                        """,
                        (context_policy_id,),
                    )
                )
            for item in occurrence_items:
                if item["status"] != "approved":
                    continue
                event = connection.execute(
                    """
                    SELECT event.after_json, event.review_provenance,
                           event.owner_review_delegation_id
                    FROM annotation_events AS event
                    WHERE event.annotation_item_id = ?
                      AND event.annotation_type = 'occurrence_relation'
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
                    event is None
                    or event["review_provenance"] != "human"
                    or event["owner_review_delegation_id"] is not None
                ):
                    continue
                relationship = json.loads(event["after_json"]).get(
                    "relationship_to_context"
                )
                if relationship:
                    occurrence_review_approved += 1
                    human_occurrence_relations[str(relationship)] += 1
                    model_relationship = str(item["model_relationship"])
                    context_sample_confusion[model_relationship][
                        str(relationship)
                    ] += 1
                    if relationship == model_relationship:
                        context_sample_agreements += 1
        context_count = connection.execute(
            """
            SELECT COUNT(*) FROM occurrence_context_fusions WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        long_missing = connection.execute(
            """
            SELECT COUNT(*)
            FROM long_image_layouts AS layout
            WHERE layout.run_id = ?
              AND (
                NOT EXISTS (
                    SELECT 1 FROM image_tiles AS tile
                    WHERE tile.long_image_layout_id = layout.long_image_layout_id
                )
                OR layout.global_thumbnail_asset_id IS NULL
              )
            """,
            (run_id,),
        ).fetchone()[0]
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(content_visual_analyses)"
            )
        }
        forbidden_columns = sorted(
            columns
            & {
                "image_occurrence_id",
                "source_record_id",
                "source_ref_id",
                "source_sku_id_raw",
                "folder_context_json",
                "context_shade_json",
            }
        )
        required_tags = {
            "long_image",
            "format_mismatch",
            "duplicate_multi_occurrence",
            "folder_collision",
        }
        structural_pass = (
            50 <= sample_count <= 100
            and not (required_tags - set(tags))
            and model_count > 0
            and analysis_count == sample_count
            and context_count > 0
            and model_artifact_missing == 0
            and duplicate_paid_success == 0
            and long_missing == 0
            and not forbidden_columns
        )
        review_ready = (
            human_status.get("approved", 0) == sample_count
            and reviewed_content_items == sample_count
            and len(reviewed_role_counts) == 8
            and invalid_review_provenance_count == 0
            and context_policy_id is not None
            and occurrence_review_total == CONTEXT_REVIEW_TARGET
            and occurrence_review_approved == occurrence_review_total
        )
        gate_decision = connection.execute(
            """
            SELECT decision, approved_by, created_at
            FROM pilot_gate_decisions
            WHERE pilot_run_id = ?
            ORDER BY created_at DESC, pilot_gate_decision_id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if decision is not None:
            if not structural_pass or not review_ready:
                raise RuntimeError(
                    "Go/No-Go cannot be recorded before structural and "
                    "review prerequisites pass"
                )
            decision_created_at = utc_now()
            evidence = {
                "schema_version": "pilot-gate-evidence-2",
                "sample_count": sample_count,
                "reviewed_role_counts": dict(
                    sorted(reviewed_role_counts.items())
                ),
                "human_role_counts": dict(
                    sorted(human_role_counts.items())
                ),
                "owner_delegated_role_counts": dict(
                    sorted(owner_delegated_role_counts.items())
                ),
                "owner_review_delegation_ids": sorted(
                    owner_review_delegation_ids
                ),
                "invalid_review_provenance_count": (
                    invalid_review_provenance_count
                ),
                "context_review_sampling_policy_id": context_policy_id,
                "context_review_sample_count": occurrence_review_total,
                "context_review_approved": occurrence_review_approved,
                "context_sample_agreements": context_sample_agreements,
                "context_sample_disagreements": (
                    occurrence_review_approved - context_sample_agreements
                ),
            }
            connection.execute(
                """
                INSERT INTO pilot_gate_decisions(
                    pilot_gate_decision_id, pilot_run_id,
                    context_review_sampling_policy_id, decision,
                    approved_by, evidence_summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_id(
                        "pilot_gate_decision",
                        run_id,
                        decision,
                        approved_by,
                        decision_created_at,
                    ),
                    run_id,
                    context_policy_id,
                    decision,
                    approved_by,
                    canonical_json(evidence),
                    decision_created_at,
                ),
            )
            connection.commit()
            gate_decision = connection.execute(
                """
                SELECT decision, approved_by, created_at
                FROM pilot_gate_decisions
                WHERE pilot_run_id = ?
                ORDER BY created_at DESC,
                         pilot_gate_decision_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        gate_decision_value = (
            str(gate_decision["decision"])
            if gate_decision is not None
            else None
        )
        review_complete = review_ready and gate_decision_value == "go"
        human_only_ready = (
            human_status.get("approved", 0) == sample_count
            and sum(human_role_counts.values()) == sample_count
            and len(human_role_counts) == 8
            and context_policy_id is not None
            and occurrence_review_total == CONTEXT_REVIEW_TARGET
            and occurrence_review_approved == occurrence_review_total
        )
        status = (
            "passed"
            if structural_pass and review_complete
            else "failed"
            if not structural_pass
            else "awaiting_review"
            if not review_ready
            else "no_go"
            if gate_decision_value == "no_go"
            else "awaiting_go"
        )
        context_agreement_rate = (
            context_sample_agreements / occurrence_review_approved
            if occurrence_review_approved
            else None
        )
        summary = {
            "schema_version": "stage1-5-validation-3",
            "run_id": run_id,
            "status": status,
            "sample_count": sample_count,
            "coverage_tags": dict(sorted(tags.items())),
            "model_attempt_count": model_count,
            "model_artifact_missing": model_artifact_missing,
            "duplicate_paid_success_cache_keys": duplicate_paid_success,
            "analyzed_unique_images": analysis_count,
            "primary_role_counts": model_role_counts,
            "model_primary_role_counts": model_role_counts,
            "human_primary_role_counts": dict(
                sorted(human_role_counts.items())
            ),
            "owner_delegated_primary_role_counts": dict(
                sorted(owner_delegated_role_counts.items())
            ),
            "reviewed_primary_role_counts": dict(
                sorted(reviewed_role_counts.items())
            ),
            "human_reviewed_content_items": sum(
                human_role_counts.values()
            ),
            "owner_delegated_reviewed_content_items": sum(
                owner_delegated_role_counts.values()
            ),
            "reviewed_content_items": reviewed_content_items,
            "owner_review_delegation_ids": sorted(
                owner_review_delegation_ids
            ),
            "invalid_review_provenance_count": (
                invalid_review_provenance_count
            ),
            "occurrence_context_population_count": occurrence_population_total,
            "occurrence_context_machine_prelabel_unreviewed": max(
                0,
                occurrence_population_total - occurrence_review_total,
            ),
            "occurrence_review_total": occurrence_review_total,
            "occurrence_review_approved": occurrence_review_approved,
            "context_review_sampling_policy_id": context_policy_id,
            "context_review_sampling_policy_version": context_policy_version,
            "human_occurrence_relation_counts": dict(
                sorted(human_occurrence_relations.items())
            ),
            "context_sample_agreements": context_sample_agreements,
            "context_sample_disagreements": (
                occurrence_review_approved - context_sample_agreements
            ),
            "context_sample_agreement_rate": context_agreement_rate,
            "context_sample_confusion": {
                model_relationship: dict(sorted(human_counts.items()))
                for model_relationship, human_counts in sorted(
                    context_sample_confusion.items()
                )
            },
            "context_fusion_count": context_count,
            "long_layouts_missing_assets": long_missing,
            "forbidden_a_layer_columns": forbidden_columns,
            "human_review_status": dict(sorted(human_status.items())),
            "review_status": dict(sorted(human_status.items())),
            "human_review_status_field_semantics": (
                "legacy status column; review provenance is reported "
                "separately"
            ),
            "structural_hard_gates_passed": structural_pass,
            "review_prerequisites_passed": review_ready,
            "human_review_prerequisites_passed": human_only_ready,
            "latest_gate_decision": (
                {
                    "decision": gate_decision["decision"],
                    "approved_by": gate_decision["approved_by"],
                    "created_at": gate_decision["created_at"],
                }
                if gate_decision is not None
                else None
            ),
            "review_hard_gate_passed": review_complete,
            "human_hard_gate_passed": (
                human_only_ready and gate_decision_value == "go"
            ),
        }
        write_json_snapshot(
            run_dir / "reports",
            "stage1_5_validation",
            summary,
        )
        export_tables_jsonl(
            connection,
            run_dir / "jsonl",
            PILOT_TABLES,
        )
        if finalize:
            finish_pipeline_run(
                connection,
                run_id,
                status=(
                    "completed"
                    if status == "passed"
                    else status
                ),
                error_summary={
                    "validation_status": status,
                    "review_pending": not review_complete,
                },
            )
        return summary
