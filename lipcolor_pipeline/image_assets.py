"""Read-only image inspection and versioned analysis derivatives."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageFile, ImageOps

from image_preprocessing_pipeline.preprocess_product_images import (
    composite_for_working_image,
    convert_color_managed_rgb,
    extract_alpha,
    inspect_icc_profile,
)

from .settings import canonical_json, sha256_json
from .stage1_manifest import detect_image_format, sha256_file, stable_id
from .workspace import Workspace, utc_now


MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "TIFF": "image/tiff",
    "BMP": "image/bmp",
}
VLM_MIN_EDGE_PX = 11
VLM_TRANSPORT_VERSION = "1.0.0"
COORDINATE_TRANSFORM_VERSION = "exif-coordinate-transform-1.0"
CROSS_TILE_TEXT_DEDUP_VERSION = "cross-tile-text-dedup-1.0"


class ImagePolicyRejected(RuntimeError):
    """Raised when an image violates a deterministic safety policy."""


@dataclass(frozen=True)
class ImageInspection:
    source_format: str
    mime_type: str
    source_mode: str
    width: int
    height: int
    oriented_width: int
    oriented_height: int
    frame_count: int
    selected_frame: int
    exif_orientation: int
    orientation_corrected: bool
    icc_status: str
    working_color_space: str
    converted_to_srgb: bool
    has_alpha: bool
    transparent_pixel_ratio: float
    is_long: bool
    is_extreme_aspect_ratio: bool
    is_semantic_invalid_candidate: bool
    reading_axis: str
    format_mismatch: bool


@dataclass(frozen=True)
class PreparedAsset:
    derived_asset_id: str
    image_id: str
    asset_type: str
    path: Path
    relative_path: str
    sha256: str
    width: int
    height: int
    format: str
    transform_name: str
    transform_version: str
    transform_fingerprint: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PreparedContent:
    image_id: str
    inspection: ImageInspection
    analysis_assets: tuple[PreparedAsset, ...]
    long_image_layout_id: str | None


def _actual_format(path: Path) -> str:
    with path.open("rb") as handle:
        return detect_image_format(handle.read(32))


def _orientation_value(image: Image.Image) -> int:
    try:
        return int(image.getexif().get(274, 1))
    except (AttributeError, TypeError, ValueError):
        return 1


def exif_coordinate_transforms(
    width: int,
    height: int,
    orientation: int,
) -> dict[str, Any]:
    """Return explicit pixel-edge transforms before and after EXIF transpose."""

    if width <= 0 or height <= 0:
        raise ValueError("encoded image dimensions must be positive")
    matrices: dict[int, tuple[list[list[float]], list[list[float]]]] = {
        1: (
            [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        ),
        2: (
            [[-1, 0, width], [0, 1, 0], [0, 0, 1]],
            [[-1, 0, width], [0, 1, 0], [0, 0, 1]],
        ),
        3: (
            [[-1, 0, width], [0, -1, height], [0, 0, 1]],
            [[-1, 0, width], [0, -1, height], [0, 0, 1]],
        ),
        4: (
            [[1, 0, 0], [0, -1, height], [0, 0, 1]],
            [[1, 0, 0], [0, -1, height], [0, 0, 1]],
        ),
        5: (
            [[0, 1, 0], [1, 0, 0], [0, 0, 1]],
            [[0, 1, 0], [1, 0, 0], [0, 0, 1]],
        ),
        6: (
            [[0, -1, height], [1, 0, 0], [0, 0, 1]],
            [[0, 1, 0], [-1, 0, height], [0, 0, 1]],
        ),
        7: (
            [[0, -1, height], [-1, 0, width], [0, 0, 1]],
            [[0, -1, width], [-1, 0, height], [0, 0, 1]],
        ),
        8: (
            [[0, 1, 0], [-1, 0, width], [0, 0, 1]],
            [[0, -1, width], [1, 0, 0], [0, 0, 1]],
        ),
    }
    encoded_to_oriented, oriented_to_encoded = matrices.get(
        orientation,
        matrices[1],
    )
    oriented_size = (
        [height, width] if orientation in {5, 6, 7, 8} else [width, height]
    )
    return {
        "version": COORDINATE_TRANSFORM_VERSION,
        "coordinate_convention": (
            "continuous pixel-edge coordinates; boxes are half-open"
        ),
        "encoded_size": [width, height],
        "oriented_size": oriented_size,
        "encoded_to_oriented_matrix_3x3": encoded_to_oriented,
        "oriented_to_encoded_matrix_3x3": oriented_to_encoded,
    }


def apply_affine_point(
    point: Sequence[float],
    matrix: Sequence[Sequence[float]],
) -> tuple[float, float]:
    if len(point) != 2 or len(matrix) != 3 or any(
        len(row) != 3 for row in matrix
    ):
        raise ValueError("point and matrix dimensions are invalid")
    x, y = (float(value) for value in point)
    transformed = [
        sum(float(matrix[row][column]) * value for column, value in enumerate((x, y, 1.0)))
        for row in range(3)
    ]
    if transformed[2] == 0:
        raise ValueError("affine transform produced a zero homogeneous coordinate")
    return transformed[0] / transformed[2], transformed[1] / transformed[2]


def load_oriented_working_image(
    path: Path,
    config: Mapping[str, Any],
) -> tuple[Image.Image, Image.Image | None, ImageInspection]:
    """Strictly decode *path* and return an oriented sRGB-like RGB working image."""

    actual_format = _actual_format(path)
    hard_max_pixels = int(config["hard_max_pixels"])
    selected_frame = int(config["selected_frame"])
    previous_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                source_format = (opened.format or actual_format or "UNKNOWN").upper()
                source_mode = opened.mode
                width, height = opened.size
                pixels = width * height
                if pixels >= hard_max_pixels:
                    raise ImagePolicyRejected(
                        f"pixel_count={pixels} reaches hard_max_pixels={hard_max_pixels}"
                    )
                frame_count = int(getattr(opened, "n_frames", 1))
                if not (0 <= selected_frame < frame_count):
                    raise IndexError(
                        f"selected_frame={selected_frame} outside frame_count={frame_count}"
                    )
                if frame_count > 1:
                    opened.seek(selected_frame)
                orientation = _orientation_value(opened)
                icc_bytes = opened.info.get("icc_profile")
                opened.load()
                oriented = ImageOps.exif_transpose(opened).copy()

        profile_info, _ = inspect_icc_profile(icc_bytes)
        alpha = extract_alpha(oriented)
        rgb, converted, review_required, _note = convert_color_managed_rgb(
            oriented,
            icc_bytes,
            profile_info,
            rendering_intent="perceptual",
        )
        working = composite_for_working_image(rgb, alpha, "white")
        if working.mode != "RGB":
            working = working.convert("RGB")

        transparent_ratio = 0.0
        if alpha is not None:
            extrema = alpha.histogram()
            total = max(1, alpha.width * alpha.height)
            transparent_ratio = extrema[0] / total

        oriented_width, oriented_height = working.size
        short_edge = min(oriented_width, oriented_height)
        long_edge = max(oriented_width, oriented_height)
        aspect_ratio = long_edge / max(short_edge, 1)
        semantic_invalid = (
            short_edge < int(config["semantic_invalid_short_edge"])
            or oriented_width * oriented_height
            < int(config["semantic_invalid_min_pixels"])
        )
        is_long = (
            not semantic_invalid
            and aspect_ratio >= float(config["long_aspect_ratio"])
            and long_edge >= int(config["long_min_edge"])
            and short_edge >= int(config["long_min_short_edge"])
        )
        extension_format = {
            ".jpg": "JPEG",
            ".jpeg": "JPEG",
            ".png": "PNG",
            ".gif": "GIF",
            ".webp": "WEBP",
            ".tif": "TIFF",
            ".tiff": "TIFF",
            ".bmp": "BMP",
        }.get(path.suffix.casefold(), "UNKNOWN")
        status = str(profile_info["status"])
        if status == "profile_missing":
            working_color_space = "assumed_sRGB"
        elif status == "profile_invalid" or review_required:
            working_color_space = "assumed_sRGB_low_confidence"
        else:
            working_color_space = "sRGB"
        inspection = ImageInspection(
            source_format=source_format,
            mime_type=MIME_BY_FORMAT.get(source_format, "application/octet-stream"),
            source_mode=source_mode,
            width=width,
            height=height,
            oriented_width=oriented_width,
            oriented_height=oriented_height,
            frame_count=frame_count,
            selected_frame=selected_frame,
            exif_orientation=orientation,
            orientation_corrected=orientation in {2, 3, 4, 5, 6, 7, 8},
            icc_status=status,
            working_color_space=working_color_space,
            converted_to_srgb=bool(converted),
            has_alpha=alpha is not None,
            transparent_pixel_ratio=round(transparent_ratio, 8),
            is_long=is_long,
            is_extreme_aspect_ratio=aspect_ratio >= 10.0,
            is_semantic_invalid_candidate=semantic_invalid,
            reading_axis=(
                "vertical" if oriented_height >= oriented_width else "horizontal"
            ),
            format_mismatch=source_format != extension_format,
        )
        return working, alpha, inspection
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated


def _asset_destination(
    workspace: Workspace,
    asset_type: str,
    image_id: str,
    transform_fingerprint: str,
    extension: str,
) -> Path:
    return (
        workspace.assets_root
        / asset_type
        / image_id[:2]
        / image_id
        / f"{transform_fingerprint}{extension}"
    )


def _save_analysis_jpeg(
    image: Image.Image,
    destination: Path,
    *,
    max_long_edge: int,
    quality: int,
) -> tuple[int, int, float, float]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_width, source_height = image.size
    scale = min(1.0, max_long_edge / max(source_width, source_height))
    target = (
        max(1, round(source_width * scale)),
        max(1, round(source_height * scale)),
    )
    resized = (
        image.resize(target, Image.Resampling.LANCZOS)
        if target != image.size
        else image.copy()
    )
    temporary = destination.with_name(destination.name + ".tmp")
    resized.save(
        temporary,
        format="JPEG",
        quality=quality,
        subsampling=0,
        optimize=False,
        progressive=False,
    )
    temporary.replace(destination)
    return target[0], target[1], target[0] / source_width, target[1] / source_height


def _register_asset(
    connection: sqlite3.Connection,
    workspace: Workspace,
    run_id: str,
    *,
    image_id: str,
    asset_type: str,
    path: Path,
    width: int,
    height: int,
    image_format: str,
    transform_name: str,
    transform_version: str,
    transform_fingerprint: str,
    metadata: Mapping[str, Any],
    image_occurrence_id: str | None = None,
) -> PreparedAsset:
    relative_path = path.relative_to(workspace.output_root).as_posix()
    asset_sha = sha256_file(path)
    asset_id = stable_id(
        "asset",
        run_id,
        image_id,
        image_occurrence_id or "",
        asset_type,
        transform_fingerprint,
        relative_path,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO derived_assets(
            derived_asset_id, image_id, image_occurrence_id, run_id,
            asset_type, relative_path, sha256, width, height, format,
            transform_name, transform_version, transform_fingerprint,
            root_alias, created_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id,
            image_id,
            image_occurrence_id,
            run_id,
            asset_type,
            relative_path,
            asset_sha,
            width,
            height,
            image_format,
            transform_name,
            transform_version,
            transform_fingerprint,
            "pipeline_output",
            utc_now(),
            canonical_json(dict(metadata)),
        ),
    )
    return PreparedAsset(
        derived_asset_id=asset_id,
        image_id=image_id,
        asset_type=asset_type,
        path=path,
        relative_path=relative_path,
        sha256=asset_sha,
        width=width,
        height=height,
        format=image_format,
        transform_name=transform_name,
        transform_version=transform_version,
        transform_fingerprint=transform_fingerprint,
        metadata=dict(metadata),
    )


def register_existing_asset(
    connection: sqlite3.Connection,
    workspace: Workspace,
    run_id: str,
    *,
    image_id: str,
    asset_type: str,
    path: Path,
    width: int,
    height: int,
    image_format: str,
    transform_name: str,
    transform_version: str,
    transform_fingerprint: str,
    metadata: Mapping[str, Any],
    image_occurrence_id: str | None = None,
) -> PreparedAsset:
    """Register an already-created asset located below pipeline_output."""

    return _register_asset(
        connection,
        workspace,
        run_id,
        image_id=image_id,
        asset_type=asset_type,
        path=path,
        width=width,
        height=height,
        image_format=image_format,
        transform_name=transform_name,
        transform_version=transform_version,
        transform_fingerprint=transform_fingerprint,
        metadata=metadata,
        image_occurrence_id=image_occurrence_id,
    )


def ensure_vlm_compatible_asset(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    source_asset: PreparedAsset,
) -> PreparedAsset | None:
    """Pad sub-11-pixel previews without altering or deleting the source asset."""

    if (
        source_asset.width >= VLM_MIN_EDGE_PX
        and source_asset.height >= VLM_MIN_EDGE_PX
    ):
        return None
    target_width = max(VLM_MIN_EDGE_PX, source_asset.width)
    target_height = max(VLM_MIN_EDGE_PX, source_asset.height)
    translate_x = (target_width - source_asset.width) // 2
    translate_y = (target_height - source_asset.height) // 2
    transform_payload = {
        "name": "vlm_minimum_edge_padding",
        "version": VLM_TRANSPORT_VERSION,
        "source_asset_sha256": source_asset.sha256,
        "source_size": [source_asset.width, source_asset.height],
        "target_size": [target_width, target_height],
        "translate": [translate_x, translate_y],
        "fill": [255, 255, 255],
    }
    fingerprint = sha256_json(transform_payload)
    destination = _asset_destination(
        workspace,
        "vlm_input_preview",
        source_asset.image_id,
        fingerprint,
        ".jpg",
    )
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source_asset.path) as opened:
            source = opened.convert("RGB")
            canvas = Image.new(
                "RGB",
                (target_width, target_height),
                (255, 255, 255),
            )
            canvas.paste(source, (translate_x, translate_y))
            temporary = destination.with_name(destination.name + ".tmp")
            canvas.save(
                temporary,
                format="JPEG",
                quality=95,
                subsampling=0,
                optimize=False,
                progressive=False,
            )
            temporary.replace(destination)
    source_transform = source_asset.metadata.get(
        "image_to_asset_transform",
        {
            "scale_x": 1.0,
            "scale_y": 1.0,
            "translate_x": 0,
            "translate_y": 0,
        },
    )
    metadata = {
        **source_asset.metadata,
        "source_asset_id": source_asset.derived_asset_id,
        "transport_compatibility": {
            "provider_constraint": "width_and_height_must_be_greater_than_10",
            "padding_only": True,
            "version": VLM_TRANSPORT_VERSION,
        },
        "image_to_asset_transform": {
            "scale_x": source_transform.get("scale_x", 1.0),
            "scale_y": source_transform.get("scale_y", 1.0),
            "translate_x": (
                source_transform.get("translate_x", 0) + translate_x
            ),
            "translate_y": (
                source_transform.get("translate_y", 0) + translate_y
            ),
        },
    }
    asset = _register_asset(
        connection,
        workspace,
        run_id,
        image_id=source_asset.image_id,
        asset_type="vlm_input_preview",
        path=destination,
        width=target_width,
        height=target_height,
        image_format="JPEG",
        transform_name="vlm_minimum_edge_padding",
        transform_version=VLM_TRANSPORT_VERSION,
        transform_fingerprint=fingerprint,
        metadata=metadata,
    )
    connection.commit()
    return asset


def _tile_starts(length: int, tile_size: int, overlap: int) -> list[int]:
    if length <= tile_size:
        return [0]
    step = tile_size - overlap
    starts = list(range(0, max(1, length - tile_size + 1), step))
    last = length - tile_size
    if starts[-1] != last:
        starts.append(last)
    return starts


def prepare_analysis_assets(
    connection: sqlite3.Connection,
    workspace: Workspace,
    *,
    run_id: str,
    image_id: str,
    source_path: Path,
    config: Mapping[str, Any],
    decoded_working: Image.Image | None = None,
    decoded_inspection: ImageInspection | None = None,
) -> PreparedContent:
    """Create versioned image-only analysis assets and register their geometry."""

    if (decoded_working is None) != (decoded_inspection is None):
        raise ValueError(
            "decoded_working and decoded_inspection must be supplied together"
        )
    if decoded_working is None:
        working, _alpha, inspection = load_oriented_working_image(
            source_path,
            config,
        )
    else:
        working = decoded_working
        inspection = decoded_inspection
        assert inspection is not None
        if working.size != (
            inspection.oriented_width,
            inspection.oriented_height,
        ):
            raise ValueError("predecoded working image size does not match inspection")
    quality = int(config["analysis_jpeg_quality"])
    implementation_version = str(config["implementation_version"])
    coordinate_transforms = exif_coordinate_transforms(
        inspection.width,
        inspection.height,
        inspection.exif_orientation,
    )
    orientation_metadata = {
        "coordinate_system": "exif_oriented_image_pixels",
        "bbox_convention": "[x_min,y_min,x_max,y_max) half-open",
        "encoded_size": [inspection.width, inspection.height],
        "oriented_size": [
            inspection.oriented_width,
            inspection.oriented_height,
        ],
        "exif_orientation": inspection.exif_orientation,
        "raw_to_oriented": "Pillow.ImageOps.exif_transpose",
        "coordinate_transforms": coordinate_transforms,
    }

    if not inspection.is_long:
        transform_payload = {
            "name": "analysis_preview",
            "version": implementation_version,
            "max_long_edge": int(config["ordinary_max_long_edge"]),
            "jpeg_quality": quality,
            "orientation": orientation_metadata,
        }
        fingerprint = sha256_json(transform_payload)
        path = _asset_destination(
            workspace, "analysis_preview", image_id, fingerprint, ".jpg"
        )
        if not path.exists():
            width, height, scale_x, scale_y = _save_analysis_jpeg(
                working,
                path,
                max_long_edge=int(config["ordinary_max_long_edge"]),
                quality=quality,
            )
        else:
            with Image.open(path) as existing:
                width, height = existing.size
            scale_x = width / working.width
            scale_y = height / working.height
        metadata = {
            **orientation_metadata,
            "image_to_asset_transform": {
                "scale_x": scale_x,
                "scale_y": scale_y,
                "translate_x": 0,
                "translate_y": 0,
            },
            "semantic_invalid_candidate": inspection.is_semantic_invalid_candidate,
        }
        asset = _register_asset(
            connection,
            workspace,
            run_id,
            image_id=image_id,
            asset_type="analysis_preview",
            path=path,
            width=width,
            height=height,
            image_format="JPEG",
            transform_name="analysis_preview",
            transform_version=implementation_version,
            transform_fingerprint=fingerprint,
            metadata=metadata,
        )
        connection.commit()
        return PreparedContent(image_id, inspection, (asset,), None)

    global_payload = {
        "name": "global_thumbnail",
        "version": implementation_version,
        "max_long_edge": int(config["global_thumbnail_max_long_edge"]),
        "jpeg_quality": quality,
        "orientation": orientation_metadata,
    }
    global_fingerprint = sha256_json(global_payload)
    global_path = _asset_destination(
        workspace,
        "global_thumbnail",
        image_id,
        global_fingerprint,
        ".jpg",
    )
    if not global_path.exists():
        global_width, global_height, global_scale_x, global_scale_y = (
            _save_analysis_jpeg(
                working,
                global_path,
                max_long_edge=int(config["global_thumbnail_max_long_edge"]),
                quality=quality,
            )
        )
    else:
        with Image.open(global_path) as existing:
            global_width, global_height = existing.size
        global_scale_x = global_width / working.width
        global_scale_y = global_height / working.height
    global_metadata = {
        **orientation_metadata,
        "image_to_asset_transform": {
            "scale_x": global_scale_x,
            "scale_y": global_scale_y,
            "translate_x": 0,
            "translate_y": 0,
        },
    }
    global_asset = _register_asset(
        connection,
        workspace,
        run_id,
        image_id=image_id,
        asset_type="global_thumbnail",
        path=global_path,
        width=global_width,
        height=global_height,
        image_format="JPEG",
        transform_name="global_thumbnail",
        transform_version=implementation_version,
        transform_fingerprint=global_fingerprint,
        metadata=global_metadata,
    )

    tiling_payload = {
        "name": "overlap_tiles",
        "version": implementation_version,
        "tile_long_axis": int(config["tile_long_axis"]),
        "tile_overlap": int(config["tile_overlap"]),
        "jpeg_quality": quality,
        "orientation": orientation_metadata,
    }
    tiling_fingerprint = sha256_json(tiling_payload)
    layout_id = stable_id(
        "long_layout", run_id, image_id, tiling_fingerprint
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO long_image_layouts(
            long_image_layout_id, image_id, run_id,
            global_thumbnail_asset_id, original_width, original_height,
            global_thumbnail_width, global_thumbnail_height, reading_axis,
            layout_type, global_layout_json,
            image_to_thumbnail_transform_json, tiling_strategy_version,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            layout_id,
            image_id,
            run_id,
            global_asset.derived_asset_id,
            working.width,
            working.height,
            global_width,
            global_height,
            inspection.reading_axis,
            "long_detail_strip",
            canonical_json(
                {
                    "schema_version": "global-layout-1",
                    "reading_axis": inspection.reading_axis,
                    "panel_detection_status": "not_run",
                    "coordinate_transforms": coordinate_transforms,
                    "cross_tile_text_deduplication": {
                        "status": "foundation_ready_ocr_not_run",
                        "version": CROSS_TILE_TEXT_DEDUP_VERSION,
                        "deduplication_coordinates": (
                            "exif_oriented_image_pixels"
                        ),
                    },
                }
            ),
            canonical_json(global_metadata["image_to_asset_transform"]),
            tiling_fingerprint,
            utc_now(),
        ),
    )

    axis_length = (
        working.height if inspection.reading_axis == "vertical" else working.width
    )
    tile_size = int(config["tile_long_axis"])
    overlap = int(config["tile_overlap"])
    starts = _tile_starts(axis_length, tile_size, overlap)
    assets: list[PreparedAsset] = [global_asset]
    for index, start in enumerate(starts):
        end = min(axis_length, start + tile_size)
        if inspection.reading_axis == "vertical":
            bbox = [0, start, working.width, end]
        else:
            bbox = [start, 0, end, working.height]
        crop = working.crop(tuple(bbox))
        transform_payload = {
            **tiling_payload,
            "tile_index": index,
            "bbox_image": bbox,
        }
        fingerprint = sha256_json(transform_payload)
        tile_path = _asset_destination(
            workspace, "image_tile", image_id, fingerprint, ".jpg"
        )
        if not tile_path.exists():
            width, height, scale_x, scale_y = _save_analysis_jpeg(
                crop,
                tile_path,
                max_long_edge=int(config["ordinary_max_long_edge"]),
                quality=quality,
            )
        else:
            with Image.open(tile_path) as existing:
                width, height = existing.size
            scale_x = width / crop.width
            scale_y = height / crop.height
        previous_end = (
            min(axis_length, starts[index - 1] + tile_size)
            if index > 0
            else start
        )
        next_start = starts[index + 1] if index + 1 < len(starts) else end
        overlap_before = max(0, previous_end - start)
        overlap_after = max(0, end - next_start)
        image_to_tile = {
            "source_bbox": bbox,
            "translate_x": -bbox[0],
            "translate_y": -bbox[1],
            "scale_x": scale_x,
            "scale_y": scale_y,
        }
        tile_to_image = {
            "scale_x": 1.0 / scale_x,
            "scale_y": 1.0 / scale_y,
            "translate_x": bbox[0],
            "translate_y": bbox[1],
        }
        metadata = {
            **orientation_metadata,
            "tile_index": index,
            "bbox_image": bbox,
            "overlap_before_px": overlap_before,
            "overlap_after_px": overlap_after,
            "image_to_tile_transform": image_to_tile,
            "tile_to_image_transform": tile_to_image,
        }
        asset = _register_asset(
            connection,
            workspace,
            run_id,
            image_id=image_id,
            asset_type="image_tile",
            path=tile_path,
            width=width,
            height=height,
            image_format="JPEG",
            transform_name="overlap_tile",
            transform_version=implementation_version,
            transform_fingerprint=fingerprint,
            metadata=metadata,
        )
        tile_id = stable_id("tile", layout_id, index, fingerprint)
        connection.execute(
            """
            INSERT OR IGNORE INTO image_tiles(
                image_tile_id, long_image_layout_id, image_id, tile_asset_id,
                tile_index, bbox_image_json, overlap_before_px,
                overlap_after_px, tile_width, tile_height,
                image_to_tile_transform_json, tile_to_image_transform_json,
                transform_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tile_id,
                layout_id,
                image_id,
                asset.derived_asset_id,
                index,
                canonical_json(bbox),
                overlap_before,
                overlap_after,
                width,
                height,
                canonical_json(image_to_tile),
                canonical_json(tile_to_image),
                fingerprint,
                utc_now(),
            ),
        )
        assets.append(asset)
    connection.commit()
    return PreparedContent(image_id, inspection, tuple(assets), layout_id)


def map_tile_bbox_to_image(
    bbox_tile: Sequence[float],
    tile_to_image_transform: Mapping[str, Any],
) -> tuple[float, float, float, float]:
    """Map a half-open tile-space box back to oriented source-image pixels."""

    if len(bbox_tile) != 4:
        raise ValueError("bbox_tile must contain four coordinates")
    x0, y0, x1, y1 = (float(value) for value in bbox_tile)
    if not (x0 < x1 and y0 < y1):
        raise ValueError("bbox_tile must have positive area")
    scale_x = float(tile_to_image_transform["scale_x"])
    scale_y = float(tile_to_image_transform["scale_y"])
    translate_x = float(tile_to_image_transform["translate_x"])
    translate_y = float(tile_to_image_transform["translate_y"])
    if scale_x <= 0 or scale_y <= 0:
        raise ValueError("tile transform scales must be positive")
    return (
        x0 * scale_x + translate_x,
        y0 * scale_y + translate_y,
        x1 * scale_x + translate_x,
        y1 * scale_y + translate_y,
    )


def _bbox_iou(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
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
    left_area = (float(left[2]) - float(left[0])) * (
        float(left[3]) - float(left[1])
    )
    right_area = (float(right[2]) - float(right[0])) * (
        float(right[3]) - float(right[1])
    )
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0


def _normalized_ocr_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in {"-", "_"}
    )


def deduplicate_tile_text_observations(
    observations: Iterable[Mapping[str, Any]],
    tile_transforms: Mapping[int, Mapping[str, Any]],
    *,
    iou_threshold: float = 0.5,
) -> list[dict[str, Any]]:
    """Map tile OCR boxes to the source and merge overlap duplicates.

    This is deliberately OCR-engine agnostic. It establishes the persisted
    coordinate and deduplication semantics used by later OCR stages without
    claiming that OCR itself ran during Stage 2.
    """

    if not 0.0 <= iou_threshold <= 1.0:
        raise ValueError("iou_threshold must be within [0, 1]")
    mapped: list[dict[str, Any]] = []
    for observation in observations:
        tile_index = int(observation["tile_index"])
        transform = tile_transforms.get(tile_index)
        if transform is None:
            raise KeyError(f"missing transform for tile_index={tile_index}")
        text_value = str(observation.get("text") or "")
        bbox_image = map_tile_bbox_to_image(
            observation["bbox_tile"],
            transform,
        )
        mapped.append(
            {
                **dict(observation),
                "text": text_value,
                "normalized_text": _normalized_ocr_text(text_value),
                "bbox_image": list(bbox_image),
                "source_tile_indices": [tile_index],
                "deduplication_version": CROSS_TILE_TEXT_DEDUP_VERSION,
            }
        )

    deduplicated: list[dict[str, Any]] = []
    for candidate in sorted(
        mapped,
        key=lambda row: (
            str(row["normalized_text"]),
            -float(row.get("confidence") or 0.0),
            int(row["tile_index"]),
        ),
    ):
        duplicate = next(
            (
                existing
                for existing in deduplicated
                if candidate["normalized_text"]
                and existing["normalized_text"] == candidate["normalized_text"]
                and _bbox_iou(
                    existing["bbox_image"],
                    candidate["bbox_image"],
                )
                >= iou_threshold
            ),
            None,
        )
        if duplicate is None:
            deduplicated.append(candidate)
            continue
        duplicate["source_tile_indices"] = sorted(
            {
                *duplicate["source_tile_indices"],
                *candidate["source_tile_indices"],
            }
        )
    return deduplicated


def asset_data_url(asset: PreparedAsset) -> str:
    import base64

    payload = base64.b64encode(asset.path.read_bytes()).decode("ascii")
    mime = MIME_BY_FORMAT.get(asset.format.upper(), "application/octet-stream")
    return f"data:{mime};base64,{payload}"
