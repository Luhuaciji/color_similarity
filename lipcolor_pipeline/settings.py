"""Versioned configuration loading for stages 1.5 through 2.6."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import load_env_file


DEFAULT_CONFIG_RELATIVE_PATH = Path("configs/pipeline.yaml")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


DEFAULTS: dict[str, Any] = {
    "project": {
        "stage1_database": "stage1_output/runs/stage1_full_20260728/manifest.sqlite",
        "raw_root": "downloaded_images",
        "legacy_output_root": "image_preprocessing_output",
        "output_root": "pipeline_output",
    },
    "vlm": {
        "provider": "dashscope_openai_compatible",
        "model_env": "DASHSCOPE_MODEL",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "api_key_env": "DASHSCOPE_API_KEY",
        "prompt_name": "image_role",
        "prompt_version": "1.0.2",
        "schema_version": "content_visual_analysis-1.0",
        "timeout_seconds": 120,
        "max_retries": 3,
        "concurrency": 4,
        "enable_thinking": False,
        "temperature": 0.0,
    },
    "preprocessing": {
        "implementation_version": "2.0.0",
        "ordinary_max_long_edge": 2048,
        "analysis_jpeg_quality": 92,
        "long_aspect_ratio": 4.0,
        "long_min_edge": 3000,
        "long_min_short_edge": 64,
        "global_thumbnail_max_long_edge": 4096,
        "tile_long_axis": 2048,
        "tile_overlap": 512,
        "semantic_invalid_short_edge": 16,
        "semantic_invalid_min_pixels": 1024,
        "hard_max_pixels": 100_000_000,
        "selected_frame": 0,
        "allow_truncated_recovery": False,
    },
    "pilot": {
        "initial_unique_images": 64,
        "maximum_unique_images": 100,
        "selection_seed": "stage1-5-pilot-v1",
    },
    "annotation": {
        "accepted_unique_images": 480,
        "target_per_primary_role": 60,
        "mask_subset": 160,
        "multi_shade_subset": 80,
        "blind_review_subset": 96,
        "split_seed": "stage2-5-split-v1",
    },
    "quick_extract": {
        "implementation_version": "1.0.0",
        "prompt_name": "quick_extract",
        "prompt_version": "1.0.0",
        "schema_version": "quick-image-extraction-1.0",
        "stage2_run_id": "stage2_full_20260728",
        "ordinary_preview_max_long_edge": 2048,
        "ordinary_preview_jpeg_quality": 92,
        "bbox_inset_fraction": 0.03,
        "color_max_long_edge": 256,
        "color_min_valid_pixels": 300,
        "color_min_valid_ratio": 0.05,
        "kmeans_seed": 260,
        "kmeans_max_clusters": 3,
        "kmeans_iterations": 15,
        "text_dedup_iou": 0.5,
        "region_dedup_iou": 0.6,
        "online_validation_hard_cap": 100,
    },
    "thresholds": {
        "status": "provisional_target",
        "threshold_version": "pilot-and-annotation-draft-v1",
        "phash_high_confidence_distance": 4,
        "phash_possible_duplicate_distance": 8,
        "classification_low_confidence": 0.70,
    },
}


@dataclass(frozen=True)
class PipelineSettings:
    repo_root: Path
    config_path: Path
    values: dict[str, Any]
    config_hash: str

    def section(self, name: str) -> dict[str, Any]:
        value = self.values.get(name)
        if not isinstance(value, dict):
            raise KeyError(f"missing configuration section: {name}")
        return value

    def project_path(self, key: str) -> Path:
        raw = Path(str(self.section("project")[key]))
        return raw if raw.is_absolute() else (self.repo_root / raw).resolve()


def load_settings(
    repo_root: Path,
    config_path: Path | None = None,
    *,
    load_dotenv: bool = True,
) -> PipelineSettings:
    repo_root = repo_root.resolve()
    resolved_config = (
        config_path.resolve()
        if config_path is not None
        else (repo_root / DEFAULT_CONFIG_RELATIVE_PATH).resolve()
    )
    if load_dotenv:
        load_env_file(repo_root / ".env")

    values = dict(DEFAULTS)
    if resolved_config.exists():
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - dependency preflight.
            raise RuntimeError(
                "PyYAML is required to read configs/pipeline.yaml"
            ) from exc
        payload = yaml.safe_load(resolved_config.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, Mapping):
            raise ValueError("pipeline configuration must be a mapping")
        values = _deep_merge(DEFAULTS, payload)

    _validate(values)
    return PipelineSettings(
        repo_root=repo_root,
        config_path=resolved_config,
        values=values,
        config_hash=sha256_json(values),
    )


def _validate(values: Mapping[str, Any]) -> None:
    preprocessing = values["preprocessing"]
    tile_size = int(preprocessing["tile_long_axis"])
    overlap = int(preprocessing["tile_overlap"])
    if tile_size <= 0 or overlap < 0 or overlap >= tile_size:
        raise ValueError("tile_overlap must satisfy 0 <= overlap < tile_long_axis")
    initial = int(values["pilot"]["initial_unique_images"])
    maximum = int(values["pilot"]["maximum_unique_images"])
    if not (50 <= initial <= maximum <= 100):
        raise ValueError("Pilot image counts must satisfy 50 <= initial <= max <= 100")
    if values["thresholds"].get("status") != "provisional_target":
        raise ValueError("unreviewed thresholds must remain provisional_target")
