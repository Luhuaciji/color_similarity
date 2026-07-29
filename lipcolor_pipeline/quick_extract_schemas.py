"""Strict image-only response schema for Stage 2.6 quick extraction."""

from __future__ import annotations

import json
import math
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
QuickScope = Literal["image", "global_thumbnail", "tile"]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
BBox = tuple[float, float, float, float]


def _validate_bbox(value: BBox) -> BBox:
    if not all(math.isfinite(item) for item in value):
        raise ValueError("bbox_norm values must be finite")
    x0, y0, x1, y1 = value
    if not all(0.0 <= item <= 1.0 for item in value):
        raise ValueError("bbox_norm values must be within [0, 1]")
    if not (x0 < x1 and y0 < y1):
        raise ValueError("bbox_norm must have positive area")
    return value


class QuickTextItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text_item_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=1000)
    text_type: Literal[
        "shade_code",
        "shade_name",
        "brand",
        "product_name",
        "claim",
        "price",
        "instruction",
        "other",
    ]
    bbox_norm: BBox | None = None
    confidence: Confidence

    @model_validator(mode="after")
    def validate_optional_bbox(self) -> "QuickTextItem":
        if self.bbox_norm is not None:
            _validate_bbox(self.bbox_norm)
        return self


class QuickColorRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region_id: str = Field(min_length=1, max_length=128)
    region_type: Literal[
        "bullet",
        "swatch",
        "lip",
        "color_block",
        "product_fill",
        "other",
    ]
    bbox_norm: BBox
    shade_code_text: str | None = Field(default=None, max_length=300)
    shade_name_text: str | None = Field(default=None, max_length=300)
    visual_color_name: str | None = Field(default=None, max_length=300)
    confidence: Confidence
    risks: list[str] = Field(default_factory=list, max_length=20)
    linked_text_item_ids: list[str] = Field(default_factory=list, max_length=20)
    association_confidence: Confidence | None = None

    @model_validator(mode="after")
    def validate_region(self) -> "QuickColorRegion":
        _validate_bbox(self.bbox_norm)
        if len(set(self.linked_text_item_ids)) != len(
            self.linked_text_item_ids
        ):
            raise ValueError("linked_text_item_ids must be unique")
        if self.linked_text_item_ids and self.association_confidence is None:
            raise ValueError(
                "association_confidence is required for linked text"
            )
        if not self.linked_text_item_ids and self.association_confidence is not None:
            raise ValueError(
                "association_confidence requires at least one linked text item"
            )
        return self


class QuickImageExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["quick-image-extraction-1.0"]
    scope: QuickScope
    input_context_policy: Literal["image_only"]
    primary_role: RoleCode
    secondary_roles: list[RoleCode] = Field(default_factory=list, max_length=7)
    role_confidence: Confidence
    layout_type: Literal[
        "single_panel",
        "collage",
        "grid",
        "long_detail_strip",
        "decorative_strip",
        "unknown",
    ]
    layout_summary: str = Field(max_length=1000)
    representative_color_eligible: bool
    eligibility_confidence: Confidence
    eligibility_reasons: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(max_length=2000)
    quality_risks: list[str] = Field(default_factory=list, max_length=20)
    text_items: list[QuickTextItem] = Field(default_factory=list, max_length=20)
    color_regions: list[QuickColorRegion] = Field(
        default_factory=list,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_contract(self) -> "QuickImageExtraction":
        if self.primary_role in self.secondary_roles:
            raise ValueError("primary_role must not repeat in secondary_roles")
        if len(set(self.secondary_roles)) != len(self.secondary_roles):
            raise ValueError("secondary_roles must be unique")
        text_ids = [item.text_item_id for item in self.text_items]
        if len(set(text_ids)) != len(text_ids):
            raise ValueError("text_item_id values must be unique")
        region_ids = [item.region_id for item in self.color_regions]
        if len(set(region_ids)) != len(region_ids):
            raise ValueError("region_id values must be unique")
        known_text_ids = set(text_ids)
        unknown_links = sorted(
            {
                text_id
                for region in self.color_regions
                for text_id in region.linked_text_item_ids
                if text_id not in known_text_ids
            }
        )
        if unknown_links:
            raise ValueError(
                f"color regions reference unknown text IDs: {unknown_links}"
            )
        if self.scope == "global_thumbnail" and (
            self.text_items or self.color_regions
        ):
            raise ValueError(
                "global_thumbnail must not contain text_items or color_regions"
            )
        return self


_FENCE = re.compile(
    r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
    flags=re.IGNORECASE | re.DOTALL,
)


def strip_code_fence(value: str) -> str:
    match = _FENCE.match(value)
    return match.group(1) if match else value.strip()


def _normalize_bbox(
    value: Any,
    *,
    width: int,
    height: int,
) -> tuple[list[float] | Any, str | None]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return value, None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return value, None
    if not (0 <= x0 < x1 and 0 <= y0 < y1):
        return value, None
    if all(item <= 1.0 for item in (x0, y0, x1, y1)):
        return [x0, y0, x1, y1], None
    if x1 <= width and y1 <= height:
        return [x0 / width, y0 / height, x1 / width, y1 / height], "pixels"
    if x1 <= 1000 and y1 <= 1000 and (x1 > width or y1 > height):
        return [x0 / 1000, y0 / 1000, x1 / 1000, y1 / 1000], "millesimal"
    return value, None


def parse_quick_image_extraction(
    value: str,
    *,
    expected_scope: QuickScope,
    image_width: int,
    image_height: int,
) -> tuple[QuickImageExtraction, tuple[str, ...]]:
    """Parse strictly, allowing only fence removal and bbox normalization."""

    payload = json.loads(strip_code_fence(value))
    try:
        parsed = QuickImageExtraction.model_validate(payload)
        actions: tuple[str, ...] = ()
    except Exception as direct_error:
        if not isinstance(payload, dict):
            raise direct_error
        repaired = dict(payload)
        actions_list: list[str] = []
        for collection_name in ("text_items", "color_regions"):
            collection = repaired.get(collection_name)
            if not isinstance(collection, list):
                continue
            repaired_collection: list[Any] = []
            for index, item in enumerate(collection):
                if not isinstance(item, dict) or item.get("bbox_norm") is None:
                    repaired_collection.append(item)
                    continue
                candidate = dict(item)
                normalized, convention = _normalize_bbox(
                    candidate["bbox_norm"],
                    width=image_width,
                    height=image_height,
                )
                candidate["bbox_norm"] = normalized
                if convention:
                    actions_list.append(
                        f"{collection_name}[{index}]:{convention}_to_normalized"
                    )
                repaired_collection.append(candidate)
            repaired[collection_name] = repaired_collection
        if not actions_list:
            raise direct_error
        parsed = QuickImageExtraction.model_validate(repaired)
        actions = tuple(actions_list)
    if parsed.scope != expected_scope:
        raise ValueError(
            f"response scope {parsed.scope!r} does not match "
            f"request scope {expected_scope!r}"
        )
    return parsed, actions
