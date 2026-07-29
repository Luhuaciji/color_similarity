"""Deterministic local representative-colour extraction for Stage 2.6."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image


ALGORITHM_VERSION = "stage2.6-lab-kmeans-medoid-1.0"


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert uint8/float sRGB values to CIE Lab (D65)."""

    values = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(
        values <= 0.04045,
        values / 12.92,
        ((values + 0.055) / 1.055) ** 2.4,
    )
    xyz = linear @ np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float64,
    ).T
    xyz /= np.array([0.95047, 1.0, 1.08883], dtype=np.float64)
    delta = 6.0 / 29.0
    transformed = np.where(
        xyz > delta**3,
        np.cbrt(xyz),
        xyz / (3 * delta**2) + 4.0 / 29.0,
    )
    lab = np.empty_like(transformed)
    lab[..., 0] = 116 * transformed[..., 1] - 16
    lab[..., 1] = 500 * (
        transformed[..., 0] - transformed[..., 1]
    )
    lab[..., 2] = 200 * (
        transformed[..., 1] - transformed[..., 2]
    )
    return lab


def _deterministic_kmeans(
    lab: np.ndarray,
    *,
    seed: int,
    maximum_clusters: int,
    iterations: int,
) -> tuple[np.ndarray, np.ndarray]:
    unique = np.unique(lab, axis=0)
    cluster_count = min(maximum_clusters, len(unique))
    if cluster_count <= 1:
        return np.zeros(len(lab), dtype=np.int32), unique[:1].copy()
    rng = np.random.default_rng(seed)
    first = int(rng.integers(0, len(lab)))
    centers = [lab[first]]
    for _ in range(1, cluster_count):
        distances = np.min(
            np.sum(
                (lab[:, None, :] - np.asarray(centers)[None, :, :]) ** 2,
                axis=2,
            ),
            axis=1,
        )
        total = float(distances.sum())
        if total <= 0:
            remaining = [
                row
                for row in unique
                if not any(np.array_equal(row, center) for center in centers)
            ]
            centers.append(remaining[0])
            continue
        threshold = float(rng.random()) * total
        index = int(np.searchsorted(np.cumsum(distances), threshold, side="right"))
        centers.append(lab[min(index, len(lab) - 1)])
    centroids = np.asarray(centers, dtype=np.float64)
    labels = np.zeros(len(lab), dtype=np.int32)
    for _ in range(iterations):
        distances = np.sum(
            (lab[:, None, :] - centroids[None, :, :]) ** 2,
            axis=2,
        )
        next_labels = np.argmin(distances, axis=1).astype(np.int32)
        next_centroids = centroids.copy()
        for cluster in range(cluster_count):
            members = lab[next_labels == cluster]
            if len(members):
                next_centroids[cluster] = members.mean(axis=0)
            else:
                nearest = np.min(distances, axis=1)
                next_centroids[cluster] = lab[int(np.argmax(nearest))]
        converged = np.array_equal(next_labels, labels) and np.allclose(
            next_centroids,
            centroids,
            atol=1e-10,
        )
        labels, centroids = next_labels, next_centroids
        if converged:
            break
    return labels, centroids


def _shrunken_box(
    bbox: Sequence[float],
    width: int,
    height: int,
    fraction: float,
) -> tuple[int, int, int, int]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain four coordinates")
    x0, y0, x1, y1 = (float(item) for item in bbox)
    x0, x1 = max(0.0, x0), min(float(width), x1)
    y0, y1 = max(0.0, y0), min(float(height), y1)
    if not (x0 < x1 and y0 < y1):
        raise ValueError("bbox must overlap the image with positive area")
    dx = (x1 - x0) * fraction
    dy = (y1 - y0) * fraction
    left = max(0, min(width - 1, math.floor(x0 + dx)))
    top = max(0, min(height - 1, math.floor(y0 + dy)))
    right = max(left + 1, min(width, math.ceil(x1 - dx)))
    bottom = max(top + 1, min(height, math.ceil(y1 - dy)))
    return left, top, right, bottom


def extract_observed_color(
    working_path: Path,
    *,
    bbox_image: Sequence[float],
    region_type: str,
    alpha_path: Path | None = None,
    seed: int = 260,
    max_clusters: int = 3,
    iterations: int = 15,
    shrink_fraction: float = 0.03,
    max_long_edge: int = 256,
    minimum_valid_pixels: int = 300,
    minimum_valid_ratio: float = 0.05,
) -> dict[str, Any]:
    """Extract a traceable image-observed colour candidate from a source crop."""

    diagnostics: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "seed": seed,
        "maximum_clusters": max_clusters,
        "iterations": iterations,
        "shrink_fraction": shrink_fraction,
        "max_long_edge": max_long_edge,
        "filters": {
            "alpha_lte": 16,
            "near_white_all_channels_gte": 245,
            "near_black_all_channels_lte": 8,
        },
    }
    try:
        with Image.open(working_path) as opened:
            working = opened.convert("RGB")
        crop_box = _shrunken_box(
            bbox_image,
            working.width,
            working.height,
            shrink_fraction,
        )
        crop = working.crop(crop_box)
        alpha_crop: Image.Image | None = None
        if alpha_path is not None:
            with Image.open(alpha_path) as opened_alpha:
                alpha = opened_alpha.convert("L")
            if alpha.size != working.size:
                raise ValueError("alpha asset dimensions differ from working asset")
            alpha_crop = alpha.crop(crop_box)
        scale = min(1.0, max_long_edge / max(crop.size))
        target = (
            max(1, round(crop.width * scale)),
            max(1, round(crop.height * scale)),
        )
        if target != crop.size:
            crop = crop.resize(target, Image.Resampling.LANCZOS)
            if alpha_crop is not None:
                alpha_crop = alpha_crop.resize(target, Image.Resampling.NEAREST)
        pixels = np.asarray(crop, dtype=np.uint8).reshape(-1, 3)
        valid = ~(
            np.all(pixels >= 245, axis=1)
            | np.all(pixels <= 8, axis=1)
        )
        if alpha_crop is not None:
            valid &= np.asarray(alpha_crop, dtype=np.uint8).reshape(-1) > 16
        valid_pixels = pixels[valid]
        valid_count = int(len(valid_pixels))
        valid_ratio = valid_count / max(1, len(pixels))
        diagnostics.update(
            {
                "source_bbox_image": [float(item) for item in bbox_image],
                "crop_bbox_image": list(crop_box),
                "resampled_size": list(target),
                "resampled_pixel_count": int(len(pixels)),
                "valid_pixel_count": valid_count,
                "valid_pixel_ratio": valid_ratio,
            }
        )
        if (
            valid_count < minimum_valid_pixels
            or valid_ratio < minimum_valid_ratio
        ):
            return {
                "status": "insufficient_pixels",
                "output_semantics": "image_observed_color_candidate",
                "valid_pixel_count": valid_count,
                "valid_pixel_ratio": valid_ratio,
                "diagnostics": diagnostics,
                "risks": ["insufficient_valid_pixels"],
            }

        lab = srgb_to_lab(valid_pixels)
        labels, centroids = _deterministic_kmeans(
            lab,
            seed=seed,
            maximum_clusters=max_clusters,
            iterations=iterations,
        )
        cluster_count = len(centroids)
        counts = np.bincount(labels, minlength=cluster_count)
        proportions = counts / valid_count
        if region_type == "lip":
            eligible_clusters = np.flatnonzero(proportions >= 0.15)
            if not len(eligible_clusters):
                eligible_clusters = np.array([int(np.argmax(proportions))])
            chroma = np.linalg.norm(centroids[:, 1:3], axis=1)
            selected = int(
                eligible_clusters[
                    np.argmax(chroma[eligible_clusters])
                ]
            )
            selection_rule = "highest_chroma_among_clusters_gte_15_percent"
        else:
            selected = int(np.argmax(counts))
            selection_rule = "largest_valid_cluster"
        member_indexes = np.flatnonzero(labels == selected)
        member_lab = lab[member_indexes]
        centroid = centroids[selected]
        squared = np.sum((member_lab - centroid) ** 2, axis=1)
        medoid_member_index = int(np.argmin(squared))
        source_index = int(member_indexes[medoid_member_index])
        representative_rgb = valid_pixels[source_index]
        representative_lab = lab[source_index]
        dispersion = float(
            np.sqrt(np.mean(np.sum((member_lab - centroid) ** 2, axis=1)))
        )
        dominance = float(proportions[selected])
        risks: list[str] = []
        if dominance < 0.5 or dispersion > 15.0:
            risks.append("mixed_region_low_confidence")
            color_confidence = "low"
        elif dominance < 0.7 or dispersion > 9.0:
            color_confidence = "medium"
        else:
            color_confidence = "high"
        rgb_list = [int(item) for item in representative_rgb]
        diagnostics.update(
            {
                "cluster_count": cluster_count,
                "cluster_counts": [int(item) for item in counts],
                "cluster_proportions": [
                    float(item) for item in proportions
                ],
                "selected_cluster": selected,
                "selection_rule": selection_rule,
                "selected_centroid_lab": [
                    float(item) for item in centroid
                ],
                "representative_rule": (
                    "actual_pixel_nearest_cluster_centroid_"
                    "squared_euclidean_medoid"
                ),
            }
        )
        return {
            "status": "succeeded",
            "output_semantics": "image_observed_color_candidate",
            "hex": "#{:02X}{:02X}{:02X}".format(*rgb_list),
            "rgb": rgb_list,
            "lab": [float(item) for item in representative_lab],
            "valid_pixel_count": valid_count,
            "valid_pixel_ratio": valid_ratio,
            "cluster_proportion": dominance,
            "dispersion": dispersion,
            "color_confidence": color_confidence,
            "risks": risks,
            "diagnostics": diagnostics,
        }
    except Exception as error:
        diagnostics["error_type"] = type(error).__name__
        diagnostics["error_message"] = str(error)
        return {
            "status": "failed",
            "output_semantics": "image_observed_color_candidate",
            "diagnostics": diagnostics,
            "risks": ["local_color_extraction_failed"],
        }
