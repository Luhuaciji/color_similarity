"""Deterministic colour-science primitives for observed-colour similarity."""

from __future__ import annotations

import math
import re
from typing import Sequence

import numpy as np


ALGORITHM_VERSION = "ciede2000-observed-similarity-1.0"
DISPLAY_SCORE_VERSION = "inverse-quadratic-d10-v1"
_HEX_PATTERN = re.compile(r"^#?([0-9A-F]{6})$")
_QUALITY_ORDER = {"medium": 0, "high": 1}


def normalize_hex(hex_value: str) -> str:
    """Validate and normalize a six-digit sRGB Hex value."""

    if not isinstance(hex_value, str):
        raise ValueError("Hex value must be a string")
    match = _HEX_PATTERN.fullmatch(hex_value.strip().upper())
    if match is None:
        raise ValueError("Hex value must contain exactly six hexadecimal digits")
    return f"#{match.group(1)}"


def hex_to_rgb(hex_value: str) -> tuple[int, int, int]:
    """Convert a validated Hex value to 8-bit sRGB."""

    normalized = normalize_hex(hex_value)
    return tuple(
        int(normalized[index : index + 2], 16)
        for index in (1, 3, 5)
    )


def lab_to_lch(
    lab: Sequence[float],
) -> tuple[float, float, float]:
    """Convert CIELAB to LCh with hue in the half-open [0, 360) range."""

    if len(lab) != 3:
        raise ValueError("Lab must contain exactly three components")
    lightness, a_value, b_value = (float(value) for value in lab)
    if not all(math.isfinite(value) for value in (lightness, a_value, b_value)):
        raise ValueError("Lab components must be finite")
    chroma = math.hypot(a_value, b_value)
    hue = math.degrees(math.atan2(b_value, a_value)) % 360.0
    return lightness, chroma, hue


def delta_e_ciede2000_array(
    lab_a: np.ndarray | Sequence[float],
    lab_b: np.ndarray | Sequence[float],
) -> np.ndarray:
    """Vectorized CIEDE2000 supporting NumPy broadcasting."""

    first = np.asarray(lab_a, dtype=np.float64)
    second = np.asarray(lab_b, dtype=np.float64)
    if first.shape[-1:] != (3,) or second.shape[-1:] != (3,):
        raise ValueError("CIEDE2000 inputs must end with three Lab components")
    if not np.all(np.isfinite(first)) or not np.all(np.isfinite(second)):
        raise ValueError("CIEDE2000 inputs must be finite")

    l1, a1, b1 = np.moveaxis(first, -1, 0)
    l2, a2, b2 = np.moveaxis(second, -1, 0)
    c1 = np.hypot(a1, b1)
    c2 = np.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0
    c_bar_seventh = c_bar**7
    g = 0.5 * (
        1.0
        - np.sqrt(c_bar_seventh / (c_bar_seventh + 25.0**7))
    )
    a1_prime = (1.0 + g) * a1
    a2_prime = (1.0 + g) * a2
    c1_prime = np.hypot(a1_prime, b1)
    c2_prime = np.hypot(a2_prime, b2)
    h1_prime = np.mod(np.degrees(np.arctan2(b1, a1_prime)), 360.0)
    h2_prime = np.mod(np.degrees(np.arctan2(b2, a2_prime)), 360.0)
    h1_prime = np.where(c1_prime == 0.0, 0.0, h1_prime)
    h2_prime = np.where(c2_prime == 0.0, 0.0, h2_prime)

    delta_l_prime = l2 - l1
    delta_c_prime = c2_prime - c1_prime
    raw_delta_h = h2_prime - h1_prime
    chroma_product = c1_prime * c2_prime
    delta_h_prime = np.where(
        chroma_product == 0.0,
        0.0,
        np.where(
            np.abs(raw_delta_h) <= 180.0,
            raw_delta_h,
            np.where(
                raw_delta_h > 180.0,
                raw_delta_h - 360.0,
                raw_delta_h + 360.0,
            ),
        ),
    )
    delta_big_h_prime = (
        2.0
        * np.sqrt(chroma_product)
        * np.sin(np.radians(delta_h_prime / 2.0))
    )

    l_bar_prime = (l1 + l2) / 2.0
    c_bar_prime = (c1_prime + c2_prime) / 2.0
    hue_sum = h1_prime + h2_prime
    h_bar_prime = np.where(
        chroma_product == 0.0,
        hue_sum,
        np.where(
            np.abs(h1_prime - h2_prime) <= 180.0,
            hue_sum / 2.0,
            np.where(
                hue_sum < 360.0,
                (hue_sum + 360.0) / 2.0,
                (hue_sum - 360.0) / 2.0,
            ),
        ),
    )
    t_value = (
        1.0
        - 0.17 * np.cos(np.radians(h_bar_prime - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * h_bar_prime))
        + 0.32 * np.cos(np.radians(3.0 * h_bar_prime + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * h_bar_prime - 63.0))
    )
    delta_theta = 30.0 * np.exp(
        -((h_bar_prime - 275.0) / 25.0) ** 2
    )
    c_bar_prime_seventh = c_bar_prime**7
    r_c = 2.0 * np.sqrt(
        c_bar_prime_seventh
        / (c_bar_prime_seventh + 25.0**7)
    )
    s_l = 1.0 + (
        0.015 * (l_bar_prime - 50.0) ** 2
        / np.sqrt(20.0 + (l_bar_prime - 50.0) ** 2)
    )
    s_c = 1.0 + 0.045 * c_bar_prime
    s_h = 1.0 + 0.015 * c_bar_prime * t_value
    r_t = -np.sin(np.radians(2.0 * delta_theta)) * r_c

    l_term = delta_l_prime / s_l
    c_term = delta_c_prime / s_c
    h_term = delta_big_h_prime / s_h
    result = np.sqrt(
        l_term**2
        + c_term**2
        + h_term**2
        + r_t * c_term * h_term
    )
    return np.asarray(result, dtype=np.float64)


def delta_e_ciede2000(
    lab_a: Sequence[float],
    lab_b: Sequence[float],
) -> float:
    """Calculate scalar CIEDE2000 distance."""

    result = float(delta_e_ciede2000_array(lab_a, lab_b))
    if result < 0.0 or not math.isfinite(result):
        raise ValueError("CIEDE2000 produced an invalid distance")
    return result


def delta_hue_degrees(hue_a: float, hue_b: float) -> float:
    """Return the shortest unsigned angular difference."""

    first = float(hue_a) % 360.0
    second = float(hue_b) % 360.0
    difference = abs(first - second)
    return min(difference, 360.0 - difference)


def delta_e_to_similarity(
    delta_e00: float,
    *,
    scale: float = 10.0,
) -> float:
    """Map distance monotonically to a non-probabilistic display score."""

    distance = float(delta_e00)
    scale_value = float(scale)
    if distance < 0.0 or not math.isfinite(distance):
        raise ValueError("delta_e00 must be finite and non-negative")
    if scale_value <= 0.0 or not math.isfinite(scale_value):
        raise ValueError("display score scale must be positive and finite")
    return 100.0 / (1.0 + (distance / scale_value) ** 2)


def classify_distance_band(delta_e00: float) -> str:
    """Classify an operational, explicitly uncalibrated distance band."""

    distance = float(delta_e00)
    if distance < 0.0 or not math.isfinite(distance):
        raise ValueError("delta_e00 must be finite and non-negative")
    if distance <= 2.0:
        return "de00_le_2"
    if distance <= 5.0:
        return "de00_gt_2_le_5"
    if distance <= 10.0:
        return "de00_gt_5_le_10"
    if distance <= 20.0:
        return "de00_gt_10_le_20"
    return "de00_gt_20"


def color_difference_diagnostics(
    lab_a: Sequence[float],
    lab_b: Sequence[float],
) -> tuple[float, float, float]:
    """Return absolute lightness, chroma, and hue-angle differences."""

    first = lab_to_lch(lab_a)
    second = lab_to_lch(lab_b)
    return (
        abs(first[0] - second[0]),
        abs(first[1] - second[1]),
        delta_hue_degrees(first[2], second[2]),
    )


def pair_quality_tier(first: str, second: str) -> str:
    """Return the conservative lower categorical quality."""

    if first not in _QUALITY_ORDER or second not in _QUALITY_ORDER:
        raise ValueError("pair quality requires high/medium profile qualities")
    return min((first, second), key=_QUALITY_ORDER.__getitem__)
