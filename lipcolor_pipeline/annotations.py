"""Stage 2.5 append-only annotation sets, leakage-safe splits, and baselines."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageDraw

from .image_assets import register_existing_asset
from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import stable_id
from .workspace import (
    Workspace,
    begin_pipeline_run,
    ensure_run_directory,
    export_tables_jsonl,
    finish_pipeline_run,
    open_database,
    utc_now,
    write_json_snapshot,
)


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
OCCURRENCE_RELATION_CODES = (
    "exact_shade_match",
    "contains_context_shade",
    "same_product_unspecified_shade",
    "shade_conflict",
    "unrelated",
    "insufficient_evidence",
)
ANNOTATION_EVENT_TYPES = (
    "role",
    "eligibility",
    "region",
    "mask",
    "multi_shade",
    "occurrence_relation",
    "revoke",
    "adjudication",
)

STAGE2_5_TABLES = (
    "annotation_sets",
    "annotation_items",
    "annotation_events",
    "evaluation_sets",
    "evaluation_set_items",
    "evaluation_runs",
    "evaluation_metrics",
    "performance_threshold_versions",
)

ANNOTATION_SELECTION_QUOTAS = (
    ("long_image", 24),
    ("format_mismatch", 24),
    ("duplicate_multi_occurrence", 48),
    ("folder_collision", 24),
    ("invalid_or_decorative_candidate", 12),
)


class UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}
        self.rank = {value: 0 for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1


def _rank(seed: str, label: str, image_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{label}\0{image_id}".encode()).hexdigest()


def _assert_stage2_complete(
    connection: sqlite3.Connection, stage2_run_id: str
) -> None:
    row = connection.execute(
        "SELECT status FROM pipeline_runs WHERE run_id = ? AND stage = '2'",
        (stage2_run_id,),
    ).fetchone()
    if row is None or row[0] != "completed":
        raise RuntimeError(
            "Stage 2.5 candidate creation is blocked until Stage 2 passes"
        )


def _content_groups(
    connection: sqlite3.Connection,
    stage2_run_id: str,
) -> dict[str, str]:
    image_ids = [
        row[0]
        for row in connection.execute(
            "SELECT image_id FROM image_contents ORDER BY image_id"
        )
    ]
    union = UnionFind(image_ids)
    for row in connection.execute(
        """
        SELECT image_id_a, image_id_b
        FROM duplicate_edges
        WHERE run_id = ? AND confidence_class IN ('high', 'possible')
        """,
        (stage2_run_id,),
    ):
        union.union(row[0], row[1])
    # Product/folder grouping is conservative: all images from the current
    # physical product group remain in one split.
    folder_members: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT DISTINCT folder_group_id, image_id
        FROM image_occurrences ORDER BY folder_group_id, image_id
        """
    ):
        folder_members[row["folder_group_id"]].append(row["image_id"])
    for members in folder_members.values():
        for image_id in members[1:]:
            union.union(members[0], image_id)
    grouped: dict[str, list[str]] = defaultdict(list)
    for image_id in image_ids:
        grouped[union.find(image_id)].append(image_id)
    result: dict[str, str] = {}
    for members in grouped.values():
        group_id = stable_id("eval_group", *sorted(members))
        for image_id in members:
            result[image_id] = group_id
    return result


def _split_for_group(group_id: str, seed: str) -> str:
    bucket = int(hashlib.sha256(f"{seed}\0{group_id}".encode()).hexdigest()[:8], 16)
    percentile = bucket % 100
    if percentile < 60:
        return "train"
    if percentile < 80:
        return "validation"
    return "test"


def create_pilot_review_set(
    workspace: Workspace,
    *,
    pilot_run_id: str,
) -> dict[str, Any]:
    """Create image-only and occurrence-context review items for a Pilot."""

    with open_database(workspace.database_path) as connection:
        annotation_set_id = stable_id(
            "annotation_set", "pilot_review", pilot_run_id
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO annotation_sets(
                annotation_set_id, run_id, name, version, purpose,
                label_schema_version, selection_rules_json,
                content_grouping_method, status, created_at, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                annotation_set_id,
                pilot_run_id,
                f"stage1_5_pilot_review:{pilot_run_id}",
                "1.0.0",
                "pilot_review",
                "annotation-event-1.0",
                canonical_json({"pilot_run_id": pilot_run_id}),
                "image_sha256",
                "open",
                utc_now(),
            ),
        )
        content_items = 0
        occurrence_items = 0
        samples = connection.execute(
            """
            SELECT sample.image_id,
                   layout.global_thumbnail_asset_id,
                   (
                     SELECT asset.derived_asset_id
                     FROM derived_assets AS asset
                     WHERE asset.run_id = sample.pilot_run_id
                       AND asset.image_id = sample.image_id
                       AND asset.asset_type IN (
                           'analysis_preview', 'global_thumbnail'
                       )
                     ORDER BY CASE asset.asset_type
                         WHEN 'global_thumbnail' THEN 0 ELSE 1 END
                     LIMIT 1
                   ) AS display_asset_id,
                   sample.coverage_tags_json
            FROM pilot_samples AS sample
            LEFT JOIN long_image_layouts AS layout
              ON layout.run_id = sample.pilot_run_id
             AND layout.image_id = sample.image_id
            WHERE sample.pilot_run_id = ?
            ORDER BY sample.image_id
            """,
            (pilot_run_id,),
        )
        for sample in samples:
            item_id = stable_id(
                "annotation_item", annotation_set_id, sample["image_id"], "content"
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO annotation_items(
                    annotation_item_id, annotation_set_id, image_id,
                    image_occurrence_id, global_thumbnail_asset_id,
                    task_types_json, content_context_visibility,
                    coverage_tags_json, group_id, split, status, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    item_id,
                    annotation_set_id,
                    sample["image_id"],
                    sample["display_asset_id"],
                    canonical_json(["role", "eligibility", "region"]),
                    "image_only",
                    sample["coverage_tags_json"],
                    stable_id("pilot_group", sample["image_id"]),
                    "pending",
                    utc_now(),
                ),
            )
            content_items += max(0, cursor.rowcount)
            for occurrence in connection.execute(
                """
                SELECT image_occurrence_id
                FROM image_occurrences WHERE image_id = ?
                ORDER BY image_occurrence_id
                """,
                (sample["image_id"],),
            ):
                occurrence_item_id = stable_id(
                    "annotation_item",
                    annotation_set_id,
                    sample["image_id"],
                    occurrence["image_occurrence_id"],
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO annotation_items(
                        annotation_item_id, annotation_set_id, image_id,
                        image_occurrence_id, global_thumbnail_asset_id,
                        task_types_json, content_context_visibility,
                        coverage_tags_json, group_id, split, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        occurrence_item_id,
                        annotation_set_id,
                        sample["image_id"],
                        occurrence["image_occurrence_id"],
                        sample["display_asset_id"],
                        canonical_json(["occurrence_relation"]),
                        "occurrence_context",
                        sample["coverage_tags_json"],
                        stable_id("pilot_group", sample["image_id"]),
                        "pending",
                        utc_now(),
                    ),
                )
                occurrence_items += max(0, cursor.rowcount)
        connection.commit()
    return {
        "annotation_set_id": annotation_set_id,
        "content_items_inserted": content_items,
        "occurrence_items_inserted": occurrence_items,
    }


def _stage2_candidates(
    connection: sqlite3.Connection,
    stage2_run_id: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            observation.image_id,
            observation.working_asset_id,
            layout.global_thumbnail_asset_id,
            MAX(occurrence.extension_mismatch) AS extension_mismatch,
            COUNT(DISTINCT occurrence.image_occurrence_id) AS occurrence_count,
            MAX(CASE folder.collision_status
                WHEN 'multi_source_record' THEN 1 ELSE 0 END) AS folder_collision,
            MAX(CASE flag.flag_code WHEN 'is_long' THEN 1 ELSE 0 END) AS is_long,
            MAX(CASE flag.flag_code WHEN 'semantic_invalid_candidate'
                THEN 1 ELSE 0 END) AS semantic_invalid,
            MAX(CASE flag.flag_code WHEN 'is_extreme_aspect_ratio'
                THEN 1 ELSE 0 END) AS extreme_aspect
        FROM image_preprocessing_observations AS observation
        JOIN image_occurrences AS occurrence
          ON occurrence.image_id = observation.image_id
        JOIN folder_groups AS folder
          ON folder.folder_group_id = occurrence.folder_group_id
        LEFT JOIN quality_flags AS flag
          ON flag.image_id = observation.image_id
         AND flag.run_id = observation.run_id
        LEFT JOIN long_image_layouts AS layout
          ON layout.image_id = observation.image_id
         AND layout.run_id = observation.run_id
        WHERE observation.run_id = ? AND observation.decode_status = 'ok'
        GROUP BY observation.image_id
        ORDER BY observation.image_id
        """,
        (stage2_run_id,),
    )
    candidates: list[dict[str, Any]] = []
    for row in rows:
        tags: list[str] = []
        if row["is_long"]:
            tags.append("long_image")
        if row["extension_mismatch"]:
            tags.append("format_mismatch")
        if row["occurrence_count"] > 1:
            tags.append("duplicate_multi_occurrence")
        if row["folder_collision"]:
            tags.append("folder_collision")
        if row["semantic_invalid"]:
            tags.append("semantic_invalid_candidate")
        if row["semantic_invalid"] or (
            row["extreme_aspect"] and not row["is_long"]
        ):
            tags.append("invalid_or_decorative_candidate")
        candidates.append(
            {
                "image_id": row["image_id"],
                "display_asset_id": (
                    row["global_thumbnail_asset_id"] or row["working_asset_id"]
                ),
                "coverage_tags": tags,
            }
        )
    return candidates


def create_stage2_5_annotation_set(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    stage2_run_id: str,
    pilot_run_id: str,
    resume: bool = False,
) -> dict[str, Any]:
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    annotation = settings.section("annotation")
    target = int(annotation["accepted_unique_images"])
    candidate_pool_target = max(target, target + 160)
    seed = str(annotation["split_seed"])
    with open_database(workspace.database_path) as connection:
        _assert_stage2_complete(connection, stage2_run_id)
        begin_pipeline_run(
            connection,
            workspace,
            settings,
            run_id=run_id,
            stage="2.5",
            resume=resume,
            extra_config={
                "stage2_run_id": stage2_run_id,
                "pilot_run_id": pilot_run_id,
                "annotation_target": target,
                "candidate_pool_target": candidate_pool_target,
            },
        )
        annotation_set_id = stable_id(
            "annotation_set", run_id, "stage2_5_ground_truth", "1.0.0"
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO annotation_sets(
                annotation_set_id, run_id, name, version, purpose,
                label_schema_version, selection_rules_json,
                content_grouping_method, status, created_at, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                annotation_set_id,
                run_id,
                f"stage2_5_ground_truth:{run_id}",
                "1.0.0",
                "role_eligibility_mask_multishade",
                "annotation-event-1.0",
                canonical_json(
                    {
                        "accepted_unique_images": target,
                        "candidate_pool_images": candidate_pool_target,
                        "slice_quotas": dict(ANNOTATION_SELECTION_QUOTAS),
                        "selection_policy_version": (
                            "stage2-5-candidate-selection-1.1"
                        ),
                        "candidate_split_ratios": {
                            "train": 0.6,
                            "validation": 0.2,
                            "test": 0.2,
                        },
                        "invalid_slice_definition": (
                            "semantic_invalid_candidate OR "
                            "(extreme_aspect_ratio AND NOT long_image)"
                        ),
                        "role_target_per_class": annotation[
                            "target_per_primary_role"
                        ],
                    }
                ),
                "sha256_near_duplicate_and_folder_union",
                "open",
                utc_now(),
            ),
        )
        existing = connection.execute(
            """
            SELECT COUNT(*) FROM annotation_items
            WHERE annotation_set_id = ?
            """,
            (annotation_set_id,),
        ).fetchone()[0]
        if existing and existing != candidate_pool_target:
            raise RuntimeError("existing annotation selection has a changed size")
        if not existing:
            candidates = _stage2_candidates(connection, stage2_run_id)
            groups = _content_groups(connection, stage2_run_id)
            for candidate in candidates:
                group_id = groups[candidate["image_id"]]
                candidate["group_id"] = group_id
                candidate["split"] = _split_for_group(group_id, seed)
            split_targets = {
                "train": candidate_pool_target * 60 // 100,
                "validation": candidate_pool_target * 20 // 100,
            }
            split_targets["test"] = (
                candidate_pool_target
                - split_targets["train"]
                - split_targets["validation"]
            )
            selected: dict[str, dict[str, Any]] = {}
            selected_split_counts: Counter[str] = Counter()
            for tag, quota in ANNOTATION_SELECTION_QUOTAS:
                tagged = [
                    candidate
                    for candidate in candidates
                    if tag in candidate["coverage_tags"]
                    and candidate["image_id"] not in selected
                ]
                tagged.sort(key=lambda row: _rank(seed, tag, row["image_id"]))
                added = 0
                for candidate in tagged:
                    split = str(candidate["split"])
                    if selected_split_counts[split] >= split_targets[split]:
                        continue
                    selected[candidate["image_id"]] = candidate
                    selected_split_counts[split] += 1
                    added += 1
                    if added >= quota:
                        break
                if added != quota:
                    raise RuntimeError(
                        f"could only select {added}/{quota} candidates for {tag}"
                    )
            for split, split_target in split_targets.items():
                remaining = [
                    candidate
                    for candidate in candidates
                    if candidate["image_id"] not in selected
                    and candidate["split"] == split
                ]
                remaining.sort(
                    key=lambda row: _rank(
                        seed,
                        f"fill:{split}",
                        row["image_id"],
                    )
                )
                needed = split_target - selected_split_counts[split]
                for candidate in remaining[:needed]:
                    selected[candidate["image_id"]] = candidate
                    selected_split_counts[split] += 1
                if selected_split_counts[split] != split_target:
                    raise RuntimeError(
                        "could not satisfy deterministic candidate split "
                        f"target for {split}"
                    )
            if len(selected) != candidate_pool_target:
                raise RuntimeError(
                    f"could only select {len(selected)} annotation candidates"
                )
            blind_pool_target = min(
                candidate_pool_target,
                max(
                    int(annotation["blind_review_subset"]),
                    int(annotation["blind_review_subset"]) + 64,
                ),
            )
            blind_image_ids = {
                candidate["image_id"]
                for candidate in sorted(
                    selected.values(),
                    key=lambda row: _rank(
                        seed,
                        "blind_review_pool",
                        row["image_id"],
                    ),
                )[:blind_pool_target]
            }
            for image_id, candidate in sorted(selected.items()):
                group_id = str(candidate["group_id"])
                split = str(candidate["split"])
                coverage_tags = list(candidate["coverage_tags"])
                if image_id in blind_image_ids:
                    coverage_tags.append("blind_review_required")
                connection.execute(
                    """
                    INSERT INTO annotation_items(
                        annotation_item_id, annotation_set_id, image_id,
                        image_occurrence_id, global_thumbnail_asset_id,
                        task_types_json, content_context_visibility,
                        coverage_tags_json, group_id, split, status, created_at
                    ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id(
                            "annotation_item", annotation_set_id, image_id
                        ),
                        annotation_set_id,
                        image_id,
                        candidate["display_asset_id"],
                        canonical_json(
                            [
                                "role",
                                "eligibility",
                                "region",
                                "mask",
                                "multi_shade",
                            ]
                        ),
                        "image_only",
                        canonical_json(sorted(coverage_tags)),
                        group_id,
                        split,
                        "pending",
                        utc_now(),
                    ),
                )
            connection.commit()

        rows = list(
            connection.execute(
                """
                SELECT annotation_item_id, coverage_tags_json, group_id, split
                FROM annotation_items
                WHERE annotation_set_id = ?
                ORDER BY annotation_item_id
                """,
                (annotation_set_id,),
            )
        )
        # Deliberately omit image_id and all source context.
        manifest_text = "".join(
            canonical_json(dict(row)) + "\n" for row in rows
        )
        manifest_digest = hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        manifest_path = (
            run_dir / f"annotation_selection.{manifest_digest[:16]}.jsonl"
        )
        if not manifest_path.exists():
            manifest_path.write_text(
                manifest_text,
                encoding="utf-8",
                newline="\n",
            )
        slice_counts = Counter()
        split_counts = Counter()
        for row in rows:
            slice_counts.update(json.loads(row["coverage_tags_json"]))
            split_counts[row["split"]] += 1
        summary = {
            "schema_version": "annotation-selection-summary-2",
            "run_id": run_id,
            "annotation_set_id": annotation_set_id,
            "candidate_item_count": len(rows),
            "accepted_target": target,
            "slice_counts": dict(sorted(slice_counts.items())),
            "split_counts": dict(sorted(split_counts.items())),
            "candidate_split_policy": {
                "ratios": {
                    "train": 0.6,
                    "validation": 0.2,
                    "test": 0.2,
                },
                "selection_unit": "leakage_safe_group",
                "assignment": "stable_group_hash_then_split_quota_fill",
            },
            "selection_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "status": "awaiting_human_annotation",
        }
        write_json_snapshot(
            run_dir / "reports",
            "annotation_selection_summary",
            summary,
        )
        return summary


def supersede_stage2_5_annotation_set(
    workspace: Workspace,
    *,
    annotation_set_id: str,
    replacement_run_id: str,
    reason: str,
) -> dict[str, Any]:
    """Close an unused candidate selection while retaining all evidence."""

    if not replacement_run_id.strip() or not reason.strip():
        raise ValueError("replacement_run_id and reason are required")
    with open_database(workspace.database_path) as connection:
        annotation_set = connection.execute(
            """
            SELECT annotation_set.*, run.stage
            FROM annotation_sets AS annotation_set
            JOIN pipeline_runs AS run ON run.run_id = annotation_set.run_id
            WHERE annotation_set.annotation_set_id = ?
            """,
            (annotation_set_id,),
        ).fetchone()
        if annotation_set is None:
            raise KeyError("annotation set not found")
        if (
            annotation_set["stage"] != "2.5"
            or annotation_set["purpose"]
            != "role_eligibility_mask_multishade"
        ):
            raise ValueError("only a Stage 2.5 candidate set can be superseded")
        if annotation_set["status"] == "superseded":
            return {
                "annotation_set_id": annotation_set_id,
                "run_id": annotation_set["run_id"],
                "replacement_run_id": replacement_run_id,
                "reason": reason,
                "status": "superseded",
                "reused": True,
            }
        event_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM annotation_events AS event
            JOIN annotation_items AS item
              ON item.annotation_item_id = event.annotation_item_id
            WHERE item.annotation_set_id = ?
            """,
            (annotation_set_id,),
        ).fetchone()[0]
        non_pending = connection.execute(
            """
            SELECT COUNT(*) FROM annotation_items
            WHERE annotation_set_id = ? AND status <> 'pending'
            """,
            (annotation_set_id,),
        ).fetchone()[0]
        if event_count or non_pending:
            raise ValueError(
                "a candidate set with review work cannot be superseded"
            )
        summary = {
            "schema_version": "annotation-selection-supersession-1",
            "annotation_set_id": annotation_set_id,
            "run_id": annotation_set["run_id"],
            "replacement_run_id": replacement_run_id,
            "reason": reason,
            "event_count": event_count,
            "non_pending_item_count": non_pending,
            "evidence_retained": True,
            "status": "superseded",
            "reused": False,
        }
        connection.execute(
            """
            UPDATE annotation_sets
            SET status = 'superseded'
            WHERE annotation_set_id = ?
            """,
            (annotation_set_id,),
        )
        finish_pipeline_run(
            connection,
            annotation_set["run_id"],
            status="superseded",
            error_summary={
                "reason": reason,
                "replacement_run_id": replacement_run_id,
            },
        )
        connection.commit()
    write_json_snapshot(
        workspace.run_dir(str(annotation_set["run_id"])) / "reports",
        "annotation_selection_supersession",
        summary,
    )
    return summary


def _latest_event(
    connection: sqlite3.Connection,
    item_id: str,
    annotation_type: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT event.* FROM annotation_events AS event
        WHERE event.annotation_item_id = ?
          AND event.annotation_type = ?
          AND NOT EXISTS (
              SELECT 1 FROM annotation_events AS revocation
              WHERE revocation.annotation_item_id = event.annotation_item_id
                AND revocation.annotation_type = 'revoke'
                AND revocation.supersedes_event_id =
                    event.annotation_event_id
          )
        ORDER BY event.created_at DESC, event.annotation_event_id DESC
        LIMIT 1
        """,
        (item_id, annotation_type),
    ).fetchone()


def _latest_event_for_annotator(
    connection: sqlite3.Connection,
    item_id: str,
    annotation_type: str,
    annotator_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT event.* FROM annotation_events AS event
        WHERE event.annotation_item_id = ?
          AND event.annotation_type = ?
          AND event.annotator_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM annotation_events AS revocation
              WHERE revocation.annotation_item_id = event.annotation_item_id
                AND revocation.annotation_type = 'revoke'
                AND revocation.supersedes_event_id =
                    event.annotation_event_id
          )
        ORDER BY event.created_at DESC, event.annotation_event_id DESC
        LIMIT 1
        """,
        (item_id, annotation_type, annotator_id),
    ).fetchone()


def _validate_owner_delegation(
    connection: sqlite3.Connection,
    *,
    item: sqlite3.Row,
    annotator_id: str,
    annotation_type: str,
    role_code: str | None,
    owner_review_delegation_id: str | None,
) -> sqlite3.Row:
    if owner_review_delegation_id is None:
        raise ValueError(
            "owner-delegated review requires owner_review_delegation_id"
        )
    delegation = connection.execute(
        """
        SELECT * FROM owner_review_delegations
        WHERE owner_review_delegation_id = ?
        """,
        (owner_review_delegation_id,),
    ).fetchone()
    if delegation is None:
        raise ValueError("owner review delegation not found")
    if item["annotation_set_purpose"] != "pilot_review":
        raise ValueError("owner-delegated review is limited to Pilot review")
    if delegation["pilot_run_id"] != item["run_id"]:
        raise ValueError("owner review delegation belongs to another Pilot")
    if delegation["delegated_agent"] != annotator_id:
        raise ValueError("annotator_id does not match the delegated agent")
    if delegation["scope"] != "stage1_5_color_card_topup":
        raise ValueError("owner review delegation scope is not supported")
    evidence = json.loads(delegation["evidence_json"])
    if item["image_id"] not in set(evidence.get("authorized_image_ids", [])):
        raise ValueError("delegation does not authorize this image")
    if annotation_type not in set(
        evidence.get("authorized_annotation_types", [])
    ):
        raise ValueError("delegation does not authorize this annotation type")
    if role_code is not None and role_code not in set(
        evidence.get("authorized_role_codes", [])
    ):
        raise ValueError("delegation does not authorize this role")
    return delegation


def append_annotation_event(
    workspace: Workspace,
    *,
    annotation_item_id: str,
    annotator_id: str,
    annotation_type: str,
    payload: Mapping[str, Any],
    supersedes_event_id: str | None = None,
    review_provenance: str = "human",
    owner_review_delegation_id: str | None = None,
) -> dict[str, Any]:
    with open_database(workspace.database_path) as connection:
        item = connection.execute(
            """
            SELECT item.*, annotation_set.run_id,
                   annotation_set.status AS annotation_set_status,
                   annotation_set.purpose AS annotation_set_purpose
            FROM annotation_items AS item
            JOIN annotation_sets AS annotation_set
              ON annotation_set.annotation_set_id = item.annotation_set_id
            WHERE item.annotation_item_id = ?
            """,
            (annotation_item_id,),
        ).fetchone()
        if item is None:
            raise KeyError("annotation item not found")
        if item["annotation_set_status"] != "open":
            raise ValueError("annotation set is not open")
        if annotation_type not in ANNOTATION_EVENT_TYPES:
            raise ValueError("invalid annotation_type")
        if (
            item["content_context_visibility"] == "image_only"
            and annotation_type == "occurrence_relation"
        ):
            raise ValueError("occurrence relation is forbidden on image-only items")
        if (
            item["content_context_visibility"] == "occurrence_context"
            and annotation_type
            not in {"occurrence_relation", "revoke", "adjudication"}
        ):
            raise ValueError(
                "content labels are forbidden on occurrence-context items"
            )
        if (
            item["annotation_set_purpose"] == "pilot_review"
            and item["content_context_visibility"] == "occurrence_context"
            and annotation_type == "occurrence_relation"
        ):
            policy = connection.execute(
                """
                SELECT context_review_sampling_policy_id
                FROM context_review_sampling_policies
                WHERE annotation_set_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (item["annotation_set_id"],),
            ).fetchone()
            if policy is not None and connection.execute(
                """
                SELECT 1 FROM context_review_sample_items
                WHERE context_review_sampling_policy_id = ?
                  AND annotation_item_id = ?
                """,
                (
                    policy["context_review_sampling_policy_id"],
                    annotation_item_id,
                ),
            ).fetchone() is None:
                raise ValueError(
                    "this occurrence is machine-prelabel-only and is not "
                    "part of the frozen human review sample"
                )
        is_blind_item = "blind_review_required" in set(
            json.loads(item["coverage_tags_json"])
        )
        is_blind_label = (
            is_blind_item and annotation_type in {"role", "eligibility"}
        )
        previous = (
            _latest_event_for_annotator(
                connection,
                annotation_item_id,
                annotation_type,
                annotator_id,
            )
            if is_blind_label
            else _latest_event(
                connection,
                annotation_item_id,
                annotation_type,
            )
        )
        if annotation_type == "revoke":
            if supersedes_event_id is None:
                raise ValueError("revoke must identify an event")
            previous = connection.execute(
                """
                SELECT * FROM annotation_events
                WHERE annotation_event_id = ?
                  AND annotation_item_id = ?
                  AND annotation_type <> 'revoke'
                """,
                (supersedes_event_id, annotation_item_id),
            ).fetchone()
            if previous is None:
                raise ValueError("revoke target is not an event on this item")
            if connection.execute(
                """
                SELECT 1 FROM annotation_events
                WHERE annotation_type = 'revoke'
                  AND supersedes_event_id = ?
                """,
                (supersedes_event_id,),
            ).fetchone():
                raise ValueError("event is already revoked")
        elif supersedes_event_id:
            if previous is None or previous["annotation_event_id"] != supersedes_event_id:
                raise ValueError("supersedes_event_id must reference the latest event")
        elif previous is not None:
            raise ValueError("a revision must explicitly supersede the latest event")

        role_code = payload.get("role_code")
        if role_code is not None and role_code not in ROLE_CODES:
            raise ValueError("invalid role_code")
        if review_provenance not in {"human", "owner_delegated_agent"}:
            raise ValueError("invalid review_provenance")
        if review_provenance == "human":
            if owner_review_delegation_id is not None:
                raise ValueError(
                    "human review cannot reference an owner delegation"
                )
        else:
            _validate_owner_delegation(
                connection,
                item=item,
                annotator_id=annotator_id,
                annotation_type=annotation_type,
                role_code=role_code,
                owner_review_delegation_id=owner_review_delegation_id,
            )
        eligibility = payload.get("eligibility_label")
        if eligibility is not None and not isinstance(eligibility, bool):
            raise ValueError("eligibility_label must be boolean")
        bbox = payload.get("bbox_image")
        polygon = payload.get("polygon_image")
        observation = connection.execute(
            """
            SELECT width, height FROM image_preprocessing_observations
            WHERE image_id = ? AND decode_status = 'ok'
            ORDER BY created_at DESC LIMIT 1
            """,
            (item["image_id"],),
        ).fetchone()
        if annotation_type == "role" and role_code is None:
            raise ValueError("role annotation requires role_code")
        if annotation_type == "eligibility" and eligibility is None:
            raise ValueError(
                "eligibility annotation requires eligibility_label"
            )
        if annotation_type == "region" and bbox is None and polygon is None:
            raise ValueError("region annotation requires bbox or polygon")
        if annotation_type == "multi_shade":
            if observation is None:
                raise ValueError(
                    "multi_shade annotation requires an oriented image size"
                )
            _validate_multi_shade_annotation(
                payload.get("multi_shade"),
                observation["width"],
                observation["height"],
            )
        if annotation_type == "occurrence_relation":
            relationship = payload.get("relationship_to_context")
            if relationship not in OCCURRENCE_RELATION_CODES:
                raise ValueError("invalid occurrence relationship")
        if annotation_type == "adjudication" and (
            role_code is None and eligibility is None
        ):
            raise ValueError(
                "adjudication requires role_code or eligibility_label"
            )
        if observation and bbox is not None:
            _validate_bbox(bbox, observation["width"], observation["height"])
        if observation and polygon is not None:
            _validate_polygon(
                polygon, observation["width"], observation["height"]
            )

        event_created_at = utc_now()
        event_id = stable_id(
            "annotation_event",
            annotation_item_id,
            annotator_id,
            annotation_type,
            event_created_at,
            sha256_json(payload),
            review_provenance,
            owner_review_delegation_id or "",
        )
        mask_asset_id = None
        if annotation_type == "mask":
            if not polygon or not observation:
                raise ValueError("mask annotation requires a polygon and image size")
            mask_asset_id = _create_mask_asset(
                workspace,
                connection,
                run_id=item["run_id"],
                image_id=item["image_id"],
                event_id=event_id,
                polygon=polygon,
                width=observation["width"],
                height=observation["height"],
                source_asset_id=item["global_thumbnail_asset_id"],
            )
        before = json.loads(previous["after_json"]) if previous else {}
        after = dict(payload)
        connection.execute(
            """
            INSERT INTO annotation_events(
                annotation_event_id, annotation_item_id, annotator_id,
                annotation_type, role_code, eligibility_label,
                eligibility_reason_codes_json, region_type,
                bbox_image_json, polygon_image_json, mask_asset_id,
                multi_shade_annotation_json, before_json, after_json,
                supersedes_event_id, created_at, review_provenance,
                owner_review_delegation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                annotation_item_id,
                annotator_id,
                annotation_type,
                role_code,
                int(eligibility) if eligibility is not None else None,
                canonical_json(payload.get("eligibility_reason_codes", [])),
                payload.get("region_type"),
                canonical_json(bbox) if bbox is not None else None,
                canonical_json(polygon) if polygon is not None else None,
                mask_asset_id,
                canonical_json(payload.get("multi_shade", {})),
                canonical_json(before),
                canonical_json(after),
                supersedes_event_id,
                event_created_at,
                review_provenance,
                owner_review_delegation_id,
            ),
        )
        if item["content_context_visibility"] == "occurrence_context":
            required_complete = (
                _latest_event(
                    connection,
                    annotation_item_id,
                    "occurrence_relation",
                )
                is not None
            )
        elif is_blind_item:
            reviewer_counts = {
                event_type: connection.execute(
                    """
                    SELECT COUNT(DISTINCT event.annotator_id)
                    FROM annotation_events AS event
                    WHERE event.annotation_item_id = ?
                      AND event.annotation_type = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM annotation_events AS revocation
                          WHERE revocation.annotation_type = 'revoke'
                            AND revocation.supersedes_event_id =
                                event.annotation_event_id
                      )
                    """,
                    (annotation_item_id, event_type),
                ).fetchone()[0]
                for event_type in ("role", "eligibility")
            }
            required_complete = all(
                count >= 2 for count in reviewer_counts.values()
            )
        else:
            required_complete = all(
                _latest_event(connection, annotation_item_id, event_type)
                is not None
                for event_type in ("role", "eligibility")
            )
        item_status = "completed" if required_complete else "in_progress"
        connection.execute(
            "UPDATE annotation_items SET status = ? WHERE annotation_item_id = ?",
            (item_status, annotation_item_id),
        )
        if (
            item["annotation_set_purpose"] == "pilot_review"
            and item["content_context_visibility"] == "image_only"
        ):
            connection.execute(
                """
                UPDATE pilot_samples
                SET human_review_status = 'pending',
                    review_provenance = ?
                WHERE pilot_run_id = ? AND image_id = ?
                """,
                (
                    review_provenance,
                    item["run_id"],
                    item["image_id"],
                ),
            )
        elif (
            item["annotation_set_purpose"] == "pilot_review"
            and item["content_context_visibility"] == "occurrence_context"
        ):
            connection.execute(
                """
                UPDATE occurrence_context_fusions SET review_status = 'pending'
                WHERE run_id = ? AND image_occurrence_id = ?
                """,
                (item["run_id"], item["image_occurrence_id"]),
            )
        connection.commit()
        return {
            "annotation_event_id": event_id,
            "mask_asset_id": mask_asset_id,
            "item_status": item_status,
        }


def _validate_bbox(value: Sequence[Any], width: int, height: int) -> None:
    if len(value) != 4:
        raise ValueError("bbox_image must contain four coordinates")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("bbox_image is outside the oriented image")


def _validate_polygon(
    value: Sequence[Sequence[Any]], width: int, height: int
) -> None:
    if len(value) < 3:
        raise ValueError("polygon_image must contain at least three points")
    for point in value:
        if len(point) != 2:
            raise ValueError("each polygon point must contain x and y")
        x, y = (float(item) for item in point)
        if not (0 <= x <= width and 0 <= y <= height):
            raise ValueError("polygon point is outside the oriented image")


def _validate_multi_shade_annotation(
    value: Any,
    width: int,
    height: int,
) -> None:
    if not isinstance(value, dict):
        raise ValueError("multi_shade must be an object")
    pairs = value.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("multi_shade.pairs must be a non-empty list")
    pair_ids: set[str] = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ValueError(f"multi_shade.pairs[{index}] must be an object")
        pair_id = str(pair.get("pair_id") or "").strip()
        shade_code = str(pair.get("shade_code") or "").strip()
        if not pair_id or pair_id in pair_ids:
            raise ValueError("multi_shade pair_id values must be unique")
        if not shade_code:
            raise ValueError("multi_shade pair requires shade_code")
        pair_ids.add(pair_id)
        for prefix in ("text", "color"):
            bbox = pair.get(f"{prefix}_bbox_image")
            polygon = pair.get(f"{prefix}_polygon_image")
            if bbox is None and polygon is None:
                raise ValueError(
                    f"multi_shade pair requires a {prefix} bbox or polygon"
                )
            if bbox is not None:
                _validate_bbox(bbox, width, height)
            if polygon is not None:
                _validate_polygon(polygon, width, height)


def _create_mask_asset(
    workspace: Workspace,
    connection: sqlite3.Connection,
    *,
    run_id: str,
    image_id: str,
    event_id: str,
    polygon: Sequence[Sequence[Any]],
    width: int,
    height: int,
    source_asset_id: str | None,
) -> str:
    fingerprint = sha256_json(
        {
            "name": "annotation_polygon_mask",
            "version": "1.0",
            "event_id": event_id,
            "polygon": polygon,
            "width": width,
            "height": height,
        }
    )
    destination = (
        workspace.assets_root
        / "annotation_mask"
        / image_id[:2]
        / image_id
        / f"{fingerprint}.png"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.polygon(
            [(round(float(x)), round(float(y))) for x, y in polygon],
            fill=255,
        )
        temporary = destination.with_name(destination.name + ".tmp")
        mask.save(temporary, format="PNG", compress_level=6, optimize=False)
        temporary.replace(destination)
    display_asset = (
        connection.execute(
            """
            SELECT metadata_json FROM derived_assets
            WHERE derived_asset_id = ?
            """,
            (source_asset_id,),
        ).fetchone()
        if source_asset_id
        else None
    )
    source_metadata = (
        json.loads(display_asset["metadata_json"])
        if display_asset is not None
        else {}
    )
    asset = register_existing_asset(
        connection,
        workspace,
        run_id,
        image_id=image_id,
        asset_type="annotation_mask",
        path=destination,
        width=width,
        height=height,
        image_format="PNG",
        transform_name="annotation_polygon_mask",
        transform_version="1.0",
        transform_fingerprint=fingerprint,
        metadata={
            "coordinate_system": "exif_oriented_image_pixels",
            "source_annotation_event_id": event_id,
            "coordinate_transforms": source_metadata.get(
                "coordinate_transforms",
                {},
            ),
        },
    )
    return asset.derived_asset_id


def approve_pilot_item(
    workspace: Workspace,
    *,
    annotation_item_id: str,
    annotator_id: str,
) -> None:
    with open_database(workspace.database_path) as connection:
        item = connection.execute(
            """
            SELECT item.*, annotation_set.run_id, annotation_set.purpose,
                   annotation_set.purpose AS annotation_set_purpose,
                   annotation_set.status AS annotation_set_status
            FROM annotation_items AS item
            JOIN annotation_sets AS annotation_set
              ON annotation_set.annotation_set_id = item.annotation_set_id
            WHERE item.annotation_item_id = ?
            """,
            (annotation_item_id,),
        ).fetchone()
        if item is None or item["purpose"] != "pilot_review":
            raise ValueError("item is not a Pilot review item")
        if item["annotation_set_status"] != "open":
            raise ValueError("annotation set is not open")
        if item["content_context_visibility"] == "image_only":
            required_events = {
                event_type: _latest_event_for_annotator(
                    connection,
                    annotation_item_id,
                    event_type,
                    annotator_id,
                )
                for event_type in ("role", "eligibility")
            }
            if not all(required_events.values()):
                raise ValueError(
                    "the approving reviewer must review role and eligibility first"
                )
            provenances = {
                str(event["review_provenance"])
                for event in required_events.values()
                if event is not None
            }
            delegation_ids = {
                event["owner_review_delegation_id"]
                for event in required_events.values()
                if event is not None
            }
            if len(provenances) != 1 or len(delegation_ids) != 1:
                raise ValueError(
                    "role and eligibility must use one review provenance"
                )
            review_provenance = provenances.pop()
            delegation_id = delegation_ids.pop()
            if review_provenance == "owner_delegated_agent":
                for event_type, event in required_events.items():
                    assert event is not None
                    _validate_owner_delegation(
                        connection,
                        item=item,
                        annotator_id=annotator_id,
                        annotation_type=event_type,
                        role_code=event["role_code"],
                        owner_review_delegation_id=delegation_id,
                    )
            elif review_provenance != "human" or delegation_id is not None:
                raise ValueError("invalid Pilot review provenance")
            connection.execute(
                """
                UPDATE pilot_samples
                SET human_review_status = 'approved',
                    review_provenance = ?
                WHERE pilot_run_id = ? AND image_id = ?
                """,
                (
                    review_provenance,
                    item["run_id"],
                    item["image_id"],
                ),
            )
        elif item["content_context_visibility"] == "occurrence_context":
            policy = connection.execute(
                """
                SELECT context_review_sampling_policy_id
                FROM context_review_sampling_policies
                WHERE annotation_set_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (item["annotation_set_id"],),
            ).fetchone()
            if policy is not None and connection.execute(
                """
                SELECT 1 FROM context_review_sample_items
                WHERE context_review_sampling_policy_id = ?
                  AND annotation_item_id = ?
                """,
                (
                    policy["context_review_sampling_policy_id"],
                    annotation_item_id,
                ),
            ).fetchone() is None:
                raise ValueError(
                    "only frozen context-sample items can be approved"
                )
            if (
                _latest_event_for_annotator(
                    connection,
                    annotation_item_id,
                    "occurrence_relation",
                    annotator_id,
                )
                is None
            ):
                raise ValueError(
                    "the approving reviewer must review the occurrence relation first"
                )
            connection.execute(
                """
                UPDATE occurrence_context_fusions SET review_status = 'approved'
                WHERE run_id = ? AND image_occurrence_id = ?
                """,
                (item["run_id"], item["image_occurrence_id"]),
            )
        else:
            raise ValueError("invalid Pilot review item visibility")
        connection.execute(
            "UPDATE annotation_items SET status = 'approved' WHERE annotation_item_id = ?",
            (annotation_item_id,),
        )
        connection.commit()


def apply_owner_delegated_pilot_review(
    workspace: Workspace,
    *,
    pilot_run_id: str,
    image_id: str,
    annotator_id: str,
    owner_review_delegation_id: str,
    role_code: str,
    eligibility_label: bool,
    eligibility_reason_codes: Sequence[str],
) -> dict[str, Any]:
    """Apply the narrowly scoped owner-delegated Pilot top-up review."""

    if role_code != "color_card":
        raise ValueError("the supported owner delegation only permits color_card")
    with open_database(workspace.database_path, readonly=True) as connection:
        item = connection.execute(
            """
            SELECT item.annotation_item_id
            FROM annotation_items AS item
            JOIN annotation_sets AS annotation_set
              ON annotation_set.annotation_set_id = item.annotation_set_id
            WHERE annotation_set.run_id = ?
              AND annotation_set.purpose = 'pilot_review'
              AND item.image_id = ?
              AND item.content_context_visibility = 'image_only'
            ORDER BY item.created_at DESC
            LIMIT 1
            """,
            (pilot_run_id, image_id),
        ).fetchone()
        if item is None:
            raise ValueError("Pilot image-only review item not found")
        annotation_item_id = str(item["annotation_item_id"])

    expected_payloads: dict[str, dict[str, Any]] = {
        "role": {"role_code": role_code},
        "eligibility": {
            "eligibility_label": eligibility_label,
            "eligibility_reason_codes": list(eligibility_reason_codes),
        },
    }
    event_ids: dict[str, str] = {}
    for event_type, payload in expected_payloads.items():
        with open_database(workspace.database_path, readonly=True) as connection:
            existing = _latest_event_for_annotator(
                connection,
                annotation_item_id,
                event_type,
                annotator_id,
            )
            if existing is not None:
                if (
                    json.loads(existing["after_json"]) != payload
                    or existing["review_provenance"]
                    != "owner_delegated_agent"
                    or existing["owner_review_delegation_id"]
                    != owner_review_delegation_id
                ):
                    raise RuntimeError(
                        "delegated reviewer already recorded different evidence"
                    )
                event_ids[event_type] = str(
                    existing["annotation_event_id"]
                )
                continue
        result = append_annotation_event(
            workspace,
            annotation_item_id=annotation_item_id,
            annotator_id=annotator_id,
            annotation_type=event_type,
            payload=payload,
            review_provenance="owner_delegated_agent",
            owner_review_delegation_id=owner_review_delegation_id,
        )
        event_ids[event_type] = str(result["annotation_event_id"])

    approve_pilot_item(
        workspace,
        annotation_item_id=annotation_item_id,
        annotator_id=annotator_id,
    )
    return {
        "pilot_run_id": pilot_run_id,
        "image_id": image_id,
        "annotation_item_id": annotation_item_id,
        "annotator_id": annotator_id,
        "role_code": role_code,
        "eligibility_label": eligibility_label,
        "eligibility_reason_codes": list(eligibility_reason_codes),
        "review_provenance": "owner_delegated_agent",
        "owner_review_delegation_id": owner_review_delegation_id,
        "annotation_event_ids": event_ids,
        "status": "approved",
    }


def annotation_progress(
    workspace: Workspace,
    *,
    annotation_set_id: str,
) -> dict[str, Any]:
    with open_database(workspace.database_path, readonly=True) as connection:
        set_row = connection.execute(
            "SELECT * FROM annotation_sets WHERE annotation_set_id = ?",
            (annotation_set_id,),
        ).fetchone()
        if set_row is None:
            raise KeyError("annotation set not found")
        item_status = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM annotation_items WHERE annotation_set_id = ?
                GROUP BY status
                """,
                (annotation_set_id,),
            )
        }
        event_types = {
            row["annotation_type"]: row["count"]
            for row in connection.execute(
                """
                SELECT event.annotation_type, COUNT(*) AS count
                FROM annotation_events AS event
                JOIN annotation_items AS item
                  ON item.annotation_item_id = event.annotation_item_id
                WHERE item.annotation_set_id = ?
                GROUP BY event.annotation_type
                """,
                (annotation_set_id,),
            )
        }
        context_sample = None
        policy = connection.execute(
            """
            SELECT * FROM context_review_sampling_policies
            WHERE annotation_set_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (annotation_set_id,),
        ).fetchone()
        if policy is not None:
            sample_status = {
                row["status"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT item.status, COUNT(*) AS count
                    FROM context_review_sample_items AS sample
                    JOIN annotation_items AS item
                      ON item.annotation_item_id =
                         sample.annotation_item_id
                    WHERE sample.context_review_sampling_policy_id = ?
                    GROUP BY item.status
                    """,
                    (policy["context_review_sampling_policy_id"],),
                )
            }
            context_sample = {
                "policy_id": policy[
                    "context_review_sampling_policy_id"
                ],
                "policy_version": policy["policy_version"],
                "target_count": policy["target_count"],
                "status": sample_status,
            }
        return {
            "annotation_set_id": annotation_set_id,
            "status": set_row["status"],
            "item_status": item_status,
            "event_types": event_types,
            "context_review_sample": context_sample,
        }


def set_annotation_item_decision(
    workspace: Workspace,
    *,
    annotation_item_id: str,
    decision: str,
) -> dict[str, str]:
    if decision not in {"accepted", "rejected"}:
        raise ValueError("decision must be accepted or rejected")
    with open_database(workspace.database_path) as connection:
        item = connection.execute(
            """
            SELECT item.*, annotation_set.purpose, annotation_set.status AS set_status
            FROM annotation_items AS item
            JOIN annotation_sets AS annotation_set
              ON annotation_set.annotation_set_id = item.annotation_set_id
            WHERE item.annotation_item_id = ?
            """,
            (annotation_item_id,),
        ).fetchone()
        if item is None:
            raise KeyError("annotation item not found")
        if item["purpose"] != "role_eligibility_mask_multishade":
            raise ValueError("accept/reject decisions are only for Stage 2.5")
        if item["set_status"] != "open":
            raise ValueError("annotation set is not open")
        if decision == "accepted" and not all(
            _latest_event(connection, annotation_item_id, event_type)
            is not None
            for event_type in ("role", "eligibility")
        ):
            raise ValueError("role and eligibility are required before acceptance")
        coverage_tags = set(json.loads(item["coverage_tags_json"]))
        if decision == "accepted" and "blind_review_required" in coverage_tags:
            blind_values: dict[str, dict[str, Any]] = {}
            for event_type, value_column in (
                ("role", "role_code"),
                ("eligibility", "eligibility_label"),
            ):
                latest_by_annotator: dict[str, Any] = {}
                for event in connection.execute(
                    f"""
                    SELECT event.annotator_id, event.{value_column}
                    FROM annotation_events AS event
                    WHERE event.annotation_item_id = ?
                      AND event.annotation_type = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM annotation_events AS revocation
                          WHERE revocation.annotation_type = 'revoke'
                            AND revocation.supersedes_event_id =
                                event.annotation_event_id
                      )
                    ORDER BY event.created_at, event.annotation_event_id
                    """,
                    (annotation_item_id, event_type),
                ):
                    latest_by_annotator[event["annotator_id"]] = event[
                        value_column
                    ]
                if len(latest_by_annotator) < 2:
                    raise ValueError(
                        "blind-review items require two distinct reviewers"
                    )
                blind_values[event_type] = latest_by_annotator
            adjudication = _latest_event(
                connection,
                annotation_item_id,
                "adjudication",
            )
            adjudicated = (
                json.loads(adjudication["after_json"])
                if adjudication is not None
                else {}
            )
            if (
                len(set(blind_values["role"].values())) > 1
                and adjudicated.get("role_code") not in ROLE_CODES
            ):
                raise ValueError("role conflict requires adjudication")
            if (
                len(set(blind_values["eligibility"].values())) > 1
                and not isinstance(
                    adjudicated.get("eligibility_label"),
                    bool,
                )
            ):
                raise ValueError("eligibility conflict requires adjudication")
        connection.execute(
            "UPDATE annotation_items SET status = ? WHERE annotation_item_id = ?",
            (decision, annotation_item_id),
        )
        connection.commit()
    return {
        "annotation_item_id": annotation_item_id,
        "status": decision,
    }


def validate_and_freeze_stage2_5(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    annotation_set_id: str,
    approved_by: str | None,
) -> dict[str, Any]:
    annotation = settings.section("annotation")
    with open_database(workspace.database_path) as connection:
        annotation_set = connection.execute(
            "SELECT * FROM annotation_sets WHERE annotation_set_id = ?",
            (annotation_set_id,),
        ).fetchone()
        if annotation_set is None:
            raise KeyError("annotation set not found")
        candidate_items = list(
            connection.execute(
                """
                SELECT * FROM annotation_items
                WHERE annotation_set_id = ?
                """,
                (annotation_set_id,),
            )
        )
        items = [
            item for item in candidate_items if item["status"] == "accepted"
        ]
        role_counts: Counter[str] = Counter()
        completed = 0
        mask_items = 0
        multishade_items = 0
        mask_role_counts: Counter[str] = Counter()
        multishade_role_counts: Counter[str] = Counter()
        blind_review_items = 0
        unresolved_conflicts = 0
        slice_counts: Counter[str] = Counter()
        for item in items:
            coverage_tags = set(json.loads(item["coverage_tags_json"]))
            slice_counts.update(coverage_tags)
            role_events = list(
                connection.execute(
                    """
                    SELECT role_code, annotator_id, created_at,
                           annotation_event_id
                    FROM annotation_events AS event
                    WHERE event.annotation_item_id = ?
                      AND event.annotation_type = 'role'
                      AND NOT EXISTS (
                          SELECT 1 FROM annotation_events AS revocation
                          WHERE revocation.annotation_type = 'revoke'
                            AND revocation.supersedes_event_id =
                                event.annotation_event_id
                      )
                    ORDER BY created_at, annotation_event_id
                    """,
                    (item["annotation_item_id"],),
                )
            )
            eligibility_events = list(
                connection.execute(
                    """
                    SELECT eligibility_label, annotator_id, created_at,
                           annotation_event_id
                    FROM annotation_events AS event
                    WHERE event.annotation_item_id = ?
                      AND event.annotation_type = 'eligibility'
                      AND NOT EXISTS (
                          SELECT 1 FROM annotation_events AS revocation
                          WHERE revocation.annotation_type = 'revoke'
                            AND revocation.supersedes_event_id =
                                event.annotation_event_id
                      )
                    ORDER BY created_at, annotation_event_id
                    """,
                    (item["annotation_item_id"],),
                )
            )
            latest_role_by_annotator = {
                event["annotator_id"]: event["role_code"]
                for event in role_events
            }
            latest_eligibility_by_annotator = {
                event["annotator_id"]: bool(event["eligibility_label"])
                for event in eligibility_events
            }
            adjudication = _latest_event(
                connection,
                item["annotation_item_id"],
                "adjudication",
            )
            adjudicated_payload = (
                json.loads(adjudication["after_json"])
                if adjudication is not None
                else {}
            )
            is_blind = "blind_review_required" in coverage_tags

            resolved_role = adjudicated_payload.get("role_code")
            resolved_eligibility = adjudicated_payload.get(
                "eligibility_label"
            )
            role_conflict = False
            eligibility_conflict = False
            if resolved_role not in ROLE_CODES:
                resolved_role = None
            if not isinstance(resolved_eligibility, bool):
                resolved_eligibility = None

            if resolved_role is None:
                role_values = set(latest_role_by_annotator.values())
                if is_blind:
                    if (
                        len(latest_role_by_annotator) >= 2
                        and len(role_values) == 1
                    ):
                        resolved_role = next(iter(role_values))
                    elif len(role_values) > 1:
                        role_conflict = True
                elif role_events:
                    resolved_role = role_events[-1]["role_code"]
            if resolved_eligibility is None:
                eligibility_values = set(
                    latest_eligibility_by_annotator.values()
                )
                if is_blind:
                    if (
                        len(latest_eligibility_by_annotator) >= 2
                        and len(eligibility_values) == 1
                    ):
                        resolved_eligibility = next(iter(eligibility_values))
                    elif len(eligibility_values) > 1:
                        eligibility_conflict = True
                elif eligibility_events:
                    resolved_eligibility = bool(
                        eligibility_events[-1]["eligibility_label"]
                    )

            if role_conflict or eligibility_conflict:
                unresolved_conflicts += 1
            if resolved_role is not None and resolved_eligibility is not None:
                completed += 1
                role_counts[resolved_role] += 1
            has_mask = _latest_event(
                connection,
                item["annotation_item_id"],
                "mask",
            )
            if has_mask:
                mask_items += 1
                if resolved_role is not None:
                    mask_role_counts[resolved_role] += 1
            has_multishade = _latest_event(
                connection, item["annotation_item_id"], "multi_shade"
            )
            if has_multishade:
                multishade_items += 1
                if resolved_role is not None:
                    multishade_role_counts[resolved_role] += 1
            if (
                is_blind
                and len(latest_role_by_annotator) >= 2
                and len(latest_eligibility_by_annotator) >= 2
            ):
                blind_review_items += 1
        groups_by_split: dict[str, set[str]] = defaultdict(set)
        for item in items:
            groups_by_split[item["split"]].add(item["group_id"])
        split_leakage = sum(
            bool(groups_by_split[left] & groups_by_split[right])
            for index, left in enumerate(("train", "validation", "test"))
            for right in ("train", "validation", "test")[index + 1 :]
        )
        target = int(annotation["accepted_unique_images"])
        role_target = int(annotation["target_per_primary_role"])
        accepted_split_counts = Counter(
            item["split"] for item in items
        )
        accepted_split_targets = {
            "train": target * 60 // 100,
            "validation": target * 20 // 100,
        }
        accepted_split_targets["test"] = (
            target
            - accepted_split_targets["train"]
            - accepted_split_targets["validation"]
        )
        hard_gates = {
            "candidate_pool": len(candidate_items) >= target,
            "accepted_item_count": len(items) == target,
            "completed": completed == target,
            "role_balance": all(
                role_counts.get(role, 0) == role_target for role in ROLE_CODES
            ),
            "mask_subset": mask_items >= int(annotation["mask_subset"]),
            "mask_role_coverage": all(
                mask_role_counts[role] > 0
                for role in (
                    "single_bullet",
                    "single_swatch",
                    "lip_effect",
                    "multi_shade_comparison",
                    "color_card",
                )
            ),
            "multi_shade_subset": multishade_items
            >= int(annotation["multi_shade_subset"]),
            "multi_shade_role_coverage": all(
                multishade_role_counts[role] > 0
                for role in ("multi_shade_comparison", "color_card")
            ),
            "blind_review_subset": blind_review_items
            >= int(annotation["blind_review_subset"]),
            "long_slice": slice_counts["long_image"] >= 24,
            "format_mismatch_slice": slice_counts["format_mismatch"] >= 24,
            "duplicate_slice": slice_counts["duplicate_multi_occurrence"] >= 48,
            "collision_slice": slice_counts["folder_collision"] >= 24,
            "invalid_slice": (
                slice_counts["invalid_or_decorative_candidate"] >= 12
            ),
            "split_leakage": split_leakage == 0,
            "split_balance": all(
                accepted_split_counts[split] == split_target
                for split, split_target in accepted_split_targets.items()
            ),
            "adjudicated_conflicts": unresolved_conflicts == 0,
        }
        can_freeze = all(hard_gates.values()) and bool(approved_by)
        evaluation_set_id = None
        if can_freeze:
            run_config = connection.execute(
                "SELECT config_json FROM pipeline_runs WHERE run_id = ?",
                (annotation_set["run_id"],),
            ).fetchone()
            config_payload = json.loads(run_config["config_json"])
            pilot_run_id = config_payload.get("run_overrides", {}).get(
                "pilot_run_id"
            )
            if not pilot_run_id:
                raise RuntimeError(
                    "Stage 2.5 run is missing its Pilot lineage"
                )
            evaluation_set_id = stable_id(
                "evaluation_set", annotation_set_id, "1.0.0"
            )
            connection.execute(
                """
                INSERT INTO evaluation_sets(
                    evaluation_set_id, run_id, name, version,
                    source_annotation_set_ids_json, selection_rules_json,
                    content_grouping_method, split_policy_json,
                    metric_schema_version, status, created_at, frozen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_set_id,
                    annotation_set["run_id"],
                    (
                        "stage2_5_fixed_evaluation:"
                        f"{annotation_set['run_id']}"
                    ),
                    "1.0.0",
                    canonical_json([annotation_set_id]),
                    annotation_set["selection_rules_json"],
                    annotation_set["content_grouping_method"],
                    canonical_json(
                        {
                            "ratios": {
                                "train": 0.6,
                                "validation": 0.2,
                                "test": 0.2,
                            },
                            "no_group_leakage": True,
                        }
                    ),
                    "lipcolor-metrics-1.0",
                    "frozen",
                    utc_now(),
                    utc_now(),
                ),
            )
            for item in items:
                connection.execute(
                    """
                    INSERT INTO evaluation_set_items(
                        evaluation_set_item_id, evaluation_set_id,
                        annotation_item_id, image_id, image_occurrence_id,
                        group_id, split, slice_tags_json, ground_truth_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stable_id(
                            "evaluation_item",
                            evaluation_set_id,
                            item["annotation_item_id"],
                        ),
                        evaluation_set_id,
                        item["annotation_item_id"],
                        item["image_id"],
                        item["image_occurrence_id"],
                        item["group_id"],
                        item["split"],
                        item["coverage_tags_json"],
                        "annotation-event-1.0",
                    ),
                )
            connection.execute(
                """
                UPDATE annotation_sets SET status = 'frozen', frozen_at = ?
                WHERE annotation_set_id = ?
                """,
                (utc_now(), annotation_set_id),
            )
            _insert_provisional_threshold_definitions(
                connection,
                pilot_run_id,
                annotation_set_id,
                approved_by,
            )
            finish_pipeline_run(
                connection,
                annotation_set["run_id"],
                status="completed",
                error_summary={},
            )
        connection.commit()
        summary = {
            "schema_version": "stage2-5-validation-1",
            "annotation_set_id": annotation_set_id,
            "status": (
                "frozen"
                if can_freeze
                else "awaiting_approval"
                if all(hard_gates.values())
                else "incomplete"
            ),
            "evaluation_set_id": evaluation_set_id,
            "candidate_item_count": len(candidate_items),
            "accepted_item_count": len(items),
            "completed": completed,
            "role_counts": dict(role_counts),
            "mask_items": mask_items,
            "mask_role_counts": dict(mask_role_counts),
            "multi_shade_items": multishade_items,
            "multi_shade_role_counts": dict(multishade_role_counts),
            "blind_review_items": blind_review_items,
            "unresolved_conflicts": unresolved_conflicts,
            "slice_counts": dict(slice_counts),
            "split_leakage": split_leakage,
            "split_counts": dict(sorted(accepted_split_counts.items())),
            "split_targets": accepted_split_targets,
            "hard_gates": hard_gates,
            "approved_by": approved_by,
        }
        run_dir = workspace.run_dir(annotation_set["run_id"])
        write_json_snapshot(
            run_dir / "reports",
            "stage2_5_validation",
            summary,
        )
        export_tables_jsonl(
            connection, run_dir / "jsonl", STAGE2_5_TABLES
        )
        return summary


def _insert_provisional_threshold_definitions(
    connection: sqlite3.Connection,
    pilot_run_id: str,
    annotation_set_id: str,
    approved_by: str,
) -> None:
    definitions = (
        ("role_macro_f1", ">=", 0.85),
        ("eligibility_precision", ">=", 0.90),
        ("eligibility_recall", ">=", None),
        ("eligibility_f1", ">=", None),
        ("eligibility_coverage", ">=", None),
        ("product_color_evidence_coverage", ">=", None),
        ("ocr_shade_code_exact_match", ">=", 0.95),
        ("ocr_cer", "<=", 0.05),
        ("color_median_delta_e00", "<=", 5.0),
        ("color_p90_delta_e00", "<=", 10.0),
        ("multi_shade_whole_image_exact_match", ">=", None),
    )
    for metric, operator, target in definitions:
        connection.execute(
            """
            INSERT OR IGNORE INTO performance_threshold_versions(
                threshold_version, metric_name, metric_definition_version,
                slice_name, operator, target_value, status, pilot_run_id,
                annotation_set_id, baseline_value, sample_count, rationale,
                approved_by, created_at, frozen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, NULL)
            """,
            (
                "pilot-and-annotation-draft-v1",
                metric,
                "lipcolor-metrics-1.0",
                "all",
                operator,
                target,
                "provisional_target",
                pilot_run_id,
                annotation_set_id,
                (
                    "Metric definition approved; numeric target remains "
                    "provisional until adequate evaluated predictions exist."
                ),
                approved_by,
                utc_now(),
            ),
        )


def compute_pilot_baseline(
    workspace: Workspace,
    *,
    evaluation_set_id: str,
    pilot_run_id: str,
) -> dict[str, Any]:
    with open_database(workspace.database_path) as connection:
        evaluation = connection.execute(
            "SELECT * FROM evaluation_sets WHERE evaluation_set_id = ?",
            (evaluation_set_id,),
        ).fetchone()
        if evaluation is None:
            raise KeyError("evaluation set not found")
        prediction_rows = list(
            connection.execute(
                """
                SELECT
                    item.annotation_item_id,
                    prediction.primary_role AS predicted_role,
                    prediction.representative_color_eligible
                        AS predicted_eligible
                FROM evaluation_set_items AS evaluation_item
                JOIN annotation_items AS item
                  ON item.annotation_item_id = evaluation_item.annotation_item_id
                JOIN content_visual_analyses AS prediction
                  ON prediction.image_id = item.image_id
                 AND prediction.run_id = ?
                 AND prediction.analysis_scope IN (
                     'image', 'merged_content_summary'
                 )
                WHERE evaluation_item.evaluation_set_id = ?
                """,
                (pilot_run_id, evaluation_set_id),
            )
        )
        pairs: list[dict[str, Any]] = []
        for row in prediction_rows:
            truth = _resolved_item_truth(
                connection,
                row["annotation_item_id"],
            )
            pairs.append(
                {
                    "annotation_item_id": row["annotation_item_id"],
                    "predicted_role": row["predicted_role"],
                    "predicted_eligible": row["predicted_eligible"],
                    "true_role": truth["role_code"],
                    "true_eligible": truth["eligibility_label"],
                }
            )
        role_details = _role_metrics(pairs)
        eligibility = _binary_metrics(
            [
                (bool(row["predicted_eligible"]), bool(row["true_eligible"]))
                for row in pairs
                if row["true_eligible"] is not None
            ]
        )
        run_id = evaluation["run_id"]
        evaluation_run_id = stable_id(
            "evaluation_run", evaluation_set_id, pilot_run_id, utc_now()
        )
        connection.execute(
            """
            INSERT INTO evaluation_runs(
                evaluation_run_id, run_id, evaluation_set_id,
                prediction_run_id, metric_schema_version, status,
                started_at, finished_at, summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_run_id,
                run_id,
                evaluation_set_id,
                pilot_run_id,
                "lipcolor-metrics-1.0",
                "completed",
                utc_now(),
                utc_now(),
                canonical_json(
                    {
                        "evaluated_overlap": len(pairs),
                        "unimplemented_metrics_are_not_zero": True,
                    }
                ),
            ),
        )
        metrics = [
            (
                "role_macro_f1",
                role_details.get("macro_f1"),
                "evaluated" if pairs else "not_evaluated",
                len(pairs),
                role_details,
            ),
            (
                "eligibility_precision",
                eligibility.get("precision"),
                "evaluated" if eligibility["count"] else "not_evaluated",
                eligibility["count"],
                eligibility,
            ),
            (
                "eligibility_recall",
                eligibility.get("recall"),
                "evaluated" if eligibility["count"] else "not_evaluated",
                eligibility["count"],
                eligibility,
            ),
            (
                "eligibility_f1",
                eligibility.get("f1"),
                "evaluated" if eligibility["count"] else "not_evaluated",
                eligibility["count"],
                eligibility,
            ),
            (
                "eligibility_coverage",
                1.0 if eligibility["count"] else None,
                "evaluated" if eligibility["count"] else "not_evaluated",
                eligibility["count"],
                {"abstain": 0},
            ),
        ]
        for name in (
            "product_color_evidence_coverage",
            "ocr_shade_code_exact_match",
            "ocr_cer",
            "color_median_delta_e00",
            "color_p90_delta_e00",
            "multi_shade_whole_image_exact_match",
        ):
            metrics.append((name, None, "not_evaluated", 0, {}))
        for name, value, status, count, details in metrics:
            connection.execute(
                """
                INSERT INTO evaluation_metrics(
                    evaluation_metric_id, evaluation_run_id, metric_name,
                    slice_name, metric_value, evaluation_status,
                    sample_count, details_json
                ) VALUES (?, ?, ?, 'all', ?, ?, ?, ?)
                """,
                (
                    stable_id(
                        "evaluation_metric", evaluation_run_id, name, "all"
                    ),
                    evaluation_run_id,
                    name,
                    value,
                    status,
                    count,
                    canonical_json(details),
                ),
            )
        connection.commit()
        return {
            "evaluation_run_id": evaluation_run_id,
            "evaluated_overlap": len(pairs),
            "role": role_details,
            "eligibility": eligibility,
            "not_evaluated": [
                name for name, _, status, _, _ in metrics if status == "not_evaluated"
            ],
        }


def _resolved_item_truth(
    connection: sqlite3.Connection,
    annotation_item_id: str,
) -> dict[str, Any]:
    item = connection.execute(
        """
        SELECT coverage_tags_json FROM annotation_items
        WHERE annotation_item_id = ?
        """,
        (annotation_item_id,),
    ).fetchone()
    if item is None:
        raise KeyError("annotation item not found")
    is_blind = "blind_review_required" in set(
        json.loads(item["coverage_tags_json"])
    )
    adjudication = _latest_event(
        connection,
        annotation_item_id,
        "adjudication",
    )
    adjudicated = (
        json.loads(adjudication["after_json"])
        if adjudication is not None
        else {}
    )
    resolved: dict[str, Any] = {
        "role_code": (
            adjudicated.get("role_code")
            if adjudicated.get("role_code") in ROLE_CODES
            else None
        ),
        "eligibility_label": (
            adjudicated.get("eligibility_label")
            if isinstance(adjudicated.get("eligibility_label"), bool)
            else None
        ),
    }
    for annotation_type, output_key, value_column in (
        ("role", "role_code", "role_code"),
        ("eligibility", "eligibility_label", "eligibility_label"),
    ):
        if resolved[output_key] is not None:
            continue
        events = list(
            connection.execute(
                f"""
                SELECT event.annotator_id, event.{value_column}
                FROM annotation_events AS event
                WHERE event.annotation_item_id = ?
                  AND event.annotation_type = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM annotation_events AS revocation
                      WHERE revocation.annotation_type = 'revoke'
                        AND revocation.supersedes_event_id =
                            event.annotation_event_id
                  )
                ORDER BY event.created_at, event.annotation_event_id
                """,
                (annotation_item_id, annotation_type),
            )
        )
        if not events:
            continue
        if not is_blind:
            value = events[-1][value_column]
            resolved[output_key] = (
                bool(value) if output_key == "eligibility_label" else value
            )
            continue
        latest_by_annotator = {
            event["annotator_id"]: event[value_column] for event in events
        }
        values = set(latest_by_annotator.values())
        if len(latest_by_annotator) >= 2 and len(values) == 1:
            value = next(iter(values))
            resolved[output_key] = (
                bool(value) if output_key == "eligibility_label" else value
            )
    return resolved


def _role_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if row["true_role"] is not None:
            confusion[row["true_role"]][row["predicted_role"]] += 1
    per_class: dict[str, dict[str, float | int]] = {}
    f1_values: list[float] = []
    for role in ROLE_CODES:
        tp = confusion[role][role]
        fp = sum(confusion[truth][role] for truth in ROLE_CODES if truth != role)
        fn = sum(
            count for predicted, count in confusion[role].items() if predicted != role
        )
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
        support = sum(confusion[role].values())
        per_class[role] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        if support:
            f1_values.append(f1)
    return {
        "macro_f1": sum(f1_values) / len(f1_values) if f1_values else None,
        "per_class": per_class,
        "confusion": {
            truth: dict(predictions)
            for truth, predictions in confusion.items()
        },
    }


def _binary_metrics(pairs: Sequence[tuple[bool, bool]]) -> dict[str, Any]:
    tp = sum(predicted and truth for predicted, truth in pairs)
    fp = sum(predicted and not truth for predicted, truth in pairs)
    fn = sum(not predicted and truth for predicted, truth in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "count": len(pairs),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }
