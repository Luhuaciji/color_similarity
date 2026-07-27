#!/usr/bin/env python3
"""按品牌和商品目录批量下载 CSV 中的商品图片。

目录结构：

    输出目录/
      品牌名/
        sku_concat_name（为空时使用 sku_name）/
          pic_list_001_<URL哈希>.jpg
          show_pic_001_<URL哈希>.gif

脚本只使用 Python 标准库，无需安装第三方依赖。
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_CSV = (
    Path(__file__).resolve().parent
    / "data"
    / "dim_pub_sku_20260513_115554_口红唇膏唇蜜唇釉.csv"
)
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "downloaded_images"
IMAGE_FIELDS = ("pic_list", "show_pic")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
USER_AGENT = "Mozilla/5.0 (compatible; ProductImageDownloader/1.0)"


@dataclass(frozen=True)
class DownloadTask:
    row_number: int
    sku_id: str
    brand_name: str
    product_name: str
    source_field: str
    image_index: int
    url: str
    target_path: Path


@dataclass(frozen=True)
class Failure:
    row_number: int
    sku_id: str
    brand_name: str
    product_name: str
    source_field: str
    image_index: int | str
    url: str
    target_path: str
    error: str


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是大于或等于 0 的整数")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的数字")
    return number


def sanitize_component(value: str, fallback: str, max_length: int = 120) -> str:
    """生成可安全用作 Windows/macOS/Linux 目录名的单个路径组件。"""
    original = unicodedata.normalize("NFC", (value or "").strip())
    cleaned = INVALID_FILENAME_CHARS.sub("_", original)
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(" .")
    if not cleaned:
        cleaned = fallback

    # Windows 保留名称即使带扩展名也不可直接使用。
    if cleaned.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        digest = hashlib.sha1(original.encode("utf-8")).hexdigest()[:10]
        cleaned = f"{cleaned[: max_length - 11].rstrip()}_{digest}"
    return cleaned


def parse_image_urls(raw_value: str | None) -> list[str]:
    """解析 JSON/Python 列表形式的图片 URL，也兼容单个 URL 字符串。"""
    text = (raw_value or "").strip()
    if not text or text.lower() in {"null", "none", "nan"}:
        return []

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, ValueError) as exc:
            raise ValueError("不是有效的 JSON 图片数组") from exc

    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"应为图片数组，实际类型为 {type(value).__name__}")

    urls: list[str] = []
    for item in value:
        # 兼容 [{"url": "..."}] 这种常见结构。
        if isinstance(item, dict):
            item = item.get("url") or item.get("src")
        if item is None:
            continue
        url = str(item).strip()
        if url:
            urls.append(url)
    return urls


def url_extension(url: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    return ".jpg"


def output_filename(source_field: str, image_index: int, url: str) -> str:
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{source_field}_{image_index:03d}_{url_hash}{url_extension(url)}"


def set_large_csv_field_limit() -> None:
    """允许读取含很长图片列表或详情文本的 CSV 字段。"""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def build_tasks(
    csv_path: Path,
    output_dir: Path,
    encoding: str,
    max_rows: int | None,
) -> tuple[list[DownloadTask], list[Failure], int]:
    set_large_csv_field_limit()
    tasks_by_target: dict[Path, DownloadTask] = {}
    failures: list[Failure] = []
    row_count = 0

    with csv_path.open("r", encoding=encoding, newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        fieldnames = set(reader.fieldnames or [])
        missing = [field for field in IMAGE_FIELDS if field not in fieldnames]
        if missing:
            raise ValueError(f"CSV 缺少必要列：{', '.join(missing)}")

        for row_number, row in enumerate(reader, start=2):
            if max_rows is not None and row_count >= max_rows:
                break
            row_count += 1

            sku_id = (row.get("sku_id") or "").strip()
            raw_brand = (row.get("brand_name") or "").strip()
            raw_product = (
                (row.get("sku_concat_name") or "").strip()
                or (row.get("sku_name") or "").strip()
            )
            brand_name = raw_brand or "未知品牌"
            product_name = raw_product or f"未命名商品_{sku_id or row_number}"
            brand_dir = sanitize_component(brand_name, "未知品牌", max_length=80)
            product_dir = sanitize_component(
                product_name, f"未命名商品_{sku_id or row_number}"
            )
            destination = output_dir / brand_dir / product_dir

            for source_field in IMAGE_FIELDS:
                try:
                    urls = parse_image_urls(row.get(source_field))
                except ValueError as exc:
                    failures.append(
                        Failure(
                            row_number=row_number,
                            sku_id=sku_id,
                            brand_name=brand_name,
                            product_name=product_name,
                            source_field=source_field,
                            image_index="",
                            url="",
                            target_path=str(destination),
                            error=f"字段解析失败：{exc}",
                        )
                    )
                    continue

                for image_index, url in enumerate(urls, start=1):
                    scheme = urlsplit(url).scheme.lower()
                    target_path = destination / output_filename(
                        source_field, image_index, url
                    )
                    if scheme not in {"http", "https"}:
                        failures.append(
                            Failure(
                                row_number=row_number,
                                sku_id=sku_id,
                                brand_name=brand_name,
                                product_name=product_name,
                                source_field=source_field,
                                image_index=image_index,
                                url=url,
                                target_path=str(target_path),
                                error=f"不支持的 URL 协议：{scheme or '空'}",
                            )
                        )
                        continue

                    task = DownloadTask(
                        row_number=row_number,
                        sku_id=sku_id,
                        brand_name=brand_name,
                        product_name=product_name,
                        source_field=source_field,
                        image_index=image_index,
                        url=url,
                        target_path=target_path,
                    )
                    # 相同商品目录、字段、序号和 URL 只下载一次。
                    tasks_by_target.setdefault(target_path, task)

    return list(tasks_by_target.values()), failures, row_count


def download_one(
    task: DownloadTask,
    timeout: float,
    retries: int,
    overwrite: bool,
) -> tuple[str, str]:
    """返回 (状态, 错误信息)，状态为 downloaded/skipped/failed。"""
    target = task.target_path
    if not overwrite and target.is_file() and target.stat().st_size > 0:
        return "skipped", ""

    target.parent.mkdir(parents=True, exist_ok=True)
    part_path = target.with_name(f"{target.name}.{os.getpid()}.part")
    last_error = "未知错误"

    for attempt in range(retries + 1):
        try:
            request = Request(
                task.url,
                headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"},
            )
            with urlopen(request, timeout=timeout) as response, part_path.open(
                "wb"
            ) as output_file:
                while chunk := response.read(1024 * 256):
                    output_file.write(chunk)

            if part_path.stat().st_size == 0:
                raise OSError("服务器返回了空文件")
            os.replace(part_path, target)
            return "downloaded", ""
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            try:
                part_path.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < retries:
                time.sleep(min(1.5 * (2**attempt), 8.0))

    return "failed", last_error


def failure_from_task(task: DownloadTask, error: str) -> Failure:
    return Failure(
        row_number=task.row_number,
        sku_id=task.sku_id,
        brand_name=task.brand_name,
        product_name=task.product_name,
        source_field=task.source_field,
        image_index=task.image_index,
        url=task.url,
        target_path=str(task.target_path),
        error=error,
    )


def write_failure_report(output_dir: Path, failures: Iterable[Failure]) -> Path:
    report_path = output_dir / "download_failures.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8-sig", newline="") as report_file:
        fieldnames = [
            "row_number",
            "sku_id",
            "brand_name",
            "product_name",
            "source_field",
            "image_index",
            "url",
            "target_path",
            "error",
        ]
        writer = csv.DictWriter(report_file, fieldnames=fieldnames)
        writer.writeheader()
        for failure in failures:
            writer.writerow(
                {
                    field: getattr(failure, field)
                    for field in fieldnames
                }
            )
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下载 CSV 的 pic_list/show_pic 图片，并按品牌和商品分类。"
    )
    parser.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=DEFAULT_CSV,
        help=f"输入 CSV 路径（默认：{DEFAULT_CSV}）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"图片输出目录（默认：{DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=8,
        help="并发下载数（默认：8）",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=30.0,
        help="单次请求超时秒数（默认：30）",
    )
    parser.add_argument(
        "--retries",
        type=non_negative_int,
        default=2,
        help="下载失败后的重试次数（默认：2）",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="CSV 编码（默认：utf-8-sig）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="重新下载并覆盖已有的非空图片",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析并展示计划，不创建目录或下载图片",
    )
    parser.add_argument(
        "--max-rows",
        type=positive_int,
        help="最多处理前 N 行，适合小批量试运行",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = args.csv_path.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not csv_path.is_file():
        print(f"错误：找不到 CSV 文件：{csv_path}", file=sys.stderr)
        return 2

    try:
        tasks, failures, row_count = build_tasks(
            csv_path=csv_path,
            output_dir=output_dir,
            encoding=args.encoding,
            max_rows=args.max_rows,
        )
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        print(f"错误：无法读取 CSV：{exc}", file=sys.stderr)
        return 2

    print(f"CSV：{csv_path}")
    print(f"输出目录：{output_dir}")
    print(f"已解析商品行数：{row_count}")
    print(f"待处理图片数（去重后）：{len(tasks)}")
    if failures:
        print(f"解析阶段发现问题：{len(failures)}")

    if args.dry_run:
        print("\n预览前 5 个目标文件：")
        for task in tasks[:5]:
            print(f"  {task.target_path}")
        print("\n这是 dry-run，未创建目录、未下载文件。")
        return 1 if failures else 0

    downloaded = 0
    skipped = 0
    total = len(tasks)
    progress_step = max(1, min(100, total // 20 or 1))

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_task = {
            executor.submit(
                download_one,
                task,
                args.timeout,
                args.retries,
                args.overwrite,
            ): task
            for task in tasks
        }
        for completed, future in enumerate(as_completed(future_to_task), start=1):
            task = future_to_task[future]
            try:
                status, error = future.result()
            except Exception as exc:  # 防止单个线程异常终止整个批次
                status = "failed"
                error = f"未预期错误 {type(exc).__name__}: {exc}"

            if status == "downloaded":
                downloaded += 1
            elif status == "skipped":
                skipped += 1
            else:
                failures.append(failure_from_task(task, error))

            if completed % progress_step == 0 or completed == total:
                print(
                    f"进度：{completed}/{total} | "
                    f"已下载 {downloaded} | 已跳过 {skipped} | 失败 {len(failures)}"
                )

    report_path = write_failure_report(output_dir, failures)
    print("\n处理完成：")
    print(f"  下载成功：{downloaded}")
    print(f"  已有文件跳过：{skipped}")
    print(f"  失败：{len(failures)}")
    print(f"  失败清单：{report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
