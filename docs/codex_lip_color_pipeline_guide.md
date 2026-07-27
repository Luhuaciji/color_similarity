# 口红/唇部彩妆图片理解、代表色提取与色号知识数据库构建指南

> 文档定位：供 Codex 在现有项目仓库和真实数据集上进行代码审计、架构设计、实现、测试和持续修订。
>
> 适用数据：多品牌、多产品、多图片的口红、唇膏、唇彩、唇蜜、唇釉等商品图片。每个商品文件夹包含若干图片，图片可能为膏体图、试色图、唇部效果图、多色号对比图、色卡、包装图、文字宣传图或无效图。
>
> 当前视觉大模型：`qwen3.6-plus`
>
> OpenAI 兼容 Base URL：`https://dashscope.aliyuncs.com/compatible-mode/v1`
>
> 完整请求端点：`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`
>
> 最新修订：2026-07-28；详细阶段输入、输出、字段、测试和验收以 `docs/implementation_plan.md` 为准

---

## 1. 总体目标

构建一条可复现、可审计、可扩展的处理流水线，从商品文件夹中的原始图片出发，完成：

1. 图片角色分类；
2. 判断图片是否适合提取商品代表色；
3. 对适合的图片定位有效颜色区域并提取代表色；
4. 检测并提取图片中的文字信息；
5. 从多色号对比图中识别多个色块、膏泥或试色区域，并与对应的色号文字建立空间关系；
6. 融合文件夹名称、图片内容、OCR、视觉大模型结果和像素颜色结果；
7. 建立品牌、商品、系列、色号、图片证据、代表色和描述属性之间的知识数据库；
8. 生成低置信度和冲突样本，供人工审核；
9. 将人工审核结果回流到规则、提示词、分类器和后续监督学习数据中。

目标流水线：

```text
商品文件夹
    │
    ├── 文件夹名称解析
    ├── 图片读取、去重、质量检测
    │
    ▼
图片角色分类
    │
    ├── 单色膏体图
    ├── 单色试色图
    ├── 唇部效果图
    ├── 多色号对比图
    ├── 色卡/色块图
    ├── 包装图
    ├── 文字宣传图
    └── 无效图
    │
    ├──────────────────────┐
    ▼                      ▼
代表色提取流水线           信息抽取流水线
    │                      │
区域检测与分割             OCR文字检测
    │                      │
颜色校正与像素过滤         视觉大模型结构化理解
    │                      │
Lab聚类与代表色计算         色号—名称—色块空间匹配
    │                      │
多图片颜色融合             文件夹名称与图片信息融合
    │                      │
置信度和异常检测           实体归一化与新色号发现
    └──────────┬───────────┘
               ▼
       商品色号知识数据库
               │
               ▼
       人工审核与持续学习
```

其中“图片角色分类/资格判断”和“文件夹名称与图片信息融合”必须分成两层：先做不含当前 SKU、folder 目标色号或 `context_shade` 的内容视觉分析，再在 occurrence/source context 层判断该图片与当前商品的关系。两层不能写入同一张混合语义表。

---

## 2. Codex 的工作方式

本指南是“可修订的规范”，不是不可修改的静态需求。Codex 必须先审计项目，再实施代码，并在真实证据支持下修订本指南。

### 2.1 开始编码前必须执行的审计

Codex 应先完成以下检查：

1. 扫描仓库目录结构、现有脚本、配置文件、数据库文件和历史输出；
2. 统计商品文件夹数量、图片总数、扩展名分布、图片尺寸分布、文件大小分布和损坏文件数量；
3. 抽样检查各品牌、各商品和各图片类型；
4. 检查是否已有 SHA256、pHash、EXIF、ICC、sRGB 转换、质量检测或错误日志；
5. 检查现有代码是否已经定义商品 ID、图片 ID、色号 ID 和输出目录；
6. 检查 API 调用封装、环境变量、重试、缓存、并发和费用统计是否已存在；
7. 查找历史运行中已知问题，例如截断图片、颜色配置文件异常、透明通道、超大图片或内容审核失败；
8. 输出一份仓库审计报告，再开始修改代码。

建议生成：

```text
docs/repository_audit.md
docs/data_profile.md
docs/implementation_plan.md
```

### 2.2 允许 Codex 修改本指南的条件

Codex 可以根据项目文件和真实数据修改本指南，但必须遵守以下规则：

- 不得静默修改核心标签语义、数据库主键、原始数据保留策略或审计要求；
- 所有实质性修改必须记录变更原因和证据；
- 如果真实项目已有成熟结构，应优先兼容现有结构，而不是机械重建；
- 如果数据分布与本文假设不符，应更新标签、阈值、字段或阶段划分；
- 如果某种方案在样本上效果差，可以替换，但必须保留对比实验或失败记录；
- 如果修改会破坏旧输出兼容性，必须提供迁移脚本或版本转换说明。

每次修订本指南时，在文末维护：

```text
## 变更记录
- 日期
- 修改章节
- 修改内容
- 证据来源
- 兼容性影响
- 修改人/代理
```

重大架构变更建议使用 ADR：

```text
docs/decisions/ADR-0001-xxx.md
```

### 2.3 不允许被取消的底线约束

以下约束不得因实现方便而删除：

1. 原始图片只读保存，不覆盖；
2. 所有派生结果可追溯到原始图片；
3. 所有模型结果记录模型名、提示词版本、参数、时间和错误；
4. 代表色不能只依赖视觉大模型直接生成的 HEX；
5. 多色号图必须保留“文字—色块—空间位置”证据；
6. 低置信度结果必须进入审核队列，不能强行写成确定事实；
7. 所有自动决策必须有置信度、规则版本或模型版本；
8. 同一输入和相同版本配置应尽可能得到一致结果；
9. 任何人工修改都必须留痕；
10. 数据库必须区分“原始证据”“模型推断”“融合结论”和“人工确认”。

---

## 3. 推荐项目结构

Codex 应优先适配现有仓库。如果仓库尚无明确结构，可参考：

```text
project_root/
├── README.md
├── pyproject.toml
├── requirements.txt
├── .env.example
├── configs/
│   ├── pipeline.yaml
│   ├── labels.yaml
│   ├── prompts/
│   │   ├── image_role_v1.txt
│   │   ├── image_understanding_v1.txt
│   │   └── multi_shade_matching_v1.txt
│   └── thresholds.yaml
├── src/
│   └── lipcolor_pipeline/
│       ├── cli.py
│       ├── config.py
│       ├── logging_utils.py
│       ├── ids.py
│       ├── inventory/
│       ├── preprocessing/
│       ├── classification/
│       ├── vlm/
│       ├── ocr/
│       ├── detection/
│       ├── segmentation/
│       ├── color/
│       ├── matching/
│       ├── fusion/
│       ├── database/
│       ├── review/
│       └── evaluation/
├── scripts/
│   ├── audit_dataset.py
│   ├── run_pipeline.py
│   ├── export_review_batch.py
│   └── migrate_database.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/
│   ├── repository_audit.md
│   ├── data_profile.md
│   ├── implementation_plan.md
│   └── decisions/
├── data/
│   ├── raw/                 # 原始图片，只读
│   ├── manifests/
│   ├── cache/
│   ├── interim/
│   ├── processed/
│   ├── review/
│   └── database/
└── outputs/
    ├── logs/
    ├── reports/
    ├── overlays/
    ├── color_patches/
    └── failed/
```

---

## 4. 核心数据对象与 ID 设计

所有处理阶段必须围绕稳定 ID 工作，禁止依赖易变的绝对路径作为唯一标识。

### 4.1 推荐 ID

- `brand_id`：标准化品牌 ID；
- `product_id`：商品级 ID；
- `shade_id`：色号级 ID；
- `dataset_snapshot_id`：输入 CSV/数据集快照 ID；
- `source_record_id`：源数据行级 ID；
- `source_ref_id`：源数据中的图片 URL 引用 ID；
- `folder_group_id`：原始品牌/商品目录分组 ID，不等同于最终商品 ID；
- `image_id`：图片内容级 ID；
- `image_occurrence_id`：图片物理路径/出现位置级 ID；
- `region_id`：图中区域级 ID；
- `ocr_span_id`：OCR 文本框 ID；
- `run_id`：流水线运行 ID；
- `model_run_id`：模型调用 ID；
- `review_task_id`：人工审核任务 ID。

建议：

```text
image_id = SHA256(原始文件字节)
image_occurrence_id = UUIDv5(dataset_snapshot_id + 规范化原始相对路径)
source_record_id = UUIDv5(dataset_snapshot_id + 源行号 + 源行哈希)
source_ref_id = UUIDv5(source_record_id + 图片字段 + 图片序号 + 规范化源 URL)
folder_group_id = UUIDv5(dataset_snapshot_id + 原始品牌/商品目录相对路径)
product_id = UUIDv5(规范品牌 ID + 实体归一化后的产品身份)
region_id = UUIDv5(image_id + 区域类型 + 坐标 + 算法版本)
```

同一字节完全一致的图片使用同一 `image_id`，但保留多个来源路径记录。

2026-07-27 首轮仓库审计确认，原始商品目录并不总是一行一商品或一行一色号：16 个目录合并了 48 个源 CSV 行，单目录最多对应 5 个 SKU；同一 `brand_id` 还可能对应多个品牌目录名。因此：

- 原始目录只能生成 `folder_group_id`，不能直接生成最终 `product_id` 或 `shade_id`；
- `product_id` 和 `shade_id` 只能在源记录、文件夹、OCR、图片和人工映射完成实体归一化后生成；
- 一个物理图片 occurrence 可以被多个 `source_ref_id` 引用；
- 现有预处理脚本生成的路径相关 `image_id` 必须迁移为 `legacy_image_id`，不得静默解释为内容 ID。

当前 CSV 与当前下载器能够重建当前 31,513 条 URL 引用到 31,511 个物理目标路径的关系，但下载器没有保存不可变成功 manifest。该可重建性依赖 CSV 快照、路径清洗/命名规则和文件集合保持不变，不能等同于长期稳定追溯。

### 4.2 Manifest 是流水线入口

第一阶段生成统一 manifest，至少包含：

```json
{
  "dataset_snapshot_id": "dataset-...",
  "source_record_ids": ["source-record-..."],
  "source_ref_ids": ["source-ref-..."],
  "image_id": "sha256...",
  "image_occurrence_id": "occurrence-...",
  "folder_group_id": "folder-group-...",
  "source_path": "品牌/商品/图片.jpg",
  "brand_folder": "品牌",
  "product_folder": "商品",
  "filename": "图片.jpg",
  "extension": ".jpg",
  "detected_format": "JPEG",
  "mime_type": "image/jpeg",
  "extension_mismatch": false,
  "byte_size": 123456,
  "width": 1200,
  "height": 1200,
  "exif_orientation": 1,
  "icc_profile_present": true,
  "decode_status": "ok",
  "sha256": "...",
  "phash": "...",
  "duplicate_group_id": null,
  "created_at": "ISO-8601"
}
```

上例是便于查看的 occurrence 级联表。正式存储应至少拆分为“数据集快照/源记录/源图片引用”“图片内容”“图片 occurrence”三层，并用多对多关系保留全部来源，不要把多个源 URL 或 SKU 压进一个不可查询的字符串字段。

阶段 1 强制使用 SQLite 保存关系/约束，并使用 JSONL 保存可审计 manifest。Parquet 和全量 CSV 镜像后置为可选导出，不作为阶段 1 门禁；如后续生成，必须能由同一 `run_id`、schema version 和 SQLite/JSONL 规范记录复现。

---

## 5. 图片读取、预处理、去重与质量检测

虽然本指南重点从图片角色分类开始，但后续结果依赖稳定的预处理输入，因此 Codex 必须复用或补全以下能力。

### 5.1 读取与容错

- 处理 EXIF Orientation；
- 尝试严格解码；
- 对 `image file is truncated` 等错误记录到错误表；
- 是否启用 Pillow 的截断容错必须配置化；
- 容错解码成功的图片要标记 `decode_recovered=true`；
- 不允许悄悄跳过错误文件；
- 记录文件扩展名、实际解码格式、MIME、颜色模式、透明通道和动画帧信息；
- 使用文件头和实际解码结果识别格式，不得只信扩展名；
- 扩展名和实际格式不一致时标记 `extension_mismatch=true`，但不得改名或覆盖原文件；
- 显式支持 GIF/多帧输入，并记录选帧策略；
- 将 `corrupt`、`policy_rejected` 和 `decode_recovered` 分开；
- Pillow 的安全像素阈值、自定义硬上限和“仅生成缩小分析副本”阈值必须使用一致、可测试的策略。

### 5.2 色彩空间

- 保留原图；
- 派生分析图统一为 sRGB；
- 有 ICC 时执行受控颜色转换；
- 无 ICC 时记录“按 sRGB 假设”；
- 透明图片需要定义背景合成策略，默认输出白底和棋盘格诊断版本；
- 不要为了“看起来更鲜艳”进行不可追溯的增强；
- 所有颜色计算必须明确基于哪个派生版本。

### 5.3 去重

- SHA256：识别字节完全相同图片；
- pHash/dHash：识别缩放、压缩、裁剪或轻度编辑的近重复图片；
- 只对完全重复图片直接复用模型结果；
- 近重复图片可共享候选结果，但必须保留独立质量和颜色分析；
- 不应仅因近似重复而删除可能含不同文字、裁剪或色彩变化的图片。

### 5.4 质量指标

至少记录：

- 分辨率；
- 长宽比；
- 模糊度；
- 过曝比例；
- 欠曝比例；
- 高光比例；
- 有效像素比例；
- 压缩伪影指标；
- 纯色边框或大面积白底比例；
- 图片是否过小；
- 图片是否严重截断；
- 扩展名与实际格式是否一致；
- 是否为极端长宽比、整页详情长图或装饰条；
- 长图全局缩略图、重叠 tile，以及 tile 到原图的坐标变换；
- 是否因安全/资源策略拒绝，而不是文件本身损坏。

质量分数不能直接决定代表色，只作为角色分类和代表色置信度的特征。

首轮真实数据中存在 1074×28190 的整页详情长图、750×1 的装饰条和 229 个扩展名/实际格式不一致文件。长图必须同时生成保留整体比例的全局缩略图和只读重叠 tile，不能只做 tile。必须保存全局布局/阅读轴、tile 原图坐标、重叠范围、双向坐标变换、跨 tile OCR 去重链和原图坐标回映；严格解码成功也不能自动等同于业务有效。

---

## 6. 图片角色分类

### 6.1 标签体系

推荐使用稳定英文代码和中文显示名：

| role_code | 中文名称 | 定义 |
|---|---|---|
| `single_bullet` | 单色膏体图 | 单一色号口红膏体、唇膏棒、膏体近景 |
| `single_swatch` | 单色试色图 | 单一色号在手臂、纸面、板面或其他基底上的试色 |
| `lip_effect` | 唇部效果图 | 真人或模型唇部上妆效果 |
| `multi_shade_comparison` | 多色号对比图 | 同图包含多个色号的膏体、膏泥、试色或唇部对比 |
| `color_card` | 色卡/色块图 | 规则排列的色块、色卡、色谱或数字色块 |
| `packaging` | 包装图 | 外壳、纸盒、瓶体、管体、品牌包装为主体 |
| `text_promo` | 文字宣传图 | 文字、卖点、成分、色号描述占主导 |
| `invalid` | 无效图 | 非目标商品、严重损坏、无法识别、纯装饰、无有效信息 |

### 6.2 主标签与辅助标签

真实图片可能同时具备多种属性，例如“多色号对比图 + 大量文字”。因此不要只保留一个字符串。

建议输出：

- `primary_role`：主角色；
- `secondary_roles`：辅助角色列表；
- `contains_text`；
- `contains_multiple_shades`；
- `contains_lips`；
- `contains_skin_swatch`；
- `contains_product_bullet`；
- `contains_packaging`；
- `representative_color_eligible`；
- `information_extraction_eligible`。

首轮真实数据还要求保留版式维度，但不改变上述八类核心角色语义：

- `layout_type`：例如 `single_panel`、`collage`、`grid`、`long_detail_strip`；
- `is_extreme_aspect_ratio`；
- `is_decorative_strip`；
- `global_layout`：长图全局缩略图上的整体布局和阅读顺序；
- `tile_role_results`：长图各重叠 tile/region 的角色、置信度和原图坐标。

`pic_list`、`show_pic`、SKU、商品目录和 `context_shade` 不得输入内容角色/资格分析。它们只能在后续 occurrence 来源上下文融合层作为弱证据，不能直接映射到角色或代表色资格。

### 6.3 内容视觉分析与来源上下文融合必须分层

#### A 层：`image_id`/tile 内容视觉分析

- 输入只包含 `image_id` 对应内容，或其版本化全局缩略图/重叠 tile；
- 不输入当前 SKU、folder、商品目录目标色号、`context_shade` 或 source record；
- 输出八类角色、布局、可见对象、`depicted_shades` 候选、代表色资格和候选区域；
- 同一 SHA256 在相同模型、提示词、参数和分析资产下只调用/缓存一次；
- 长图先用全局缩略图识别整体布局，再用重叠 tile 补充局部，最终回映原图。

#### B 层：`image_occurrence_id` 来源上下文融合

- 输入 A 层事实和 `image_occurrence_id`；
- 结合 folder、CSV 原始行、SKU、source ref 与 `context_shade`；
- 输出该图片与当前来源商品/色号的关系、`depicted_shades` 与 `context_shade` 的包含/冲突状态、置信度和审核状态；
- 同一内容的不同 occurrence/source context 可以有不同关系判断；
- 上下文或融合规则改变时只重算 B 层，不使 A 层缓存失效。

数据库分别使用 `content_visual_analyses` 和 `occurrence_context_fusions`。不得继续用一张 `image_roles` 写表同时保存 A/B 两层结果，也不得通过上下文暗示污染 A 层提示词。

### 6.4 视觉大模型结构化输出

`qwen3.6-plus` 负责：

- 图片角色分类；
- 场景和对象识别；
- 判断是否存在多个色号；
- 判断是否含文字；
- 估计可用于颜色提取的区域类型；
- 给出候选区域的归一化边界框；
- 识别遮挡、高光、滤镜、拼贴、文字覆盖等风险；
- 输出结构化理由和置信度。

视觉大模型不负责最终权威 HEX。其颜色名称或 HEX 仅作为语义参考和异常对照。

建议输出 JSON：

```json
{
  "schema_version": "1.0",
  "analysis_scope": "merged_content_summary",
  "input_context_policy": "image_only",
  "primary_role": "single_swatch",
  "secondary_roles": ["text_promo"],
  "role_confidence": 0.94,
  "contains_text": true,
  "contains_multiple_shades": false,
  "representative_color_eligible": true,
  "eligibility_confidence": 0.91,
  "candidate_color_regions": [
    {
      "region_type": "swatch",
      "bbox_norm": [0.18, 0.22, 0.74, 0.83],
      "confidence": 0.92,
      "risks": ["specular_highlight"]
    }
  ],
  "observed_objects": ["forearm", "lipstick swatch"],
  "quality_risks": ["warm lighting"],
  "reason": "图中主要区域为单一手臂试色，颜色面积较大且与背景可分离。"
}
```

### 6.5 提示词要求

提示词应：

- 明确定义每个角色；
- 要求只输出 JSON；
- 明确坐标格式为 `[x_min, y_min, x_max, y_max]`，范围 0–1；
- 要求不确定时降低置信度；
- 不允许将包装颜色当作膏体代表色；
- 不允许将文字背景色直接当作商品色；
- 内容视觉提示词禁止包含当前 SKU、folder 目标色号或 `context_shade`；
- 对多色号图必须报告色号数量估计和布局类型；
- 对唇部效果图必须报告皮肤、牙齿、高光、阴影和滤镜风险。

提示词必须版本化，例如：

```text
prompt_name=image_role
prompt_version=1.0.0
```

### 6.6 分类实现策略

第一版可以采用：

1. 规则预筛选；
2. `qwen3.6-plus` 结构化分类；
3. 本地轻量分类器或人工标注数据形成后再替换高频调用；
4. 大模型用于困难样本和抽样复核。

长期建议形成级联：

```text
快速本地分类器 → 高置信度直接通过
                  ↓低置信度
             qwen3.6-plus
                  ↓冲突/低置信度
               人工审核
```

---

## 7. 代表色提取资格判断

图片角色和“是否适合代表色提取”不是同一概念。

这里的资格首先是 A 层对图片自身可见内容的判断，不使用当前 SKU 或目录目标色号。图片是否能作为**当前**商品/色号的证据，由 B 层 occurrence 来源上下文融合另行判断。

### 7.1 通常优先级

一般优先级可设为：

```text
单色试色图 ≈ 单色膏体图 > 色卡/色块图 > 多色号对比图 > 唇部效果图
```

包装图和纯文字宣传图通常不参与代表色计算，但可用于文字和商品信息提取。

### 7.2 适合提取的条件

- 有明确、足够大的颜色区域；
- 颜色区域属于膏体、膏泥、试色或明确色块；
- 区域没有被大面积文字、反光、阴影或遮挡覆盖；
- 图片没有严重滤镜或色偏；
- 目标颜色不是包装、背景或装饰色；
- 多色号图中各颜色区域可分割并能与文字匹配；
- 唇部效果图中能较稳定分离唇部区域。

### 7.3 不适合或低权重的情况

- 包装颜色与膏体颜色混淆；
- 膏体面积过小；
- 高光覆盖膏体主体；
- 黑底或强色光环境；
- 大量美颜、滤镜、磨皮；
- 唇部边界不清晰；
- 手臂试色混有肤色且无法分离；
- 拼贴图被压缩或缩放严重；
- 色块只是设计元素，不是实际色号；
- 图中同一色号出现多个不一致版本。

### 7.4 资格输出

每张图片至少输出：

```json
{
  "eligible": true,
  "eligibility_score": 0.87,
  "recommended_strategy": "single_swatch_segmentation",
  "rejection_reasons": [],
  "weight_hint": 0.9
}
```

---

## 8. 区域检测与分割

### 8.1 设计原则

视觉大模型提供“在哪里”，像素算法负责“精确到哪些像素”。不要将粗边界框直接作为最终代表色区域。

### 8.2 按图片角色处理

#### 单色膏体图

- 检测膏体主体；
- 排除管体、底座、品牌文字和背景；
- 对圆柱或斜面膏体，排除高光带和极暗边缘；
- 优先取膏体中部、低高光、低阴影区域；
- 保存分割 mask 和可视化 overlay。

#### 单色试色图

- 检测试色块；
- 与皮肤或基底分离；
- 可利用局部色差、边缘和饱和度变化；
- 对渐变试色，记录中心色、深色端和浅色端；
- 最终代表色一般使用稳健中心或面积加权 medoid。

#### 唇部效果图

- 分割唇部；
- 排除皮肤、牙齿、口腔、强高光和阴影；
- 可进一步区分上唇、下唇和高光区；
- 因肤色、相机和滤镜影响较大，默认权重低于膏体/试色；
- 建议保留原始唇色估计风险字段。

#### 多色号对比图

- 检测每个独立膏泥、试色、膏体或色块；
- 给每个区域单独建立 `region_id`；
- 记录布局：网格、横向、纵向、环形、任意布局；
- 每个区域单独颜色计算；
- 再与 OCR 文本进行空间匹配。

#### 色卡/色块图

- 检测规则色块；
- 排除边框、文字、阴影和渐变背景；
- 检查色块是否为真实色号展示，而非装饰色；
- 可利用矩形检测、轮廓、网格和重复尺寸。

### 8.3 算法选择

第一版可组合：

- OpenCV 轮廓、颜色阈值、GrabCut、超像素；
- 基于 VLM 边界框的局部分割；
- SAM/SAM2 或其他分割模型；
- 对规则色块使用几何检测；
- 对唇部使用人脸关键点和唇部语义分割。

Codex 应通过小规模标注集比较方案，而不是预设某一个模型一定最好。

---

## 9. 色彩归一化、像素过滤与 image-observed 代表色计算

### 9.1 基本原则

- 代表色来自图像像素计算；
- 视觉大模型生成的 HEX 只能作为候选或一致性检查；
- 颜色计算默认在 sRGB 派生图上进行；
- 聚类和色差计算使用 CIE Lab；
- 所有结果保留原始 RGB、sRGB HEX、Lab 和算法版本；
- 数据库颜色的默认语义是 `image-observed representative color`，即特定图片、后期处理、照明和显示色彩空间下的像素观测代表色，不是未经校准的真实物理颜色。

### 9.2 不要进行无依据的全局白平衡

电商图经常经过后期处理。如果没有灰卡、色卡或可靠白色参考，不应自动进行强白平衡并把结果当作真实商品颜色。

建议保存：

- `raw_srgb_color`：统一 sRGB 后的结果；
- `normalized_color`：经过有版本、参数和证据的确定性标准化/归一化后的观测结果；
- `normalization_method`；
- `normalization_reference_id`；
- `normalization_evidence_json`；
- `normalization_confidence`；
- `color_semantics=image_observed_representative`。

不使用含义不清的 `corrected_*` 字段。只有存在可验证的色卡、设备和照明校准链时，才可另增 `calibrated_*` 字段及校准记录；`normalized_*` 不能被解释为 calibrated 或真实物理颜色。

### 9.3 像素过滤

按角色配置过滤规则：

- 排除透明像素；
- 排除近白背景；
- 排除近黑阴影；
- 排除极高亮高光；
- 排除边缘混合像素；
- 排除低置信度分割区域；
- 对裸色、棕色、灰调色不能使用过强的低饱和过滤；
- 对唇釉和亮面产品，不能把所有高亮像素删除，应保留主体色并单独记录光泽特征。

### 9.4 Lab 聚类

建议：

1. 将有效像素转换为 Lab；
2. 进行抽样以控制速度；
3. 使用 KMeans、GMM、HDBSCAN 或基于 ΔE00 的聚类；
4. 排除明显背景簇、高光簇和阴影簇；
5. 对剩余候选簇按面积、中心位置、饱和度稳定性和角色先验评分；
6. 选择 medoid 或稳健中心作为代表色；
7. 计算簇内离散度、有效像素数量和不确定性。

建议输出：

```json
{
  "color_semantics": "image_observed_representative",
  "representative_hex": "#A84F5B",
  "rgb": [168, 79, 91],
  "lab": [46.8, 37.2, 14.1],
  "method": "lab_kmeans_medoid_v1",
  "valid_pixel_count": 38542,
  "dominant_cluster_ratio": 0.72,
  "within_cluster_delta_e_p50": 2.7,
  "within_cluster_delta_e_p95": 7.8,
  "color_confidence": 0.88
}
```

### 9.5 代表色不是“出现面积最大的颜色”

最大面积颜色可能是：

- 白色背景；
- 黑色包装；
- 肤色；
- 阴影；
- 高光；
- 宣传图底色。

必须先由角色分类和区域分割确定语义目标，再计算颜色。

---

## 10. OCR 与文字信息提取

### 10.1 OCR 分层设计

建议同时保留：

1. OCR 引擎的原始文本框、坐标和置信度；
2. 视觉大模型对文本语义和版面关系的理解；
3. 归一化后的品牌、系列、色号编号、色号名称和描述属性。

可用 OCR 包括 PaddleOCR 等。本项目不应仅依赖视觉大模型“读图后口述”，因为需要精确坐标和可审计文本框。

### 10.2 OCR 原始记录

```json
{
  "ocr_span_id": "...",
  "image_id": "...",
  "text_raw": "N19 白桃生巧色",
  "text_normalized": "N19 白桃生巧色",
  "bbox_norm": [0.12, 0.08, 0.35, 0.14],
  "confidence": 0.96,
  "language": "zh",
  "engine": "paddleocr",
  "engine_version": "..."
}
```

### 10.3 需要抽取的实体

- 品牌；
- 商品系列；
- 产品类型；
- 色号编号；
- 色号名称；
- 英文别名；
- 中文别名；
- 颜色描述；
- 冷暖调；
- 明度或深浅；
- 质地，如哑光、镜面、水光、奶油、丝绒；
- 适用肤色；
- 适用场景；
- 宣传卖点；
- 容量或规格；
- 图中是否明确表示“实拍”“试色”“仅供参考”等限制。

模型必须区分：

- 图片中明确写出的事实；
- 根据图像推断出的属性；
- 文件夹名称提供的信息；
- 数据库已有信息；
- 人工确认的信息。

---

## 11. 多色号图中的“色号—名称—色块”空间匹配

这是信息抽取中最关键且容易出错的部分。

### 11.1 输入对象

- 色块、膏泥、试色或膏体区域列表；
- OCR 文本框列表；
- VLM 提供的布局与阅读顺序；
- 长图全局缩略图提供的整体布局，以及重叠 tile 的原图坐标映射；
- 可能存在的连接线、编号、箭头、表格结构；
- 文件夹和商品系列上下文。

### 11.2 匹配方法

构建候选边：

- 文本框与色块的中心距离；
- 上下左右相对位置；
- 是否处于同一网格单元；
- 是否有连接线或邻近编号；
- 阅读顺序；
- 文本是否符合色号格式；
- 系列内已知色号名称；
- VLM 对配对关系的判断。

然后使用：

- 规则评分；
- 匈牙利算法；
- 二分图匹配；
- 图优化；
- 对复杂布局使用 VLM 结构化复核。

长图必须先在全局缩略图上确定整体布局/阅读顺序，再把 tile 区域和 OCR 框回映到原图坐标。跨 tile OCR 使用规范化文本、坐标重叠和置信度形成去重组，保留全部来源 span、canonical span 和去重理由；配对算法只能对回映、去重后的对象计数。

### 11.3 输出

```json
{
  "region_id": "region-01",
  "shade_code": "N19",
  "shade_name": "白桃生巧色",
  "linked_ocr_span_ids": ["ocr-03", "ocr-04"],
  "match_score": 0.93,
  "match_method": "grid_plus_vlm_v1",
  "ambiguity": false,
  "alternative_matches": []
}
```

### 11.4 不确定性处理

以下情况必须进入人工审核：

- 色块数与色号数不一致；
- 多个文本框距离相近；
- OCR 色号编号无法识别；
- 色号名称换行或跨多个区域；
- 图像被裁剪；
- 同一色块附近出现多个候选名称；
- 模型和几何匹配结论冲突；
- 色块顺序与文本顺序疑似错位。

---

## 12. 文件夹名称与图片信息融合

### 12.1 文件夹名称解析

文件夹名称通常可能包含：

- 品牌；
- 产品中文名；
- 产品英文名；
- 系列；
- 色号编号；
- 色号名称；
- 容量；
- 宣传描述。

Codex 应实现可追溯解析器，保留：

```json
{
  "raw_folder_name": "橘朵-Judydoll-唇粉霜-N19 趋势裸【白桃生巧色】-1.8g",
  "parsed": {
    "brand_candidates": ["橘朵", "Judydoll"],
    "product_type": "唇粉霜",
    "shade_code": "N19",
    "shade_name": "白桃生巧色",
    "marketing_descriptor": "趋势裸",
    "net_content": "1.8g"
  },
  "parser_version": "folder_parser_v1",
  "parse_confidence": 0.94
}
```

#### 12.1.1 源数据和目录必须分层

本仓库首轮审计确认：

- 源 CSV 有 2309 行、2308 个唯一 `sku_id`；
- 16 个商品目录合并了 48 个源行，单目录最多对应 5 个 SKU/色号；
- 92 个非空 `brand_id` 形成 101 个品牌目录名，其中 9 个品牌 ID 有两个目录别名；
- 部分 `sku_color_no` 实际是 `3g`、`5ml` 等容量，真正色号存在于 `sku_name`；
- N19 商品目录中的图片可能同时展示 N18、N19、N03、N05。

因此必须原样保存 `asset_id`、`sku_id`、`goods_id`、`brand_id`、`brand_name`、`sku_name`、`sku_concat_name`、`sku_color_no`、源 CSV 行号、源 URL 和完整原始行。文件夹解析结果只是 evidence claim，不能覆盖源字段；`sku_color_no` 也不能无条件写成确认色号。

图片与源行使用多对多关系。目录中的上下文色号写为 `context_shade`，图片实际展示的一个或多个色号写为 `depicted_shades`。前者只能进入 B 层 `occurrence_context_fusions`；后者首先来自 A 层内容观察，再由 B 层与每个 occurrence/source context 比较。不得回填 A 层角色结果来迎合当前目录色号。

### 12.2 融合优先级

不能简单规定所有字段都以某一来源为准。推荐按字段设置来源优先级。

示例：

- 品牌：人工映射表 > 文件夹名称 > 包装 OCR > VLM；
- 色号编号：清晰 OCR/文件夹一致 > 文件夹名称 > VLM；
- 色号名称：清晰 OCR > 文件夹名称 > 品牌官方别名表 > VLM；
- 代表色：像素提取 > 多图融合 > VLM 颜色描述；
- 质地：文字明确说明 > 商品类型/系列知识 > 视觉推断；
- 适用肤色：图片文字明确说明 > 人工标注 > 模型推断。

### 12.3 冲突保留

不要覆盖冲突。应保存：

- 候选值；
- 来源；
- 来源置信度；
- 融合结论；
- 冲突类型；
- 是否需要审核。

---

## 13. 多图片商品级代表色融合

同一商品或色号可能有多张可用图片，单图代表色不应直接等同于商品最终代表色。

### 13.1 图片权重

建议综合：

- 图片角色权重；
- 分割置信度；
- 有效像素数量；
- 高光/阴影风险；
- 图片质量；
- 色偏风险；
- 与其他图片的一致性；
- 是否为多色号图中的小色块；
- 是否为唇部效果图。

### 13.2 融合方法

推荐在 Lab 空间进行：

1. 收集同一色号的单图颜色候选；
2. 计算两两 ΔE00；
3. 聚类或稳健离群检测；
4. 排除明显异常图；
5. 按证据权重计算加权 medoid 或稳健中心；
6. 保存主代表色、可信区间和备选颜色；
7. 如果存在明显双峰，不强制合并，标记可能存在拍摄条件差异、质地差异或错误匹配。

### 13.3 商品级输出

```json
{
  "shade_id": "...",
  "color_semantics": "image_observed_representative",
  "representative_srgb_hex": "#A84F5B",
  "representative_lab": [46.8, 37.2, 14.1],
  "fusion_method": "weighted_lab_medoid_v1",
  "evidence_image_count": 5,
  "accepted_image_count": 4,
  "rejected_image_count": 1,
  "cross_image_delta_e_median": 3.9,
  "cross_image_delta_e_max": 12.4,
  "confidence": 0.86,
  "review_required": false
}
```

---

## 14. 商品色号知识数据库

### 14.1 数据库分层

建议至少区分四层：

1. 原始层：文件、路径、图片哈希、OCR 原文；
2. 证据层：图片角色、区域、颜色候选、文本框和空间关系；
3. 推断层：标准化实体、匹配结果、商品级融合结果；
4. 审核层：人工确认、修改、否决和备注。

### 14.2 推荐表结构

#### `dataset_snapshots`

- `dataset_snapshot_id`
- `source_path`
- `source_sha256`
- `row_count`
- `column_schema_json`
- `created_at`

#### `source_records`

- `source_record_id`
- `dataset_snapshot_id`
- `row_number`
- `row_hash`
- `asset_id_raw`
- `sku_id_raw`
- `goods_id_raw`
- `brand_id_raw`
- `brand_name_raw`
- `sku_name_raw`
- `sku_concat_name_raw`
- `sku_color_no_raw`
- `raw_record_json`

#### `source_image_refs`

- `source_ref_id`
- `source_record_id`
- `source_field`
- `image_index`
- `source_url`
- `source_url_hash`
- `download_status`

#### `image_occurrences`

- `image_occurrence_id`
- `image_id`
- `folder_group_id`
- `relative_path`
- `filename`
- `extension`
- `detected_format`
- `extension_mismatch`
- `brand_folder_raw`
- `product_folder_raw`
- `legacy_image_id`

#### `source_ref_occurrences`

- `source_ref_id`
- `image_occurrence_id`
- `match_method`
- `match_confidence`

#### `pipeline_runs`

- `run_id`
- `dataset_snapshot_id`
- `stage`
- `pipeline_version`
- `schema_version`
- `git_commit`
- `config_json`
- `config_hash`
- `dependency_snapshot_json`
- `started_at`
- `finished_at`
- `status`

#### `brands`

- `brand_id`
- `canonical_name`
- `english_name`
- `aliases_json`

#### `products`

- `product_id`
- `brand_id`
- `canonical_product_name`
- `product_type`
- `series_name`
- `raw_folder_name`

#### `shades`

- `shade_id`
- `product_id`
- `shade_code`
- `shade_name`
- `shade_aliases_json`
- `normalized_descriptor_json`
- `status`

#### `image_contents`（旧草案名 `images`）

- `image_id`
- `sha256`
- `byte_size`
- `detected_format`
- `mime_type`
- `first_seen_at`

路径、下载来源、解码观察和质量指标不要直接压在内容级 `image_contents` 一行中；分别关联 `image_occurrences`、`source_image_refs` 和版本化预处理观察表。同一 `image_id` 可以有多个 occurrence。旧草案名 `images` 只作为文档别名，正式 schema 使用 `image_contents`。

#### `content_visual_analyses`（A 层）

- `content_visual_analysis_id`
- `image_id`
- `analysis_scope`
- `analysis_asset_id`
- `parent_content_visual_analysis_id`
- `tile_bbox_image_json`
- `primary_role`
- `secondary_roles_json`
- `layout_type`
- `global_layout_json`
- `role_confidence`
- `depicted_shades_json`
- `representative_color_eligible`
- `eligibility_score`
- `recommended_strategy`
- `model_run_id`
- `schema_version`

该表只保存内容级事实，禁止出现 `image_occurrence_id`、当前 SKU、folder 目标色号或 `context_shade`。

#### `occurrence_context_fusions`（B 层）

- `occurrence_context_fusion_id`
- `image_occurrence_id`
- `source_record_id`
- `source_ref_id`
- `folder_group_id`
- `content_visual_analysis_id`
- `source_sku_id_raw`
- `folder_context_json`
- `csv_context_json`
- `context_shade_json`
- `depicted_shades_json`
- `relationship_to_context`
- `context_conflicts_json`
- `fusion_method`
- `fusion_version`
- `confidence`
- `review_status`
- `run_id`

同一 occurrence 关联多个 source record 时分行保存，不覆盖。不得创建可写的单一 `image_roles` 表把两层重新混合。

#### `long_image_layouts`

- `long_image_layout_id`
- `image_id`
- `global_thumbnail_asset_id`
- `global_layout_json`
- `reading_axis`
- `image_to_thumbnail_transform_json`
- `tiling_strategy_version`

#### `image_tiles`

- `image_tile_id`
- `long_image_layout_id`
- `tile_asset_id`
- `tile_index`
- `bbox_image_json`
- `overlap_before_px`
- `overlap_after_px`
- `tile_to_image_transform_json`
- `transform_fingerprint`

#### `regions`

- `region_id`
- `image_id`
- `region_type`
- `bbox_json`
- `polygon_json`
- `mask_path`
- `detection_confidence`

#### `ocr_spans`

- `ocr_span_id`
- `image_id`
- `image_occurrence_id`
- `tile_asset_id`
- `text_raw`
- `text_normalized`
- `bbox_tile_json`
- `bbox_image_json`
- `source_tile_ids_json`
- `dedupe_group_id`
- `canonical_ocr_span_id`
- `dedupe_method`
- `ocr_confidence`
- `engine`

#### `region_text_links`

- `region_id`
- `ocr_span_id`
- `relation_type`
- `match_score`
- `match_method`

#### `image_color_candidates`

- `color_candidate_id`
- `image_id`
- `image_occurrence_id`
- `region_id`
- `raw_srgb_hex`
- `raw_srgb_rgb_json`
- `raw_srgb_lab_json`
- `normalized_srgb_hex`
- `normalized_srgb_rgb_json`
- `normalized_lab_json`
- `normalization_method`
- `normalization_evidence_json`
- `color_semantics`
- `method`
- `confidence`
- `diagnostics_json`

#### `shade_representative_colors`

- `shade_id`
- `representative_srgb_hex`
- `representative_srgb_rgb_json`
- `representative_lab_json`
- `color_semantics`
- `normalization_summary_json`
- `fusion_method`
- `confidence`
- `evidence_summary_json`
- `version`

#### `evidence_claims`

- `claim_id`
- `entity_type`
- `entity_id`
- `field_name`
- `candidate_value_json`
- `source_type`
- `source_id`
- `confidence`
- `status`

#### `model_runs`

- `model_run_id`
- `run_id`
- `model_name`
- `base_url_alias`
- `prompt_name`
- `prompt_version`
- `analysis_layer`
- `analysis_unit_type`
- `analysis_unit_id`
- `input_context_policy`
- `request_hash`
- `request_path`
- `raw_response_path`
- `parsed_response_path`
- `schema_validation_status`
- `latency_ms`
- `token_usage_json`
- `status`
- `error_json`

#### `review_tasks`

- `review_task_id`
- `task_type`
- `entity_id`
- `priority`
- `reason_codes_json`
- `payload_json`
- `status`
- `reviewer`
- `review_result_json`

### 14.3 数据库技术选择

- 阶段 1 规范门禁：SQLite + JSONL；
- 后续批处理/训练导出：可选 Parquet；全量 CSV 镜像也仅作可选兼容导出；
- 多用户、持续写入和服务化：PostgreSQL；
- 大量图片和 mask：文件系统或对象存储，数据库只存路径和哈希；
- 原始模型响应建议保存为压缩 JSONL 文件，不全部塞入关系数据库。

---

## 15. `qwen3.6-plus` 调用封装要求

### 15.1 配置

不得在代码中硬编码 API Key。

仓库审计已在受 Git 跟踪的 `test_qwen36_vision.py:21` 发现明文 Key，因此安全处置是阶段 0.5 `hard_gate`，必须早于阶段 1 及任何 Pilot：

1. 在供应商侧轮换或吊销旧 Key；
2. 从受跟踪代码删除真实值；
3. 本地开发使用被 `.gitignore` 排除的 `.env`，只提交含无效占位符的 `.env.example`，生产环境使用环境变量或密钥管理；
4. 扫描工作树、索引和全部本地可达 Git 提交，并用假密钥 fixture 验证扫描规则有效；
5. 明确泄露提交是否已经推送；若已推送，检查远程可达分支/tag 历史并记录处置结论；
6. 如需重写共享历史，必须另行评估协作者影响并协调执行，不能无记录强推；
7. 日志、异常、测试和扫描报告不得回显真实 Key 或完整认证头。

```text
DASHSCOPE_API_KEY
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen3.6-plus
```

使用 OpenAI SDK 时，`base_url` 只填写到 `/v1`，SDK 自动调用 `/chat/completions`。

### 15.2 本地图片输入

本地图片需编码为 Base64 Data URL。调用层应：

- 检查图片格式；
- 检查编码后大小；
- 对超大/长图片生成全局缩略图和必要的重叠 tile 分析副本，不能只提交 tile；
- 记录分析副本尺寸和哈希；
- 记录全局布局、tile 原图坐标、重叠范围和双向坐标变换；
- 不修改原图；
- 可选使用 JPEG/WEBP 压缩以降低请求体积，但必须记录压缩参数。

### 15.3 强制 JSON 校验

模型返回必须经过：

1. 去除 Markdown 代码围栏；
2. JSON 解析；
3. Pydantic/JSON Schema 校验；
4. 坐标范围校验；
5. 标签枚举校验；
6. 置信度范围校验；
7. 失败时执行有限次数修复或重试；
8. 原始响应始终保存。

### 15.4 缓存键

```text
content_cache_key = SHA256(
    image_sha256
    + analysis_asset_sha256
    + model_name
    + prompt_name
    + prompt_version
    + generation_parameters
)

context_fusion_cache_key = SHA256(
    image_occurrence_id
    + source_record_id/source_ref_id
    + content_visual_analysis_id
    + folder_csv_sku_context_hash
    + fusion_version
)
```

A 层 `content_cache_key` 禁止包含当前 SKU、folder 或 `context_shade`；同一内容/分析资产和提示词不变时复用一次，避免重复费用。B 层上下文变化只使 `context_fusion_cache_key` 失效，不得使 A 层重复调用。

### 15.5 重试与错误处理

处理：

- 网络超时；
- 429 限流；
- 5xx；
- 无效 JSON；
- 内容审核失败；
- 图片过大；
- 模型权限错误；
- API Key 错误。

重试采用指数退避和随机抖动。内容审核失败不能无限重试，应记录并进入人工队列。

### 15.6 并发

- 并发数配置化；
- 使用异步或任务队列；
- 限制请求速率；
- 每张图片独立提交并落盘；
- 支持断点续跑；
- 批处理失败不影响已完成结果。

---

## 16. 置信度与异常检测

### 16.1 置信度组成

不要直接把模型自报置信度当作最终置信度。可组合：

- VLM 角色置信度；
- 分类规则一致性；
- OCR 置信度；
- 区域检测置信度；
- 分割质量；
- 颜色簇内离散度；
- 多图颜色一致性；
- 文件夹信息与图片信息一致性；
- 文字—色块匹配分数；
- 是否存在冲突来源。

### 16.2 主要异常类型

- `role_conflict`
- `shade_count_mismatch`
- `ocr_folder_conflict`
- `text_region_ambiguous`
- `color_outlier`
- `low_valid_pixel_count`
- `high_specular_ratio`
- `strong_color_cast`
- `duplicate_color_conflict`
- `unknown_brand`
- `unknown_shade`
- `possible_new_shade`
- `model_invalid_json`
- `decode_recovered`
- `content_moderation_blocked`

### 16.3 新色号发现

只有在以下条件下才标记 `possible_new_shade`：

- 图片中存在清晰色号编号或名称；
- 与当前商品上下文一致；
- 数据库中不存在对应实体；
- OCR 和 VLM 至少两个证据来源支持，或人工确认；
- 多色号图中的色块和文字匹配置信度达到阈值。

自动发现结果不能直接成为“已确认色号”。

---

## 17. 人工审核与持续学习

人工能力分两步建设：阶段 2.5 先提供最小标注/核查工具，用于创建角色、代表色资格、mask 和多色号固定评估集；阶段 8 再扩展为完整审核系统。阶段 2.5 不得被解释为已经完成阶段 8 的任务队列、权限、并发、优先级、裁决、回归和持续学习能力。

### 17.1 审核对象

优先审核：

- 图片角色低置信度；
- 是否可提色存在冲突；
- 多色号图匹配不确定；
- OCR 与文件夹名称冲突；
- 多图代表色差异过大；
- 新色号候选；
- 唇部效果图颜色异常；
- VLM 与像素结果明显不一致；
- 高价值品牌或重点商品。

### 17.2 审核界面最少展示

- 原图；
- 角色分类；
- 分割 mask overlay；
- OCR 框；
- 色块—文字连线；
- 单图颜色候选色块；
- 商品级融合色块；
- 来源信息和置信度；
- 冲突原因；
- 一键确认、修改、拒绝。

### 17.3 回流数据

人工审核结果应生成训练数据：

- 图片角色标签；
- 代表色资格标签；
- 区域边界框或 mask；
- OCR 修正文本；
- 色号—色块配对；
- 最终代表色选择；
- 实体归一化映射。

后续可以训练：

- 本地图片角色分类器；
- 代表色资格分类器；
- 色块检测模型；
- 文本—色块匹配模型；
- 图像和文本联合色号相似度模型。

---

## 18. 评估方案

Codex 必须建立固定评估集，不允许只看几个示例图。

### 18.1 抽样原则

评估集应覆盖：

- 不同品牌；
- 不同商品类型；
- 八类图片角色；
- 高低分辨率；
- 白底、黑底、复杂背景；
- 中文、英文、中英混合文字；
- 单色图和多色号图；
- 长图全局缩略图 + 重叠 tile；
- 扩展名/真实格式错配；
- 相同 SHA256 的多个 occurrence；
- 商品目录碰撞和多 source context；
- 哑光、亮面、透明、珠光；
- 裸色、深色、高饱和和低饱和颜色；
- 正常图片和损坏图片。

### 18.2 指标

#### 角色分类

- Accuracy；
- Macro-F1；
- 各类别 Precision/Recall；
- 代表色资格 Precision、Recall、F1、Coverage；
- 混淆矩阵。

对“可提取代表色”应优先控制假阳性，即不要把包装图或宣传图错误送入颜色流水线。

资格 Coverage 定义为“非 abstain 的自动资格判断数 / 资格评估项总数”。同时报告“每商品至少一个有效颜色证据覆盖率”：至少有一个通过资格、region/mask 与质量门的颜色证据的 in-scope 商品上下文数 / 全部 in-scope 商品上下文数，并分列无可用证据原因。

#### OCR

- CER；
- shade-code exact match；
- 色号名称准确率；
- 文本框检测召回率。

shade-code exact match 和 CER 使用版本化的 Unicode、大小写、空白与连字符归一化规则；长图另报跨 tile 去重前后结果。

#### 色号—色块匹配

- 匹配准确率；
- 多色号整图完全匹配率：全部期望配对正确且无多配/漏配；
- 歧义检测召回率。

#### 代表色

- 与同图人工选区 image-observed 结果的 Median ΔE00 和 P90 ΔE00；
- 同一色号跨图片一致性；
- mask IoU；
- 高光/背景误选率；
- 商品级融合稳定性。

### 18.3 初始验收目标

以下性能数字全部是 `provisional_target`，不是已冻结门禁。只有完成阶段 1.5 Pilot 和阶段 2.5 首轮人工标注、检查分层样本量与误差后，才能写入版本化 `frozen` 阈值：

- `provisional_target`：图片角色 Macro-F1 ≥ 0.85；
- `provisional_target`：代表色资格 Precision ≥ 0.90；
- `provisional_target`：代表色资格 Recall、F1、Coverage 待 Pilot/首轮标注基线后填写；
- `provisional_target`：每商品至少一个有效颜色证据覆盖率待 Pilot/首轮标注基线后填写；
- `provisional_target`：清晰多色号图配对准确率 ≥ 0.90；
- `provisional_target`：多色号整图完全匹配率待 Pilot/首轮标注基线后填写；
- `provisional_target`：shade-code exact match ≥ 0.95，CER ≤ 0.05；
- `provisional_target`：高质量单色图自动结果与同图人工选区的 Median ΔE00 ≤ 5、P90 ΔE00 ≤ 10。

以下属于 `hard_gate`，不因性能阈值尚未冻结而放宽：

- 所有失败样本均可定位到明确阶段和错误类型；
- 断点续跑不会重复处理已成功且版本未变化的数据；
- 所有模型尝试保留请求、原始响应、解析/Schema 状态；
- 原图不修改且所有结果可追溯。

---

## 19. 运行阶段与里程碑

### 阶段 0：仓库和数据审计

产出：

- 数据画像；
- 现有代码能力矩阵；
- 已知问题清单；
- 修订后的实施计划；
- 本指南的第一轮变更记录。

### 阶段 0.5：明文密钥泄露处置（阻断）

> 实施状态（2026-07-28）：Git 当前树、本地可达历史和远端 `origin/main` 历史清理及 `.env`/扫描门禁已完成。仓库所有者明确豁免供应商侧失效确认，因此状态为 `passed_with_owner_override`，不是“已验证轮换/吊销”。详见 `docs/stage0_5_security_report.md`。

在任何后续阶段前完成：

- 轮换或吊销已暴露 Key，并从受跟踪代码移除；
- 本地 `.env` + 安全 `.env.example`/环境变量接入；
- 工作树、索引和本地可达 Git 历史密钥扫描；
- 核查泄露提交是否已推送；若已推送，检查远程可达历史并记录处置。

### 阶段 1：Manifest、稳定 ID 和最小数据库

> 实施状态（2026-07-28）：正式运行 `stage1_full_20260728` 已完成并通过独立验收；SQLite + JSONL、稳定 ID、旧 ID 映射和原图完整性基线均已生成。详见 `docs/stage1_completion_report.md`。

完成：

- 源 CSV/数据集快照和完整原始行留存；
- `source_record_id`、`source_ref_id`、`folder_group_id`、内容 `image_id` 和 `image_occurrence_id`；
- 将当前可重建关系固化为不可变的源图片引用—物理路径—内容哈希多对多 manifest；
- SQLite + JSONL 规范存储；
- 现有路径相关 `image_id` 的显式迁移。

Parquet 和全量 CSV 镜像是后续可选导出，不是阶段 1 门禁。

### 阶段 1.5：50–100 唯一 SHA256 VLM Pilot

用覆盖八类角色、长图、格式错配、重复内容多 occurrence 和目录碰撞的样本跑通：

- 图片读取；
- 全局缩略图 + 重叠 tile；
- A 层内容分类；
- JSON Schema 校验；
- 请求/原始响应/解析结果双留存；
- SQLite；
- B 层 occurrence 上下文融合；
- 人工核查。

### 阶段 2：预处理加固与迁移

完成：

- 扩展名/实际格式/MIME 检测；
- 去重；
- 质量检测；
- 长图全局缩略图、重叠 tile、全局布局、坐标回映和跨 tile OCR 去重基础；
- 不可覆盖的运行日志；
- 配置快照、Git 提交、依赖快照和 transform fingerprint；
- 断点续跑和历史产物迁移。

### 阶段 2.5：最小人工标注与评估工具

在正式模型批处理前完成：

- 角色、代表色资格、mask 和多色号配对标注；
- 固定评估集版本化与 SHA256/产品分组防泄漏；
- 内容标签页面隐藏当前 SKU/folder/context shade；
- 首轮标注和 Pilot 基线；
- `provisional_target` 评审及版本化冻结候选。

完整多用户审核、优先级队列和持续学习仍在阶段 8。

### 阶段 3：内容视觉分析与 occurrence 来源上下文融合

完成：

- A 层 `content_visual_analyses`：不含当前 SKU/folder/context shade 的角色、布局和资格；
- B 层 `occurrence_context_fusions`：结合 folder、CSV、SKU、context shade 判断与当前商品的关系；
- 两层独立缓存、版本、表和评估；
- 请求、原始响应、解析/Schema 状态双留存；
- 角色与资格 Precision/Recall/F1/Coverage 报告。

### 阶段 4：OCR 和视觉信息抽取

完成：

- OCR 文本框；
- shade-code exact match 和 CER；
- 长图跨 tile OCR 去重与原图坐标回映；
- VLM 实体抽取；
- 文件夹名称解析；
- 证据表；
- 冲突检测。

### 阶段 5：单色图 image-observed 代表色提取

优先实现：

- 单色试色图；
- 单色膏体图；
- 色卡图；
- mask 和颜色诊断输出；
- Median/P90 ΔE00 评估；
- `color_semantics=image_observed_representative`。

### 阶段 6：多色号图

完成：

- 多区域检测；
- OCR 版面理解；
- 色号—名称—色块匹配；
- 每个色号独立颜色候选；
- 多色号整图完全匹配率；
- 全局布局约束下的长图 tile 合并。

### 阶段 7：商品级融合和数据库

完成：

- 多图融合；
- 实体归一化；
- 新色号候选；
- 在阶段 1 最小数据库之上完成知识层表；
- 每商品至少一个有效颜色证据覆盖率；
- 查询和导出接口。

### 阶段 8：完整人工审核和持续学习

完成：

- 在阶段 2.5 最小工具上增加任务分配、权限、并发、优先级和裁决；
- 审核任务导出与结果回写；
- 标注数据集版本化；
- 版本化评估、回归和阈值管理；
- 本地模型训练准备。

---

## 20. CLI 建议

```bash
# 数据审计
python -m lipcolor_pipeline.cli audit --config configs/pipeline.yaml

# 创建或更新 manifest
python -m lipcolor_pipeline.cli inventory --config configs/pipeline.yaml

# 图片角色分类
python -m lipcolor_pipeline.cli classify-roles --resume

# OCR 和视觉信息抽取
python -m lipcolor_pipeline.cli extract-info --resume

# 代表色提取
python -m lipcolor_pipeline.cli extract-colors --roles single_bullet single_swatch color_card

# 多色号图处理
python -m lipcolor_pipeline.cli process-multi-shade --resume

# 商品级融合
python -m lipcolor_pipeline.cli fuse-products

# 构建数据库
python -m lipcolor_pipeline.cli build-database

# 导出人工审核批次
python -m lipcolor_pipeline.cli export-review --priority high

# 生成评估报告
python -m lipcolor_pipeline.cli evaluate
```

所有命令应支持：

- `--dry-run`
- `--resume`
- `--limit`
- `--brand`
- `--product`
- `--image-id`
- `--force`
- `--config`
- `--run-id`

---

## 21. 配置示例

```yaml
project:
  raw_root: "data/raw"
  output_root: "outputs"
  database_path: "data/database/lipcolor.sqlite"

vlm:
  provider: "dashscope_openai_compatible"
  model: "qwen3.6-plus"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  api_key_env: "DASHSCOPE_API_KEY"
  timeout_seconds: 120
  max_retries: 3
  concurrency: 4
  enable_thinking: false

preprocessing:
  convert_to_srgb: true
  preserve_original: true
  allow_truncated_recovery: false
  max_analysis_long_edge: 2048
  analysis_jpeg_quality: 92

classification:
  prompt_version: "1.0.0"

color:
  working_space: "CIELAB"
  delta_e_method: "CIEDE2000"
  save_masks: true
  save_overlays: true
  save_color_patches: true

thresholds:
  status: "provisional_target"
  threshold_version: "draft_after_audit"
  classification_low_confidence: 0.70
  color_min_valid_pixels: 1000

review:
  export_format: "jsonl"
  include_overlays: true
  include_raw_model_response: true
```

阈值必须放在配置中，不要散落在代码里。首轮标注和阶段 1.5 Pilot 前，所有模型/算法性能阈值的 `status` 必须是 `provisional_target`；评审冻结后创建新的 `threshold_version`，不能原地改写。

---

## 22. 测试要求

### 22.1 单元测试

至少覆盖：

- 文件夹名称解析；
- ID 稳定性；
- Data URL 编码；
- JSON 修复与 Schema 校验；
- A 层请求上下文白名单与 A/B 缓存键隔离；
- 坐标转换；
- 全局缩略图、重叠 tile、跨 tile OCR 去重和原图坐标回映；
- sRGB/Lab/HEX 转换；
- ΔE00；
- OCR 文本归一化；
- 二分图匹配；
- 多图颜色融合；
- 缓存键；
- 数据库迁移。

### 22.2 集成测试

建立小型固定测试集，至少包含：

- 1 张单色膏体图；
- 1 张单色试色图；
- 1 张唇部图；
- 1 张多色号图；
- 1 张色卡；
- 1 张包装图；
- 1 张文字宣传图；
- 1 张无效或损坏图。

集成测试不应默认真实调用付费 API。使用缓存响应或 mock；另提供显式的在线测试命令。

正式阶段 3 前另执行 50–100 个唯一 SHA256 的阶段 1.5 在线 Pilot，覆盖八类角色、长图、格式错配、重复内容多 occurrence 和目录碰撞。内容角色核查时默认隐藏当前 SKU、folder 目标色号和 `context_shade`。

### 22.3 回归测试

每次修改提示词、分割算法、颜色算法或融合规则后，输出：

- 角色分类变化数量；
- 可提色判断变化数量；
- OCR 实体变化数量；
- 色号—色块匹配变化数量；
- 最终代表色 ΔE00 变化分布；
- 新增和消失的审核任务。

---

## 23. 日志、审计和可视化产物

每张图片应能生成或追踪到：

- 原始图片路径；
- 标准化分析图；
- A 层内容视觉分析 JSON；
- B 层 occurrence 来源上下文融合 JSON；
- OCR JSON；
- VLM 原始响应；
- 检测框 overlay；
- 分割 mask；
- 颜色像素分布；
- Lab 聚类结果；
- 代表色色块；
- 多色号配对连线图；
- 商品级融合报告；
- 错误和审核任务。

建议目录：

```text
outputs/runs/{run_id}/
├── manifest/
├── model_responses/
├── roles/
├── ocr/
├── regions/
├── masks/
├── overlays/
├── colors/
├── matches/
├── product_fusion/
├── review/
├── errors/
└── reports/
```

---

## 24. 关键实现原则总结

1. 先理解图片角色，再决定后续处理；
2. 先定位语义区域，再计算颜色；
3. VLM 负责理解，像素算法负责精确颜色；
4. OCR 负责文字和坐标，VLM 负责语义和版面；
5. 多色号图必须显式建模空间关系；
6. 商品级颜色来自多图片证据融合，不等同于单图主色；
7. 文件夹名称是证据之一，不是无条件真值；
8. 冲突必须保存，不得覆盖；
9. 低置信度必须审核；
10. 所有模型、规则、提示词和数据库结构都要版本化；
11. 允许 Codex 根据数据修改实现，但必须有审计、变更记录和回归对比；
12. 内容视觉事实与 occurrence 来源上下文必须分层，A 层不得接收当前 SKU/folder/context shade；
13. 长图同时保留全局缩略图与重叠 tile，并保存 OCR 去重和原图坐标链；
14. 数据库颜色是 image-observed representative color，无校准证据时不得声称真实物理颜色；
15. 第一版应优先形成稳定、可审计的流水线，再追求复杂模型和端到端自动化。

---

## 25. Codex 首轮执行指令

Codex 接收本指南后，应按以下顺序执行：

1. 阅读整个仓库，不要立即重写；
2. 定位原始数据目录、现有预处理结果和数据库；
3. 统计真实数据分布并抽样查看图片；
4. 识别本指南与现有项目冲突之处；
5. 创建 `docs/repository_audit.md`；
6. 创建或更新 `docs/implementation_plan.md`；
7. 在本指南“变更记录”中记录第一轮修订；
8. 先完成阶段 0.5 明文密钥处置，未通过不得进入后续阶段；
9. 优先复用已有可用模块；
10. 先完成 SQLite + JSONL manifest 和稳定 ID；
11. 用 50–100 个唯一 SHA256 完成阶段 1.5 VLM Pilot；
12. 加固预处理并建立阶段 2.5 最小标注/固定评估集；
13. 冻结有证据的阈值版本后再扩大 A/B 两层分析；
14. 每完成一个阶段，生成可视化和评估报告；
15. 不得在没有证据的情况下声称整个数据集已经正确处理。

---

## 26. 变更记录

- 初始版本：建立从图片角色分类、代表色提取、OCR、多色号空间匹配、多图片融合到知识数据库和人工审核的总体实现规范。
- 2026-07-27：
  - 修改章节：4.1、4.2、5.1、5.4、6.2、12.1、14.2、19。
  - 修改内容：显式增加数据集快照、源记录、图片 URL 引用、目录分组、内容图片和物理 occurrence 的分层 ID/表；规定扩展名与实际格式分开、GIF/长图/极端尺寸策略；补充目录上下文色号与图片实际展示色号；把最小数据库和运行指纹提前到阶段 1。
  - 修改原因：真实目录不是稳定实体边界，扩展名也不是可靠格式；必须先修复来源血缘和运行可复现性。
  - 证据来源：
    - `docs/repository_audit.md` 的全量统计和视觉抽样；
    - `data/dim_pub_sku_20260513_115554_口红唇膏唇蜜唇釉.csv`：2309 行、2308 个唯一 `sku_id`；
    - `downloaded_images/`：101 个品牌目录、2277 个商品目录、31,511 张图片；
    - 16 个商品目录合并 48 个源行，9 个 `brand_id` 存在两个目录别名；
    - `image_preprocessing_output/metadata/image_preprocessing.csv`：12,386 个唯一 SHA256、229 个扩展名/实际格式错配、3 个严格解码失败；
    - `image_preprocessing_pipeline/preprocess_product_images.py:402`：现有路径相关 `image_id`；
    - `download_product_images.py:151`：Windows 双斜杠 URL 后缀识别问题。
  - 兼容性影响：八类核心角色标签、原图只读、模型原始/解析双留存和 `image_id=SHA256` 底线不变；现有预处理 `image_id` 迁移为 `legacy_image_id`，原始目录只生成 `folder_group_id`，不再直接充当最终 `product_id`。正式实现必须提供迁移映射，不覆盖旧元数据。
  - 修改人/代理：Codex。
- 2026-07-28：
  - 修改章节：1、4.1–4.2、5.4、6.2–6.6、7、9、11–15、17–19、22–25。
  - 修改内容：
    - 新增阶段 0.5 安全阻断项：轮换/吊销泄露 Key、从受跟踪代码移除、采用未跟踪 `.env`/环境变量、执行密钥扫描，并在已推送时检查远程可达 Git 历史；
    - 明确当前 CSV + 当前下载器只能重建当前路径关系；没有不可变成功 manifest 时不能保证长期稳定追溯；
    - 将模型分析拆为 A 层 `content_visual_analyses` 与 B 层 `occurrence_context_fusions`；A 层禁止输入当前 SKU、folder 目标色号和 `context_shade`；
    - 增加阶段 1.5 的 50–100 唯一 SHA256 VLM Pilot，以及阶段 2.5 最小标注/固定评估集；完整审核系统保留在阶段 8；
    - 阶段 1 存储门禁改为 SQLite + JSONL，Parquet/全量 CSV 镜像后置为可选导出；
    - 所有性能候选值标记 `provisional_target`，补充资格 Precision/Recall/F1/Coverage、每商品颜色证据覆盖率、OCR shade-code exact match/CER、颜色 Median/P90 ΔE00 和多色号整图完全匹配率；
    - 明确数据库颜色是 `image-observed representative color`，将含义不清的 `corrected_*` 改为 `normalized_*`；无可验证校准依据时不得声称真实物理颜色；
    - 长图改为全局缩略图 + 重叠 tile，保存全局布局、tile 坐标、跨 tile OCR 去重链和原图坐标回映。
  - 修改原因：降低上下文标签泄漏和重复付费风险；在正式批处理前验证模型/Schema/持久化闭环并建立固定真值；避免把当前可重建路径、图像观测色和小样本候选阈值误表述为更强的事实。
  - 证据来源：
    - `docs/repository_audit.md` 4.4：当前 31,513 条 URL 引用可由当前 CSV/下载器重建为 31,511 个目标路径，但下载器没有不可变成功 manifest；
    - `test_qwen36_vision.py:21`：受 Git 跟踪文件中存在明文 API Key；
    - `docs/repository_audit.md` 4.2–4.3：9 组品牌目录别名、16 个目录碰撞组涉及 48 个源行；
    - `docs/repository_audit.md` 5.1、5.3、6：229 个扩展名/真实格式错配；存在 1074×28190 整页长图和 750×1 装饰条；
    - `docs/repository_audit.md` 7.2–7.3：31,511 个文件只有 12,386 个唯一 SHA256，精确重复额外文件占 60.69%，且现有路径相关 ID 与内容 ID 语义不同；
    - `preprocess_product_images.py:402-404`：现有 `image_id` 包含路径哈希；`test_qwen36_vision.py` 当前只打印响应且没有 Schema/双留存闭环。
  - 兼容性影响：
    - 原始目标、八类核心角色代码、原图只读、`image_id=SHA256`、模型原始/解析双留存和证据追溯底线均保留；
    - 内容表规范名统一为 `image_contents`；旧草案名 `images` 仅为文档别名，迁移必须显式；
    - 不再创建可写的混合 `image_roles` 表；旧设计字段须显式迁移到 A/B 两表，必要兼容 view 只能只读并标明来源；
    - `corrected_* → normalized_*` 是显式字段迁移，旧输出不得静默重解释；真正校准颜色只能另用有证据的 `calibrated_*`；
    - 阶段号和门禁扩展为 0.5、1.5、2.5；详细迁移和验收以 `docs/implementation_plan.md` 2026-07-28 修订版为准。
  - 修改人/代理：Codex。
- 2026-07-28（阶段 0.5/1 实施状态回写）：
  - 修改章节：16 的阶段 0.5/1 状态说明、26 变更记录。
  - 修改原因：阶段 0.5 和阶段 1 已实际执行；需要区分原始安全目标、所有者明确豁免和已经验证的结果，避免继续把文档修订轮次的“尚未实现”当作当前状态。
  - 代码证据：
    - `.gitignore`、`.env.example`、`lipcolor_pipeline/config.py`；
    - `lipcolor_pipeline/security.py` 与 `scripts/scan_secrets.py`；
    - `database/migrations/001_stage1.sql`；
    - `lipcolor_pipeline/stage1_manifest.py`、`lipcolor_pipeline/stage1_validate.py` 及其 CLI；
    - `tests/test_security.py`、`tests/test_stage1_manifest.py`。
  - 数据证据：
    - `stage1_full_20260728`：2,309 个 source record、31,513 个 source ref、31,511 个 occurrence、12,386 个内容 SHA256、31,513 条来源关系；
    - 16 个目录碰撞组和 9 个品牌别名组保留；SQLite/JSONL 主键与行数一致；原图全集 stat 和两轮各 100 张 SHA256 抽样通过。
  - 安全证据：目标脚本路径在当前树和全部本地可达对象中无结果，远端 `main` 已带租约重写；三层扫描 0 条发现且正向 fixture 有效。供应商失效状态未验证，记录为 `owner_waived_unverified`。
  - 兼容性影响：总体目标、核心标签、数据库语义、原图只读、模型双留存和后续 1.5/2/2.5 阶段设计均不变；只回写实际完成状态和显式例外。
  - 修改人/代理：Codex。
- 后续由 Codex 根据仓库审计、固定评估集和真实数据分析继续维护。
