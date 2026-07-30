"""Auditable Stage 2.6 observed-colour similarity baseline."""

from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np

from .color_extraction import srgb_to_lab
from .color_similarity import (
    ALGORITHM_VERSION as CIEDE2000_VERSION,
    classify_distance_band,
    color_difference_diagnostics,
    delta_e_ciede2000_array,
    delta_e_to_similarity,
    hex_to_rgb,
    lab_to_lch,
    normalize_hex,
    pair_quality_tier,
)
from .settings import PipelineSettings, canonical_json
from .stage1_manifest import sha256_file, stable_id
from .workspace import (
    Workspace,
    begin_pipeline_run,
    ensure_run_directory,
    finish_pipeline_run,
    open_database,
    utc_now,
    write_json_snapshot,
)


SOURCE_MANIFEST_SCHEMA = "observed-similarity-source-1.0"
OUTPUT_SEMANTICS = "image_observed_color_similarity_baseline"
ENTITY_ALGORITHM_VERSION = "shade-entity-profile-1.0"
EXPORT_SCHEMA_VERSION = "observed-similarity-export-1.0"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHADE_CODE_PATTERN = re.compile(
    r"(?<![A-Z0-9])#?\s*([A-Z]*\d+[A-Z0-9-]*)"
)
_QUALITY_ORDER = {"medium": 0, "high": 1}


@dataclass(frozen=True)
class SimilaritySourceItem:
    sequence: int
    image_id: str
    source_quick_run_id: str
    selection_reason: str
    manifest_payload: Mapping[str, Any]


@dataclass(frozen=True)
class SimilarityOptions:
    top_k: int
    max_delta_e00: float | None
    lab_integrity_tolerance: float
    display_score_scale: float
    display_score_version: str
    pair_block_size: int
    pair_insert_batch_size: int
    formal_region_types: tuple[str, ...]
    accepted_color_confidence: tuple[str, ...]
    implementation_version: str


def normalize_shade_code_tokens(value: str | None) -> tuple[str, ...]:
    """Extract ordered unique alphanumeric shade-code tokens with digits."""

    normalized = unicodedata.normalize("NFKC", value or "").upper()
    return tuple(
        dict.fromkeys(
            match.group(1).replace(" ", "")
            for match in _SHADE_CODE_PATTERN.finditer(normalized)
        )
    )


def _resolve_manifest_path(
    settings: PipelineSettings,
    source_manifest: Path,
) -> Path:
    path = Path(source_manifest)
    if not path.is_absolute():
        path = settings.repo_root / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"source manifest not found: {path}")
    return path


def load_similarity_source_manifest(
    path: Path,
) -> list[SimilaritySourceItem]:
    """Read and strictly validate the per-image Stage 2.6 source manifest."""

    items: list[SimilaritySourceItem] = []
    seen_images: set[str] = set()
    seen_sequences: set[int] = set()
    manifest_version: str | None = None
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"invalid source manifest JSON at line {line_number}"
            ) from error
        if not isinstance(payload, dict):
            raise ValueError(
                f"source manifest line {line_number} must be an object"
            )
        if payload.get("schema_version") != SOURCE_MANIFEST_SCHEMA:
            raise ValueError(
                f"source manifest line {line_number} has wrong schema_version"
            )
        current_version = str(payload.get("manifest_version", "")).strip()
        if not current_version:
            raise ValueError(
                f"source manifest line {line_number} needs manifest_version"
            )
        if manifest_version is None:
            manifest_version = current_version
        elif current_version != manifest_version:
            raise ValueError("source manifest versions must be identical")
        try:
            sequence = int(payload["sequence"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"source manifest line {line_number} needs integer sequence"
            ) from error
        image_id = str(payload.get("image_id", "")).strip()
        source_run = str(payload.get("source_quick_run_id", "")).strip()
        reason = str(payload.get("selection_reason", "")).strip()
        if sequence < 1 or sequence in seen_sequences:
            raise ValueError("source manifest sequences must be unique and positive")
        if not _SHA256_PATTERN.fullmatch(image_id):
            raise ValueError(
                f"source manifest line {line_number} has invalid image_id"
            )
        if payload.get("sha256") not in (None, image_id):
            raise ValueError(
                f"source manifest line {line_number} has SHA mismatch"
            )
        if image_id in seen_images:
            raise ValueError("source manifest image IDs must be unique")
        if not source_run or not reason:
            raise ValueError(
                f"source manifest line {line_number} needs source run and reason"
            )
        seen_sequences.add(sequence)
        seen_images.add(image_id)
        items.append(
            SimilaritySourceItem(
                sequence=sequence,
                image_id=image_id,
                source_quick_run_id=source_run,
                selection_reason=reason,
                manifest_payload=payload,
            )
        )
    if not items:
        raise ValueError("source manifest contains no items")
    items.sort(key=lambda item: item.sequence)
    if [item.sequence for item in items] != list(range(1, len(items) + 1)):
        raise ValueError("source manifest sequences must be contiguous from one")
    return items


def _options(
    settings: PipelineSettings,
    *,
    top_k: int | None,
    max_delta_e00: float | None,
) -> SimilarityOptions:
    config = settings.section("shade_similarity")
    if config.get("source_manifest_schema") != SOURCE_MANIFEST_SCHEMA:
        raise ValueError("configured source manifest schema is unsupported")
    if config.get("output_semantics") != OUTPUT_SEMANTICS:
        raise ValueError("configured output semantics is unsupported")
    resolved_top_k = int(config["top_k"] if top_k is None else top_k)
    configured_max = config.get("max_delta_e00")
    resolved_max = configured_max if max_delta_e00 is None else max_delta_e00
    if resolved_top_k <= 0:
        raise ValueError("top_k must be positive")
    if resolved_max is not None:
        resolved_max = float(resolved_max)
        if not math.isfinite(resolved_max) or resolved_max < 0.0:
            raise ValueError("max_delta_e00 must be finite and non-negative")
    tolerance = float(config["lab_integrity_tolerance"])
    score_scale = float(config["display_score_scale"])
    block_size = int(config["pair_block_size"])
    insert_size = int(config["pair_insert_batch_size"])
    if tolerance <= 0.0:
        raise ValueError("lab_integrity_tolerance must be positive")
    if score_scale <= 0.0:
        raise ValueError("display_score_scale must be positive")
    if block_size <= 0 or insert_size <= 0:
        raise ValueError("pair block and insert sizes must be positive")
    formal_types = tuple(str(value) for value in config["formal_region_types"])
    accepted_quality = tuple(
        str(value) for value in config["accepted_color_confidence"]
    )
    if formal_types != ("swatch",):
        raise ValueError("similarity MVP formal profile must remain swatch-only")
    if set(accepted_quality) != {"medium", "high"}:
        raise ValueError("similarity MVP accepts exactly medium/high colours")
    return SimilarityOptions(
        top_k=resolved_top_k,
        max_delta_e00=resolved_max,
        lab_integrity_tolerance=tolerance,
        display_score_scale=score_scale,
        display_score_version=str(config["display_score_version"]),
        pair_block_size=block_size,
        pair_insert_batch_size=insert_size,
        formal_region_types=formal_types,
        accepted_color_confidence=accepted_quality,
        implementation_version=str(config["implementation_version"]),
    )


def _chunks(values: Sequence[str], size: int = 500) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _validated_inputs(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    items: Sequence[SimilaritySourceItem],
    manifest_path: Path,
    manifest_sha256: str,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in items:
        row = connection.execute(
            """
            SELECT extraction.*, source_run.stage AS source_stage
            FROM quick_image_extractions AS extraction
            JOIN pipeline_runs AS source_run
              ON source_run.run_id = extraction.run_id
            WHERE extraction.run_id = ? AND extraction.image_id = ?
            """,
            (item.source_quick_run_id, item.image_id),
        ).fetchone()
        if row is None:
            raise KeyError(
                "source manifest points to missing quick extraction: "
                f"{item.source_quick_run_id}/{item.image_id}"
            )
        if row["source_stage"] != "stage2.6":
            raise ValueError("source manifest may only reference Stage 2.6 runs")
        if row["status"] != "success":
            raise ValueError(
                "source manifest must reference successful canonical images: "
                f"{item.source_quick_run_id}/{item.image_id}={row['status']}"
            )
        if row["output_semantics"] != "image_observed_color_candidate":
            raise ValueError("source quick extraction has unexpected semantics")
        selected.append(
            {
                "shade_similarity_input_id": stable_id(
                    "shade_similarity_input",
                    run_id,
                    item.image_id,
                ),
                "run_id": run_id,
                "source_quick_run_id": item.source_quick_run_id,
                "quick_image_extraction_id": row[
                    "quick_image_extraction_id"
                ],
                "image_id": item.image_id,
                "sequence": item.sequence,
                "input_status": "selected",
                "source_manifest_path": manifest_path.as_posix(),
                "source_manifest_sha256": manifest_sha256,
                "selection_reason": item.selection_reason,
                "evidence_json": canonical_json(dict(item.manifest_payload)),
                "created_at": utc_now(),
            }
        )
    return selected


def _source_records_by_image(
    connection: sqlite3.Connection,
    image_ids: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for chunk in _chunks(list(image_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"""
            SELECT occurrence.image_id,
                   source.source_record_id,
                   source.sku_id_raw,
                   source.goods_id_raw,
                   source.brand_id_raw,
                   source.brand_name_raw,
                   source.sku_name_raw,
                   source.sku_concat_name_raw,
                   source.sku_color_no_raw
            FROM image_occurrences AS occurrence
            JOIN source_ref_occurrences AS link
              ON link.image_occurrence_id =
                 occurrence.image_occurrence_id
            JOIN source_image_refs AS reference
              ON reference.source_ref_id = link.source_ref_id
            JOIN source_records AS source
              ON source.source_record_id = reference.source_record_id
            WHERE occurrence.image_id IN ({placeholders})
            ORDER BY occurrence.image_id, source.source_record_id
            """,
            list(chunk),
        ):
            image_key = str(row["image_id"])
            result.setdefault(image_key, {})[
                str(row["source_record_id"])
            ] = dict(row)
    return {
        image_id: list(records.values())
        for image_id, records in result.items()
    }


def _decode_json_list(value: Any, field: str) -> tuple[list[Any], str | None]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return [], f"invalid_{field}"
    if not isinstance(decoded, list):
        return [], f"invalid_{field}"
    return decoded, None


def _color_payload(
    region: Mapping[str, Any],
    *,
    tolerance: float,
) -> tuple[dict[str, Any], str | None]:
    if (
        region["color_hex"] is None
        or region["rgb_json"] is None
        or region["lab_json"] is None
    ):
        return {}, "missing_color_payload"
    try:
        color_hex = normalize_hex(str(region["color_hex"]))
        rgb_decoded = json.loads(str(region["rgb_json"]))
        lab_decoded = json.loads(str(region["lab_json"]))
        if (
            not isinstance(rgb_decoded, list)
            or len(rgb_decoded) != 3
            or any(
                isinstance(value, bool)
                or int(value) != value
                or not 0 <= int(value) <= 255
                for value in rgb_decoded
            )
        ):
            raise ValueError("invalid RGB")
        rgb = tuple(int(value) for value in rgb_decoded)
        if rgb != hex_to_rgb(color_hex):
            raise ValueError("Hex and RGB disagree")
        if not isinstance(lab_decoded, list) or len(lab_decoded) != 3:
            raise ValueError("invalid Lab")
        lab = tuple(float(value) for value in lab_decoded)
        if not all(math.isfinite(value) for value in lab):
            raise ValueError("non-finite Lab")
        recomputed = srgb_to_lab(np.asarray(rgb, dtype=np.uint8))
        maximum_error = float(
            np.max(np.abs(recomputed - np.asarray(lab, dtype=np.float64)))
        )
        if maximum_error > tolerance:
            raise ValueError(
                f"stored Lab differs from RGB by {maximum_error}"
            )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return {"integrity_error": str(error)}, "invalid_color_payload"
    return {
        "color_hex": color_hex,
        "rgb": rgb,
        "lab": lab,
        "lab_integrity_max_abs_error": maximum_error,
    }, None


def _identity_for_code(
    *,
    dataset_snapshot_id: str,
    image_id: str,
    normalized_code: str,
    source_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    matching: list[Mapping[str, Any]] = []
    for record in source_records:
        tokens = normalize_shade_code_tokens(
            str(record.get("sku_color_no_raw") or "")
        )
        if tokens == (normalized_code,):
            matching.append(record)
    candidate_skus = sorted(
        {
            str(record.get("sku_id_raw") or "").strip()
            for record in matching
            if str(record.get("sku_id_raw") or "").strip()
        }
    )
    source_record_ids = sorted(
        str(record["source_record_id"]) for record in matching
    )
    if len(candidate_skus) == 1:
        sku_id = candidate_skus[0]
        canonical = min(
            (
                record
                for record in matching
                if str(record.get("sku_id_raw") or "").strip() == sku_id
            ),
            key=lambda record: str(record["source_record_id"]),
        )
        return {
            "shade_id": stable_id(
                "shade_sku",
                dataset_snapshot_id,
                sku_id,
            ),
            "identity_status": "business_resolved",
            "source_sku_id_raw": sku_id,
            "candidate_sku_ids": candidate_skus,
            "source_record_ids": source_record_ids,
            "brand_id_raw": canonical.get("brand_id_raw"),
            "brand_name_raw": canonical.get("brand_name_raw"),
            "product_name_raw": canonical.get("sku_name_raw"),
            "source_shade_name_raw": canonical.get("sku_color_no_raw"),
        }
    status = (
        "image_local_unmatched"
        if not candidate_skus
        else "image_local_ambiguous"
    )
    return {
        "shade_id": stable_id(
            "shade_local",
            dataset_snapshot_id,
            image_id,
            normalized_code,
        ),
        "identity_status": status,
        "source_sku_id_raw": None,
        "candidate_sku_ids": candidate_skus,
        "source_record_ids": source_record_ids,
        "brand_id_raw": None,
        "brand_name_raw": None,
        "product_name_raw": None,
        "source_shade_name_raw": None,
    }


def _build_observations(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    inputs: Sequence[Mapping[str, Any]],
    options: SimilarityOptions,
) -> list[dict[str, Any]]:
    source_by_image = _source_records_by_image(
        connection,
        [str(item["image_id"]) for item in inputs],
    )
    observations: list[dict[str, Any]] = []
    for input_row in inputs:
        quick_extraction_id = str(
            input_row["quick_image_extraction_id"]
        )
        text_rows = list(
            connection.execute(
                """
                SELECT * FROM quick_text_items
                WHERE quick_image_extraction_id = ?
                ORDER BY quick_text_item_id
                """,
                (quick_extraction_id,),
            )
        )
        text_by_model_id = {
            str(row["text_item_id"]): row for row in text_rows
        }
        region_rows = list(
            connection.execute(
                """
                SELECT * FROM quick_color_regions
                WHERE quick_image_extraction_id = ?
                ORDER BY quick_color_region_id
                """,
                (quick_extraction_id,),
            )
        )
        for region in region_rows:
            reasons: list[str] = []
            evidence_errors: list[str] = []
            linked_ids, linked_error = _decode_json_list(
                region["linked_text_item_ids_json"],
                "linked_text_item_ids",
            )
            if linked_error:
                reasons.append(linked_error)
            linked_shade_rows = [
                text_by_model_id[str(linked_id)]
                for linked_id in linked_ids
                if str(linked_id) in text_by_model_id
                and text_by_model_id[str(linked_id)]["text_type"]
                == "shade_code"
            ]
            linked_shade_rows.sort(
                key=lambda row: str(row["quick_text_item_id"])
            )
            raw_texts = list(
                dict.fromkeys(
                    str(row["raw_text"]) for row in linked_shade_rows
                )
            )
            linked_tokens = {
                token
                for row in linked_shade_rows
                for token in normalize_shade_code_tokens(
                    str(row["raw_text"])
                )
            }
            direct_tokens = set(
                normalize_shade_code_tokens(region["shade_code_text"])
            )
            normalized_code: str | None = None
            if not linked_shade_rows:
                reasons.append("no_linked_shade_code")
            elif not linked_tokens:
                reasons.append("shade_code_unparseable")
            elif len(linked_tokens) > 1:
                reasons.append("shade_code_ambiguous")
            else:
                normalized_code = next(iter(linked_tokens))
            if (
                normalized_code is not None
                and direct_tokens
                and direct_tokens != {normalized_code}
            ):
                reasons.append("direct_linked_shade_code_conflict")

            identity: dict[str, Any]
            if normalized_code is None:
                identity = {
                    "shade_id": None,
                    "identity_status": "excluded",
                    "source_sku_id_raw": None,
                    "candidate_sku_ids": [],
                    "source_record_ids": [],
                    "brand_id_raw": None,
                    "brand_name_raw": None,
                    "product_name_raw": None,
                    "source_shade_name_raw": None,
                }
            else:
                identity = _identity_for_code(
                    dataset_snapshot_id=workspace.dataset_snapshot_id,
                    image_id=str(input_row["image_id"]),
                    normalized_code=normalized_code,
                    source_records=source_by_image.get(
                        str(input_row["image_id"]),
                        [],
                    ),
                )

            color_payload, color_error = _color_payload(
                region,
                tolerance=options.lab_integrity_tolerance,
            )
            if color_error:
                reasons.append(color_error)
                if color_payload.get("integrity_error"):
                    evidence_errors.append(
                        str(color_payload["integrity_error"])
                    )
            if region["extraction_status"] != "succeeded":
                reasons.append(
                    f"extraction_status_{region['extraction_status']}"
                )
            if (
                region["output_semantics"]
                != "image_observed_color_candidate"
            ):
                reasons.append("unexpected_source_semantics")
            if region["region_type"] not in options.formal_region_types:
                reasons.append("region_type_not_formal")
            if (
                region["color_confidence"]
                not in options.accepted_color_confidence
            ):
                reasons.append("color_confidence_not_accepted")
            if region["association_confidence"] is None:
                reasons.append("missing_association_confidence")
            reasons = list(dict.fromkeys(reasons))
            formal_eligible = not reasons
            canonical_text = linked_shade_rows[0] if linked_shade_rows else None
            observation_id = stable_id(
                "shade_observation",
                run_id,
                region["quick_color_region_id"],
            )
            evidence = {
                "source_quick_region_id": region[
                    "quick_color_region_id"
                ],
                "source_region_id": region["region_id"],
                "source_model_text_item_ids": [
                    str(row["text_item_id"]) for row in linked_shade_rows
                ],
                "source_region_shade_code_text": region["shade_code_text"],
                "source_region_shade_name_text": region["shade_name_text"],
                "source_region_risks": json.loads(
                    region["risks_json"] or "[]"
                ),
                "source_observations": json.loads(
                    region["source_observations_json"] or "[]"
                ),
                "deduplication": json.loads(
                    region["deduplication_json"] or "{}"
                ),
                "identity_resolution": {
                    "normalized_code": normalized_code,
                    "candidate_sku_ids": identity["candidate_sku_ids"],
                    "matching_source_record_ids": identity[
                        "source_record_ids"
                    ],
                },
                "lab_integrity_max_abs_error": color_payload.get(
                    "lab_integrity_max_abs_error"
                ),
                "errors": evidence_errors,
            }
            observations.append(
                {
                    "shade_color_observation_id": observation_id,
                    "run_id": run_id,
                    "shade_similarity_input_id": input_row[
                        "shade_similarity_input_id"
                    ],
                    "source_quick_run_id": input_row[
                        "source_quick_run_id"
                    ],
                    "quick_color_region_id": region[
                        "quick_color_region_id"
                    ],
                    "image_id": input_row["image_id"],
                    "quick_text_item_id": (
                        canonical_text["quick_text_item_id"]
                        if canonical_text is not None
                        else None
                    ),
                    "linked_shade_text_item_ids_json": canonical_json(
                        [
                            str(row["quick_text_item_id"])
                            for row in linked_shade_rows
                        ]
                    ),
                    "raw_shade_texts_json": canonical_json(raw_texts),
                    "normalized_shade_code": normalized_code,
                    "shade_id": identity["shade_id"],
                    "identity_status": identity["identity_status"],
                    "source_sku_id_raw": identity["source_sku_id_raw"],
                    "candidate_sku_ids_json": canonical_json(
                        identity["candidate_sku_ids"]
                    ),
                    "source_record_ids_json": canonical_json(
                        identity["source_record_ids"]
                    ),
                    "brand_id_raw": identity["brand_id_raw"],
                    "brand_name_raw": identity["brand_name_raw"],
                    "product_name_raw": identity["product_name_raw"],
                    "shade_name_raw": (
                        region["shade_name_text"]
                        or identity["source_shade_name_raw"]
                    ),
                    "region_type": region["region_type"],
                    "representation_profile": region["region_type"],
                    "bbox_image_json": region["bbox_image_json"],
                    "extraction_status": region["extraction_status"],
                    "output_semantics": region["output_semantics"],
                    "color_hex": color_payload.get("color_hex"),
                    "rgb_json": (
                        canonical_json(list(color_payload["rgb"]))
                        if color_payload.get("rgb") is not None
                        else None
                    ),
                    "lab_json": (
                        canonical_json(list(color_payload["lab"]))
                        if color_payload.get("lab") is not None
                        else None
                    ),
                    "model_confidence": region["model_confidence"],
                    "association_confidence": region[
                        "association_confidence"
                    ],
                    "color_confidence": region["color_confidence"],
                    "valid_pixel_count": region["valid_pixel_count"],
                    "valid_pixel_ratio": region["valid_pixel_ratio"],
                    "cluster_proportion": region["cluster_proportion"],
                    "dispersion": region["dispersion"],
                    "formal_eligible": int(formal_eligible),
                    "exclusion_reasons_json": canonical_json(reasons),
                    "algorithm_version": ENTITY_ALGORITHM_VERSION,
                    "evidence_json": canonical_json(evidence),
                    "created_at": utc_now(),
                    "_raw_shade_texts": raw_texts,
                    "_lab": color_payload.get("lab"),
                    "_rgb": color_payload.get("rgb"),
                }
            )
    return observations


def _medoid_index(
    observations: Sequence[Mapping[str, Any]],
) -> tuple[int, np.ndarray]:
    labs = np.asarray(
        [observation["_lab"] for observation in observations],
        dtype=np.float64,
    )
    if len(observations) == 1:
        return 0, np.zeros((1, 1), dtype=np.float64)
    distances = delta_e_ciede2000_array(
        labs[:, None, :],
        labs[None, :, :],
    )
    sums = distances.sum(axis=1)

    def tie_breaker(index: int) -> tuple[Any, ...]:
        observation = observations[index]
        return (
            float(sums[index]),
            -_QUALITY_ORDER[str(observation["color_confidence"])],
            -float(
                observation["cluster_proportion"]
                if observation["cluster_proportion"] is not None
                else -1.0
            ),
            float(
                observation["dispersion"]
                if observation["dispersion"] is not None
                else math.inf
            ),
            str(observation["shade_color_observation_id"]),
        )

    selected = min(range(len(observations)), key=tie_breaker)
    return selected, distances


def _build_profiles(
    *,
    run_id: str,
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for observation in observations:
        if not observation["formal_eligible"]:
            continue
        key = (
            str(observation["shade_id"]),
            str(observation["representation_profile"]),
        )
        groups.setdefault(key, []).append(observation)
    profiles: list[dict[str, Any]] = []
    for (shade_id, representation_profile), members in sorted(
        groups.items()
    ):
        ordered = sorted(
            members,
            key=lambda item: str(item["shade_color_observation_id"]),
        )
        medoid_index, distances = _medoid_index(ordered)
        medoid = ordered[medoid_index]
        upper = distances[np.triu_indices(len(ordered), k=1)]
        pair_count = int(len(upper))
        lab = tuple(float(value) for value in medoid["_lab"])
        _lightness, chroma, hue = lab_to_lch(lab)
        aliases = sorted(
            {
                str(alias)
                for member in ordered
                for alias in member["_raw_shade_texts"]
            }
        )
        normalized_codes = sorted(
            {str(member["normalized_shade_code"]) for member in ordered}
        )
        identity_statuses = sorted(
            {str(member["identity_status"]) for member in ordered}
        )
        source_records = sorted(
            {
                source_record_id
                for member in ordered
                for source_record_id in json.loads(
                    member["source_record_ids_json"]
                )
            }
        )
        profile_id = stable_id(
            "shade_profile",
            run_id,
            shade_id,
            representation_profile,
        )
        profiles.append(
            {
                "shade_color_profile_id": profile_id,
                "run_id": run_id,
                "shade_id": shade_id,
                "identity_status": medoid["identity_status"],
                "source_sku_id_raw": medoid["source_sku_id_raw"],
                "normalized_shade_code": normalized_codes[0],
                "shade_code_aliases_json": canonical_json(aliases),
                "brand_id_raw": medoid["brand_id_raw"],
                "brand_name_raw": medoid["brand_name_raw"],
                "product_name_raw": medoid["product_name_raw"],
                "shade_name_raw": medoid["shade_name_raw"],
                "representation_profile": representation_profile,
                "representative_observation_id": medoid[
                    "shade_color_observation_id"
                ],
                "representative_hex": medoid["color_hex"],
                "representative_rgb_json": medoid["rgb_json"],
                "representative_lab_json": medoid["lab_json"],
                "lab_l": lab[0],
                "lab_a": lab[1],
                "lab_b": lab[2],
                "lch_c": chroma,
                "lch_h_deg": hue,
                "color_confidence": medoid["color_confidence"],
                "profile_status": (
                    "single_observation_provisional"
                    if len(ordered) == 1
                    else "multi_observation_provisional"
                ),
                "accepted_observation_count": len(ordered),
                "evidence_image_count": len(
                    {str(member["image_id"]) for member in ordered}
                ),
                "within_profile_pair_count": pair_count,
                "within_profile_delta_e00_p50": (
                    float(np.median(upper)) if pair_count else None
                ),
                "within_profile_delta_e00_max": (
                    float(np.max(upper)) if pair_count else None
                ),
                "output_semantics": OUTPUT_SEMANTICS,
                "algorithm_version": ENTITY_ALGORITHM_VERSION,
                "evidence_json": canonical_json(
                    {
                        "accepted_observation_ids": [
                            str(member["shade_color_observation_id"])
                            for member in ordered
                        ],
                        "image_ids": sorted(
                            {str(member["image_id"]) for member in ordered}
                        ),
                        "source_record_ids": source_records,
                        "identity_statuses": identity_statuses,
                        "normalized_shade_codes": normalized_codes,
                        "medoid_rule": (
                            "minimum_unweighted_delta_e00_sum_then_"
                            "quality_cluster_dispersion_id"
                        ),
                    }
                ),
                "created_at": utc_now(),
                "_lab": lab,
                "_lch": (lab[0], chroma, hue),
            }
        )
    return profiles


def _iter_pair_rows(
    *,
    run_id: str,
    profiles: Sequence[Mapping[str, Any]],
    options: SimilarityOptions,
) -> Iterator[dict[str, Any]]:
    by_profile: dict[str, list[Mapping[str, Any]]] = {}
    for profile in profiles:
        by_profile.setdefault(
            str(profile["representation_profile"]),
            [],
        ).append(profile)
    for representation_profile, group in sorted(by_profile.items()):
        ordered = sorted(
            group,
            key=lambda item: str(item["shade_color_profile_id"]),
        )
        block_size = options.pair_block_size
        for first_start in range(0, len(ordered), block_size):
            first_block = ordered[first_start : first_start + block_size]
            first_labs = np.asarray(
                [item["_lab"] for item in first_block],
                dtype=np.float64,
            )
            for second_start in range(first_start, len(ordered), block_size):
                second_block = ordered[
                    second_start : second_start + block_size
                ]
                second_labs = np.asarray(
                    [item["_lab"] for item in second_block],
                    dtype=np.float64,
                )
                distances = delta_e_ciede2000_array(
                    first_labs[:, None, :],
                    second_labs[None, :, :],
                )
                for left_index, left in enumerate(first_block):
                    absolute_left = first_start + left_index
                    for right_index, right in enumerate(second_block):
                        absolute_right = second_start + right_index
                        if absolute_right <= absolute_left:
                            continue
                        distance = float(distances[left_index, right_index])
                        delta_l, delta_c, delta_h = (
                            color_difference_diagnostics(
                                left["_lab"],
                                right["_lab"],
                            )
                        )
                        pair_id = stable_id(
                            "shade_similarity_pair",
                            run_id,
                            left["shade_color_profile_id"],
                            right["shade_color_profile_id"],
                        )
                        yield {
                            "shade_similarity_pair_id": pair_id,
                            "run_id": run_id,
                            "shade_color_profile_id_a": left[
                                "shade_color_profile_id"
                            ],
                            "shade_color_profile_id_b": right[
                                "shade_color_profile_id"
                            ],
                            "shade_id_a": left["shade_id"],
                            "shade_id_b": right["shade_id"],
                            "representation_profile": representation_profile,
                            "delta_e00": distance,
                            "display_score": delta_e_to_similarity(
                                distance,
                                scale=options.display_score_scale,
                            ),
                            "display_score_version": (
                                options.display_score_version
                            ),
                            "distance_band": classify_distance_band(distance),
                            "delta_l": delta_l,
                            "delta_c": delta_c,
                            "delta_h_deg": delta_h,
                            "pair_quality_tier": pair_quality_tier(
                                str(left["color_confidence"]),
                                str(right["color_confidence"]),
                            ),
                            "output_semantics": OUTPUT_SEMANTICS,
                            "algorithm_version": CIEDE2000_VERSION,
                            "created_at": utc_now(),
                        }


def _summary_from_records(
    *,
    run_id: str,
    inputs: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    pair_count: int,
    topk_count: int,
    options: SimilarityOptions,
    manifest_path: Path,
    manifest_sha256: str,
) -> dict[str, Any]:
    exclusion_counts: dict[str, int] = {}
    for observation in observations:
        for reason in json.loads(observation["exclusion_reasons_json"]):
            exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
    return {
        "schema_version": "observed-similarity-summary-1.0",
        "run_id": run_id,
        "output_semantics": OUTPUT_SEMANTICS,
        "quality_status": "not_evaluated_without_ground_truth",
        "source_manifest": manifest_path.as_posix(),
        "source_manifest_sha256": manifest_sha256,
        "options": {
            "top_k": options.top_k,
            "max_delta_e00": options.max_delta_e00,
            "formal_region_types": list(options.formal_region_types),
            "accepted_color_confidence": list(
                options.accepted_color_confidence
            ),
            "display_score_version": options.display_score_version,
        },
        "algorithm_versions": {
            "implementation": options.implementation_version,
            "entity_profile": ENTITY_ALGORITHM_VERSION,
            "ciede2000": CIEDE2000_VERSION,
        },
        "counts": {
            "selected_images": len(inputs),
            "source_regions": len(observations),
            "successful_colors": sum(
                observation["extraction_status"] == "succeeded"
                and observation["color_hex"] is not None
                for observation in observations
            ),
            "formal_observations": sum(
                bool(observation["formal_eligible"])
                for observation in observations
            ),
            "profiles": len(profiles),
            "business_resolved_profiles": sum(
                profile["identity_status"] == "business_resolved"
                for profile in profiles
            ),
            "image_local_profiles": sum(
                str(profile["identity_status"]).startswith("image_local_")
                for profile in profiles
            ),
            "pairs": pair_count,
            "topk_rows": topk_count,
            "model_api_calls": 0,
        },
        "exclusion_reason_counts": dict(sorted(exclusion_counts.items())),
    }


def _prepare(
    connection: sqlite3.Connection,
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    source_manifest: Path,
    top_k: int | None,
    max_delta_e00: float | None,
) -> tuple[
    Path,
    str,
    SimilarityOptions,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    manifest_path = _resolve_manifest_path(settings, source_manifest)
    manifest_sha256 = sha256_file(manifest_path)
    items = load_similarity_source_manifest(manifest_path)
    options = _options(
        settings,
        top_k=top_k,
        max_delta_e00=max_delta_e00,
    )
    inputs = _validated_inputs(
        connection,
        run_id=run_id,
        items=items,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    observations = _build_observations(
        connection,
        workspace,
        run_id=run_id,
        inputs=inputs,
        options=options,
    )
    profiles = _build_profiles(
        run_id=run_id,
        observations=observations,
    )
    return (
        manifest_path,
        manifest_sha256,
        options,
        inputs,
        observations,
        profiles,
    )


def plan_shade_similarity(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    source_manifest: Path,
    top_k: int | None = None,
    max_delta_e00: float | None = None,
) -> dict[str, Any]:
    """Plan exact local inputs and output counts without writing anything."""

    with open_database(workspace.database_path, readonly=True) as connection:
        (
            manifest_path,
            manifest_sha256,
            options,
            inputs,
            observations,
            profiles,
        ) = _prepare(
            connection,
            workspace,
            settings,
            run_id=run_id,
            source_manifest=source_manifest,
            top_k=top_k,
            max_delta_e00=max_delta_e00,
        )
    pair_count = 0
    eligible_neighbor_counts = {
        str(profile["shade_color_profile_id"]): 0 for profile in profiles
    }
    for pair in _iter_pair_rows(
        run_id=run_id,
        profiles=profiles,
        options=options,
    ):
        pair_count += 1
        if (
            options.max_delta_e00 is None
            or pair["delta_e00"] <= options.max_delta_e00
        ):
            eligible_neighbor_counts[
                str(pair["shade_color_profile_id_a"])
            ] += 1
            eligible_neighbor_counts[
                str(pair["shade_color_profile_id_b"])
            ] += 1
    profile_sizes: dict[str, int] = {}
    for profile in profiles:
        key = str(profile["representation_profile"])
        profile_sizes[key] = profile_sizes.get(key, 0) + 1
    topk_count = sum(
        min(options.top_k, count)
        for count in eligible_neighbor_counts.values()
    )
    summary = _summary_from_records(
        run_id=run_id,
        inputs=inputs,
        observations=observations,
        profiles=profiles,
        pair_count=pair_count,
        topk_count=topk_count,
        options=options,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
    )
    summary["status"] = "planned_no_writes"
    summary["profile_sizes"] = profile_sizes
    return summary


_INPUT_COLUMNS = (
    "shade_similarity_input_id",
    "run_id",
    "source_quick_run_id",
    "quick_image_extraction_id",
    "image_id",
    "sequence",
    "input_status",
    "source_manifest_path",
    "source_manifest_sha256",
    "selection_reason",
    "evidence_json",
    "created_at",
)
_OBSERVATION_COLUMNS = (
    "shade_color_observation_id",
    "run_id",
    "shade_similarity_input_id",
    "source_quick_run_id",
    "quick_color_region_id",
    "image_id",
    "quick_text_item_id",
    "linked_shade_text_item_ids_json",
    "raw_shade_texts_json",
    "normalized_shade_code",
    "shade_id",
    "identity_status",
    "source_sku_id_raw",
    "candidate_sku_ids_json",
    "source_record_ids_json",
    "brand_id_raw",
    "brand_name_raw",
    "product_name_raw",
    "shade_name_raw",
    "region_type",
    "representation_profile",
    "bbox_image_json",
    "extraction_status",
    "output_semantics",
    "color_hex",
    "rgb_json",
    "lab_json",
    "model_confidence",
    "association_confidence",
    "color_confidence",
    "valid_pixel_count",
    "valid_pixel_ratio",
    "cluster_proportion",
    "dispersion",
    "formal_eligible",
    "exclusion_reasons_json",
    "algorithm_version",
    "evidence_json",
    "created_at",
)
_PROFILE_COLUMNS = (
    "shade_color_profile_id",
    "run_id",
    "shade_id",
    "identity_status",
    "source_sku_id_raw",
    "normalized_shade_code",
    "shade_code_aliases_json",
    "brand_id_raw",
    "brand_name_raw",
    "product_name_raw",
    "shade_name_raw",
    "representation_profile",
    "representative_observation_id",
    "representative_hex",
    "representative_rgb_json",
    "representative_lab_json",
    "lab_l",
    "lab_a",
    "lab_b",
    "lch_c",
    "lch_h_deg",
    "color_confidence",
    "profile_status",
    "accepted_observation_count",
    "evidence_image_count",
    "within_profile_pair_count",
    "within_profile_delta_e00_p50",
    "within_profile_delta_e00_max",
    "output_semantics",
    "algorithm_version",
    "evidence_json",
    "created_at",
)
_PAIR_COLUMNS = (
    "shade_similarity_pair_id",
    "run_id",
    "shade_color_profile_id_a",
    "shade_color_profile_id_b",
    "shade_id_a",
    "shade_id_b",
    "representation_profile",
    "delta_e00",
    "display_score",
    "display_score_version",
    "distance_band",
    "delta_l",
    "delta_c",
    "delta_h_deg",
    "pair_quality_tier",
    "output_semantics",
    "algorithm_version",
    "created_at",
)
_TOPK_COLUMNS = (
    "shade_similarity_topk_id",
    "run_id",
    "shade_similarity_pair_id",
    "query_profile_id",
    "candidate_profile_id",
    "query_shade_id",
    "candidate_shade_id",
    "representation_profile",
    "rank",
    "delta_e00",
    "display_score",
    "display_score_version",
    "distance_band",
    "delta_l",
    "delta_c",
    "delta_h_deg",
    "pair_quality_tier",
    "output_semantics",
    "algorithm_version",
    "created_at",
)


def _insert_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> int:
    placeholders = ",".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {table}({','.join(columns)}) "
        f"VALUES ({placeholders})"
    )
    count = 0
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(tuple(row[column] for column in columns))
        if len(batch) >= 5000:
            connection.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        connection.executemany(sql, batch)
        count += len(batch)
    return count


def _insert_pairs(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    profiles: Sequence[Mapping[str, Any]],
    options: SimilarityOptions,
) -> int:
    placeholders = ",".join("?" for _ in _PAIR_COLUMNS)
    sql = (
        f"INSERT INTO shade_similarity_pairs({','.join(_PAIR_COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    batch: list[tuple[Any, ...]] = []
    count = 0
    for row in _iter_pair_rows(
        run_id=run_id,
        profiles=profiles,
        options=options,
    ):
        batch.append(tuple(row[column] for column in _PAIR_COLUMNS))
        if len(batch) >= options.pair_insert_batch_size:
            connection.executemany(sql, batch)
            count += len(batch)
            batch.clear()
    if batch:
        connection.executemany(sql, batch)
        count += len(batch)
    return count


def _insert_topk(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    options: SimilarityOptions,
) -> int:
    directed_rows = connection.execute(
        """
        WITH directed AS (
            SELECT shade_similarity_pair_id,
                   shade_color_profile_id_a AS query_profile_id,
                   shade_color_profile_id_b AS candidate_profile_id,
                   shade_id_a AS query_shade_id,
                   shade_id_b AS candidate_shade_id,
                   representation_profile,
                   delta_e00,
                   display_score,
                   display_score_version,
                   distance_band,
                   delta_l,
                   delta_c,
                   delta_h_deg,
                   pair_quality_tier,
                   output_semantics,
                   algorithm_version
            FROM shade_similarity_pairs
            WHERE run_id = ?
            UNION ALL
            SELECT shade_similarity_pair_id,
                   shade_color_profile_id_b AS query_profile_id,
                   shade_color_profile_id_a AS candidate_profile_id,
                   shade_id_b AS query_shade_id,
                   shade_id_a AS candidate_shade_id,
                   representation_profile,
                   delta_e00,
                   display_score,
                   display_score_version,
                   distance_band,
                   delta_l,
                   delta_c,
                   delta_h_deg,
                   pair_quality_tier,
                   output_semantics,
                   algorithm_version
            FROM shade_similarity_pairs
            WHERE run_id = ?
        ),
        ranked AS (
            SELECT directed.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY query_profile_id
                       ORDER BY delta_e00,
                                delta_l,
                                delta_c,
                                candidate_shade_id
                   ) AS result_rank
            FROM directed
            WHERE (? IS NULL OR delta_e00 <= ?)
        )
        SELECT * FROM ranked
        WHERE result_rank <= ?
        ORDER BY query_profile_id, result_rank
        """,
        (
            run_id,
            run_id,
            options.max_delta_e00,
            options.max_delta_e00,
            options.top_k,
        ),
    ).fetchall()
    rows = []
    created_at = utc_now()
    for row in directed_rows:
        rows.append(
            {
                "shade_similarity_topk_id": stable_id(
                    "shade_similarity_topk",
                    run_id,
                    row["query_profile_id"],
                    row["candidate_profile_id"],
                ),
                "run_id": run_id,
                "shade_similarity_pair_id": row[
                    "shade_similarity_pair_id"
                ],
                "query_profile_id": row["query_profile_id"],
                "candidate_profile_id": row["candidate_profile_id"],
                "query_shade_id": row["query_shade_id"],
                "candidate_shade_id": row["candidate_shade_id"],
                "representation_profile": row["representation_profile"],
                "rank": row["result_rank"],
                "delta_e00": row["delta_e00"],
                "display_score": row["display_score"],
                "display_score_version": row["display_score_version"],
                "distance_band": row["distance_band"],
                "delta_l": row["delta_l"],
                "delta_c": row["delta_c"],
                "delta_h_deg": row["delta_h_deg"],
                "pair_quality_tier": row["pair_quality_tier"],
                "output_semantics": row["output_semantics"],
                "algorithm_version": row["algorithm_version"],
                "created_at": created_at,
            }
        )
    return _insert_rows(
        connection,
        "shade_similarity_topk",
        _TOPK_COLUMNS,
        rows,
    )


def _clear_similarity_rows(
    connection: sqlite3.Connection,
    run_id: str,
) -> None:
    for table in (
        "shade_similarity_topk",
        "shade_similarity_pairs",
        "shade_color_profiles",
        "shade_color_observations",
        "shade_similarity_inputs",
    ):
        connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))


def _source_hash_snapshot(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    image_ids: Sequence[str],
) -> dict[str, Any]:
    aliases_row = connection.execute(
        """
        SELECT root_aliases_json FROM dataset_snapshots
        WHERE dataset_snapshot_id = ?
        """,
        (workspace.dataset_snapshot_id,),
    ).fetchone()
    aliases = json.loads(aliases_row[0]) if aliases_row else {}
    roots: dict[str, Path] = {}
    for alias, uri in aliases.items():
        if str(uri).startswith("repo://"):
            roots[str(alias)] = (
                workspace.repo_root / str(uri)[len("repo://") :]
            ).resolve()
        elif str(uri).startswith("external://"):
            roots[str(alias)] = Path(
                str(uri)[len("external://") :]
            ).resolve()
    occurrences: list[dict[str, Any]] = []
    for chunk in _chunks(list(image_ids)):
        placeholders = ",".join("?" for _ in chunk)
        for row in connection.execute(
            f"""
            SELECT image_occurrence_id, image_id, root_alias, relative_path
            FROM image_occurrences
            WHERE image_id IN ({placeholders})
            ORDER BY image_occurrence_id
            """,
            list(chunk),
        ):
            root = roots.get(str(row["root_alias"]))
            path = root / row["relative_path"] if root else None
            exists = bool(path and path.is_file())
            actual_sha = sha256_file(path) if exists and path else None
            occurrences.append(
                {
                    "image_occurrence_id": row["image_occurrence_id"],
                    "image_id": row["image_id"],
                    "root_alias": row["root_alias"],
                    "relative_path": row["relative_path"],
                    "exists": exists,
                    "sha256": actual_sha,
                    "matches_registered_image_id": (
                        actual_sha == row["image_id"]
                        if actual_sha
                        else False
                    ),
                }
            )
    occurrences.sort(key=lambda item: str(item["image_occurrence_id"]))
    return {
        "schema_version": "selected-source-hashes-1.0",
        "dataset_snapshot_id": workspace.dataset_snapshot_id,
        "selected_image_count": len(image_ids),
        "occurrence_count": len(occurrences),
        "occurrences": occurrences,
    }


def _existing_run_summary(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, Any] | None:
    run = connection.execute(
        "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if (
        run is None
        or run["stage"] != "shade_similarity_mvp"
        or run["status"]
        not in ("completed", "completed_insufficient_candidates")
    ):
        return None
    input_row = connection.execute(
        """
        SELECT source_manifest_path, source_manifest_sha256
        FROM shade_similarity_inputs
        WHERE run_id = ? ORDER BY sequence LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if input_row is None:
        return None
    profiles = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM shade_color_profiles WHERE run_id = ?",
            (run_id,),
        )
    ]
    counts = {
        "selected_images": connection.execute(
            "SELECT COUNT(*) FROM shade_similarity_inputs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0],
        "source_regions": connection.execute(
            "SELECT COUNT(*) FROM shade_color_observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0],
        "successful_colors": connection.execute(
            """
            SELECT COUNT(*) FROM shade_color_observations
            WHERE run_id = ?
              AND extraction_status = 'succeeded'
              AND color_hex IS NOT NULL
            """,
            (run_id,),
        ).fetchone()[0],
        "formal_observations": connection.execute(
            """
            SELECT COUNT(*) FROM shade_color_observations
            WHERE run_id = ? AND formal_eligible = 1
            """,
            (run_id,),
        ).fetchone()[0],
        "profiles": len(profiles),
        "business_resolved_profiles": sum(
            row["identity_status"] == "business_resolved" for row in profiles
        ),
        "image_local_profiles": sum(
            str(row["identity_status"]).startswith("image_local_")
            for row in profiles
        ),
        "pairs": connection.execute(
            "SELECT COUNT(*) FROM shade_similarity_pairs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0],
        "topk_rows": connection.execute(
            "SELECT COUNT(*) FROM shade_similarity_topk WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0],
        "model_api_calls": 0,
    }
    return {
        "schema_version": "observed-similarity-summary-1.0",
        "status": run["status"],
        "run_id": run_id,
        "output_semantics": OUTPUT_SEMANTICS,
        "quality_status": "not_evaluated_without_ground_truth",
        "source_manifest": input_row["source_manifest_path"],
        "source_manifest_sha256": input_row["source_manifest_sha256"],
        "counts": counts,
        "resume": "completed_run_reused",
    }


def run_shade_similarity(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    source_manifest: Path,
    resume: bool,
    top_k: int | None = None,
    max_delta_e00: float | None = None,
) -> dict[str, Any]:
    """Build profiles, calculate local CIEDE2000 pairs, and persist."""

    manifest_path = _resolve_manifest_path(settings, source_manifest)
    manifest_sha256 = sha256_file(manifest_path)
    options = _options(
        settings,
        top_k=top_k,
        max_delta_e00=max_delta_e00,
    )
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    extra_config = {
        "source_manifest": manifest_path.as_posix(),
        "source_manifest_sha256": manifest_sha256,
        "top_k": options.top_k,
        "max_delta_e00": options.max_delta_e00,
        "algorithm_version": ENTITY_ALGORITHM_VERSION,
        "ciede2000_version": CIEDE2000_VERSION,
    }
    try:
        with open_database(workspace.database_path) as connection:
            begin_pipeline_run(
                connection,
                workspace,
                settings,
                run_id=run_id,
                stage="shade_similarity_mvp",
                resume=resume,
                extra_config=extra_config,
            )
            existing = _existing_run_summary(connection, run_id=run_id)
            if existing is not None:
                return existing
            if resume:
                _clear_similarity_rows(connection, run_id)
                connection.execute(
                    """
                    UPDATE pipeline_runs
                    SET status = 'running', finished_at = NULL,
                        error_summary_json = '{}'
                    WHERE run_id = ?
                    """,
                    (run_id,),
                )
            (
                prepared_manifest_path,
                prepared_manifest_sha,
                prepared_options,
                inputs,
                observations,
                profiles,
            ) = _prepare(
                connection,
                workspace,
                settings,
                run_id=run_id,
                source_manifest=manifest_path,
                top_k=options.top_k,
                max_delta_e00=options.max_delta_e00,
            )
            before_hashes = _source_hash_snapshot(
                connection,
                workspace,
                image_ids=[str(item["image_id"]) for item in inputs],
            )
            _insert_rows(
                connection,
                "shade_similarity_inputs",
                _INPUT_COLUMNS,
                inputs,
            )
            _insert_rows(
                connection,
                "shade_color_observations",
                _OBSERVATION_COLUMNS,
                observations,
            )
            _insert_rows(
                connection,
                "shade_color_profiles",
                _PROFILE_COLUMNS,
                profiles,
            )
            pair_count = _insert_pairs(
                connection,
                run_id=run_id,
                profiles=profiles,
                options=prepared_options,
            )
            topk_count = _insert_topk(
                connection,
                run_id=run_id,
                options=prepared_options,
            )
            after_hashes = _source_hash_snapshot(
                connection,
                workspace,
                image_ids=[str(item["image_id"]) for item in inputs],
            )
            if before_hashes != after_hashes:
                raise RuntimeError("source image hashes changed during similarity run")
            if any(
                not occurrence["matches_registered_image_id"]
                for occurrence in after_hashes["occurrences"]
            ):
                raise RuntimeError("selected source image hash audit failed")
            summary = _summary_from_records(
                run_id=run_id,
                inputs=inputs,
                observations=observations,
                profiles=profiles,
                pair_count=pair_count,
                topk_count=topk_count,
                options=prepared_options,
                manifest_path=prepared_manifest_path,
                manifest_sha256=prepared_manifest_sha,
            )
            final_status = (
                "completed"
                if len(profiles) >= 2
                else "completed_insufficient_candidates"
            )
            summary["status"] = final_status
            finish_pipeline_run(
                connection,
                run_id,
                status=final_status,
            )
        write_json_snapshot(run_dir / "reports", "similarity_plan", {
            **summary,
            "status": "planned_inputs_materialized",
        })
        write_json_snapshot(
            run_dir / "reports",
            "selected_source_hashes_before",
            before_hashes,
        )
        write_json_snapshot(
            run_dir / "reports",
            "selected_source_hashes_after",
            after_hashes,
        )
        write_json_snapshot(
            run_dir / "reports",
            "similarity_execution",
            summary,
        )
        return summary
    except Exception as error:
        with open_database(workspace.database_path) as connection:
            existing = connection.execute(
                """
                SELECT stage, status FROM pipeline_runs
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                existing is not None
                and existing["stage"] == "shade_similarity_mvp"
                and existing["status"]
                not in ("completed", "completed_insufficient_candidates")
            ):
                finish_pipeline_run(
                    connection,
                    run_id,
                    status="failed",
                    error_summary={
                        "error_type": type(error).__name__,
                        "message": str(error),
                    },
                )
        raise


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    temporary.replace(path)


def _atomic_cursor_csv(
    path: Path,
    cursor: sqlite3.Cursor,
) -> int:
    """Stream a SQLite cursor to an atomically replaced CSV file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fields = [str(item[0]) for item in (cursor.description or ())]
    count = 0
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in cursor:
            writer.writerow(dict(row))
            count += 1
    temporary.replace(path)
    return count


def export_shade_similarity(
    workspace: Workspace,
    *,
    run_id: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Export observations, profiles, all pairs, Top-K, and summary."""

    destination = (
        output_dir.resolve()
        if output_dir is not None
        else workspace.run_dir(run_id) / "exports"
    )
    destination.mkdir(parents=True, exist_ok=True)
    observation_path = destination / "shade_observations.csv"
    profile_path = destination / "shade_profiles.csv"
    pair_path = destination / "similarity_pairs.csv"
    topk_path = destination / "top_k.csv"
    with open_database(workspace.database_path, readonly=True) as connection:
        run = connection.execute(
            "SELECT stage, status FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run is None or run["stage"] != "shade_similarity_mvp":
            raise KeyError(f"not a shade similarity run: {run_id}")
        input_row = connection.execute(
            """
            SELECT source_manifest_path, source_manifest_sha256
            FROM shade_similarity_inputs
            WHERE run_id = ? ORDER BY sequence LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if input_row is None:
            raise KeyError(f"run has no shade similarity inputs: {run_id}")
        observation_count = _atomic_cursor_csv(
            observation_path,
            connection.execute(
                """
                SELECT * FROM shade_color_observations
                WHERE run_id = ?
                ORDER BY image_id, quick_color_region_id
                """,
                (run_id,),
            ),
        )
        profile_count = _atomic_cursor_csv(
            profile_path,
            connection.execute(
                """
                SELECT * FROM shade_color_profiles
                WHERE run_id = ?
                ORDER BY representation_profile, shade_id
                """,
                (run_id,),
            ),
        )
        pair_count = _atomic_cursor_csv(
            pair_path,
            connection.execute(
                """
                SELECT pair.*,
                       a.identity_status AS identity_status_a,
                       a.normalized_shade_code AS shade_code_a,
                       a.brand_name_raw AS brand_name_a,
                       a.product_name_raw AS product_name_a,
                       b.identity_status AS identity_status_b,
                       b.normalized_shade_code AS shade_code_b,
                       b.brand_name_raw AS brand_name_b,
                       b.product_name_raw AS product_name_b
                FROM shade_similarity_pairs AS pair
                JOIN shade_color_profiles AS a
                  ON a.shade_color_profile_id =
                     pair.shade_color_profile_id_a
                JOIN shade_color_profiles AS b
                  ON b.shade_color_profile_id =
                     pair.shade_color_profile_id_b
                WHERE pair.run_id = ?
                ORDER BY pair.representation_profile,
                         pair.delta_e00,
                         pair.shade_id_a,
                         pair.shade_id_b
                """,
                (run_id,),
            ),
        )
        topk_count = _atomic_cursor_csv(
            topk_path,
            connection.execute(
                """
                SELECT result.*,
                       query.identity_status AS query_identity_status,
                       query.normalized_shade_code AS query_shade_code,
                       query.brand_name_raw AS query_brand_name,
                       query.product_name_raw AS query_product_name,
                       candidate.identity_status AS candidate_identity_status,
                       candidate.normalized_shade_code AS candidate_shade_code,
                       candidate.brand_name_raw AS candidate_brand_name,
                       candidate.product_name_raw AS candidate_product_name
                FROM shade_similarity_topk AS result
                JOIN shade_color_profiles AS query
                  ON query.shade_color_profile_id =
                     result.query_profile_id
                JOIN shade_color_profiles AS candidate
                  ON candidate.shade_color_profile_id =
                     result.candidate_profile_id
                WHERE result.run_id = ?
                ORDER BY result.query_profile_id, result.rank
                """,
                (run_id,),
            ),
        )
    file_hashes = {
        "shade_observations.csv": sha256_file(observation_path),
        "shade_profiles.csv": sha256_file(profile_path),
        "similarity_pairs.csv": sha256_file(pair_path),
        "top_k.csv": sha256_file(topk_path),
    }
    summary = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "run_status": run["status"],
        "output_semantics": OUTPUT_SEMANTICS,
        "quality_status": "not_evaluated_without_ground_truth",
        "source_manifest": input_row["source_manifest_path"],
        "source_manifest_sha256": input_row["source_manifest_sha256"],
        "counts": {
            "observations": observation_count,
            "profiles": profile_count,
            "pairs": pair_count,
            "topk_rows": topk_count,
        },
        "file_sha256": file_hashes,
    }
    summary_path = destination / "similarity_summary.json"
    _atomic_text(
        summary_path,
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    file_hashes["similarity_summary.json"] = sha256_file(summary_path)
    return {
        **summary,
        "output_dir": destination,
        "files": {
            "shade_observations": observation_path,
            "shade_profiles": profile_path,
            "similarity_pairs": pair_path,
            "top_k": topk_path,
            "similarity_summary": summary_path,
        },
        "file_sha256": file_hashes,
    }
