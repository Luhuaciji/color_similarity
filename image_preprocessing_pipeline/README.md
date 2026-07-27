# 商品图片基础预处理流水线

该脚本面向以下目录结构：

```text
downloaded_images/
├─ 品牌A/
│  ├─ 商品a/
│  │  ├─ 图片1.jpg
│  │  └─ 图片2.png
│  └─ 商品b/
└─ 品牌B/
```

它只执行不会主动“美化”或校正商品颜色的基础处理：原图副本、EXIF 方向修正、ICC 检查与 sRGB 转换、透明区域处理、哈希去重标记和轻量质量统计。脚本不会执行自动白平衡、曝光、对比度、饱和度、Gamma、锐化、滤镜、去雾或颜色归一化。

## 1. 文件说明

```text
image_preprocessing_pipeline/
├─ preprocess_product_images.py  # 主脚本
├─ config.yaml                   # 推荐配置
├─ requirements.txt              # 运行依赖
└─ README.md
```

## 2. 安装依赖

建议使用独立的 Python 3.10–3.13 环境：

```bash
python -m pip install -r requirements.txt
```

OpenCV 只用于计算 Laplacian 模糊指标。即使 OpenCV 导入失败，脚本也会自动使用 NumPy 降级实现。

## 3. 运行

当脚本目录和 `downloaded_images` 位于同一级目录时：

```bash
python preprocess_product_images.py \
  --input downloaded_images \
  --output image_preprocessing_output \
  --config config.yaml
```

Windows PowerShell：

```powershell
python .\preprocess_product_images.py --input .\downloaded_images --output .\image_preprocessing_output --config .\config.yaml
```

使用绝对路径：

```powershell
python .\preprocess_product_images.py `
  --input "D:\lipstick_project\downloaded_images" `
  --output "D:\lipstick_project\image_preprocessing_output" `
  --config ".\config.yaml"
```

输出目录必须独立于输入目录，不能放在 `downloaded_images` 内部。

常用参数：

```text
--workers 8    临时覆盖 YAML 中的线程数
--overwrite    覆盖已有副本、工作图和 Alpha Mask
--verbose      在控制台显示 DEBUG 日志
```

如果有损坏图片，脚本仍会完成其余文件并生成报告，但进程退出码为 `2`，用于提示存在单图错误；这不表示整个批处理结果不可用。

## 4. 默认输出结构

```text
image_preprocessing_output/
├─ original_copies/                 # 原文件逐字节副本，镜像品牌/商品目录
├─ working_images/                  # 方向修正后的 8-bit RGB PNG
├─ alpha_masks/                     # 透明图的 0–255 Alpha Mask
├─ metadata/
│  ├─ image_preprocessing.csv
│  └─ image_preprocessing.jsonl
├─ errors/
│  └─ preprocessing_errors.csv
├─ logs/
│  └─ preprocessing.log
├─ duplicate_groups.json
├─ duplicate_pairs.csv
└─ preprocessing_summary.json
```

输出文件名包含原始 SHA256 的前 16 位，例如：

```text
品牌A/商品a/主图__4f47a12d08b8b451.png
```

因此相同目录内的同名不同格式图片不会互相覆盖，源文件内容发生变化时也会生成新的可追溯版本。

## 5. 原图保护与“在副本中处理”

默认配置 `output.copy_originals: true`。流程为：

```text
读取源文件并计算 SHA256
→ 创建 original_copies 中的逐字节副本
→ 校验副本 SHA256
→ 从副本解码和执行后续处理
```

程序不会以写入模式打开输入文件，不会修改、重命名或删除 `downloaded_images` 中的任何文件。

## 6. ICC 与 sRGB 规则

`color_profile_status` 的主要取值：

| 状态 | 处理方式 |
|---|---|
| `embedded_srgb` | 不重复进行颜色数值转换，只统一为 RGB 工作模式 |
| `embedded_non_srgb` | 使用 Pillow/ImageCms（LittleCMS）转换到 sRGB |
| `profile_missing` | 暂按 sRGB 解释，标记 `working_color_space=assumed_sRGB` |
| `profile_invalid` | 降级生成工作图，标记低可信并要求人工复核 |

无 ICC 不等于“原图一定是 sRGB”。脚本只做可追溯的工作假设。

CMYK 且无有效 ICC 时，会使用 Pillow 的 CMYK→RGB 降级转换，同时标记：

```text
source_color_space = unknown_cmyk
color_profile_review_required = true
quality_warning 包含 cmyk_without_icc
```

## 7. 透明图片

带透明通道的 PNG/WebP 会生成两份输出：

1. `working_images`：RGB PNG，透明区域仅为便于显示而合成到白色背景；
2. `alpha_masks`：保留原始 0–255 Alpha 值。

后续代表色提取应读取 Alpha Mask，并排除 Alpha 为 0 的像素。不要直接从显示用 RGB 工作图中把透明区域的白色当作商品颜色。

建议下游逻辑：

```python
valid_pixels = alpha_mask > 0
product_rgb = working_rgb[valid_pixels]
```

## 8. 去重输出

脚本不会自动删除图片。

- `sha256`：原始文件字节完全一致；
- `phash`：64-bit DCT 感知哈希；
- `dhash`：64-bit 相邻差分哈希；
- `duplicate_pairs.csv`：直接满足阈值的图片对；
- `duplicate_groups.json`：完全重复组和 pHash 连通分组。

默认阈值：

```text
pHash distance <= 4：高度疑似重复
pHash distance 5–8：可能重复
```

pHash 对纯色图、结构非常简单的图可能产生误报；阈值必须根据真实商品数据抽样验证。分组采用连通分量，同组任意两张图片不保证都直接满足阈值，应以 `duplicate_pairs.csv` 为准。

## 9. 质量指标

主要字段：

```text
source_width / source_height
working_width / working_height
aspect_ratio / megapixels / file_size
blur_score
mean_brightness
dark_pixel_ratio
bright_pixel_ratio
transparent_pixel_ratio
quality_warning
```

这些指标仅用于初筛和后续图片权重计算。脚本不会因为低分辨率、模糊、过暗或过亮自动删除图片，也不会自动增强图片。

`blur_warning_threshold` 与分辨率阈值应在真实数据上校准，不应直接作为论文或数据库的最终质量判定标准。

## 10. 幂等性

默认 `overwrite: false`。再次运行时：

- 原图副本和工作图若已存在则复用；
- 元数据、重复报告、错误报告和汇总会重新生成；
- 不会反复生成无穷嵌套目录；
- 工作图路径由“原相对路径 + SHA256”稳定确定。

修改了处理代码或关键配置后，建议更新：

```yaml
processing:
  processing_version: "1.2.0"
```

并使用新的输出目录，或显式增加 `--overwrite`。

## 11. 后续颜色提取注意事项

基础预处理只统一图片的解释方式，不解决以下差异：

- 拍摄光源和白平衡；
- 商家滤镜与后期修图；
- 屏幕截图或拼图中的非商品区域；
- 唇部试色、膏体、包装和色卡之间的语义差异；
- 同一商品多图的可靠性权重。

后续代表色模块仍应包含图片分类、目标区域分割、文字/色卡识别、透明 Mask 排除和多图鲁棒融合。

## 12. 运行测试

安装开发依赖后运行：

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

当前测试覆盖：无 ICC RGB、透明图与 Alpha Mask、EXIF 旋转、SHA256 完全重复、损坏图片不中断批处理，以及重复运行的幂等性。
