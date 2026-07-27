#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
商品图片基础预处理流水线。

输入目录示例：
    downloaded_images/
        品牌名/
            商品名/
                image_001.jpg
                image_002.png

核心保证：
1. 从不覆盖、重命名或删除输入目录中的原始文件。
2. 默认先创建逐字节原图副本，再从副本解码和处理。
3. 修正 EXIF 方向，检查 ICC；有效非 sRGB 图像通过 LittleCMS 转换到 sRGB。
4. 无 ICC 图像仅“假定为 sRGB”，不会声称其原始色彩空间一定是 sRGB。
5. 工作图统一为 8-bit RGB PNG；透明通道另存为 Alpha Mask。
6. 计算 SHA256、64-bit pHash、64-bit dHash；只报告重复，不自动删除。
7. 单图失败不会中断批处理；输出 CSV、JSONL、JSON、错误与运行日志。
8. 不执行白平衡、曝光、饱和度、滤镜、锐化、Gamma 或其他颜色增强。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import threading
import traceback
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import yaml
from PIL import Image, ImageCms, ImageFile, ImageOps, UnidentifiedImageError
from tqdm import tqdm

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover - NumPy fallback remains available.
    cv2 = None


SCRIPT_VERSION = "1.1.0"
EXIF_ORIENTATION_TAG = 274
DEFAULT_SUPPORTED_EXTENSIONS = [
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "input": {
        "supported_extensions": DEFAULT_SUPPORTED_EXTENSIONS,
    },
    "output": {
        "copy_originals": True,
        "overwrite": False,
        "working_format": "png",
        "save_csv": True,
        "save_jsonl": True,
    },
    "color_management": {
        "target_color_space": "sRGB",
        "missing_profile_policy": "assume_srgb",
        "invalid_profile_policy": "fallback_and_flag",
        "rendering_intent": "perceptual",
    },
    "alpha": {
        "save_alpha_mask": True,
        "display_background": "white",
        "exclude_transparent_pixels": True,
        "valid_alpha_threshold": 1,
    },
    "deduplication": {
        "calculate_sha256": True,
        "calculate_phash": True,
        "calculate_dhash": True,
        "phash_high_confidence_distance": 4,
        "phash_possible_duplicate_distance": 8,
    },
    "quality": {
        "calculate_blur_score": True,
        "calculate_exposure_metrics": True,
        "analysis_max_side": 1024,
        "low_resolution_short_edge": 256,
        "medium_resolution_short_edge": 512,
        "blur_warning_threshold": 50.0,
        "dark_pixel_threshold": 30,
        "bright_pixel_threshold": 225,
        "dark_ratio_warning_threshold": 0.60,
        "bright_ratio_warning_threshold": 0.60,
        "mostly_transparent_threshold": 0.95,
    },
    "processing": {
        "continue_on_error": True,
        "num_workers": max(1, min(8, os.cpu_count() or 4)),
        "processing_version": SCRIPT_VERSION,
        "max_image_pixels": 200_000_000,
        "allow_truncated_images": False,
        "selected_frame": 0,
        "verify_original_copy_sha256": True,
    },
}

_THREAD_LOCAL = threading.local()


@dataclass
class ImageRecord:
    image_id: str = ""
    brand_folder: str = ""
    product_folder: str = ""
    source_path: str = ""
    relative_path: str = ""
    filename: str = ""
    file_extension: str = ""
    file_size: int = 0
    original_copy_path: str = ""
    original_copy_verified: bool = False

    sha256: str = ""
    phash: str = ""
    dhash: str = ""
    exact_group_id: str = ""
    phash_group_id: str = ""
    duplicate_group_id: str = ""
    duplicate_type: str = "none"

    decode_success: bool = False
    error_type: str = ""
    error_message: str = ""

    source_format: str = ""
    source_width: int = 0
    source_height: int = 0
    oriented_width: int = 0
    oriented_height: int = 0
    working_width: int = 0
    working_height: int = 0
    source_mode: str = ""
    working_mode: str = ""
    is_multiframe: bool = False
    frame_count: int = 1
    selected_frame: int = 0

    exif_orientation_found: bool = False
    exif_orientation_value: int = 1
    orientation_corrected: bool = False

    icc_profile_present: bool = False
    icc_profile_name: str = ""
    icc_profile_description: str = ""
    source_color_space: str = ""
    working_color_space: str = ""
    color_profile_status: str = ""
    converted_to_srgb: bool = False
    color_profile_review_required: bool = False
    color_management_note: str = ""

    is_grayscale: bool = False
    has_alpha: bool = False
    alpha_mask_path: str = ""
    transparent_pixel_ratio: float = 0.0
    nonopaque_pixel_ratio: float = 0.0

    working_image_path: str = ""
    working_image_reused: bool = False
    working_image_sha256: str = ""

    aspect_ratio: float = 0.0
    megapixels: float = 0.0
    blur_score: float = 0.0
    mean_brightness: float = 0.0
    dark_pixel_ratio: float = 0.0
    bright_pixel_ratio: float = 0.0
    quality_warning: str = ""

    processing_version: str = SCRIPT_VERSION
    processed_at: str = ""


@dataclass
class ErrorRecord:
    relative_path: str
    stage: str
    error_type: str
    error_message: str
    traceback: str


@dataclass
class DuplicatePair:
    pair_type: str
    group_id: str
    path_a: str
    path_b: str
    sha256_a: str
    sha256_b: str
    phash_a: str
    phash_b: str
    phash_distance: int
    dhash_a: str
    dhash_b: str
    dhash_distance: int


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, a: int, b: int) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if self.rank[root_a] < self.rank[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        if self.rank[root_a] == self.rank[root_b]:
            self.rank[root_a] += 1


class BKNode:
    def __init__(self, value: int, index: int) -> None:
        self.value = value
        self.indices = [index]
        self.children: dict[int, "BKNode"] = {}


class BKTree:
    """使用 64 位整数汉明距离的 BK-tree，避免对全部图片进行 O(n²) 比较。"""

    def __init__(self) -> None:
        self.root: Optional[BKNode] = None

    @staticmethod
    def distance(a: int, b: int) -> int:
        return (a ^ b).bit_count()

    def insert(self, value: int, index: int) -> None:
        if self.root is None:
            self.root = BKNode(value, index)
            return
        node = self.root
        while True:
            distance = self.distance(value, node.value)
            if distance == 0:
                node.indices.append(index)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = BKNode(value, index)
                return
            node = child

    def query(self, value: int, max_distance: int) -> list[tuple[int, int]]:
        if self.root is None:
            return []
        matches: list[tuple[int, int]] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = self.distance(value, node.value)
            if distance <= max_distance:
                matches.extend((index, distance) for index in node.indices)
            lower, upper = distance - max_distance, distance + max_distance
            for edge_distance, child in node.children.items():
                if lower <= edge_distance <= upper:
                    stack.append(child)
        return matches


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        merged[key] = deep_merge(value, {}) if isinstance(value, dict) else value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_path: Optional[Path]) -> dict[str, Any]:
    if config_path is None:
        return deep_merge(DEFAULT_CONFIG, {})
    with config_path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("配置文件顶层必须是 YAML mapping/object")
    return deep_merge(DEFAULT_CONFIG, loaded)


def validate_config(config: dict[str, Any]) -> None:
    if str(config["output"]["working_format"]).lower() != "png":
        raise ValueError("基础工作图固定为 PNG；output.working_format 必须为 png")
    if str(config["color_management"]["target_color_space"]).casefold() != "srgb":
        raise ValueError("当前基础流水线只支持 target_color_space: sRGB")
    if str(config["color_management"]["missing_profile_policy"]).casefold() != "assume_srgb":
        raise ValueError("missing_profile_policy 当前必须为 assume_srgb")
    if str(config["color_management"]["invalid_profile_policy"]).casefold() != "fallback_and_flag":
        raise ValueError("invalid_profile_policy 当前必须为 fallback_and_flag")
    high = int(config["deduplication"]["phash_high_confidence_distance"])
    possible = int(config["deduplication"]["phash_possible_duplicate_distance"])
    if not (0 <= high <= possible <= 64):
        raise ValueError("pHash 阈值必须满足 0 <= high_confidence <= possible <= 64")
    if int(config["processing"]["num_workers"]) < 1:
        raise ValueError("processing.num_workers 必须 >= 1")
    if int(config["alpha"]["valid_alpha_threshold"]) not in range(0, 256):
        raise ValueError("alpha.valid_alpha_threshold 必须在 0~255")


def setup_logging(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("image_preprocessing")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console_handler)
    return logger


def validate_paths(input_root: Path, output_root: Path) -> None:
    if not input_root.exists() or not input_root.is_dir():
        raise FileNotFoundError(f"输入目录不存在或不是目录：{input_root}")
    input_resolved = input_root.resolve()
    output_resolved = output_root.resolve()
    if input_resolved == output_resolved:
        raise ValueError("输出目录不得与输入目录相同")
    if output_resolved.is_relative_to(input_resolved):
        raise ValueError("输出目录不得放在输入目录内部，以防递归扫描处理结果")
    if input_resolved.is_relative_to(output_resolved):
        raise ValueError("输入目录不得放在输出目录内部")


def normalize_path(path: Path) -> str:
    return path.as_posix()


def discover_images(input_root: Path, extensions: set[str]) -> list[Path]:
    images: list[Path] = []
    for path in input_root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in extensions:
            images.append(path)
    images.sort(key=lambda item: normalize_path(item.relative_to(input_root)).casefold())
    return images


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stable_image_id(relative_path: Path, sha256: str) -> str:
    path_digest = hashlib.sha1(normalize_path(relative_path).encode("utf-8")).hexdigest()[:10]
    return f"{sha256[:16]}-{path_digest}"


def parse_brand_product(relative_path: Path) -> tuple[str, str]:
    parts = relative_path.parts
    brand = parts[0] if len(parts) >= 2 else ""
    product = parts[1] if len(parts) >= 3 else ""
    return brand, product


def hashed_relative_path(relative_path: Path, sha256: str, extension: str) -> Path:
    return relative_path.with_name(f"{relative_path.stem}__{sha256[:16]}{extension}")


def atomic_copy(source: Path, destination: Path, overwrite: bool) -> bool:
    """逐字节复制；返回是否新写入。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        return False
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copy2(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def get_thread_srgb_profile() -> tuple[Any, bytes]:
    if not hasattr(_THREAD_LOCAL, "srgb_profile"):
        profile = ImageCms.createProfile("sRGB")
        wrapped = ImageCms.ImageCmsProfile(profile)
        _THREAD_LOCAL.srgb_profile = profile
        _THREAD_LOCAL.srgb_bytes = wrapped.tobytes()
    return _THREAD_LOCAL.srgb_profile, _THREAD_LOCAL.srgb_bytes


def safe_profile_text(function: Any, profile: Any) -> str:
    try:
        return str(function(profile)).replace("\x00", "").strip()
    except Exception:
        return ""


def inspect_icc_profile(icc_bytes: Optional[bytes]) -> tuple[dict[str, Any], Optional[Any]]:
    if not icc_bytes:
        return {
            "present": False,
            "valid": False,
            "name": "",
            "description": "",
            "source_color_space": "unknown",
            "status": "profile_missing",
            "is_srgb": False,
        }, None

    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        name = safe_profile_text(ImageCms.getProfileName, profile)
        description = safe_profile_text(ImageCms.getProfileDescription, profile)
        info = safe_profile_text(ImageCms.getProfileInfo, profile)
        try:
            source_space = str(profile.profile.xcolor_space).strip()
        except Exception:
            source_space = "unknown"
        searchable = " ".join([name, description, info]).casefold()
        srgb_tokens = ("srgb", "s-rgb", "iec 61966", "iec61966", "61966-2.1")
        is_srgb = any(token in searchable for token in srgb_tokens)
        return {
            "present": True,
            "valid": True,
            "name": name,
            "description": description,
            "source_color_space": source_space or "unknown",
            "status": "embedded_srgb" if is_srgb else "embedded_non_srgb",
            "is_srgb": is_srgb,
        }, profile
    except Exception as exc:
        return {
            "present": True,
            "valid": False,
            "name": "",
            "description": "",
            "source_color_space": "unknown",
            "status": "profile_invalid",
            "is_srgb": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, None


def image_has_alpha(image: Image.Image) -> bool:
    # LAB 的 A 是色度通道，不能仅用 getbands() 是否包含 A 判断。
    if image.mode in {"RGBA", "LA", "PA", "RGBa", "La"}:
        return True
    return image.mode == "P" and "transparency" in image.info


def extract_alpha(image: Image.Image) -> Optional[Image.Image]:
    if not image_has_alpha(image):
        return None
    return image.convert("RGBA").getchannel("A")


def fallback_convert_to_rgb(image: Image.Image) -> tuple[Image.Image, str, bool]:
    """无可用 ICC 时的降级转换。返回 RGB、说明、是否需人工复核。"""
    mode = image.mode
    if mode == "LAB":
        try:
            lab_profile = ImageCms.createProfile("LAB")
            srgb_profile, _ = get_thread_srgb_profile()
            rgb = ImageCms.profileToProfile(
                image,
                lab_profile,
                srgb_profile,
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                outputMode="RGB",
            )
            return rgb, "missing_or_invalid_icc_assumed_standard_lab", True
        except Exception:
            return image.convert("RGB"), "missing_or_invalid_icc_lab_direct_fallback", True
    if mode == "CMYK":
        return image.convert("RGB"), "missing_or_invalid_icc_cmyk_pillow_fallback", True
    if mode in {"RGB", "RGBA", "P", "L", "LA", "1"}:
        return image.convert("RGB"), "missing_or_invalid_icc_assumed_srgb", False
    return image.convert("RGB"), f"missing_or_invalid_icc_{mode}_direct_fallback", True


def convert_color_managed_rgb(
    image: Image.Image,
    icc_bytes: Optional[bytes],
    profile_info: dict[str, Any],
    rendering_intent: str,
) -> tuple[Image.Image, bool, bool, str]:
    """
    返回：RGB 图、是否进行了 ICC->sRGB 数值转换、是否需复核、处理说明。
    Alpha 在外部独立保存，不在此函数中参与颜色转换。
    """
    if image_has_alpha(image):
        color_input = image.convert("RGBA").convert("RGB")
    elif image.mode == "P":
        color_input = image.convert("RGB")
    else:
        color_input = image

    if profile_info["status"] == "embedded_srgb":
        return color_input.convert("RGB"), False, False, "embedded_srgb_no_numeric_conversion"

    if profile_info["status"] == "embedded_non_srgb" and icc_bytes:
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
            target_profile, _ = get_thread_srgb_profile()
            intent_map = {
                "perceptual": ImageCms.Intent.PERCEPTUAL,
                "relative_colorimetric": ImageCms.Intent.RELATIVE_COLORIMETRIC,
                "saturation": ImageCms.Intent.SATURATION,
                "absolute_colorimetric": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
            }
            intent = intent_map.get(rendering_intent.casefold(), ImageCms.Intent.PERCEPTUAL)
            converted = ImageCms.profileToProfile(
                color_input,
                source_profile,
                target_profile,
                renderingIntent=intent,
                outputMode="RGB",
            )
            return converted, True, False, "embedded_non_srgb_converted_to_srgb"
        except Exception as exc:
            fallback, note, _ = fallback_convert_to_rgb(color_input)
            return (
                fallback,
                False,
                True,
                f"icc_conversion_failed_fallback:{type(exc).__name__};{note}",
            )

    fallback, note, fallback_review = fallback_convert_to_rgb(color_input)
    review = fallback_review or profile_info["status"] == "profile_invalid"
    return fallback, False, review, note


def composite_for_working_image(rgb: Image.Image, alpha: Optional[Image.Image], background: str) -> Image.Image:
    rgb = rgb.convert("RGB")
    if alpha is None:
        return rgb
    background_values = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (127, 127, 127),
        "grey": (127, 127, 127),
    }
    background_rgb = background_values.get(background.casefold(), (255, 255, 255))
    canvas = Image.new("RGB", rgb.size, background_rgb)
    return Image.composite(rgb, canvas, alpha)


def atomic_save_png(image: Image.Image, path: Path, overwrite: bool, icc_profile: Optional[bytes] = None) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return False
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        save_kwargs: dict[str, Any] = {"format": "PNG", "compress_level": 6, "optimize": False}
        if icc_profile:
            save_kwargs["icc_profile"] = icc_profile
        image.save(temporary_path, **save_kwargs)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return True


def crop_hash_content(image: Image.Image, alpha: Optional[Image.Image]) -> Image.Image:
    if alpha is None:
        return image.convert("RGB")
    bbox = alpha.getbbox()
    if bbox is None:
        return Image.new("RGB", (1, 1), (255, 255, 255))
    return image.crop(bbox).convert("RGB")


def dct_matrix(size: int) -> np.ndarray:
    x = np.arange(size, dtype=np.float64)
    k = x[:, None]
    matrix = np.cos(np.pi * (2.0 * x + 1.0) * k / (2.0 * size))
    matrix[0, :] *= math.sqrt(1.0 / size)
    matrix[1:, :] *= math.sqrt(2.0 / size)
    return matrix


_DCT_32 = dct_matrix(32)


def phash64(image: Image.Image) -> str:
    gray = image.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    transformed = _DCT_32 @ pixels @ _DCT_32.T
    low_frequency = transformed[:8, :8].reshape(-1)
    median = float(np.median(low_frequency[1:]))
    bits = low_frequency > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def dhash64(image: Image.Image) -> str:
    gray = np.asarray(
        image.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.uint8
    )
    bits = gray[:, 1:] > gray[:, :-1]
    value = 0
    for bit in bits.reshape(-1):
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def hamming_hex(hash_a: str, hash_b: str) -> int:
    return (int(hash_a, 16) ^ int(hash_b, 16)).bit_count()


def resize_for_quality(
    image: Image.Image,
    alpha: Optional[Image.Image],
    max_side: int,
) -> tuple[np.ndarray, Optional[np.ndarray]]:
    working = image
    mask = alpha
    if max(working.size) > max_side:
        scale = max_side / max(working.size)
        new_size = (
            max(1, round(working.width * scale)),
            max(1, round(working.height * scale)),
        )
        working = working.resize(new_size, Image.Resampling.LANCZOS)
        if mask is not None:
            mask = mask.resize(new_size, Image.Resampling.NEAREST)
    rgb = np.asarray(working.convert("RGB"), dtype=np.float32)
    alpha_array = np.asarray(mask, dtype=np.uint8) if mask is not None else None
    return rgb, alpha_array


def compute_quality_metrics(
    image: Image.Image,
    alpha: Optional[Image.Image],
    config: dict[str, Any],
    extra_flags: Iterable[str],
) -> dict[str, Any]:
    quality_config = config["quality"]
    valid_alpha_threshold = int(config["alpha"]["valid_alpha_threshold"])
    rgb, alpha_array = resize_for_quality(
        image,
        alpha,
        max_side=int(quality_config["analysis_max_side"]),
    )
    luminance = (
        0.2126 * rgb[:, :, 0]
        + 0.7152 * rgb[:, :, 1]
        + 0.0722 * rgb[:, :, 2]
    ).astype(np.float32)

    if alpha_array is None:
        valid = np.ones(luminance.shape, dtype=bool)
    else:
        valid = alpha_array >= valid_alpha_threshold

    flags = list(extra_flags)
    if not np.any(valid):
        valid_values = np.asarray([], dtype=np.float32)
        flags.append("fully_transparent")
    else:
        valid_values = luminance[valid]

    if valid_values.size:
        mean_brightness = float(np.mean(valid_values))
        dark_ratio = float(
            np.mean(valid_values <= float(quality_config["dark_pixel_threshold"]))
        )
        bright_ratio = float(
            np.mean(valid_values >= float(quality_config["bright_pixel_threshold"]))
        )
    else:
        mean_brightness = 0.0
        dark_ratio = 0.0
        bright_ratio = 0.0

    blur_score = 0.0
    if bool(quality_config["calculate_blur_score"]) and luminance.size:
        filled = luminance.copy()
        if not np.all(valid):
            fill_value = float(np.median(valid_values)) if valid_values.size else 255.0
            filled[~valid] = fill_value
        if cv2 is not None:
            # 某些 OpenCV 构建不支持 float32 -> CV_64F 的组合；
            # 使用 8-bit 灰度输入可获得稳定且跨平台的 Laplacian 方差。
            filled_u8 = np.clip(filled, 0, 255).astype(np.uint8)
            laplacian = cv2.Laplacian(filled_u8, cv2.CV_64F)
            blur_score = float(np.var(laplacian[valid])) if np.any(valid) else 0.0
        elif filled.shape[0] >= 3 and filled.shape[1] >= 3:
            laplacian = (
                -4.0 * filled[1:-1, 1:-1]
                + filled[:-2, 1:-1]
                + filled[2:, 1:-1]
                + filled[1:-1, :-2]
                + filled[1:-1, 2:]
            )
            inner_valid = valid[1:-1, 1:-1]
            blur_score = float(np.var(laplacian[inner_valid])) if np.any(inner_valid) else 0.0

    short_edge = min(image.size)
    if short_edge < int(quality_config["low_resolution_short_edge"]):
        flags.append("low_resolution")
    elif short_edge < int(quality_config["medium_resolution_short_edge"]):
        flags.append("medium_resolution")
    if blur_score < float(quality_config["blur_warning_threshold"]):
        flags.append("possibly_blurry")
    if dark_ratio > float(quality_config["dark_ratio_warning_threshold"]):
        flags.append("mostly_dark")
    if bright_ratio > float(quality_config["bright_ratio_warning_threshold"]):
        flags.append("mostly_bright")

    return {
        "blur_score": round(blur_score, 4),
        "mean_brightness": round(mean_brightness, 4),
        "dark_pixel_ratio": round(dark_ratio, 6),
        "bright_pixel_ratio": round(bright_ratio, 6),
        "quality_warning": ";".join(sorted(set(flag for flag in flags if flag))),
    }


def process_single_image(
    source_path: Path,
    input_root: Path,
    output_root: Path,
    config: dict[str, Any],
    logger: logging.Logger,
) -> tuple[ImageRecord, Optional[ErrorRecord]]:
    relative = source_path.relative_to(input_root)
    brand, product = parse_brand_product(relative)
    record = ImageRecord(
        brand_folder=brand,
        product_folder=product,
        source_path=str(source_path),
        relative_path=normalize_path(relative),
        filename=source_path.name,
        file_extension=source_path.suffix.lower(),
        file_size=source_path.stat().st_size,
        selected_frame=int(config["processing"]["selected_frame"]),
        processing_version=str(config["processing"]["processing_version"]),
    )
    stage = "initialize"
    quality_flags: list[str] = []

    try:
        stage = "sha256"
        record.sha256 = sha256_file(source_path)
        record.image_id = stable_image_id(relative, record.sha256)

        processing_path = source_path
        if bool(config["output"]["copy_originals"]):
            stage = "copy_original"
            original_relative = hashed_relative_path(relative, record.sha256, relative.suffix)
            original_copy = output_root / "original_copies" / original_relative
            atomic_copy(
                source_path,
                original_copy,
                overwrite=bool(config["output"]["overwrite"]),
            )
            record.original_copy_path = normalize_path(original_copy.relative_to(output_root))
            if bool(config["processing"]["verify_original_copy_sha256"]):
                record.original_copy_verified = sha256_file(original_copy) == record.sha256
                if not record.original_copy_verified:
                    raise IOError("原图副本 SHA256 与源文件不一致")
            else:
                record.original_copy_verified = original_copy.stat().st_size == record.file_size
            processing_path = original_copy

        stage = "decode_and_exif"
        ImageFile.LOAD_TRUNCATED_IMAGES = bool(config["processing"]["allow_truncated_images"])
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(processing_path) as opened:
                record.source_format = (opened.format or relative.suffix.lstrip(".")).upper()
                record.source_mode = opened.mode
                record.source_width, record.source_height = opened.size
                record.frame_count = int(getattr(opened, "n_frames", 1))
                record.is_multiframe = record.frame_count > 1
                if record.selected_frame < 0 or record.selected_frame >= record.frame_count:
                    raise IndexError(
                        f"selected_frame={record.selected_frame} 超出帧数 {record.frame_count}"
                    )
                if record.is_multiframe:
                    quality_flags.append("multiframe_selected_frame_only")
                    opened.seek(record.selected_frame)

                pixel_count = opened.width * opened.height
                if pixel_count > int(config["processing"]["max_image_pixels"]):
                    raise ValueError(
                        f"像素数 {pixel_count} 超过 max_image_pixels="
                        f"{config['processing']['max_image_pixels']}"
                    )

                opened.load()
                icc_bytes = opened.info.get("icc_profile")
                exif = opened.getexif()
                orientation_value = exif.get(EXIF_ORIENTATION_TAG) if exif else None
                record.exif_orientation_found = orientation_value is not None
                try:
                    record.exif_orientation_value = int(orientation_value or 1)
                except (TypeError, ValueError):
                    record.exif_orientation_value = 1
                record.orientation_corrected = record.exif_orientation_value in {2, 3, 4, 5, 6, 7, 8}

                oriented = ImageOps.exif_transpose(opened).copy()
                record.oriented_width, record.oriented_height = oriented.size

        stage = "icc_inspection"
        profile_info, _ = inspect_icc_profile(icc_bytes)
        record.icc_profile_present = bool(profile_info["present"])
        record.icc_profile_name = str(profile_info["name"])
        record.icc_profile_description = str(profile_info["description"])
        record.source_color_space = str(profile_info["source_color_space"])
        record.color_profile_status = str(profile_info["status"])
        if record.color_profile_status == "profile_missing":
            record.working_color_space = "assumed_sRGB"
        elif record.color_profile_status == "profile_invalid":
            record.working_color_space = "assumed_sRGB_low_confidence"
        else:
            record.working_color_space = "sRGB"

        stage = "alpha_extraction"
        alpha = extract_alpha(oriented)
        record.has_alpha = alpha is not None
        if alpha is not None:
            alpha_array = np.asarray(alpha, dtype=np.uint8)
            record.transparent_pixel_ratio = round(float(np.mean(alpha_array == 0)), 6)
            record.nonopaque_pixel_ratio = round(float(np.mean(alpha_array < 255)), 6)
            if record.transparent_pixel_ratio >= float(
                config["quality"]["mostly_transparent_threshold"]
            ):
                quality_flags.append("mostly_transparent")

        stage = "color_conversion"
        rgb, converted, review_required, note = convert_color_managed_rgb(
            oriented,
            icc_bytes,
            profile_info,
            rendering_intent=str(config["color_management"]["rendering_intent"]),
        )
        record.converted_to_srgb = converted
        record.color_profile_review_required = review_required
        record.color_management_note = note
        if review_required and not converted and record.color_profile_status != "profile_missing":
            record.working_color_space = "assumed_sRGB_low_confidence"
        if record.color_profile_status == "profile_missing":
            if oriented.mode == "CMYK":
                record.source_color_space = "unknown_cmyk"
                record.color_profile_review_required = True
                quality_flags.append("cmyk_without_icc")
            elif oriented.mode == "LAB":
                record.source_color_space = "unknown_lab"
                record.color_profile_review_required = True
        elif record.color_profile_status == "profile_invalid":
            record.color_profile_review_required = True
            quality_flags.append("invalid_icc")

        record.is_grayscale = record.source_mode in {"1", "L", "LA", "I", "I;16", "F"}
        if record.is_grayscale:
            quality_flags.append("grayscale_source")

        stage = "working_image"
        working = composite_for_working_image(
            rgb,
            alpha,
            background=str(config["alpha"]["display_background"]),
        )
        if working.mode != "RGB":
            working = working.convert("RGB")
        record.working_mode = working.mode
        record.working_width, record.working_height = working.size
        record.aspect_ratio = round(working.width / working.height, 6) if working.height else 0.0
        record.megapixels = round(working.width * working.height / 1_000_000.0, 6)

        working_relative = hashed_relative_path(relative, record.sha256, ".png")
        working_path = output_root / "working_images" / working_relative
        _, srgb_profile_bytes = get_thread_srgb_profile()
        newly_saved = atomic_save_png(
            working,
            working_path,
            overwrite=bool(config["output"]["overwrite"]),
            icc_profile=srgb_profile_bytes,
        )
        record.working_image_reused = not newly_saved
        record.working_image_path = normalize_path(working_path.relative_to(output_root))
        record.working_image_sha256 = sha256_file(working_path)

        if alpha is not None and bool(config["alpha"]["save_alpha_mask"]):
            alpha_relative = working_relative.with_name(f"{working_relative.stem}_alpha.png")
            alpha_path = output_root / "alpha_masks" / alpha_relative
            atomic_save_png(
                alpha,
                alpha_path,
                overwrite=bool(config["output"]["overwrite"]),
            )
            record.alpha_mask_path = normalize_path(alpha_path.relative_to(output_root))

        stage = "hashes"
        hash_image = crop_hash_content(working, alpha)
        if bool(config["deduplication"]["calculate_phash"]):
            record.phash = phash64(hash_image)
        if bool(config["deduplication"]["calculate_dhash"]):
            record.dhash = dhash64(hash_image)

        stage = "quality_metrics"
        metrics = compute_quality_metrics(working, alpha, config, quality_flags)
        record.blur_score = metrics["blur_score"]
        record.mean_brightness = metrics["mean_brightness"]
        record.dark_pixel_ratio = metrics["dark_pixel_ratio"]
        record.bright_pixel_ratio = metrics["bright_pixel_ratio"]
        record.quality_warning = metrics["quality_warning"]

        record.decode_success = True
        record.processed_at = utc_now_iso()
        return record, None

    except Exception as exc:
        record.decode_success = False
        record.error_type = type(exc).__name__
        record.error_message = str(exc)
        record.processed_at = utc_now_iso()
        error = ErrorRecord(
            relative_path=record.relative_path,
            stage=stage,
            error_type=type(exc).__name__,
            error_message=str(exc),
            traceback=traceback.format_exc(limit=12),
        )
        logger.error("%s | stage=%s | %s: %s", relative, stage, type(exc).__name__, exc)
        return record, error


def build_exact_groups(records: list[ImageRecord]) -> tuple[list[dict[str, Any]], list[DuplicatePair]]:
    by_sha: dict[str, list[int]] = defaultdict(list)
    for index, record in enumerate(records):
        if record.sha256:
            by_sha[record.sha256].append(index)

    groups: list[dict[str, Any]] = []
    pairs: list[DuplicatePair] = []
    number = 0
    for sha256, indices in sorted(by_sha.items(), key=lambda item: item[0]):
        if len(indices) < 2:
            continue
        number += 1
        group_id = f"EXACT_{number:05d}"
        members = [records[index].relative_path for index in indices]
        for index in indices:
            records[index].exact_group_id = group_id
            records[index].duplicate_group_id = group_id
            records[index].duplicate_type = "exact_sha256"
        groups.append({"group_id": group_id, "sha256": sha256, "members": members})
        anchor = indices[0]
        for other in indices[1:]:
            pairs.append(
                DuplicatePair(
                    pair_type="exact_sha256",
                    group_id=group_id,
                    path_a=records[anchor].relative_path,
                    path_b=records[other].relative_path,
                    sha256_a=records[anchor].sha256,
                    sha256_b=records[other].sha256,
                    phash_a=records[anchor].phash,
                    phash_b=records[other].phash,
                    phash_distance=0,
                    dhash_a=records[anchor].dhash,
                    dhash_b=records[other].dhash,
                    dhash_distance=0,
                )
            )
    return groups, pairs


def build_perceptual_groups(
    records: list[ImageRecord], high_threshold: int, possible_threshold: int
) -> tuple[list[dict[str, Any]], list[DuplicatePair]]:
    valid_indices = [
        index
        for index, record in enumerate(records)
        if record.decode_success and len(record.phash) == 16
    ]
    union_find = UnionFind(len(records))
    tree = BKTree()
    raw_edges: list[tuple[int, int, int, int, str]] = []

    for index in valid_indices:
        phash_value = int(records[index].phash, 16)
        for other_index, phash_distance in tree.query(phash_value, possible_threshold):
            if records[index].sha256 == records[other_index].sha256:
                continue
            dhash_distance = (
                hamming_hex(records[index].dhash, records[other_index].dhash)
                if len(records[index].dhash) == 16 and len(records[other_index].dhash) == 16
                else -1
            )
            pair_type = (
                "phash_high_confidence"
                if phash_distance <= high_threshold
                else "phash_possible"
            )
            union_find.union(index, other_index)
            raw_edges.append((other_index, index, phash_distance, dhash_distance, pair_type))
        tree.insert(phash_value, index)

    components: dict[int, list[int]] = defaultdict(list)
    for index in valid_indices:
        components[union_find.find(index)].append(index)
    roots_with_edges = {union_find.find(a) for a, _, _, _, _ in raw_edges}
    ordered_roots = sorted(
        [root for root in roots_with_edges if len(components[root]) >= 2],
        key=lambda root: min(records[index].relative_path for index in components[root]),
    )
    group_id_by_root = {
        root: f"PHASH_{number:05d}" for number, root in enumerate(ordered_roots, start=1)
    }

    pair_rows: list[DuplicatePair] = []
    pair_type_by_index: dict[int, set[str]] = defaultdict(set)
    edges_by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for a, b, phash_distance, dhash_distance, pair_type in raw_edges:
        root = union_find.find(a)
        group_id = group_id_by_root[root]
        pair_type_by_index[a].add(pair_type)
        pair_type_by_index[b].add(pair_type)
        pair = DuplicatePair(
            pair_type=pair_type,
            group_id=group_id,
            path_a=records[a].relative_path,
            path_b=records[b].relative_path,
            sha256_a=records[a].sha256,
            sha256_b=records[b].sha256,
            phash_a=records[a].phash,
            phash_b=records[b].phash,
            phash_distance=phash_distance,
            dhash_a=records[a].dhash,
            dhash_b=records[b].dhash,
            dhash_distance=dhash_distance,
        )
        pair_rows.append(pair)
        edges_by_root[root].append(asdict(pair))

    groups: list[dict[str, Any]] = []
    for root in ordered_roots:
        group_id = group_id_by_root[root]
        members = components[root]
        for index in members:
            records[index].phash_group_id = group_id
            if not records[index].exact_group_id:
                records[index].duplicate_group_id = group_id
                records[index].duplicate_type = (
                    "phash_high_confidence"
                    if "phash_high_confidence" in pair_type_by_index[index]
                    else "phash_possible"
                )
        groups.append(
            {
                "group_id": group_id,
                "members": [records[index].relative_path for index in members],
                "pairs": edges_by_root[root],
                "note": (
                    "该组为阈值边的连通分量；同组任意两图不一定都直接满足阈值，"
                    "请以 pairs 中的直接匹配关系为准。"
                ),
            }
        )
    pair_rows.sort(key=lambda pair: (pair.group_id, pair.phash_distance, pair.path_a, pair.path_b))
    return groups, pair_rows


def write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_outputs(
    output_root: Path,
    records: list[ImageRecord],
    errors: list[ErrorRecord],
    config: dict[str, Any],
    started_at: str,
) -> dict[str, Any]:
    exact_groups, exact_pairs = build_exact_groups(records)
    perceptual_groups, perceptual_pairs = build_perceptual_groups(
        records,
        high_threshold=int(config["deduplication"]["phash_high_confidence_distance"]),
        possible_threshold=int(config["deduplication"]["phash_possible_duplicate_distance"]),
    )

    metadata_rows = [asdict(record) for record in records]
    metadata_fields = [field.name for field in fields(ImageRecord)]
    if bool(config["output"]["save_csv"]):
        write_csv(
            output_root / "metadata" / "image_preprocessing.csv",
            metadata_rows,
            metadata_fields,
        )
    if bool(config["output"]["save_jsonl"]):
        write_jsonl(output_root / "metadata" / "image_preprocessing.jsonl", metadata_rows)

    error_fields = [field.name for field in fields(ErrorRecord)]
    write_csv(
        output_root / "errors" / "preprocessing_errors.csv",
        (asdict(error) for error in errors),
        error_fields,
    )

    all_pairs = exact_pairs + perceptual_pairs
    pair_fields = [field.name for field in fields(DuplicatePair)]
    write_csv(
        output_root / "duplicate_pairs.csv",
        (asdict(pair) for pair in all_pairs),
        pair_fields,
    )

    duplicate_payload = {
        "thresholds": {
            "phash_high_confidence_distance": int(
                config["deduplication"]["phash_high_confidence_distance"]
            ),
            "phash_possible_duplicate_distance": int(
                config["deduplication"]["phash_possible_duplicate_distance"]
            ),
        },
        "exact_groups": exact_groups,
        "perceptual_groups": perceptual_groups,
    }
    with (output_root / "duplicate_groups.json").open("w", encoding="utf-8") as handle:
        json.dump(duplicate_payload, handle, ensure_ascii=False, indent=2)

    valid = [record for record in records if record.decode_success]
    invalid_profile_count = sum(
        record.color_profile_status == "profile_invalid" for record in valid
    )
    low_resolution_count = sum(
        "low_resolution" in record.quality_warning.split(";") for record in valid
    )
    blurry_count = sum(
        "possibly_blurry" in record.quality_warning.split(";") for record in valid
    )
    summary = {
        "script_version": SCRIPT_VERSION,
        "processing_version": str(config["processing"]["processing_version"]),
        "started_at_utc": started_at,
        "finished_at_utc": utc_now_iso(),
        "counts": {
            "scanned_files": len(records),
            "successfully_processed": len(valid),
            "failed": len(errors),
            "exact_duplicate_groups": len(exact_groups),
            "exact_duplicate_extra_files": sum(
                max(0, len(group["members"]) - 1) for group in exact_groups
            ),
            "perceptual_duplicate_groups": len(perceptual_groups),
            "perceptual_duplicate_direct_pairs": len(perceptual_pairs),
            "embedded_srgb": sum(
                record.color_profile_status == "embedded_srgb" for record in valid
            ),
            "embedded_non_srgb_converted": sum(record.converted_to_srgb for record in valid),
            "profile_missing_assumed_srgb": sum(
                record.color_profile_status == "profile_missing" for record in valid
            ),
            "profile_invalid": invalid_profile_count,
            "with_alpha": sum(record.has_alpha for record in valid),
            "cmyk_source": sum(record.source_mode == "CMYK" for record in valid),
            "grayscale_source": sum(record.is_grayscale for record in valid),
            "low_resolution": low_resolution_count,
            "possibly_blurry": blurry_count,
            "review_required": sum(record.color_profile_review_required for record in valid),
        },
        "notes": [
            "输入目录未被写入；默认处理基于 original_copies 中的逐字节副本。",
            "工作图为方向修正后的 8-bit RGB PNG，并嵌入 sRGB ICC。",
            "无 ICC 图像仅按 sRGB 解释并标记，不代表其源色彩空间已被证实。",
            "透明区域通过独立 Alpha Mask 标记，未作为有效颜色像素参与质量统计。",
            "所有重复结果仅作报告，不会自动删除、移动或合并图片。",
            "pHash 分组是直接匹配边的连通分量；应结合 duplicate_pairs.csv 人工复核。",
        ],
    }
    with (output_root / "preprocessing_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "对 downloaded_images/品牌/商品/图片 递归执行原图副本、EXIF、ICC->sRGB、"
            "Alpha Mask、SHA256/pHash/dHash、质量统计和重复报告。"
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("downloaded_images"),
        help="输入根目录，默认 downloaded_images",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("image_preprocessing_output"),
        help="独立输出目录，默认 image_preprocessing_output",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="YAML 配置文件；未指定时使用脚本内默认配置",
    )
    parser.add_argument("--workers", type=int, default=None, help="覆盖 processing.num_workers")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有副本和工作图")
    parser.add_argument("--verbose", action="store_true", help="控制台输出 DEBUG 日志")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    input_root = args.input.expanduser().resolve()
    output_root = args.output.expanduser().resolve()
    validate_paths(input_root, output_root)

    config = load_config(args.config.expanduser().resolve() if args.config else None)
    if args.workers is not None:
        config["processing"]["num_workers"] = args.workers
    if args.overwrite:
        config["output"]["overwrite"] = True
    validate_config(config)

    output_root.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(output_root / "logs" / "preprocessing.log", args.verbose)
    started_at = utc_now_iso()
    logger.info("script_version=%s", SCRIPT_VERSION)
    logger.info("input_root=%s", input_root)
    logger.info("output_root=%s", output_root)

    extensions = {
        str(extension).casefold()
        for extension in config["input"]["supported_extensions"]
    }
    image_paths = discover_images(input_root, extensions)
    logger.info("扫描到 %d 个受支持图片文件", len(image_paths))

    records: list[Optional[ImageRecord]] = [None] * len(image_paths)
    errors: list[ErrorRecord] = []
    workers = int(config["processing"]["num_workers"])

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-worker") as executor:
        future_map = {
            executor.submit(
                process_single_image,
                path,
                input_root,
                output_root,
                config,
                logger,
            ): index
            for index, path in enumerate(image_paths)
        }
        progress = tqdm(total=len(image_paths), desc="Processing", unit="image")
        try:
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    record, error = future.result()
                except Exception as exc:  # 最后一层线程级防线。
                    relative = image_paths[index].relative_to(input_root)
                    record = ImageRecord(
                        source_path=str(image_paths[index]),
                        relative_path=normalize_path(relative),
                        filename=image_paths[index].name,
                        file_extension=image_paths[index].suffix.lower(),
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        processed_at=utc_now_iso(),
                    )
                    error = ErrorRecord(
                        relative_path=record.relative_path,
                        stage="worker_failure",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        traceback=traceback.format_exc(limit=12),
                    )
                    logger.exception("线程级异常：%s", relative)
                records[index] = record
                if error is not None:
                    errors.append(error)
                progress.update(1)
        finally:
            progress.close()

    final_records = [record for record in records if record is not None]
    summary = write_outputs(output_root, final_records, errors, config, started_at)
    logger.info("处理完成：%s", json.dumps(summary["counts"], ensure_ascii=False))
    print(json.dumps(summary["counts"], ensure_ascii=False, indent=2))
    print(f"输出目录：{output_root}")
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        raise SystemExit(130)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"配置或路径错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
    except UnidentifiedImageError as exc:
        print(f"无法识别图片：{exc}", file=sys.stderr)
        raise SystemExit(2)
