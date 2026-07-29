"""Stage 2.6 image-only quick extraction, local colour, and exports."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image
from pydantic import ValidationError

from .color_extraction import extract_observed_color
from .config import require_env
from .image_assets import MIME_BY_FORMAT, register_existing_asset
from .quick_extract_schemas import (
    QuickImageExtraction,
    QuickScope,
    parse_quick_image_extraction,
)
from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import sha256_file, stable_id
from .workspace import (
    Workspace,
    begin_pipeline_run,
    ensure_run_directory,
    finish_pipeline_run,
    open_database,
    utc_now,
)


OUTPUT_SEMANTICS = "image_observed_color_candidate"
QUICK_SCHEMA_VERSION = "quick-image-extraction-1.0"
REQUEST_SCHEMA_VERSION = "quick-vlm-request-1.0"
PREVIEW_TRANSFORM_VERSION = "quick-vlm-preview-1.0"
AGGREGATION_VERSION = "quick-extraction-aggregation-1.0"
TEXT_NORMALIZATION_VERSION = "nfkc-whitespace-latin-uppercase-1.0"

_REQUEST_KEYS = {
    "schema_version",
    "analysis_layer",
    "analysis_unit_type",
    "analysis_unit_id",
    "image_id",
    "scope",
    "input_context_policy",
    "model",
    "prompt_name",
    "prompt_version",
    "prompt",
    "prompt_sha256",
    "response_schema_version",
    "generation_parameters",
    "image_asset",
}
_ASSET_KEYS = {
    "derived_asset_id",
    "asset_type",
    "sha256",
    "mime_type",
    "width",
    "height",
    "transform_fingerprint",
}


@dataclass(frozen=True)
class QuickUnit:
    unit_id: str
    image_id: str
    scope: QuickScope
    unit_index: int | None
    asset_id: str | None
    asset_path: Path | None
    asset_sha256: str | None
    asset_type: str | None
    asset_format: str | None
    width: int | None
    height: int | None
    transform_fingerprint: str
    asset_to_image_transform: dict[str, Any]
    working_asset_id: str | None
    working_path: Path | None
    alpha_asset_id: str | None
    alpha_path: Path | None
    long_image_layout_id: str | None
    initial_status: str = "prepared"
    failure: dict[str, Any] | None = None


@dataclass(frozen=True)
class QuickAttempt:
    model_run_id: str
    cache_key: str
    attempt: int
    status: str
    schema_status: str
    request_path: str
    raw_path: str | None
    parsed_path: str | None
    response_hash: str | None
    latency_ms: int
    token_usage: dict[str, Any]
    error: dict[str, Any]
    parsed: QuickImageExtraction | None
    provider_model_name: str | None


class ProviderCallBudget:
    def __init__(self, maximum: int) -> None:
        if maximum <= 0:
            raise ValueError("--max-calls must be positive")
        self.maximum = maximum
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> int:
        with self._lock:
            if self.used >= self.maximum:
                raise ProviderCallBudgetExceeded(
                    f"online provider call budget exhausted at {self.maximum}"
                )
            self.used += 1
            return self.used


class ProviderCallBudgetExceeded(RuntimeError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    if path.exists():
        if path.read_text(encoding="utf-8") == serialized:
            return
        raise FileExistsError(f"immutable run artifact already exists: {path}")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


def _write_report(run_dir: Path, name: str, value: Mapping[str, Any]) -> Path:
    fingerprint = sha256_json(value)[:16]
    path = run_dir / "reports" / f"{name}.{fingerprint}.json"
    _write_json(path, value)
    return path


def _redact(value: str, api_key: str) -> str:
    redacted = value.replace(api_key, "[REDACTED]") if api_key else value
    redacted = re.sub(
        r"\bsk-(?:ws-)?[A-Za-z0-9_-]{12,}\b",
        "[REDACTED]",
        redacted,
    )
    return redacted


def _redact_environment_secrets(value: str) -> str:
    redacted = value
    for variable in (
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ):
        secret = os.environ.get(variable)
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return re.sub(
        r"\bsk-(?:ws-)?[A-Za-z0-9_-]{12,}\b",
        "[REDACTED]",
        redacted,
    )


def _selector(
    *,
    image_id: str | None,
    selection_manifest: Path | None,
    folder_group_id: str | None,
    limit: int | None,
) -> tuple[str, Any]:
    supplied = [
        ("image_id", image_id),
        ("selection_manifest", selection_manifest),
        ("folder_group_id", folder_group_id),
        ("limit", limit),
    ]
    active = [(name, value) for name, value in supplied if value is not None]
    if len(active) != 1:
        raise ValueError(
            "provide exactly one selector: --image-id, "
            "--selection-manifest, --folder-group-id, or --limit"
        )
    name, value = active[0]
    if name == "limit" and int(value) <= 0:
        raise ValueError("--limit must be positive")
    return name, value


def _manifest_image_ids(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"selection manifest not found: {path}")
    image_ids: list[str] = []
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
                f"invalid selection manifest JSON at line {line_number}"
            ) from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("image_id"),
            str,
        ):
            raise ValueError(
                f"selection manifest line {line_number} needs image_id"
            )
        image_id = payload["image_id"].strip()
        if payload.get("sha256") not in (None, image_id):
            raise ValueError(
                f"selection manifest SHA mismatch at line {line_number}"
            )
        image_ids.append(image_id)
    if not image_ids:
        raise ValueError("selection manifest contains no image IDs")
    if len(set(image_ids)) != len(image_ids):
        raise ValueError("selection manifest image IDs must be unique")
    return image_ids


def _select_image_ids(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    *,
    image_id: str | None,
    selection_manifest: Path | None,
    folder_group_id: str | None,
    limit: int | None,
) -> tuple[list[str], dict[str, Any]]:
    selector_name, selector_value = _selector(
        image_id=image_id,
        selection_manifest=selection_manifest,
        folder_group_id=folder_group_id,
        limit=limit,
    )
    stage2_run_id = str(settings.section("quick_extract")["stage2_run_id"])
    if selector_name == "image_id":
        image_ids = [str(selector_value)]
    elif selector_name == "selection_manifest":
        manifest_path = Path(selector_value)
        if not manifest_path.is_absolute():
            manifest_path = (settings.repo_root / manifest_path).resolve()
        image_ids = _manifest_image_ids(manifest_path)
        selector_value = manifest_path
    elif selector_name == "folder_group_id":
        image_ids = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT occurrence.image_id
                FROM image_occurrences AS occurrence
                JOIN image_preprocessing_observations AS observation
                  ON observation.image_id = occurrence.image_id
                 AND observation.run_id = ?
                WHERE occurrence.folder_group_id = ?
                ORDER BY occurrence.image_id
                """,
                (stage2_run_id, selector_value),
            )
        ]
    else:
        image_ids = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT image_id
                FROM image_preprocessing_observations
                WHERE run_id = ?
                ORDER BY image_id
                LIMIT ?
                """,
                (stage2_run_id, int(selector_value)),
            )
        ]
    if not image_ids:
        raise KeyError(f"selector {selector_name} matched no images")
    placeholders = ",".join("?" for _ in image_ids)
    existing = {
        str(row[0])
        for row in connection.execute(
            f"""
            SELECT image_id FROM image_contents
            WHERE image_id IN ({placeholders})
            """,
            image_ids,
        )
    }
    missing = [candidate for candidate in image_ids if candidate not in existing]
    if missing:
        raise KeyError(f"unknown image IDs in selection: {missing[:5]}")
    return image_ids, {
        "selector": selector_name,
        "value": (
            selector_value.as_posix()
            if isinstance(selector_value, Path)
            else selector_value
        ),
        "image_count": len(image_ids),
        "selection_sha256": sha256_json(image_ids),
    }


def _preview_bytes(
    working_path: Path,
    *,
    source_sha256: str,
    max_long_edge: int,
    quality: int,
) -> tuple[bytes, int, int, dict[str, float], str]:
    with Image.open(working_path) as opened:
        working = opened.convert("RGB")
    scale = min(1.0, max_long_edge / max(working.size))
    resized_size = (
        max(1, round(working.width * scale)),
        max(1, round(working.height * scale)),
    )
    resized = (
        working.resize(resized_size, Image.Resampling.LANCZOS)
        if resized_size != working.size
        else working.copy()
    )
    target_width = max(11, resized.width)
    target_height = max(11, resized.height)
    translate_x = (target_width - resized.width) // 2
    translate_y = (target_height - resized.height) // 2
    if (target_width, target_height) != resized.size:
        canvas = Image.new("RGB", (target_width, target_height), "white")
        canvas.paste(resized, (translate_x, translate_y))
        resized = canvas
    transform_payload = {
        "name": "stage2_working_to_quick_vlm_preview",
        "version": PREVIEW_TRANSFORM_VERSION,
        "source_sha256": source_sha256,
        "source_size": list(working.size),
        "resized_size": list(resized_size),
        "target_size": [target_width, target_height],
        "max_long_edge": max_long_edge,
        "jpeg_quality": quality,
        "padding_translate": [translate_x, translate_y],
        "padding_fill": [255, 255, 255],
    }
    fingerprint = sha256_json(transform_payload)
    output = io.BytesIO()
    resized.save(
        output,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    image_to_asset_scale_x = resized_size[0] / working.width
    image_to_asset_scale_y = resized_size[1] / working.height
    transform = {
        "scale_x": 1.0 / image_to_asset_scale_x,
        "scale_y": 1.0 / image_to_asset_scale_y,
        "translate_x": -translate_x / image_to_asset_scale_x,
        "translate_y": -translate_y / image_to_asset_scale_y,
    }
    return (
        output.getvalue(),
        target_width,
        target_height,
        transform,
        fingerprint,
    )


def _asset_path(workspace: Workspace, relative_path: str | None) -> Path | None:
    return workspace.output_root / relative_path if relative_path else None


def _planned_units(
    connection: sqlite3.Connection,
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    image_ids: Sequence[str],
) -> list[QuickUnit]:
    quick = settings.section("quick_extract")
    stage2_run_id = str(quick["stage2_run_id"])
    units: list[QuickUnit] = []
    for image_id in image_ids:
        observation = connection.execute(
            """
            SELECT observation.*, working.relative_path AS working_path,
                   working.sha256 AS working_sha256,
                   working.width AS working_width,
                   working.height AS working_height,
                   alpha.relative_path AS alpha_path
            FROM image_preprocessing_observations AS observation
            LEFT JOIN derived_assets AS working
              ON working.derived_asset_id = observation.working_asset_id
            LEFT JOIN derived_assets AS alpha
              ON alpha.derived_asset_id = observation.alpha_asset_id
            WHERE observation.run_id = ? AND observation.image_id = ?
            """,
            (stage2_run_id, image_id),
        ).fetchone()
        unit_base = stable_id("quick_unit", run_id, image_id)
        if (
            observation is None
            or observation["decode_status"] not in ("ok", "recovered")
            or not observation["working_asset_id"]
            or not observation["working_path"]
        ):
            units.append(
                QuickUnit(
                    unit_id=stable_id(unit_base, "image", ""),
                    image_id=image_id,
                    scope="image",
                    unit_index=None,
                    asset_id=None,
                    asset_path=None,
                    asset_sha256=None,
                    asset_type=None,
                    asset_format=None,
                    width=None,
                    height=None,
                    transform_fingerprint="",
                    asset_to_image_transform={},
                    working_asset_id=(
                        observation["working_asset_id"]
                        if observation is not None
                        else None
                    ),
                    working_path=None,
                    alpha_asset_id=(
                        observation["alpha_asset_id"]
                        if observation is not None
                        else None
                    ),
                    alpha_path=None,
                    long_image_layout_id=None,
                    initial_status="skipped",
                    failure={
                        "code": "stage2_working_asset_unavailable",
                        "decode_status": (
                            observation["decode_status"]
                            if observation is not None
                            else "missing_observation"
                        ),
                    },
                )
            )
            continue
        working_path = _asset_path(workspace, observation["working_path"])
        alpha_path = _asset_path(workspace, observation["alpha_path"])
        assert working_path is not None
        layout = connection.execute(
            """
            SELECT layout.*, asset.relative_path, asset.sha256,
                   asset.width AS asset_width,
                   asset.height AS asset_height, asset.format,
                   asset.transform_fingerprint
            FROM long_image_layouts AS layout
            JOIN derived_assets AS asset
              ON asset.derived_asset_id = layout.global_thumbnail_asset_id
            WHERE layout.run_id = ? AND layout.image_id = ?
            """,
            (stage2_run_id, image_id),
        ).fetchone()
        if layout is None:
            encoded, width, height, transform, fingerprint = _preview_bytes(
                working_path,
                source_sha256=str(observation["working_sha256"]),
                max_long_edge=int(quick["ordinary_preview_max_long_edge"]),
                quality=int(quick["ordinary_preview_jpeg_quality"]),
            )
            asset_sha = hashlib.sha256(encoded).hexdigest()
            units.append(
                QuickUnit(
                    unit_id=stable_id(unit_base, "image", ""),
                    image_id=image_id,
                    scope="image",
                    unit_index=None,
                    asset_id=None,
                    asset_path=None,
                    asset_sha256=asset_sha,
                    asset_type="quick_vlm_preview",
                    asset_format="JPEG",
                    width=width,
                    height=height,
                    transform_fingerprint=fingerprint,
                    asset_to_image_transform=transform,
                    working_asset_id=observation["working_asset_id"],
                    working_path=working_path,
                    alpha_asset_id=observation["alpha_asset_id"],
                    alpha_path=alpha_path,
                    long_image_layout_id=None,
                )
            )
            continue
        global_transform = json.loads(
            layout["image_to_thumbnail_transform_json"]
        )
        units.append(
            QuickUnit(
                unit_id=stable_id(unit_base, "global_thumbnail", ""),
                image_id=image_id,
                scope="global_thumbnail",
                unit_index=None,
                asset_id=layout["global_thumbnail_asset_id"],
                asset_path=_asset_path(workspace, layout["relative_path"]),
                asset_sha256=layout["sha256"],
                asset_type="global_thumbnail",
                asset_format=layout["format"],
                width=layout["asset_width"],
                height=layout["asset_height"],
                transform_fingerprint=layout["transform_fingerprint"],
                asset_to_image_transform={
                    "scale_x": 1.0 / float(global_transform["scale_x"]),
                    "scale_y": 1.0 / float(global_transform["scale_y"]),
                    "translate_x": (
                        -float(global_transform.get("translate_x", 0))
                        / float(global_transform["scale_x"])
                    ),
                    "translate_y": (
                        -float(global_transform.get("translate_y", 0))
                        / float(global_transform["scale_y"])
                    ),
                },
                working_asset_id=observation["working_asset_id"],
                working_path=working_path,
                alpha_asset_id=observation["alpha_asset_id"],
                alpha_path=alpha_path,
                long_image_layout_id=layout["long_image_layout_id"],
            )
        )
        for tile in connection.execute(
            """
            SELECT tile.*, asset.relative_path, asset.sha256,
                   asset.format, asset.transform_fingerprint
            FROM image_tiles AS tile
            JOIN derived_assets AS asset
              ON asset.derived_asset_id = tile.tile_asset_id
            WHERE tile.long_image_layout_id = ?
            ORDER BY tile.tile_index
            """,
            (layout["long_image_layout_id"],),
        ):
            units.append(
                QuickUnit(
                    unit_id=stable_id(
                        unit_base,
                        "tile",
                        int(tile["tile_index"]),
                    ),
                    image_id=image_id,
                    scope="tile",
                    unit_index=int(tile["tile_index"]),
                    asset_id=tile["tile_asset_id"],
                    asset_path=_asset_path(workspace, tile["relative_path"]),
                    asset_sha256=tile["sha256"],
                    asset_type="image_tile",
                    asset_format=tile["format"],
                    width=tile["tile_width"],
                    height=tile["tile_height"],
                    transform_fingerprint=tile["transform_fingerprint"],
                    asset_to_image_transform=json.loads(
                        tile["tile_to_image_transform_json"]
                    ),
                    working_asset_id=observation["working_asset_id"],
                    working_path=working_path,
                    alpha_asset_id=observation["alpha_asset_id"],
                    alpha_path=alpha_path,
                    long_image_layout_id=layout["long_image_layout_id"],
                )
            )
    return units


def _scope_instruction(scope: QuickScope) -> str:
    if scope == "image":
        return (
            "本次 scope=image。执行完整角色、布局、文字和色彩区域抽取。"
        )
    if scope == "global_thumbnail":
        return (
            "本次 scope=global_thumbnail。只判断整图角色、布局、资格、摘要和"
            "质量风险；text_items 与 color_regions 必须严格为空数组。"
        )
    return (
        "本次 scope=tile。抽取本局部切片可见的角色、文字和色彩区域；"
        "不得猜测切片之外内容。"
    )


def _prompt(settings: PipelineSettings, scope: QuickScope) -> str:
    base = (
        settings.repo_root / "configs" / "prompts" / "quick_extract_v1.txt"
    ).read_text(encoding="utf-8")
    schema = QuickImageExtraction.model_json_schema()
    return "\n\n".join(
        (
            base,
            _scope_instruction(scope),
            "请严格按以下 JSON Schema 输出：",
            canonical_json(schema),
        )
    )


def _generation_parameters(settings: PipelineSettings) -> dict[str, Any]:
    vlm = settings.section("vlm")
    return {
        "temperature": float(vlm["temperature"]),
        "response_format": {"type": "json_object"},
        "enable_thinking": bool(vlm["enable_thinking"]),
    }


def _request_manifest(
    settings: PipelineSettings,
    unit: QuickUnit,
    model: str,
) -> dict[str, Any]:
    if not unit.asset_id or not unit.asset_sha256:
        raise ValueError("callable quick extraction unit has no registered asset")
    prompt = _prompt(settings, unit.scope)
    quick = settings.section("quick_extract")
    manifest = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "analysis_layer": "A",
        "analysis_unit_type": (
            "image_tile" if unit.scope == "tile" else "derived_image_asset"
        ),
        "analysis_unit_id": unit.unit_id,
        "image_id": unit.image_id,
        "scope": unit.scope,
        "input_context_policy": "image_only",
        "model": model,
        "prompt_name": quick["prompt_name"],
        "prompt_version": quick["prompt_version"],
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_version": quick["schema_version"],
        "generation_parameters": _generation_parameters(settings),
        "image_asset": {
            "derived_asset_id": unit.asset_id,
            "asset_type": unit.asset_type,
            "sha256": unit.asset_sha256,
            "mime_type": MIME_BY_FORMAT.get(
                str(unit.asset_format).upper(),
                "application/octet-stream",
            ),
            "width": unit.width,
            "height": unit.height,
            "transform_fingerprint": unit.transform_fingerprint,
        },
    }
    audit_image_only_manifest(manifest)
    return manifest


def audit_image_only_manifest(manifest: Mapping[str, Any]) -> None:
    extra = set(manifest) - _REQUEST_KEYS
    if extra:
        raise ValueError(f"quick request contains non-whitelisted keys: {extra}")
    if manifest.get("input_context_policy") != "image_only":
        raise ValueError("quick extraction request must be image_only")
    asset = manifest.get("image_asset")
    if not isinstance(asset, Mapping):
        raise ValueError("quick extraction request requires image_asset")
    extra_asset = set(asset) - _ASSET_KEYS
    if extra_asset:
        raise ValueError(
            f"quick request asset contains non-whitelisted keys: {extra_asset}"
        )


def quick_cache_key(manifest: Mapping[str, Any]) -> str:
    asset = manifest["image_asset"]
    return sha256_json(
        {
            "asset_sha256": asset["sha256"],
            "scope": manifest["scope"],
            "model": manifest["model"],
            "prompt_name": manifest["prompt_name"],
            "prompt_version": manifest["prompt_version"],
            "prompt_sha256": manifest["prompt_sha256"],
            "schema_version": manifest["response_schema_version"],
            "generation_parameters": manifest["generation_parameters"],
        }
    )


def _model_name(settings: PipelineSettings) -> str:
    return os.environ.get(
        str(settings.section("vlm")["model_env"]),
        "qwen3.6-plus",
    )


def plan_quick_extraction(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    image_id: str | None = None,
    selection_manifest: Path | None = None,
    folder_group_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Compute selection, assets, cache, and call budget without writes/API."""

    model = _model_name(settings)
    with open_database(workspace.database_path, readonly=True) as connection:
        image_ids, selector = _select_image_ids(
            connection,
            settings,
            image_id=image_id,
            selection_manifest=selection_manifest,
            folder_group_id=folder_group_id,
            limit=limit,
        )
        units = _planned_units(
            connection,
            workspace,
            settings,
            run_id=run_id,
            image_ids=image_ids,
        )
        unit_plans: list[dict[str, Any]] = []
        cache_hits = 0
        callable_units = 0
        for unit in units:
            if unit.initial_status == "skipped":
                unit_plans.append(
                    {
                        "unit_id": unit.unit_id,
                        "image_id": unit.image_id,
                        "scope": unit.scope,
                        "unit_index": unit.unit_index,
                        "status": "skipped",
                        "failure": unit.failure,
                    }
                )
                continue
            callable_units += 1
            planning_unit = unit
            if planning_unit.asset_id is None:
                planning_unit = replace(
                    planning_unit,
                    asset_id=stable_id(
                        "planned_asset",
                        unit.image_id,
                        unit.transform_fingerprint,
                    ),
                )
            manifest = _request_manifest(settings, planning_unit, model)
            cache_key = quick_cache_key(manifest)
            cached = connection.execute(
                """
                SELECT 1 FROM model_runs
                WHERE cache_key = ?
                  AND status IN ('succeeded', 'cache_hit')
                  AND parsed_response_path IS NOT NULL
                  AND raw_response_path IS NOT NULL
                LIMIT 1
                """,
                (cache_key,),
            ).fetchone()
            cache_hit = cached is not None
            cache_hits += int(cache_hit)
            unit_plans.append(
                {
                    "unit_id": unit.unit_id,
                    "image_id": unit.image_id,
                    "scope": unit.scope,
                    "unit_index": unit.unit_index,
                    "asset_type": unit.asset_type,
                    "asset_sha256": unit.asset_sha256,
                    "asset_size": [unit.width, unit.height],
                    "cache_key": cache_key,
                    "cache_status": "hit" if cache_hit else "miss",
                }
            )
    provider_calls = callable_units - cache_hits
    recommended = (
        max(provider_calls, math.ceil(provider_calls * 1.2))
        if provider_calls
        else 0
    )
    return {
        "schema_version": "quick-extraction-plan-1.0",
        "status": "planned",
        "run_id": run_id,
        "selector": selector,
        "selected_image_count": len(image_ids),
        "unit_count": len(units),
        "callable_unit_count": callable_units,
        "skipped_unit_count": len(units) - callable_units,
        "cache_hit_count": cache_hits,
        "planned_provider_success_calls": provider_calls,
        "recommended_max_calls_with_retry_headroom": recommended,
        "maximum_calls_if_every_unit_exhausts_retries": provider_calls
        * (int(settings.section("vlm")["max_retries"]) + 1),
        "model": model,
        "units": unit_plans,
    }


def _prepare_units(
    connection: sqlite3.Connection,
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    units: Sequence[QuickUnit],
) -> list[QuickUnit]:
    """Materialize only ordinary VLM previews; reuse Stage 2 long assets."""

    prepared: list[QuickUnit] = []
    quick = settings.section("quick_extract")
    for unit in units:
        current = unit
        failure = dict(unit.failure or {})
        if unit.initial_status != "skipped":
            if (
                unit.working_path is None
                or not unit.working_path.is_file()
                or unit.asset_sha256 is None
            ):
                failure = {"code": "registered_stage2_asset_missing"}
                current = replace(
                    unit,
                    initial_status="skipped",
                    failure=failure,
                )
            elif unit.asset_id is None:
                working_row = connection.execute(
                    """
                    SELECT sha256 FROM derived_assets
                    WHERE derived_asset_id = ?
                    """,
                    (unit.working_asset_id,),
                ).fetchone()
                if working_row is None:
                    current = replace(
                        unit,
                        initial_status="skipped",
                        failure={"code": "working_asset_registration_missing"},
                    )
                else:
                    encoded, width, height, transform, fingerprint = (
                        _preview_bytes(
                            unit.working_path,
                            source_sha256=str(working_row["sha256"]),
                            max_long_edge=int(
                                quick["ordinary_preview_max_long_edge"]
                            ),
                            quality=int(
                                quick["ordinary_preview_jpeg_quality"]
                            ),
                        )
                    )
                    destination = (
                        workspace.assets_root
                        / "quick_vlm_preview"
                        / unit.image_id[:2]
                        / unit.image_id
                        / f"{fingerprint}.jpg"
                    )
                    expected_sha = hashlib.sha256(encoded).hexdigest()
                    if destination.exists():
                        if sha256_file(destination) != expected_sha:
                            raise RuntimeError(
                                "quick preview path contains unexpected bytes: "
                                f"{destination}"
                            )
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_name(
                            destination.name + ".tmp"
                        )
                        temporary.write_bytes(encoded)
                        temporary.replace(destination)
                    asset = register_existing_asset(
                        connection,
                        workspace,
                        run_id,
                        image_id=unit.image_id,
                        asset_type="quick_vlm_preview",
                        path=destination,
                        width=width,
                        height=height,
                        image_format="JPEG",
                        transform_name=(
                            "stage2_working_to_quick_vlm_preview"
                        ),
                        transform_version=PREVIEW_TRANSFORM_VERSION,
                        transform_fingerprint=fingerprint,
                        metadata={
                            "source_stage2_working_asset_id": (
                                unit.working_asset_id
                            ),
                            "source_stage2_working_sha256": (
                                working_row["sha256"]
                            ),
                            "image_to_asset_transform": {
                                "scale_x": 1.0 / transform["scale_x"],
                                "scale_y": 1.0 / transform["scale_y"],
                                "translate_x": (
                                    -transform["translate_x"]
                                    / transform["scale_x"]
                                ),
                                "translate_y": (
                                    -transform["translate_y"]
                                    / transform["scale_y"]
                                ),
                            },
                            "asset_to_image_transform": transform,
                            "coordinate_system": (
                                "stage2_exif_oriented_working_pixels"
                            ),
                            "bbox_convention": (
                                "[x_min,y_min,x_max,y_max) half-open"
                            ),
                            "provider_minimum_edge_padding": {
                                "minimum_edge": 11,
                                "fill": [255, 255, 255],
                            },
                        },
                    )
                    current = replace(
                        unit,
                        asset_id=asset.derived_asset_id,
                        asset_path=destination,
                        asset_sha256=asset.sha256,
                        asset_type=asset.asset_type,
                        asset_format=asset.format,
                        width=asset.width,
                        height=asset.height,
                        transform_fingerprint=asset.transform_fingerprint,
                        asset_to_image_transform=transform,
                    )
            elif (
                unit.asset_path is None
                or not unit.asset_path.is_file()
                or sha256_file(unit.asset_path) != unit.asset_sha256
            ):
                current = replace(
                    unit,
                    initial_status="skipped",
                    failure={
                        "code": "stage2_analysis_asset_missing_or_hash_mismatch"
                    },
                )
        prepared.append(current)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO quick_extraction_units(
                quick_extraction_unit_id, run_id, image_id, scope,
                unit_index, source_asset_id, working_asset_id,
                alpha_asset_id, long_image_layout_id, asset_sha256,
                asset_to_image_transform_json, cache_key, model_run_id,
                unit_status, provider_attempt_count, failure_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0, ?, ?, ?)
            ON CONFLICT(quick_extraction_unit_id) DO UPDATE SET
                source_asset_id = excluded.source_asset_id,
                working_asset_id = excluded.working_asset_id,
                alpha_asset_id = excluded.alpha_asset_id,
                long_image_layout_id = excluded.long_image_layout_id,
                asset_sha256 = excluded.asset_sha256,
                asset_to_image_transform_json =
                    excluded.asset_to_image_transform_json,
                unit_status = CASE
                    WHEN quick_extraction_units.unit_status IN (
                        'succeeded', 'cache_hit'
                    ) THEN quick_extraction_units.unit_status
                    ELSE excluded.unit_status
                END,
                failure_json = CASE
                    WHEN quick_extraction_units.unit_status IN (
                        'succeeded', 'cache_hit'
                    ) THEN quick_extraction_units.failure_json
                    ELSE excluded.failure_json
                END,
                updated_at = excluded.updated_at
            """,
            (
                current.unit_id,
                run_id,
                current.image_id,
                current.scope,
                current.unit_index,
                current.asset_id,
                current.working_asset_id,
                current.alpha_asset_id,
                current.long_image_layout_id,
                current.asset_sha256,
                canonical_json(current.asset_to_image_transform),
                current.initial_status,
                canonical_json(current.failure or {}),
                now,
                now,
            ),
        )
    connection.commit()
    return prepared


def _data_url(path: Path, image_format: str) -> str:
    mime = MIME_BY_FORMAT.get(image_format.upper(), "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _call_once(
    *,
    client: Any,
    api_key: str,
    budget: ProviderCallBudget,
    workspace: Workspace,
    run_id: str,
    unit: QuickUnit,
    manifest: dict[str, Any],
    cache_key: str,
    attempt: int,
) -> QuickAttempt:
    model_run_id = stable_id(
        "model_run",
        run_id,
        unit.unit_id,
        cache_key,
        attempt,
    )
    run_dir = workspace.run_dir(run_id)
    request_relative = f"model/requests/{model_run_id}.json"
    raw_relative = f"model/raw/{model_run_id}.json"
    parsed_relative = f"model/parsed/{model_run_id}.json"
    _write_json(run_dir / request_relative, manifest)
    raw_file = run_dir / raw_relative
    parsed_file = run_dir / parsed_relative
    started = time.perf_counter()
    response: Any = None
    try:
        budget.take()
        if unit.asset_path is None or unit.asset_format is None:
            raise ValueError("quick extraction unit has no readable image asset")
        response = client.chat.completions.create(
            model=manifest["model"],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": _data_url(
                                    unit.asset_path,
                                    unit.asset_format,
                                )
                            },
                        },
                        {"type": "text", "text": manifest["prompt"]},
                    ],
                }
            ],
            response_format=manifest["generation_parameters"][
                "response_format"
            ],
            temperature=manifest["generation_parameters"]["temperature"],
            extra_body={
                "enable_thinking": manifest["generation_parameters"][
                    "enable_thinking"
                ]
            },
        )
        raw_payload = response.model_dump(mode="json")
        _write_json(raw_file, raw_payload)
        response_hash = sha256_file(raw_file)
        text = response.choices[0].message.content or ""
        try:
            parsed, actions = parse_quick_image_extraction(
                text,
                expected_scope=unit.scope,
                image_width=int(unit.width or 0),
                image_height=int(unit.height or 0),
            )
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            TypeError,
        ) as error:
            return QuickAttempt(
                model_run_id=model_run_id,
                cache_key=cache_key,
                attempt=attempt,
                status="schema_failed",
                schema_status="invalid",
                request_path=request_relative,
                raw_path=raw_relative,
                parsed_path=None,
                response_hash=response_hash,
                latency_ms=round((time.perf_counter() - started) * 1000),
                token_usage=(
                    response.usage.model_dump(mode="json")
                    if response.usage is not None
                    else {}
                ),
                error={
                    "type": type(error).__name__,
                    "message": _redact(str(error), api_key),
                },
                parsed=None,
                provider_model_name=getattr(response, "model", None),
            )
        _write_json(parsed_file, parsed.model_dump(mode="json"))
        return QuickAttempt(
            model_run_id=model_run_id,
            cache_key=cache_key,
            attempt=attempt,
            status="succeeded",
            schema_status=(
                "valid_after_deterministic_bbox_normalization"
                if actions
                else "valid"
            ),
            request_path=request_relative,
            raw_path=raw_relative,
            parsed_path=parsed_relative,
            response_hash=response_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            token_usage=(
                response.usage.model_dump(mode="json")
                if response.usage is not None
                else {}
            ),
            error=(
                {"deterministic_normalizations": list(actions)}
                if actions
                else {}
            ),
            parsed=parsed,
            provider_model_name=getattr(response, "model", None),
        )
    except Exception as error:
        return QuickAttempt(
            model_run_id=model_run_id,
            cache_key=cache_key,
            attempt=attempt,
            status=(
                "budget_exhausted"
                if isinstance(error, ProviderCallBudgetExceeded)
                else "request_failed"
            ),
            schema_status="not_parsed",
            request_path=request_relative,
            raw_path=raw_relative if raw_file.exists() else None,
            parsed_path=None,
            response_hash=sha256_file(raw_file) if raw_file.exists() else None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            token_usage={},
            error={
                "type": type(error).__name__,
                "message": _redact(str(error), api_key),
            },
            parsed=None,
            provider_model_name=(
                getattr(response, "model", None)
                if response is not None
                else None
            ),
        )


def _record_attempt(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    result: QuickAttempt,
) -> None:
    vlm = settings.section("vlm")
    connection.execute(
        """
        INSERT OR REPLACE INTO model_runs(
            model_run_id, run_id, analysis_layer, analysis_unit_type,
            analysis_unit_id, model_name, provider, base_url_alias,
            prompt_name, prompt_version, schema_version,
            input_context_policy, generation_parameters_json, cache_key,
            request_hash, request_path, raw_response_path,
            parsed_response_path, response_hash, schema_validation_status,
            latency_ms, token_usage_json, status, error_json,
            created_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.model_run_id,
            run_id,
            "A",
            manifest["analysis_unit_type"],
            manifest["analysis_unit_id"],
            manifest["model"],
            vlm["provider"],
            str(vlm["base_url_env"]),
            manifest["prompt_name"],
            manifest["prompt_version"],
            manifest["response_schema_version"],
            "image_only",
            canonical_json(manifest["generation_parameters"]),
            result.cache_key,
            hashlib.sha256(
                canonical_json(manifest).encode("utf-8")
            ).hexdigest(),
            result.request_path,
            result.raw_path,
            result.parsed_path,
            result.response_hash,
            result.schema_status,
            result.latency_ms,
            canonical_json(result.token_usage),
            result.status,
            canonical_json(
                {
                    **result.error,
                    "provider_model_name": result.provider_model_name,
                    "attempt": result.attempt,
                }
            ),
            utc_now(),
            utc_now(),
        ),
    )


def _cached_attempt(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    workspace: Workspace,
    *,
    run_id: str,
    unit: QuickUnit,
    model: str,
) -> tuple[dict[str, Any], QuickAttempt] | None:
    manifest = _request_manifest(settings, unit, model)
    cache_key = quick_cache_key(manifest)
    row = connection.execute(
        """
        SELECT model_run_id, run_id, raw_response_path,
               parsed_response_path, response_hash, error_json, model_name
        FROM model_runs
        WHERE cache_key = ?
          AND status IN ('succeeded', 'cache_hit')
          AND raw_response_path IS NOT NULL
          AND parsed_response_path IS NOT NULL
          AND run_id <> ?
        ORDER BY finished_at DESC, model_run_id DESC
        LIMIT 1
        """,
        (cache_key, run_id),
    ).fetchone()
    if row is None:
        return None
    source_run_dir = workspace.run_dir(row["run_id"])
    source_raw = source_run_dir / row["raw_response_path"]
    source_parsed = source_run_dir / row["parsed_response_path"]
    if not source_raw.is_file() or not source_parsed.is_file():
        return None
    try:
        parsed = QuickImageExtraction.model_validate_json(
            source_parsed.read_text(encoding="utf-8")
        )
    except (ValidationError, ValueError, json.JSONDecodeError):
        return None
    if parsed.scope != unit.scope:
        return None
    model_run_id = stable_id(
        "model_run",
        run_id,
        unit.unit_id,
        cache_key,
        "cache",
    )
    run_dir = workspace.run_dir(run_id)
    request_relative = f"model/requests/{model_run_id}.json"
    raw_relative = f"model/raw/{model_run_id}.json"
    parsed_relative = f"model/parsed/{model_run_id}.json"
    _write_json(run_dir / request_relative, manifest)
    _write_json(run_dir / parsed_relative, parsed.model_dump(mode="json"))
    destination_raw = run_dir / raw_relative
    destination_raw.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_raw, destination_raw)
    except (FileExistsError, OSError):
        if not destination_raw.exists():
            shutil.copy2(source_raw, destination_raw)
    source_error = json.loads(row["error_json"] or "{}")
    return (
        manifest,
        QuickAttempt(
            model_run_id=model_run_id,
            cache_key=cache_key,
            attempt=0,
            status="cache_hit",
            schema_status="valid",
            request_path=request_relative,
            raw_path=raw_relative,
            parsed_path=parsed_relative,
            response_hash=(
                row["response_hash"] or sha256_file(destination_raw)
            ),
            latency_ms=0,
            token_usage={
                "cached": True,
                "cached_from_model_run_id": row["model_run_id"],
            },
            error={"cached_from_model_run_id": row["model_run_id"]},
            parsed=parsed,
            provider_model_name=(
                source_error.get("provider_model_name") or row["model_name"]
            ),
        ),
    )


def _attempt_offset(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    cache_key: str,
) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM model_runs
            WHERE run_id = ? AND cache_key = ?
              AND status NOT IN ('cache_hit', 'budget_exhausted')
            """,
            (run_id, cache_key),
        ).fetchone()[0]
    )


def _execute_unit(
    *,
    client: Any,
    api_key: str,
    budget: ProviderCallBudget,
    workspace: Workspace,
    settings: PipelineSettings,
    run_id: str,
    unit: QuickUnit,
    model: str,
    attempt_offset: int,
) -> tuple[QuickUnit, dict[str, Any], list[QuickAttempt]]:
    manifest = _request_manifest(settings, unit, model)
    cache_key = quick_cache_key(manifest)
    maximum_attempts = int(settings.section("vlm")["max_retries"]) + 1
    attempts: list[QuickAttempt] = []
    for attempt in range(attempt_offset + 1, maximum_attempts + 1):
        result = _call_once(
            client=client,
            api_key=api_key,
            budget=budget,
            workspace=workspace,
            run_id=run_id,
            unit=unit,
            manifest=manifest,
            cache_key=cache_key,
            attempt=attempt,
        )
        attempts.append(result)
        if result.status in ("succeeded", "budget_exhausted"):
            break
        if result.status == "request_failed":
            time.sleep(min(8.0, 2.0 ** (attempt - 1)))
    return unit, manifest, attempts


def _set_unit_result(
    connection: sqlite3.Connection,
    *,
    unit: QuickUnit,
    result: QuickAttempt,
    provider_attempt_count: int,
) -> None:
    if result.status in ("succeeded", "cache_hit"):
        unit_status = result.status
    elif result.status == "budget_exhausted":
        unit_status = "budget_exhausted"
    else:
        unit_status = "failed"
    connection.execute(
        """
        UPDATE quick_extraction_units
        SET cache_key = ?, model_run_id = ?, unit_status = ?,
            provider_attempt_count = ?, failure_json = ?, updated_at = ?
        WHERE quick_extraction_unit_id = ?
        """,
        (
            result.cache_key,
            result.model_run_id,
            unit_status,
            provider_attempt_count,
            canonical_json(
                {}
                if result.parsed is not None
                else {
                    "last_attempt_status": result.status,
                    "last_error": result.error,
                }
            ),
            utc_now(),
            unit.unit_id,
        ),
    )


def _units_from_database(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
) -> list[QuickUnit]:
    units: list[QuickUnit] = []
    for row in connection.execute(
        """
        SELECT unit.*, source.relative_path AS source_path,
               source.format AS source_format,
               source.width AS source_width,
               source.height AS source_height,
               source.asset_type AS source_asset_type,
               source.transform_fingerprint AS source_transform_fingerprint,
               working.relative_path AS working_path,
               alpha.relative_path AS alpha_path
        FROM quick_extraction_units AS unit
        LEFT JOIN derived_assets AS source
          ON source.derived_asset_id = unit.source_asset_id
        LEFT JOIN derived_assets AS working
          ON working.derived_asset_id = unit.working_asset_id
        LEFT JOIN derived_assets AS alpha
          ON alpha.derived_asset_id = unit.alpha_asset_id
        WHERE unit.run_id = ?
        ORDER BY unit.image_id,
                 CASE unit.scope
                    WHEN 'image' THEN 0
                    WHEN 'global_thumbnail' THEN 1
                    ELSE 2
                 END,
                 unit.unit_index
        """,
        (run_id,),
    ):
        units.append(
            QuickUnit(
                unit_id=row["quick_extraction_unit_id"],
                image_id=row["image_id"],
                scope=row["scope"],
                unit_index=row["unit_index"],
                asset_id=row["source_asset_id"],
                asset_path=_asset_path(workspace, row["source_path"]),
                asset_sha256=row["asset_sha256"],
                asset_type=row["source_asset_type"],
                asset_format=row["source_format"],
                width=row["source_width"],
                height=row["source_height"],
                transform_fingerprint=(
                    row["source_transform_fingerprint"] or ""
                ),
                asset_to_image_transform=json.loads(
                    row["asset_to_image_transform_json"]
                ),
                working_asset_id=row["working_asset_id"],
                working_path=_asset_path(workspace, row["working_path"]),
                alpha_asset_id=row["alpha_asset_id"],
                alpha_path=_asset_path(workspace, row["alpha_path"]),
                long_image_layout_id=row["long_image_layout_id"],
                initial_status=row["unit_status"],
                failure=json.loads(row["failure_json"] or "{}"),
            )
        )
    return units


def recover_quick_run_artifacts(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    finalize: bool = False,
) -> dict[str, Any]:
    """Recover immutable request/raw/parsed artifacts after interruption."""

    run_dir = workspace.run_dir(run_id)
    request_dir = run_dir / "model/requests"
    if not request_dir.is_dir():
        raise FileNotFoundError(f"quick run request directory missing: {run_id}")
    maximum_attempts = int(settings.section("vlm")["max_retries"]) + 1
    recovered = {
        "succeeded": 0,
        "schema_failed": 0,
        "request_failed": 0,
        "already_recorded": 0,
        "unmatched": 0,
    }
    with open_database(workspace.database_path) as connection:
        units = _units_from_database(
            connection,
            workspace,
            run_id=run_id,
        )
        unit_lookup = {unit.unit_id: unit for unit in units}
        for request_file in sorted(request_dir.glob("*.json")):
            model_run_id = request_file.stem
            if connection.execute(
                "SELECT 1 FROM model_runs WHERE model_run_id = ?",
                (model_run_id,),
            ).fetchone():
                recovered["already_recorded"] += 1
                continue
            try:
                manifest = json.loads(request_file.read_text(encoding="utf-8"))
                audit_image_only_manifest(manifest)
                unit = unit_lookup[str(manifest["analysis_unit_id"])]
            except (KeyError, ValueError, json.JSONDecodeError):
                recovered["unmatched"] += 1
                continue
            cache_key = quick_cache_key(manifest)
            attempt = next(
                (
                    candidate
                    for candidate in range(1, maximum_attempts + 1)
                    if stable_id(
                        "model_run",
                        run_id,
                        unit.unit_id,
                        cache_key,
                        candidate,
                    )
                    == model_run_id
                ),
                None,
            )
            if attempt is None:
                recovered["unmatched"] += 1
                continue
            request_relative = request_file.relative_to(run_dir).as_posix()
            raw_file = run_dir / f"model/raw/{model_run_id}.json"
            parsed_file = run_dir / f"model/parsed/{model_run_id}.json"
            parsed: QuickImageExtraction | None = None
            token_usage: dict[str, Any] = {}
            provider_model_name: str | None = None
            response_hash: str | None = None
            error: dict[str, Any] = {
                "recovered_from_immutable_artifacts": True
            }
            if raw_file.is_file():
                raw_payload = json.loads(raw_file.read_text(encoding="utf-8"))
                response_hash = sha256_file(raw_file)
                usage = raw_payload.get("usage")
                if isinstance(usage, dict):
                    token_usage = usage
                if isinstance(raw_payload.get("model"), str):
                    provider_model_name = raw_payload["model"]
                try:
                    if parsed_file.is_file():
                        parsed = QuickImageExtraction.model_validate_json(
                            parsed_file.read_text(encoding="utf-8")
                        )
                        if parsed.scope != unit.scope:
                            raise ValueError("recovered parsed scope mismatch")
                    else:
                        text = raw_payload["choices"][0]["message"]["content"]
                        parsed, actions = parse_quick_image_extraction(
                            text,
                            expected_scope=unit.scope,
                            image_width=int(unit.width or 0),
                            image_height=int(unit.height or 0),
                        )
                        _write_json(
                            parsed_file,
                            parsed.model_dump(mode="json"),
                        )
                        if actions:
                            error["deterministic_normalizations"] = list(
                                actions
                            )
                except (
                    KeyError,
                    IndexError,
                    TypeError,
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as parse_error:
                    status = "schema_failed"
                    schema_status = "invalid"
                    error.update(
                        {
                            "type": type(parse_error).__name__,
                            "message": str(parse_error),
                        }
                    )
                    recovered["schema_failed"] += 1
                else:
                    status = "succeeded"
                    schema_status = "valid_recovered"
                    recovered["succeeded"] += 1
            else:
                status = "request_failed"
                schema_status = "not_parsed"
                error.update(
                    {
                        "type": "InterruptedAttempt",
                        "message": (
                            "request artifact exists without a raw response; "
                            "counted conservatively as a provider attempt"
                        ),
                    }
                )
                recovered["request_failed"] += 1
            result = QuickAttempt(
                model_run_id=model_run_id,
                cache_key=cache_key,
                attempt=attempt,
                status=status,
                schema_status=schema_status,
                request_path=request_relative,
                raw_path=(
                    raw_file.relative_to(run_dir).as_posix()
                    if raw_file.is_file()
                    else None
                ),
                parsed_path=(
                    parsed_file.relative_to(run_dir).as_posix()
                    if parsed is not None and parsed_file.is_file()
                    else None
                ),
                response_hash=response_hash,
                latency_ms=0,
                token_usage=token_usage,
                error=error,
                parsed=parsed,
                provider_model_name=provider_model_name,
            )
            _record_attempt(
                connection,
                settings,
                run_id=run_id,
                manifest=manifest,
                result=result,
            )
        for unit in units:
            attempts = list(
                connection.execute(
                    """
                    SELECT * FROM model_runs
                    WHERE run_id = ? AND analysis_unit_id = ?
                    ORDER BY created_at, model_run_id
                    """,
                    (run_id, unit.unit_id),
                )
            )
            successful = next(
                (
                    row
                    for row in reversed(attempts)
                    if row["status"] in ("succeeded", "cache_hit")
                    and row["parsed_response_path"]
                ),
                None,
            )
            provider_count = sum(
                row["status"] not in ("cache_hit", "budget_exhausted")
                for row in attempts
            )
            if successful is not None:
                connection.execute(
                    """
                    UPDATE quick_extraction_units
                    SET cache_key = ?, model_run_id = ?, unit_status = ?,
                        provider_attempt_count = ?, failure_json = '{}',
                        updated_at = ?
                    WHERE quick_extraction_unit_id = ?
                    """,
                    (
                        successful["cache_key"],
                        successful["model_run_id"],
                        successful["status"],
                        provider_count,
                        utc_now(),
                        unit.unit_id,
                    ),
                )
            elif attempts and unit.initial_status != "skipped":
                last = attempts[-1]
                connection.execute(
                    """
                    UPDATE quick_extraction_units
                    SET cache_key = ?, model_run_id = ?,
                        unit_status = CASE
                            WHEN ? >= ? THEN 'failed' ELSE 'prepared'
                        END,
                        provider_attempt_count = ?, failure_json = ?,
                        updated_at = ?
                    WHERE quick_extraction_unit_id = ?
                    """,
                    (
                        last["cache_key"],
                        last["model_run_id"],
                        provider_count,
                        maximum_attempts,
                        provider_count,
                        canonical_json(
                            {
                                "code": "interrupted_attempts_recovered",
                                "last_status": last["status"],
                            }
                        ),
                        utc_now(),
                        unit.unit_id,
                    ),
                )
        connection.commit()
    summary = {
        "schema_version": "quick-artifact-recovery-1.0",
        "run_id": run_id,
        **recovered,
        "provider_attempts_recovered": (
            recovered["succeeded"]
            + recovered["schema_failed"]
            + recovered["request_failed"]
        ),
    }
    if finalize:
        with open_database(workspace.database_path) as connection:
            final_units = _units_from_database(
                connection,
                workspace,
                run_id=run_id,
            )
            image_ids = sorted({unit.image_id for unit in final_units})
            image_summaries = [
                _aggregate_image(
                    connection,
                    workspace,
                    settings,
                    run_id=run_id,
                    image_id=image_id,
                    units=[
                        unit
                        for unit in final_units
                        if unit.image_id == image_id
                    ],
                )
                for image_id in image_ids
            ]
            connection.commit()
            after_hashes = _source_hash_snapshot(
                connection,
                workspace,
                image_ids=image_ids,
            )
            before_files = sorted(
                (run_dir / "reports").glob(
                    "selected_source_hashes_before.*.json"
                )
            )
            before_hashes = (
                json.loads(before_files[-1].read_text(encoding="utf-8"))
                if before_files
                else None
            )
            source_drift = bool(
                before_hashes is not None
                and before_hashes.get("occurrences")
                != after_hashes.get("occurrences")
            )
            image_statuses: dict[str, int] = {}
            for image_summary in image_summaries:
                status = str(image_summary["status"])
                image_statuses[status] = image_statuses.get(status, 0) + 1
            pipeline_status = (
                "failed"
                if source_drift
                else (
                    "completed_with_failures"
                    if any(
                        status != "success"
                        for status in image_statuses
                    )
                    else "completed"
                )
            )
            finish_pipeline_run(
                connection,
                run_id,
                status=pipeline_status,
                error_summary={
                    "artifact_recovery": True,
                    "source_drift_detected": source_drift,
                    "image_statuses": image_statuses,
                },
            )
        _write_report(
            run_dir,
            "selected_source_hashes_after_recovery",
            after_hashes,
        )
        summary.update(
            {
                "finalized": True,
                "pipeline_status": pipeline_status,
                "source_drift_detected": source_drift,
                "image_statuses": image_statuses,
                "image_summaries": image_summaries,
            }
        )
    _write_report(run_dir, "quick_artifact_recovery", summary)
    return summary


def normalize_visible_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return "".join(
        character.upper() if "a" <= character <= "z" else character
        for character in normalized
    )


def bbox_iou(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != 4 or len(right) != 4:
        raise ValueError("IoU boxes must each contain four coordinates")
    intersection_width = max(
        0.0,
        min(float(left[2]), float(right[2]))
        - max(float(left[0]), float(right[0])),
    )
    intersection_height = max(
        0.0,
        min(float(left[3]), float(right[3]))
        - max(float(left[1]), float(right[1])),
    )
    intersection = intersection_width * intersection_height
    left_area = max(0.0, float(left[2]) - float(left[0])) * max(
        0.0,
        float(left[3]) - float(left[1]),
    )
    right_area = max(0.0, float(right[2]) - float(right[0])) * max(
        0.0,
        float(right[3]) - float(right[1]),
    )
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _map_bbox_to_image(
    bbox_norm: Sequence[float],
    unit: QuickUnit,
    *,
    image_width: int,
    image_height: int,
) -> list[float]:
    if (
        unit.width is None
        or unit.height is None
        or unit.width <= 0
        or unit.height <= 0
    ):
        raise ValueError("unit asset dimensions are unavailable")
    x0, y0, x1, y1 = (
        float(bbox_norm[0]) * unit.width,
        float(bbox_norm[1]) * unit.height,
        float(bbox_norm[2]) * unit.width,
        float(bbox_norm[3]) * unit.height,
    )
    transform = unit.asset_to_image_transform
    scale_x = float(transform["scale_x"])
    scale_y = float(transform["scale_y"])
    translate_x = float(transform.get("translate_x", 0.0))
    translate_y = float(transform.get("translate_y", 0.0))
    mapped = [
        x0 * scale_x + translate_x,
        y0 * scale_y + translate_y,
        x1 * scale_x + translate_x,
        y1 * scale_y + translate_y,
    ]
    mapped = [
        max(0.0, min(float(image_width), mapped[0])),
        max(0.0, min(float(image_height), mapped[1])),
        max(0.0, min(float(image_width), mapped[2])),
        max(0.0, min(float(image_height), mapped[3])),
    ]
    if not (mapped[0] < mapped[2] and mapped[1] < mapped[3]):
        raise ValueError("mapped bbox has no area inside the working image")
    return mapped


def _load_successful_models(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    units: Sequence[QuickUnit],
) -> tuple[list[tuple[QuickUnit, QuickImageExtraction]], dict[str, str]]:
    models: list[tuple[QuickUnit, QuickImageExtraction]] = []
    statuses: dict[str, str] = {}
    unit_lookup = {unit.unit_id: unit for unit in units}
    for row in connection.execute(
        """
        SELECT unit.quick_extraction_unit_id, unit.unit_status,
               model.parsed_response_path
        FROM quick_extraction_units AS unit
        LEFT JOIN model_runs AS model
          ON model.model_run_id = unit.model_run_id
        WHERE unit.run_id = ?
        ORDER BY unit.image_id, unit.scope, unit.unit_index
        """,
        (run_id,),
    ):
        unit_id = str(row["quick_extraction_unit_id"])
        if unit_id not in unit_lookup:
            continue
        statuses[unit_id] = str(row["unit_status"])
        if row["unit_status"] not in ("succeeded", "cache_hit"):
            continue
        if not row["parsed_response_path"]:
            statuses[unit_id] = "failed"
            continue
        parsed_path = workspace.run_dir(run_id) / row["parsed_response_path"]
        if not parsed_path.is_file():
            statuses[unit_id] = "failed"
            continue
        try:
            parsed = QuickImageExtraction.model_validate_json(
                parsed_path.read_text(encoding="utf-8")
            )
        except (ValidationError, ValueError, json.JSONDecodeError):
            statuses[unit_id] = "failed"
            continue
        if parsed.scope != unit_lookup[unit_id].scope:
            statuses[unit_id] = "failed"
            continue
        models.append((unit_lookup[unit_id], parsed))
    return models, statuses


def merge_text_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    image_id: str,
    iou_threshold: float,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], str]]:
    canonical: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(item) for item in observations),
        key=lambda item: (
            str(item["unit_id"]),
            str(item["model_text_item_id"]),
        ),
    )
    for observation in ordered:
        normalized = normalize_visible_text(str(observation["raw_text"]))
        observation["normalized_text"] = normalized
        match: dict[str, Any] | None = None
        match_iou = 0.0
        for candidate in canonical:
            if candidate["normalized_text"] != normalized:
                continue
            left = candidate["bbox_image"]
            right = observation.get("bbox_image")
            if left is None or right is None:
                continue
            overlap = bbox_iou(left, right)
            if overlap >= iou_threshold and overlap >= match_iou:
                match = candidate
                match_iou = overlap
        if match is None:
            bbox = observation.get("bbox_image")
            bbox_key = (
                [round(float(value), 3) for value in bbox]
                if bbox is not None
                else []
            )
            text_item_id = stable_id(
                "qtext",
                image_id,
                normalized,
                observation["text_type"],
                canonical_json(bbox_key),
                len(canonical),
            )
            canonical.append(
                {
                    "text_item_id": text_item_id,
                    "raw_text": observation["raw_text"],
                    "normalized_text": normalized,
                    "text_type": observation["text_type"],
                    "bbox_image": bbox,
                    "confidence": float(observation["confidence"]),
                    "sources": [observation],
                    "deduplication": {
                        "version": AGGREGATION_VERSION,
                        "rule": (
                            "same_normalized_text_and_bbox_iou_gte_threshold"
                        ),
                        "iou_threshold": iou_threshold,
                        "merged_source_count": 1,
                    },
                }
            )
            continue
        match["sources"].append(observation)
        match["deduplication"]["merged_source_count"] = len(match["sources"])
        match["deduplication"].setdefault("merge_ious", []).append(match_iou)
        if float(observation["confidence"]) > float(match["confidence"]):
            match.update(
                {
                    "raw_text": observation["raw_text"],
                    "text_type": observation["text_type"],
                    "bbox_image": observation.get("bbox_image"),
                    "confidence": float(observation["confidence"]),
                }
            )
    source_to_canonical = {
        (str(source["unit_id"]), str(source["model_text_item_id"])): str(
            item["text_item_id"]
        )
        for item in canonical
        for source in item["sources"]
    }
    return canonical, source_to_canonical


def _shade_code_conflicts(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return normalize_visible_text(left) != normalize_visible_text(right)


def merge_region_observations(
    observations: Iterable[Mapping[str, Any]],
    *,
    image_id: str,
    iou_threshold: float,
    text_id_map: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    canonical: list[dict[str, Any]] = []
    ordered = sorted(
        (dict(item) for item in observations),
        key=lambda item: (
            str(item["unit_id"]),
            str(item["model_region_id"]),
        ),
    )
    for observation in ordered:
        linked_ids = sorted(
            {
                text_id_map[(str(observation["unit_id"]), str(model_id))]
                for model_id in observation["linked_model_text_item_ids"]
                if (
                    str(observation["unit_id"]),
                    str(model_id),
                )
                in text_id_map
            }
        )
        observation["linked_text_item_ids"] = linked_ids
        match: dict[str, Any] | None = None
        match_iou = 0.0
        for candidate in canonical:
            if candidate["region_type"] != observation["region_type"]:
                continue
            if _shade_code_conflicts(
                candidate.get("shade_code_text"),
                observation.get("shade_code_text"),
            ):
                continue
            overlap = bbox_iou(
                candidate["bbox_image"],
                observation["bbox_image"],
            )
            if overlap >= iou_threshold and overlap >= match_iou:
                match = candidate
                match_iou = overlap
        if match is None:
            bbox_key = [
                round(float(value), 3)
                for value in observation["bbox_image"]
            ]
            region_id = stable_id(
                "qregion",
                image_id,
                observation["region_type"],
                normalize_visible_text(
                    str(observation.get("shade_code_text") or "")
                ),
                canonical_json(bbox_key),
                len(canonical),
            )
            canonical.append(
                {
                    "region_id": region_id,
                    "region_type": observation["region_type"],
                    "bbox_image": observation["bbox_image"],
                    "model_confidence": float(observation["confidence"]),
                    "shade_code_text": observation.get("shade_code_text"),
                    "shade_name_text": observation.get("shade_name_text"),
                    "visual_color_name": observation.get("visual_color_name"),
                    "linked_text_item_ids": linked_ids,
                    "association_confidence": observation.get(
                        "association_confidence"
                    ),
                    "extraction_eligible": bool(
                        observation["source_extraction_eligible"]
                    ),
                    "risks": list(observation["risks"]),
                    "sources": [observation],
                    "deduplication": {
                        "version": AGGREGATION_VERSION,
                        "rule": (
                            "same_region_type_nonconflicting_shade_code_"
                            "and_bbox_iou_gte_threshold"
                        ),
                        "iou_threshold": iou_threshold,
                        "merged_source_count": 1,
                    },
                }
            )
            continue
        match["sources"].append(observation)
        match["deduplication"]["merged_source_count"] = len(match["sources"])
        match["deduplication"].setdefault("merge_ious", []).append(match_iou)
        match["linked_text_item_ids"] = sorted(
            set(match["linked_text_item_ids"]) | set(linked_ids)
        )
        match["extraction_eligible"] = bool(
            match["extraction_eligible"]
            or observation["source_extraction_eligible"]
        )
        match["risks"] = sorted(
            set(match["risks"]) | set(observation["risks"])
        )
        candidate_association = observation.get("association_confidence")
        if candidate_association is not None and (
            match["association_confidence"] is None
            or float(candidate_association)
            > float(match["association_confidence"])
        ):
            match["association_confidence"] = float(candidate_association)
        if float(observation["confidence"]) > float(
            match["model_confidence"]
        ):
            match.update(
                {
                    "bbox_image": observation["bbox_image"],
                    "model_confidence": float(observation["confidence"]),
                    "shade_code_text": (
                        observation.get("shade_code_text")
                        or match.get("shade_code_text")
                    ),
                    "shade_name_text": (
                        observation.get("shade_name_text")
                        or match.get("shade_name_text")
                    ),
                    "visual_color_name": (
                        observation.get("visual_color_name")
                        or match.get("visual_color_name")
                    ),
                }
            )
    return canonical


def _aggregate_image(
    connection: sqlite3.Connection,
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    image_id: str,
    units: Sequence[QuickUnit],
) -> dict[str, Any]:
    models, statuses = _load_successful_models(
        connection,
        workspace,
        run_id=run_id,
        units=units,
    )
    unit_statuses = {
        unit.unit_id: statuses.get(unit.unit_id, unit.initial_status)
        for unit in units
    }
    successful_ids = sorted(
        unit_id
        for unit_id, status in unit_statuses.items()
        if status in ("succeeded", "cache_hit")
    )
    skipped_ids = sorted(
        unit_id
        for unit_id, status in unit_statuses.items()
        if status == "skipped"
    )
    failed_ids = sorted(
        unit_id
        for unit_id, status in unit_statuses.items()
        if status not in ("succeeded", "cache_hit", "skipped")
    )
    has_long_layout = any(unit.scope == "global_thumbnail" for unit in units)
    global_models = [
        item for item in models if item[0].scope == "global_thumbnail"
    ]
    image_models = [item for item in models if item[0].scope == "image"]
    tile_models = [item for item in models if item[0].scope == "tile"]
    fallback = False
    if has_long_layout and global_models:
        role_unit, role_model = max(
            global_models,
            key=lambda item: item[1].role_confidence,
        )
        aggregation_method = "global_role_with_local_tile_merge"
    elif has_long_layout and tile_models:
        role_unit, role_model = max(
            tile_models,
            key=lambda item: item[1].role_confidence,
        )
        aggregation_method = "highest_confidence_tile_role_fallback"
        fallback = True
    elif image_models:
        role_unit, role_model = max(
            image_models,
            key=lambda item: item[1].role_confidence,
        )
        aggregation_method = "single_image_result"
    else:
        role_unit = None
        role_model = None
        aggregation_method = "no_successful_model_unit"

    if role_model is None:
        image_status = "skipped" if skipped_ids and not failed_ids else "failed"
    elif failed_ids or fallback:
        image_status = "partial"
    else:
        image_status = "success"

    working_unit = next(
        (
            unit
            for unit in units
            if unit.working_path is not None and unit.working_path.is_file()
        ),
        None,
    )
    image_width = image_height = 0
    if working_unit and working_unit.working_path:
        with Image.open(working_unit.working_path) as working_image:
            image_width, image_height = working_image.size
    content_models = tile_models if has_long_layout else image_models
    text_observations: list[dict[str, Any]] = []
    region_observations: list[dict[str, Any]] = []
    coordinate_failures: list[dict[str, Any]] = []
    for unit, parsed in content_models:
        for item in parsed.text_items:
            bbox_image: list[float] | None = None
            if item.bbox_norm is not None:
                try:
                    bbox_image = _map_bbox_to_image(
                        item.bbox_norm,
                        unit,
                        image_width=image_width,
                        image_height=image_height,
                    )
                except ValueError as error:
                    coordinate_failures.append(
                        {
                            "unit_id": unit.unit_id,
                            "item_id": item.text_item_id,
                            "error": str(error),
                        }
                    )
                    continue
            text_observations.append(
                {
                    "unit_id": unit.unit_id,
                    "scope": unit.scope,
                    "tile_index": unit.unit_index,
                    "source_asset_id": unit.asset_id,
                    "model_text_item_id": item.text_item_id,
                    "raw_text": item.text,
                    "text_type": item.text_type,
                    "bbox_norm": list(item.bbox_norm)
                    if item.bbox_norm is not None
                    else None,
                    "bbox_image": bbox_image,
                    "confidence": item.confidence,
                }
            )
        for region in parsed.color_regions:
            try:
                bbox_image = _map_bbox_to_image(
                    region.bbox_norm,
                    unit,
                    image_width=image_width,
                    image_height=image_height,
                )
            except ValueError as error:
                coordinate_failures.append(
                    {
                        "unit_id": unit.unit_id,
                        "item_id": region.region_id,
                        "error": str(error),
                    }
                )
                continue
            region_observations.append(
                {
                    "unit_id": unit.unit_id,
                    "scope": unit.scope,
                    "tile_index": unit.unit_index,
                    "source_asset_id": unit.asset_id,
                    "model_region_id": region.region_id,
                    "region_type": region.region_type,
                    "bbox_norm": list(region.bbox_norm),
                    "bbox_image": bbox_image,
                    "shade_code_text": region.shade_code_text,
                    "shade_name_text": region.shade_name_text,
                    "visual_color_name": region.visual_color_name,
                    "confidence": region.confidence,
                    "risks": list(region.risks),
                    "linked_model_text_item_ids": list(
                        region.linked_text_item_ids
                    ),
                    "association_confidence": region.association_confidence,
                    "source_extraction_eligible": (
                        parsed.representative_color_eligible
                    ),
                }
            )
    quick = settings.section("quick_extract")
    text_items, text_id_map = merge_text_observations(
        text_observations,
        image_id=image_id,
        iou_threshold=float(quick["text_dedup_iou"]),
    )
    regions = merge_region_observations(
        region_observations,
        image_id=image_id,
        iou_threshold=float(quick["region_dedup_iou"]),
        text_id_map=text_id_map,
    )

    quality_risks = (
        sorted(set(role_model.quality_risks)) if role_model else []
    )
    if fallback:
        quality_risks.append("global_thumbnail_failed_tile_role_fallback")
    if failed_ids:
        quality_risks.append("partial_unit_failure")
    if coordinate_failures:
        quality_risks.append("coordinate_mapping_failure")
    quality_risks = sorted(set(quality_risks))
    extraction_id = stable_id("quick_image", run_id, image_id)
    now = utc_now()
    evidence = {
        "aggregation_version": AGGREGATION_VERSION,
        "role_source_unit_id": role_unit.unit_id if role_unit else None,
        "role_fallback": fallback,
        "unit_statuses": unit_statuses,
        "coordinate_failures": coordinate_failures,
        "working_asset_id": (
            working_unit.working_asset_id if working_unit else None
        ),
        "alpha_asset_id": (
            working_unit.alpha_asset_id if working_unit else None
        ),
    }
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
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, image_id) DO UPDATE SET
            status = excluded.status,
            primary_role = excluded.primary_role,
            secondary_roles_json = excluded.secondary_roles_json,
            role_confidence = excluded.role_confidence,
            layout_type = excluded.layout_type,
            layout_summary = excluded.layout_summary,
            representative_color_eligible =
                excluded.representative_color_eligible,
            eligibility_confidence = excluded.eligibility_confidence,
            eligibility_reasons_json = excluded.eligibility_reasons_json,
            summary = excluded.summary,
            quality_risks_json = excluded.quality_risks_json,
            aggregation_method = excluded.aggregation_method,
            successful_unit_ids_json = excluded.successful_unit_ids_json,
            failed_unit_ids_json = excluded.failed_unit_ids_json,
            skipped_unit_ids_json = excluded.skipped_unit_ids_json,
            evidence_json = excluded.evidence_json,
            schema_version = excluded.schema_version,
            updated_at = excluded.updated_at
        """,
        (
            extraction_id,
            run_id,
            image_id,
            OUTPUT_SEMANTICS,
            image_status,
            role_model.primary_role if role_model else None,
            canonical_json(role_model.secondary_roles if role_model else []),
            role_model.role_confidence if role_model else None,
            role_model.layout_type if role_model else None,
            role_model.layout_summary if role_model else None,
            (
                int(role_model.representative_color_eligible)
                if role_model
                else None
            ),
            role_model.eligibility_confidence if role_model else None,
            canonical_json(
                role_model.eligibility_reasons if role_model else []
            ),
            role_model.summary if role_model else None,
            canonical_json(quality_risks),
            aggregation_method,
            canonical_json(successful_ids),
            canonical_json(failed_ids),
            canonical_json(skipped_ids),
            canonical_json(evidence),
            QUICK_SCHEMA_VERSION,
            now,
            now,
        ),
    )
    connection.execute(
        "DELETE FROM quick_text_items WHERE quick_image_extraction_id = ?",
        (extraction_id,),
    )
    connection.execute(
        "DELETE FROM quick_color_regions WHERE quick_image_extraction_id = ?",
        (extraction_id,),
    )
    for item in text_items:
        connection.execute(
            """
            INSERT INTO quick_text_items(
                quick_text_item_id, quick_image_extraction_id, run_id,
                image_id, text_item_id, raw_text, normalized_text,
                text_type, bbox_image_json, confidence,
                source_observations_json, deduplication_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id(
                    "quick_text_row",
                    extraction_id,
                    item["text_item_id"],
                ),
                extraction_id,
                run_id,
                image_id,
                item["text_item_id"],
                item["raw_text"],
                item["normalized_text"],
                item["text_type"],
                (
                    canonical_json(item["bbox_image"])
                    if item["bbox_image"] is not None
                    else None
                ),
                item["confidence"],
                canonical_json(item["sources"]),
                canonical_json(
                    {
                        **item["deduplication"],
                        "text_normalization_version": (
                            TEXT_NORMALIZATION_VERSION
                        ),
                    }
                ),
                now,
            ),
        )
    color_statuses: dict[str, int] = {}
    for region in regions:
        if not region["extraction_eligible"]:
            color = {
                "status": "skipped_ineligible",
                "output_semantics": OUTPUT_SEMANTICS,
                "risks": ["model_marked_region_ineligible"],
                "diagnostics": {
                    "reason": (
                        "source model unit marked representative colour "
                        "ineligible"
                    )
                },
            }
        elif working_unit is None or working_unit.working_path is None:
            color = {
                "status": "failed",
                "output_semantics": OUTPUT_SEMANTICS,
                "risks": ["stage2_working_asset_unavailable"],
                "diagnostics": {
                    "reason": "no Stage 2 working asset for local crop"
                },
            }
        else:
            color = extract_observed_color(
                working_unit.working_path,
                bbox_image=region["bbox_image"],
                region_type=region["region_type"],
                alpha_path=working_unit.alpha_path,
                seed=int(quick["kmeans_seed"]),
                max_clusters=int(quick["kmeans_max_clusters"]),
                iterations=int(quick["kmeans_iterations"]),
                shrink_fraction=float(quick["bbox_inset_fraction"]),
                max_long_edge=int(quick["color_max_long_edge"]),
                minimum_valid_pixels=int(quick["color_min_valid_pixels"]),
                minimum_valid_ratio=float(quick["color_min_valid_ratio"]),
            )
        color_status = str(color["status"])
        color_statuses[color_status] = color_statuses.get(color_status, 0) + 1
        risks = sorted(
            set(region["risks"]) | set(color.get("risks", []))
        )
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stable_id(
                    "quick_region_row",
                    extraction_id,
                    region["region_id"],
                ),
                extraction_id,
                run_id,
                image_id,
                region["region_id"],
                region["region_type"],
                canonical_json(region["bbox_image"]),
                region["model_confidence"],
                region.get("shade_code_text"),
                region.get("shade_name_text"),
                region.get("visual_color_name"),
                canonical_json(region["linked_text_item_ids"]),
                region.get("association_confidence"),
                int(region["extraction_eligible"]),
                color_status,
                OUTPUT_SEMANTICS,
                color.get("hex"),
                (
                    canonical_json(color["rgb"])
                    if color.get("rgb") is not None
                    else None
                ),
                (
                    canonical_json(color["lab"])
                    if color.get("lab") is not None
                    else None
                ),
                color.get("valid_pixel_count"),
                color.get("valid_pixel_ratio"),
                color.get("cluster_proportion"),
                color.get("dispersion"),
                color.get("color_confidence"),
                canonical_json(risks),
                canonical_json(region["sources"]),
                canonical_json(region["deduplication"]),
                canonical_json(color.get("diagnostics", {})),
                now,
            ),
        )
    return {
        "image_id": image_id,
        "status": image_status,
        "successful_units": len(successful_ids),
        "failed_units": len(failed_ids),
        "skipped_units": len(skipped_ids),
        "text_items": len(text_items),
        "color_regions": len(regions),
        "color_statuses": color_statuses,
        "role_fallback": fallback,
    }


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
    placeholders = ",".join("?" for _ in image_ids)
    occurrences: list[dict[str, Any]] = []
    for row in connection.execute(
        f"""
        SELECT image_occurrence_id, image_id, root_alias, relative_path
        FROM image_occurrences
        WHERE image_id IN ({placeholders})
        ORDER BY image_occurrence_id
        """,
        list(image_ids),
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
                    actual_sha == row["image_id"] if actual_sha else False
                ),
            }
        )
    return {
        "schema_version": "selected-source-hashes-1.0",
        "dataset_snapshot_id": workspace.dataset_snapshot_id,
        "selected_image_count": len(image_ids),
        "occurrence_count": len(occurrences),
        "occurrences": occurrences,
    }


def run_quick_extraction(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    execute_online: bool,
    max_calls: int | None,
    resume: bool,
    image_id: str | None = None,
    selection_manifest: Path | None = None,
    folder_group_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Prepare, call/cache, merge, extract local colours, and persist."""

    if execute_online and max_calls is None:
        raise ValueError("--max-calls is required with --execute-online")
    if not execute_online and max_calls is not None:
        raise ValueError("--max-calls is only valid with --execute-online")
    if bool(settings.section("vlm")["enable_thinking"]):
        raise ValueError("structured quick extraction requires enable_thinking=false")
    recovery_summary: dict[str, Any] | None = None
    existing_run_dir = workspace.run_dir(run_id)
    if resume and existing_run_dir.is_dir():
        with open_database(
            workspace.database_path,
            readonly=True,
        ) as recovery_check:
            has_units = bool(
                recovery_check.execute(
                    """
                    SELECT 1 FROM quick_extraction_units
                    WHERE run_id = ? LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
            )
        if has_units:
            recovery_summary = recover_quick_run_artifacts(
                workspace,
                settings,
                run_id=run_id,
            )
    plan = plan_quick_extraction(
        workspace,
        settings,
        run_id=run_id,
        image_id=image_id,
        selection_manifest=selection_manifest,
        folder_group_id=folder_group_id,
        limit=limit,
    )
    run_dir = ensure_run_directory(workspace, run_id, resume=resume)
    with open_database(workspace.database_path) as connection:
        image_ids, selector = _select_image_ids(
            connection,
            settings,
            image_id=image_id,
            selection_manifest=selection_manifest,
            folder_group_id=folder_group_id,
            limit=limit,
        )
        begin_pipeline_run(
            connection,
            workspace,
            settings,
            run_id=run_id,
            stage="stage2.6",
            resume=resume,
            extra_config={"selection": selector},
        )
        units = _planned_units(
            connection,
            workspace,
            settings,
            run_id=run_id,
            image_ids=image_ids,
        )
        before_hashes = _source_hash_snapshot(
            connection,
            workspace,
            image_ids=image_ids,
        )
    _write_report(run_dir, "quick_extraction_plan", plan)
    _write_report(run_dir, "selected_source_hashes_before", before_hashes)

    try:
        with open_database(workspace.database_path) as connection:
            units = _prepare_units(
                connection,
                workspace,
                settings,
                run_id=run_id,
                units=units,
            )
            model = _model_name(settings)
            queued: list[QuickUnit] = []
            offsets: dict[str, int] = {}
            cache_hits = 0
            maximum_attempts = (
                int(settings.section("vlm")["max_retries"]) + 1
            )
            for unit in units:
                if unit.initial_status == "skipped":
                    continue
                existing = connection.execute(
                    """
                    SELECT unit.unit_status, model.parsed_response_path
                    FROM quick_extraction_units AS unit
                    LEFT JOIN model_runs AS model
                      ON model.model_run_id = unit.model_run_id
                    WHERE unit.quick_extraction_unit_id = ?
                    """,
                    (unit.unit_id,),
                ).fetchone()
                if (
                    existing is not None
                    and existing["unit_status"] in ("succeeded", "cache_hit")
                    and existing["parsed_response_path"]
                    and (
                        run_dir / existing["parsed_response_path"]
                    ).is_file()
                ):
                    continue
                cached = _cached_attempt(
                    connection,
                    settings,
                    workspace,
                    run_id=run_id,
                    unit=unit,
                    model=model,
                )
                if cached is not None:
                    manifest, result = cached
                    _record_attempt(
                        connection,
                        settings,
                        run_id=run_id,
                        manifest=manifest,
                        result=result,
                    )
                    _set_unit_result(
                        connection,
                        unit=unit,
                        result=result,
                        provider_attempt_count=0,
                    )
                    cache_hits += 1
                    continue
                manifest = _request_manifest(settings, unit, model)
                cache_key = quick_cache_key(manifest)
                offset = _attempt_offset(
                    connection,
                    run_id=run_id,
                    cache_key=cache_key,
                )
                connection.execute(
                    """
                    UPDATE quick_extraction_units
                    SET cache_key = ?, provider_attempt_count = ?,
                        unit_status = CASE
                            WHEN ? >= ? THEN 'failed' ELSE 'prepared'
                        END,
                        failure_json = CASE
                            WHEN ? >= ? THEN ?
                            ELSE '{}'
                        END,
                        updated_at = ?
                    WHERE quick_extraction_unit_id = ?
                    """,
                    (
                        cache_key,
                        offset,
                        offset,
                        maximum_attempts,
                        offset,
                        maximum_attempts,
                        canonical_json(
                            {
                                "code": "provider_attempts_exhausted",
                                "attempt_count": offset,
                            }
                        ),
                        utc_now(),
                        unit.unit_id,
                    ),
                )
                if offset < maximum_attempts and execute_online:
                    queued.append(unit)
                    offsets[unit.unit_id] = offset
            connection.commit()

            prior_provider_attempts = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM model_runs AS model
                    JOIN pipeline_runs AS pipeline
                      ON pipeline.run_id = model.run_id
                    WHERE pipeline.stage = 'stage2.6'
                      AND model.status IN (
                          'succeeded', 'schema_failed', 'request_failed'
                      )
                    """
                ).fetchone()[0]
            )

        hard_cap = int(
            settings.section("quick_extract")["online_validation_hard_cap"]
        )
        remaining_global_budget = max(0, hard_cap - prior_provider_attempts)
        requested_budget = int(max_calls or 0)
        effective_budget = min(requested_budget, remaining_global_budget)
        online_calls_made = 0
        if queued and execute_online and effective_budget > 0:
            vlm = settings.section("vlm")
            api_key = require_env(str(vlm["api_key_env"]))
            base_url = require_env(str(vlm["base_url_env"]))
            model = require_env(str(vlm["model_env"]))
            try:
                from openai import OpenAI
            except ImportError as error:  # pragma: no cover
                raise RuntimeError(
                    "OpenAI SDK is required for online quick extraction"
                ) from error
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=float(vlm["timeout_seconds"]),
                max_retries=0,
            )
            budget = ProviderCallBudget(effective_budget)
            worker_count = max(1, int(vlm["concurrency"]))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="quick-vlm",
            ) as pool:
                futures = [
                    pool.submit(
                        _execute_unit,
                        client=client,
                        api_key=api_key,
                        budget=budget,
                        workspace=workspace,
                        settings=settings,
                        run_id=run_id,
                        unit=unit,
                        model=model,
                        attempt_offset=offsets[unit.unit_id],
                    )
                    for unit in queued
                ]
                for future in as_completed(futures):
                    unit, manifest, attempts = future.result()
                    with open_database(
                        workspace.database_path
                    ) as result_connection:
                        for attempt in attempts:
                            _record_attempt(
                                result_connection,
                                settings,
                                run_id=run_id,
                                manifest=manifest,
                                result=attempt,
                            )
                        if not attempts:
                            continue
                        successful = next(
                            (
                                attempt
                                for attempt in reversed(attempts)
                                if attempt.parsed is not None
                            ),
                            None,
                        )
                        final = successful or attempts[-1]
                        attempt_count = int(
                            result_connection.execute(
                                """
                                SELECT COUNT(*) FROM model_runs
                                WHERE run_id = ? AND cache_key = ?
                                  AND status NOT IN (
                                      'cache_hit', 'budget_exhausted'
                                  )
                                """,
                                (run_id, final.cache_key),
                            ).fetchone()[0]
                        )
                        _set_unit_result(
                            result_connection,
                            unit=unit,
                            result=final,
                            provider_attempt_count=attempt_count,
                        )
                        result_connection.commit()
            online_calls_made = budget.used

        with open_database(workspace.database_path) as connection:
            image_summaries = [
                _aggregate_image(
                    connection,
                    workspace,
                    settings,
                    run_id=run_id,
                    image_id=candidate,
                    units=[
                        unit for unit in units if unit.image_id == candidate
                    ],
                )
                for candidate in image_ids
            ]
            connection.commit()
            after_hashes = _source_hash_snapshot(
                connection,
                workspace,
                image_ids=image_ids,
            )
            source_drift = (
                before_hashes["occurrences"] != after_hashes["occurrences"]
            )
            if source_drift:
                finish_pipeline_run(
                    connection,
                    run_id,
                    status="failed",
                    error_summary={"source_drift_detected": True},
                )
                raise RuntimeError(
                    "selected original image hashes changed during the run"
                )
            unit_statuses = {
                row["unit_status"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT unit_status, COUNT(*) AS count
                    FROM quick_extraction_units
                    WHERE run_id = ?
                    GROUP BY unit_status
                    """,
                    (run_id,),
                )
            }
            attempt_statuses = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM model_runs
                    WHERE run_id = ?
                    GROUP BY status
                    """,
                    (run_id,),
                )
            }
            token_totals: dict[str, int] = {}
            for row in connection.execute(
                "SELECT token_usage_json FROM model_runs WHERE run_id = ?",
                (run_id,),
            ):
                usage = json.loads(row[0])
                for key, value in usage.items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        token_totals[key] = token_totals.get(key, 0) + value
            pending_units = unit_statuses.get("prepared", 0)
            image_statuses: dict[str, int] = {}
            color_statuses: dict[str, int] = {}
            for summary in image_summaries:
                image_statuses[summary["status"]] = (
                    image_statuses.get(summary["status"], 0) + 1
                )
                for status, count in summary["color_statuses"].items():
                    color_statuses[status] = (
                        color_statuses.get(status, 0) + int(count)
                    )
            pipeline_status = (
                "prepared_cache_only"
                if pending_units and not execute_online
                else (
                    "completed_with_failures"
                    if image_statuses.get("failed", 0)
                    or image_statuses.get("partial", 0)
                    or unit_statuses.get("failed", 0)
                    or unit_statuses.get("budget_exhausted", 0)
                    else "completed"
                )
            )
            finish_pipeline_run(
                connection,
                run_id,
                status=pipeline_status,
                error_summary={
                    "unit_statuses": unit_statuses,
                    "image_statuses": image_statuses,
                },
            )
        _write_report(
            run_dir,
            "selected_source_hashes_after",
            after_hashes,
        )
        summary = {
            "schema_version": "quick-extraction-run-summary-1.0",
            "run_id": run_id,
            "status": pipeline_status,
            "selector": selector,
            "selected_image_count": len(image_ids),
            "unit_statuses": unit_statuses,
            "image_statuses": image_statuses,
            "color_statuses": color_statuses,
            "cache_hits_materialized": cache_hits,
            "online_calls_made": online_calls_made,
            "requested_max_calls": max_calls,
            "effective_max_calls": effective_budget,
            "online_validation_hard_cap": hard_cap,
            "provider_attempts_before_invocation": prior_provider_attempts,
            "provider_attempts_after_invocation": (
                prior_provider_attempts + online_calls_made
            ),
            "hard_cap_reached": (
                prior_provider_attempts + online_calls_made >= hard_cap
            ),
            "attempt_statuses": attempt_statuses,
            "token_totals": token_totals,
            "source_drift_detected": False,
            "artifact_recovery": recovery_summary,
            "output_semantics": OUTPUT_SEMANTICS,
            "image_summaries": image_summaries,
        }
        _write_report(run_dir, "quick_extraction_execution", summary)
        return summary
    except Exception as error:
        with open_database(workspace.database_path) as connection:
            finish_pipeline_run(
                connection,
                run_id,
                status="failed",
                error_summary={
                    "error_type": type(error).__name__,
                    "message": _redact_environment_secrets(str(error)),
                },
            )
        raise


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    temporary.replace(path)


def _csv_text(
    rows: Sequence[Mapping[str, Any]],
    fieldnames: Sequence[str],
) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(fieldnames),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_quick_extraction(
    workspace: Workspace,
    *,
    run_id: str,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Export canonical image, text, colour, and occurrence results."""

    destination = (
        output_dir.resolve()
        if output_dir is not None
        else workspace.run_dir(run_id) / "exports"
    )
    destination.mkdir(parents=True, exist_ok=True)
    with open_database(workspace.database_path, readonly=True) as connection:
        images = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM quick_image_extractions
                WHERE run_id = ?
                ORDER BY image_id
                """,
                (run_id,),
            )
        ]
        if not images:
            raise KeyError(f"run has no quick extraction results: {run_id}")
        texts = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM quick_text_items
                WHERE run_id = ?
                ORDER BY image_id, text_item_id
                """,
                (run_id,),
            )
        ]
        regions = [
            dict(row)
            for row in connection.execute(
                """
                SELECT * FROM quick_color_regions
                WHERE run_id = ?
                ORDER BY image_id, region_id
                """,
                (run_id,),
            )
        ]
        image_ids = [str(item["image_id"]) for item in images]
        placeholders = ",".join("?" for _ in image_ids)
        occurrence_rows = list(
            connection.execute(
                f"""
                SELECT occurrence.image_occurrence_id,
                       occurrence.image_id,
                       occurrence.folder_group_id,
                       occurrence.root_alias,
                       occurrence.relative_path,
                       occurrence.filename,
                       occurrence.extension_mismatch,
                       ref.source_ref_id,
                       source.source_record_id
                FROM image_occurrences AS occurrence
                LEFT JOIN source_ref_occurrences AS link
                  ON link.image_occurrence_id =
                     occurrence.image_occurrence_id
                LEFT JOIN source_image_refs AS ref
                  ON ref.source_ref_id = link.source_ref_id
                LEFT JOIN source_records AS source
                  ON source.source_record_id = ref.source_record_id
                WHERE occurrence.image_id IN ({placeholders})
                ORDER BY occurrence.image_id,
                         occurrence.image_occurrence_id,
                         ref.source_ref_id
                """,
                image_ids,
            )
        )
    text_by_image: dict[str, list[dict[str, Any]]] = {}
    for item in texts:
        text_by_image.setdefault(str(item["image_id"]), []).append(item)
    regions_by_image: dict[str, list[dict[str, Any]]] = {}
    for item in regions:
        regions_by_image.setdefault(str(item["image_id"]), []).append(item)
    json_fields = {
        "secondary_roles_json",
        "eligibility_reasons_json",
        "quality_risks_json",
        "successful_unit_ids_json",
        "failed_unit_ids_json",
        "skipped_unit_ids_json",
        "evidence_json",
        "bbox_image_json",
        "source_observations_json",
        "deduplication_json",
        "linked_text_item_ids_json",
        "rgb_json",
        "lab_json",
        "risks_json",
        "algorithm_diagnostics_json",
    }

    def decoded(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in json_fields & set(result):
            if result[key] is not None:
                result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result

    image_lines: list[str] = []
    for image in images:
        payload = decoded(image)
        image_key = str(image["image_id"])
        payload["text_items"] = [
            decoded(item) for item in text_by_image.get(image_key, [])
        ]
        payload["color_regions"] = [
            decoded(item) for item in regions_by_image.get(image_key, [])
        ]
        image_lines.append(canonical_json(payload))
    image_path = destination / "image_results.jsonl"
    _atomic_text(
        image_path,
        "\n".join(image_lines) + ("\n" if image_lines else ""),
    )

    text_fields = [
        "run_id",
        "image_id",
        "text_item_id",
        "raw_text",
        "normalized_text",
        "text_type",
        "bbox_image_json",
        "confidence",
        "source_observations_json",
        "deduplication_json",
    ]
    text_path = destination / "text_items.csv"
    _atomic_text(text_path, _csv_text(texts, text_fields))
    region_fields = [
        "run_id",
        "image_id",
        "region_id",
        "region_type",
        "bbox_image_json",
        "model_confidence",
        "shade_code_text",
        "shade_name_text",
        "visual_color_name",
        "linked_text_item_ids_json",
        "association_confidence",
        "extraction_eligible",
        "extraction_status",
        "output_semantics",
        "color_hex",
        "rgb_json",
        "lab_json",
        "valid_pixel_count",
        "valid_pixel_ratio",
        "cluster_proportion",
        "dispersion",
        "color_confidence",
        "risks_json",
        "source_observations_json",
        "deduplication_json",
        "algorithm_diagnostics_json",
    ]
    region_path = destination / "color_regions.csv"
    _atomic_text(region_path, _csv_text(regions, region_fields))

    occurrence_groups: dict[str, dict[str, Any]] = {}
    image_status_lookup = {
        str(item["image_id"]): item for item in images
    }
    for row in occurrence_rows:
        occurrence_id = str(row["image_occurrence_id"])
        group = occurrence_groups.setdefault(
            occurrence_id,
            {
                "run_id": run_id,
                "image_occurrence_id": occurrence_id,
                "image_id": row["image_id"],
                "folder_group_id": row["folder_group_id"],
                "root_alias": row["root_alias"],
                "relative_path": row["relative_path"],
                "filename": row["filename"],
                "extension_mismatch": row["extension_mismatch"],
                "source_ref_ids": set(),
                "source_record_ids": set(),
            },
        )
        if row["source_ref_id"]:
            group["source_ref_ids"].add(str(row["source_ref_id"]))
        if row["source_record_id"]:
            group["source_record_ids"].add(str(row["source_record_id"]))
    occurrence_exports: list[dict[str, Any]] = []
    for occurrence_id in sorted(occurrence_groups):
        item = occurrence_groups[occurrence_id]
        image_result = image_status_lookup[str(item["image_id"])]
        item["source_ref_ids_json"] = canonical_json(
            sorted(item.pop("source_ref_ids"))
        )
        item["source_record_ids_json"] = canonical_json(
            sorted(item.pop("source_record_ids"))
        )
        item["extraction_status"] = image_result["status"]
        item["primary_role"] = image_result["primary_role"]
        item["representative_color_eligible"] = image_result[
            "representative_color_eligible"
        ]
        item["output_semantics"] = image_result["output_semantics"]
        occurrence_exports.append(item)
    occurrence_fields = [
        "run_id",
        "image_occurrence_id",
        "image_id",
        "folder_group_id",
        "root_alias",
        "relative_path",
        "filename",
        "extension_mismatch",
        "source_ref_ids_json",
        "source_record_ids_json",
        "extraction_status",
        "primary_role",
        "representative_color_eligible",
        "output_semantics",
    ]
    occurrence_path = destination / "occurrence_results.csv"
    _atomic_text(
        occurrence_path,
        _csv_text(occurrence_exports, occurrence_fields),
    )
    return {
        "schema_version": "quick-extraction-export-summary-1.0",
        "run_id": run_id,
        "output_dir": destination,
        "files": {
            "image_results": image_path,
            "text_items": text_path,
            "color_regions": region_path,
            "occurrence_results": occurrence_path,
        },
        "counts": {
            "images": len(images),
            "text_items": len(texts),
            "color_regions": len(regions),
            "occurrences": len(occurrence_exports),
        },
    }
