"""Strict image-only VLM response schema."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RoleCode = Literal[
    "single_bullet",
    "single_swatch",
    "lip_effect",
    "multi_shade_comparison",
    "color_card",
    "packaging",
    "text_promo",
    "invalid",
]

AnalysisScope = Literal[
    "image",
    "global_thumbnail",
    "tile",
    "merged_content_summary",
]

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class CandidateColorRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region_type: Literal[
        "bullet",
        "swatch",
        "lip",
        "color_block",
        "product_fill",
        "other",
    ]
    bbox_norm: tuple[float, float, float, float]
    confidence: Confidence
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bbox(self) -> "CandidateColorRegion":
        x_min, y_min, x_max, y_max = self.bbox_norm
        if not all(0.0 <= value <= 1.0 for value in self.bbox_norm):
            raise ValueError("bbox_norm values must be within [0, 1]")
        if not x_min < x_max or not y_min < y_max:
            raise ValueError("bbox_norm must have positive width and height")
        return self


class ContentVisualAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["content_visual_analysis-1.0"]
    analysis_scope: AnalysisScope
    input_context_policy: Literal["image_only"]
    primary_role: RoleCode
    secondary_roles: list[RoleCode] = Field(default_factory=list)
    layout_type: Literal[
        "single_panel",
        "collage",
        "grid",
        "long_detail_strip",
        "decorative_strip",
        "unknown",
    ]
    global_layout: dict = Field(default_factory=dict)
    role_confidence: Confidence
    contains_text: bool
    contains_multiple_shades: bool
    contains_lips: bool
    contains_skin_swatch: bool
    contains_product_bullet: bool
    contains_packaging: bool
    depicted_shades: list[str] = Field(default_factory=list)
    representative_color_eligible: bool
    eligibility_score: Confidence
    recommended_strategy: Literal[
        "single_bullet_segmentation",
        "single_swatch_segmentation",
        "lip_segmentation",
        "multi_region_matching",
        "color_card_detection",
        "information_only",
        "none",
        "manual_review",
    ]
    rejection_reasons: list[str] = Field(default_factory=list)
    candidate_color_regions: list[CandidateColorRegion] = Field(
        default_factory=list
    )
    observed_objects: list[str] = Field(default_factory=list)
    quality_risks: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_roles(self) -> "ContentVisualAnalysis":
        if self.primary_role in self.secondary_roles:
            raise ValueError("primary_role must not repeat in secondary_roles")
        if len(set(self.secondary_roles)) != len(self.secondary_roles):
            raise ValueError("secondary_roles must be unique")
        return self


_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def strip_code_fence(value: str) -> str:
    match = _FENCE.match(value)
    return match.group(1) if match else value.strip()


def parse_content_visual_analysis(value: str) -> ContentVisualAnalysis:
    payload = json.loads(strip_code_fence(value))
    return ContentVisualAnalysis.model_validate(payload)


def parse_with_deterministic_repair(
    value: str,
    *,
    image_width: int | None,
    image_height: int | None,
) -> tuple[ContentVisualAnalysis, tuple[str, ...]]:
    """Apply only auditable, lossless-enough schema normalizations."""

    payload = json.loads(strip_code_fence(value))
    if not isinstance(payload, dict):
        raise ValueError("VLM response must be a JSON object")
    repaired: dict[str, Any] = dict(payload)
    actions: list[str] = []

    strategy = repaired.get("recommended_strategy")
    if strategy == "single_region_matching":
        role_strategy = {
            "single_bullet": "single_bullet_segmentation",
            "single_swatch": "single_swatch_segmentation",
            "lip_effect": "lip_segmentation",
        }
        repaired["recommended_strategy"] = role_strategy.get(
            repaired.get("primary_role"),
            "multi_region_matching",
        )
        actions.append("mapped_single_region_matching_strategy")

    regions = repaired.get("candidate_color_regions")
    if isinstance(regions, list):
        repaired_regions: list[Any] = []
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                repaired_regions.append(region)
                continue
            candidate = dict(region)
            bbox = candidate.get("bbox_norm")
            if (
                isinstance(bbox, (list, tuple))
                and len(bbox) == 4
                and image_width
                and image_height
            ):
                try:
                    x0, y0, x1, y1 = (float(item) for item in bbox)
                except (TypeError, ValueError):
                    pass
                else:
                    is_pixel_bbox = (
                        any(value > 1.0 for value in (x0, y0, x1, y1))
                        and 0 <= x0 < x1 <= image_width
                        and 0 <= y0 < y1 <= image_height
                    )
                    if is_pixel_bbox:
                        candidate["bbox_norm"] = [
                            x0 / image_width,
                            y0 / image_height,
                            x1 / image_width,
                            y1 / image_height,
                        ]
                        actions.append(
                            f"normalized_pixel_bbox_to_unit_interval:{index}"
                        )
                    elif (
                        0 <= x0 < x1 <= 1000
                        and 0 <= y0 < y1 <= 1000
                        and (
                            x1 > image_width
                            or y1 > image_height
                        )
                    ):
                        # Qwen visual grounding may use a documented-style
                        # 0..1000 canvas even when a field is named bbox_norm.
                        # Apply this only when pixel coordinates are impossible
                        # for the actual asset, avoiding an ambiguous rewrite.
                        candidate["bbox_norm"] = [
                            x0 / 1000,
                            y0 / 1000,
                            x1 / 1000,
                            y1 / 1000,
                        ]
                        actions.append(
                            f"normalized_millesimal_bbox_to_unit_interval:{index}"
                        )
            repaired_regions.append(candidate)
        repaired["candidate_color_regions"] = repaired_regions

    if not actions:
        raise ValueError("no approved deterministic schema repair applies")
    return ContentVisualAnalysis.model_validate(repaired), tuple(actions)
