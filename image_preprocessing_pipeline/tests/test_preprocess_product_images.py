from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import preprocess_product_images as pipeline


def read_metadata(output_root: Path) -> list[dict[str, str]]:
    with (output_root / "metadata" / "image_preprocessing.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as handle:
        return list(csv.DictReader(handle))


def test_pipeline_core_behaviors(tmp_path: Path) -> None:
    input_root = tmp_path / "downloaded_images"
    product = input_root / "品牌A" / "商品A"
    product.mkdir(parents=True)

    rgb_path = product / "main.jpg"
    Image.new("RGB", (80, 120), (180, 30, 70)).save(rgb_path)
    shutil.copy2(rgb_path, product / "main_copy.jpg")

    rgba = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    rgba.paste((200, 50, 100, 255), (20, 20, 80, 80))
    rgba.save(product / "transparent.png")

    rotated = Image.new("RGB", (40, 90), (30, 150, 80))
    exif = rotated.getexif()
    exif[274] = 6
    rotated.save(product / "rotated.jpg", exif=exif)

    output_root = tmp_path / "output"
    exit_code = pipeline.main(
        ["--input", str(input_root), "--output", str(output_root), "--workers", "1"]
    )
    assert exit_code == 0

    rows = read_metadata(output_root)
    by_name = {row["filename"]: row for row in rows}
    assert by_name["main.jpg"]["color_profile_status"] == "profile_missing"
    assert by_name["main.jpg"]["working_color_space"] == "assumed_sRGB"
    assert by_name["transparent.png"]["has_alpha"] == "True"
    assert by_name["transparent.png"]["alpha_mask_path"]
    assert by_name["rotated.jpg"]["orientation_corrected"] == "True"
    assert by_name["rotated.jpg"]["working_width"] == "90"
    assert by_name["rotated.jpg"]["working_height"] == "40"
    assert by_name["main.jpg"]["exact_group_id"]
    assert by_name["main_copy.jpg"]["exact_group_id"] == by_name["main.jpg"]["exact_group_id"]

    with (output_root / "preprocessing_summary.json").open("r", encoding="utf-8") as handle:
        summary = json.load(handle)
    assert summary["counts"]["successfully_processed"] == 4
    assert summary["counts"]["exact_duplicate_groups"] == 1

    # 同一输入与配置重复运行时复用稳定路径，不反复创建新工作图。
    second_exit_code = pipeline.main(
        ["--input", str(input_root), "--output", str(output_root), "--workers", "1"]
    )
    assert second_exit_code == 0
    second_rows = read_metadata(output_root)
    assert all(row["working_image_reused"] == "True" for row in second_rows)


def test_corrupt_image_does_not_stop_batch(tmp_path: Path) -> None:
    input_root = tmp_path / "downloaded_images"
    product = input_root / "品牌A" / "商品A"
    product.mkdir(parents=True)
    Image.new("RGB", (64, 64), (100, 20, 20)).save(product / "valid.png")
    (product / "broken.jpg").write_bytes(b"not an image")

    output_root = tmp_path / "output"
    exit_code = pipeline.main(
        ["--input", str(input_root), "--output", str(output_root), "--workers", "1"]
    )
    assert exit_code == 2
    rows = read_metadata(output_root)
    assert sum(row["decode_success"] == "True" for row in rows) == 1
    assert sum(row["decode_success"] == "False" for row in rows) == 1
