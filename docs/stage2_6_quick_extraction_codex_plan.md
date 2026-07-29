# Stage 2.6 Quick Extraction MVP 设计与实施基线

版本：1.0.2  
修订日期：2026-07-29  
状态：实现完成，离线与分阶段在线工程验收通过

实际结果、调用总账和限制见 `docs/stage2_6_completion_report.md`。

## 1. 目标与输出语义

Stage 2.6 跑通以下可审计工程闭环：

```text
Stage 2 working/global/tile 资产
  → Qwen 可见内容理解、文字读取和区域定位
  → 原图坐标回映与长图本地合并
  → Stage 2 working/alpha 像素取色
  → SQLite + JSONL + CSV
```

所有颜色输出统一标记为：

```text
image_observed_color_candidate
```

它只表示图片中可见像素的代表色候选，不表示真实物理色、标准色卡值，也不表示正式阶段 3–6 已完成。

本 MVP 不设置 OCR 准确率、bbox/mask 准确率、文字—区域配对准确率或 ΔE 精度门禁。这些指标在没有冻结 ground truth 时统一记为：

```text
not_evaluated_without_ground_truth
```

## 2. 实际工作区证据

实现前对当前工作区数据库和代码做了只读核验，结果如下：

- `image_contents`：12,386；
- Stage 2 `image_preprocessing_observations`：12,386；
- Stage 2 可解码 working asset：12,383；
- Stage 2 alpha asset：751；
- Stage 2 长图布局：172；
- Stage 2 有序 tile：1,330；
- Stage 2 实际资产类型为：
  - `working_image_legacy`；
  - `alpha_mask_legacy`；
  - `global_thumbnail`；
  - `image_tile`；
- Stage 2 实际没有 `analysis_preview` 或 `vlm_input_preview`。

因此，旧稿中“直接查询 Stage 2 `analysis_preview`、`vlm_input_preview`、`alpha_mask`”的设计不可执行，现改为以下真实资产路径：

- 普通图：
  - 从 `image_preprocessing_observations.working_asset_id` 读取 Stage 2 sRGB working asset；
  - 从 `alpha_asset_id` 读取可选 Alpha；
  - 确定性生成 Stage 2.6 `quick_vlm_preview`；
- 长图：
  - 直接复用 `long_image_layouts.global_thumbnail_asset_id`；
  - 直接复用 `image_tiles.tile_asset_id`；
  - 不重切长图；
- 最终取色：
  - 只裁剪 Stage 2 working asset；
  - Alpha 只使用 Stage 2 `alpha_asset_id`；
  - 不从有损 JPEG preview、global thumbnail 或 tile 取色。

## 3. 边界与不变量

必须保持：

1. 原始图片只读；
2. 阶段 1–2.5 数据库事实和历史 run 目录只读；
3. Stage 2.6 只新增自己的 run、preview、模型审计文件和数据库行；
4. 每个数据库结果可追溯到：
   - `image_id`；
   - Stage 2 working asset；
   - 可选 alpha asset；
   - VLM 输入 asset；
   - model run；
   - request/raw/parsed 文件；
5. 每次选中原图在运行前后重新计算 SHA256，发现漂移立即将 run 标为失败；
6. Qwen 请求不包含 folder、SKU、商品名、文件名或 `context_shade`；
7. 不自动冻结 Stage 2.5，不新增人工审核，不自动进入正式阶段 3–6。

## 4. 普通图 preview

普通图 preview 由 Stage 2 working asset 确定性生成：

- 最大长边：2,048；
- JPEG quality：92；
- chroma subsampling：0；
- progressive：false；
- optimize：false；
- 任一边小于 11 px 时：
  - 使用白色 padding；
  - 居中原图；
  - 最终边长至少 11 px；
- transform 和 padding 被写入 `derived_assets.metadata_json`；
- preview SHA256、尺寸和 transform fingerprint 进入请求清单和缓存键。

preview 的 bbox 回映使用显式逆变换：

```text
image_x = asset_x * scale_x + translate_x
image_y = asset_y * scale_y + translate_y
```

坐标统一为 Stage 2 EXIF-oriented working image 的半开像素坐标：

```text
[x_min, y_min, x_max, y_max)
```

## 5. 长图资产与合并

长图每张图片产生：

- 1 个 `global_thumbnail` unit；
- N 个按 `tile_index` 排序的 `tile` unit。

scope 职责：

- `global_thumbnail`
  - 只输出角色、布局、资格、摘要和质量风险；
  - `text_items=[]`；
  - `color_regions=[]`；
- `tile`
  - 输出局部可见文字和颜色区域；
  - 不猜测 tile 外内容。

合并规则：

- 角色和摘要优先 global；
- global 失败时使用最高 `role_confidence` 的成功 tile；
- 使用 tile fallback 时整图状态至少为 `partial`；
- 文字只从 tile 合并；
- 区域只从 tile 合并；
- 任一 tile 失败不会丢弃成功 tile 证据。

文字去重：

- 原文不改写；
- 规范化文本相同；
- 原图 bbox IoU ≥ 0.5；
- canonical 结果保留所有来源 unit、tile 和模型文字 ID。

区域去重：

- `region_type` 相同；
- 两边都存在色号时不得冲突；
- 原图 bbox IoU ≥ 0.6；
- canonical 结果保留所有来源 unit、tile 和模型 region ID。

长图合并完全在本地执行，不额外调用模型。

## 6. Qwen 职责

Qwen 只负责当前输入资产中直接可见的内容：

- 主角色和辅角色；
- 角色置信度；
- 布局；
- 代表色候选资格及置信度；
- 可见文字原文、类型、bbox 和置信度；
- 可见颜色区域类型、bbox 和风险；
- 文字—区域关联及关联置信度；
- 摘要与质量风险。

Qwen 不负责：

- 输出最终 HEX/RGB/Lab；
- 推断真实物理颜色；
- 使用目录、SKU、商品名或业务上下文补全文字；
- 用模型二次“修复 JSON”；
- 做长图最终合并。

## 7. 严格 Schema

Schema 版本：

```text
quick-image-extraction-1.0
```

### 7.1 `QuickImageExtraction`

核心字段：

- `schema_version`；
- `scope`：`image | global_thumbnail | tile`；
- `input_context_policy=image_only`；
- `primary_role`；
- `secondary_roles`；
- `role_confidence`；
- `layout_type`；
- `layout_summary`；
- `representative_color_eligible`；
- `eligibility_confidence`；
- `eligibility_reasons`；
- `summary`；
- `quality_risks`；
- `text_items`，单次资产最多 20；
- `color_regions`，单次资产最多 20。

允许角色：

```text
single_bullet
single_swatch
lip_effect
multi_shade_comparison
color_card
packaging
text_promo
invalid
```

### 7.2 `QuickTextItem`

- 稳定且在响应内唯一的 `text_item_id`；
- 原文 `text`；
- `text_type`；
- 可空 `bbox_norm`；
- `confidence`。

### 7.3 `QuickColorRegion`

- 稳定且在响应内唯一的 `region_id`；
- `region_type`；
- 非空 `bbox_norm`；
- 可见色号、色名、视觉色名；
- `confidence`；
- `risks`；
- `linked_text_item_ids`；
- `association_confidence`。

Schema 强制：

- `extra="forbid"`；
- bbox 值有限、位于 0–1 且面积为正；
- ID 唯一；
- 区域关联的文字 ID 必须存在；
- 关联存在时必须有置信度；
- 请求 scope 与响应 scope 一致；
- global 的文字和区域必须为空；
- 单次资产列表上限为 20。

模型响应只允许：

- 去除完整 Markdown JSON fence；
- 对明确的像素 bbox 或明确的 0–1000 bbox 做确定性归一化。

其他 Schema 错误进入正常重试，不发起模型修复调用。

## 8. 缓存与模型审计

缓存键包含：

- 资产 SHA256；
- scope；
- 模型；
- prompt name/version；
- prompt SHA256；
- response Schema version；
- generation parameters。

缓存键不包含 run ID，因此可跨 run 命中。

跨 run 命中时，当前 run 仍然必须物化：

- 当前 request manifest；
- 当前 parsed JSON；
- raw response 的硬链接或副本；
- 当前 `model_runs` 行，状态为 `cache_hit`；
- 当前 `quick_extraction_units` 状态和来源 model run 证据。

每个真实尝试保存：

- `model/requests/<model_run_id>.json`；
- `model/raw/<model_run_id>.json`，供应商成功返回后先写；
- `model/parsed/<model_run_id>.json`，Schema 成功后写；
- Schema 状态；
- token；
- latency；
-错误；
- provider model name。

request manifest 只记录资产哈希、尺寸、类型、transform、prompt、Schema 和生成参数，不重复保存 Base64。

## 9. 数据库迁移 007

实际迁移文件：

```text
database/migrations/007_stage2_6_quick_extraction.sql
```

新增表：

### 9.1 `quick_extraction_units`

每个普通图/global/tile 一行，保存：

- scope 和 tile index；
- VLM source asset；
- working/alpha asset；
- long layout；
- asset SHA；
- asset-to-image transform；
- cache key；
- 可空 model run；
- `planned/prepared/cache_hit/succeeded/failed/skipped/budget_exhausted`；
- provider attempt count；
- failure JSON。

没有解析结果或 model run 时也能合法落库。

### 9.2 `quick_image_extractions`

每个 `run_id + image_id` 一行：

- `success/partial/failed/skipped`；
- 角色、布局、资格和摘要；
- successful/failed/skipped unit IDs；
- fallback、坐标失败、working/alpha 证据；
- `image_observed_color_candidate` 语义。

### 9.3 `quick_text_items`

只保存整图 canonical 文字：

- 原文和规范化文本；
- 原图 bbox；
- 全部来源 observation；
- 去重规则和 IoU 证据。

### 9.4 `quick_color_regions`

只保存整图 canonical 区域：

- 原图 bbox；
- 色号/色名/关联文字；
- 模型资格与风险；
- 本地取色状态；
- HEX/RGB/Lab；
- 有效像素、比例、簇占比和离散度；
- 全部来源和算法诊断。

workspace 默认迁移上限、CLI choices、workspace Schema 和 pipeline version 已同步更新为 Stage 2.6 / 007 / 0.3.0。

## 10. 本地颜色算法

颜色从 Stage 2 working/alpha 计算：

1. bbox 向内收缩 3%；
2. 裁剪 working 和同尺寸 alpha；
3. 最长边降采样到 256；
4. 过滤：
   - Alpha ≤ 16；
   - RGB 全通道 ≥ 245；
   - RGB 全通道 ≤ 8；
5. 要求：
   - 有效像素 ≥ 300；
   - 有效比例 ≥ 5%；
6. sRGB → CIE Lab D65；
7. 固定 seed `260`；
8. 最多 3 簇；
9. 最多 15 次迭代；
10. 非 lip 选择最大有效簇；
11. lip 在占比 ≥ 15% 的簇中选择最高色度簇；
12. 代表值选择簇内最接近质心的实际像素，即平方欧氏目标的实际像素 medoid；
13. 输出 HEX/RGB/Lab、有效像素、簇占比、离散度和诊断。

合法纯色区域使用 1 簇并成功返回，不设置 `uniform_region` 失败。

混合区域仍保留颜色，但按占比和离散度降低为 `medium/low` confidence，并增加风险。

模型资格为 false 的区域保留模型证据，颜色状态为：

```text
skipped_ineligible
```

## 11. 文字规范化

版本：

```text
nfkc-whitespace-latin-uppercase-1.0
```

步骤：

1. Unicode NFKC；
2. 首尾空白删除；
3. 连续空白折叠为单空格；
4. 拉丁小写字母转大写；
5. 原文始终保留，不用规范化文本覆盖原文。

## 12. CLI

### 12.1 规划

```powershell
.\.venv\Scripts\python.exe -m lipcolor_pipeline.cli quick-extract plan `
  --run-id stage2_6_e1_20260729 `
  --image-id <IMAGE_SHA256>
```

`plan`：

- 不访问 API；
- 不创建 preview 或 Stage 2.6 run；
- 在内存中确定性计算普通图 preview SHA；
- 返回图片、unit、scope、缓存和预算。

### 12.2 运行

```powershell
.\.venv\Scripts\python.exe -m lipcolor_pipeline.cli quick-extract run `
  --run-id stage2_6_e1_20260729 `
  --image-id <IMAGE_SHA256> `
  --execute-online `
  --max-calls 2
```

不提供 `--execute-online` 时只准备资产、物化缓存并保存可恢复状态。

真实调用必须同时提供：

```text
--execute-online --max-calls N
```

### 12.3 导出

```powershell
.\.venv\Scripts\python.exe -m lipcolor_pipeline.cli quick-extract export `
  --run-id stage2_6_e1_20260729
```

输出：

- `image_results.jsonl`；
- `text_items.csv`；
- `color_regions.csv`；
- `occurrence_results.csv`。

### 12.4 中断恢复

```powershell
.\.venv\Scripts\python.exe -m lipcolor_pipeline.cli quick-extract recover `
  --run-id <INTERRUPTED_RUN_ID>
```

`recover` 扫描不可变 request/raw/parsed：

- 已有 raw+parsed 的尝试恢复为 `succeeded`；
- raw 存在但 Schema 不合法的尝试恢复为 `schema_failed`；
- request 存在但无 raw 的中断尝试保守记为 `request_failed` 和一次真实尝试；
- 恢复后的 unit、canonical 结果、原图 SHA 和 pipeline 状态重新落库；
- 恢复证据写入版本化报告。

在线执行按每个完成 future 立即写 SQLite，避免等待整批完成后才持久化。

### 12.5 选择器

`plan/run` 必须且只能使用一个：

- `--image-id`；
- `--selection-manifest`；
- `--folder-group-id`；
- `--limit`。

没有默认全量选择，避免误调用全部 12,386 图。

Stage 2 run 固定在版本化配置：

```yaml
quick_extract:
  stage2_run_id: stage2_full_20260728
```

## 13. 版本化样本

锚点清单：

```text
configs/samples/stage2_6_mvp_anchor_v1.jsonl
```

共 28 图，角色配额：

```text
single_bullet             4
single_swatch             4
lip_effect                4
multi_shade_comparison    4
color_card                1
packaging                 4
text_promo                3
invalid                   4
```

清单记录：

- 原图 SHA256；
- 角色和资格；
- 标签来源；
- review provenance；
- Stage 2 working asset 和 SHA；
- Alpha、格式、尺寸、tile、occurrence、folder 和错配信息；
- challenge tags。

唯一 color-card 的标签来源明确为：

```text
owner_delegated_agent
```

清单包含：

- 4-tile 长图；
- 6-tile 长图；
- GIF/格式错配；
- Alpha；
- 多 occurrence；
- folder collision；
- 最小边 padding；
- 业务无效长图切片。

在线阶段使用同一锚点集合的版本化子集：

- E1：锚点第 1 图，1 个 unit；
- E2：10 图八角色混合子清单，共 14 个 unit；E1 缓存后新增 13 个；
- E3：完整 28 图，共 64 个 unit；成功 E2 缓存后新增 50 个。

E2 有意只保留一张 4-tile 长图，将 6-tile 和其他长图留到 E3；
这样 E2 的 18 次软预算内仍有 Schema/请求重试空间。E1/E2 未使用预算向 E3
滚动后可覆盖 50 个新增 unit。

E2 子清单：

```text
configs/samples/stage2_6_mvp_e2_v1.jsonl
```

完整目录固定为：

```text
fg_97d2a18d19f65a7ca04bc6695b5854d4
```

真实只读规划证据：

- 11 个唯一内容；
- 5 个 source record；
- 1 张 5-tile 长图；
- 共 16 个模型 unit。

Stage 2.5 候选只允许补充尚未出现的挑战样本；调试只能使用 train split，validation/test 不用于 prompt 或算法迭代。

## 14. 在线预算与阶段门禁

阶段预算：

- E1：软上限 2；
- E2：软上限 18；
- E3：软上限 45；
- E4：软上限 35。

未用预算可向后滚动。

数据库会累计统计所有 `pipeline_runs.stage='stage2.6'` 的真实供应商尝试，并强制：

```text
online_validation_hard_cap = 100
```

以下不计入真实尝试：

- `cache_hit`；
- `budget_exhausted`；
- 本地合并；
- 本地颜色计算。

达到 100 后不再发起 API 请求，未完成 unit 保持可恢复状态。

只有上一阶段满足结构门禁才进入下一阶段：

1. 成功尝试均有 request/raw/parsed；
2. 失败有明确状态和错误；
3. scope/Schema 合法；
4. 原图 SHA 无漂移；
5. 没有超预算。

## 15. 测试与验收

离线测试必须覆盖：

- 迁移 007；
- Schema、额外字段、ID 和关联；
- scope 约束；
- bbox 归一化；
- 真实 Stage 2 长图资产选择；
- preview 和坐标回映；
- 缓存键；
- 跨 run cache materialization；
- 失败/预算状态；
- NFKC 文字规范化；
- 跨 tile 文字和区域去重；
- Lab/K-means/medoid；
- Alpha/近白/近黑过滤；
- lip 高色度规则；
- 合法纯色成功；
- SQLite 和四类导出；
- 原图 SHA 不变。

Mock 集成覆盖：

- 普通图；
- global + 多 tile；
- 跨 tile 重复文字和区域；
- 部分 tile Schema 重试耗尽；
- `partial` fallback/证据；
- 颜色成功；
- 跨 run 缓存零重复 API；
- request/raw/parsed；
- 四类导出。

当前离线结果：

```text
43 passed, 1 existing Starlette deprecation warning
```

原有 30 个基线测试全部保留。

`engineering_mvp_passed` 必须同时满足：

- 成功尝试 request/raw/parsed 完整；
- 失败状态明确；
- 缓存复跑无重复调用；
- 至少出现文字、色号、多区域、颜色成功和长图回映示例；
- 原图零漂移；
- 四类导出生成；
- 在线真实调用总数 ≤ 100。

## 16. 回滚

不需要修改或恢复原图、阶段 1–2.5 数据库事实。

回滚 Stage 2.6 只需：

1. 停止新的 quick-extract 调用；
2. 保留对应 `pipeline_output/runs/<run_id>` 作为审计证据；
3. 下游不读取 Stage 2.6 四张表；
4. 如确需清理，只清理明确指定的 Stage 2.6 run/derived preview，禁止递归清理工作区根目录。

迁移 007 是向后兼容的新增表迁移，不改写既有业务表。

## 17. 本次修订记录、原因与证据

### 17.1 资产选择修订

旧设计：查询 Stage 2 `analysis_preview/vlm_input_preview/alpha_mask`。  
修订：普通图使用 `working_asset_id/alpha_asset_id` 生成 preview，长图复用 global/tile。  
原因：真实 Stage 2 full run 没有前两类 preview，Alpha 实际类型为 `alpha_mask_legacy`。  
证据：数据库资产类型计数与 `lipcolor_pipeline/preprocessing.py` 的实际注册逻辑。

### 17.2 数据库修订

旧设计：成功结果表承担 unit 状态，失败时需要伪造结果。  
修订：新增独立 `quick_extraction_units`，model run 和 asset 可空。  
原因：请求失败、预算耗尽、解码失败都可能没有解析结果。  
证据：迁移 007 约束和 Mock partial-failure 测试。

### 17.3 Schema 修订

旧设计：文字/区域 ID、关联和 scope 约束不足。  
修订：强制唯一 ID、合法关联、scope 一致、global 空列表、每资产上限和 extra forbid。  
原因：长图合并必须能保留并稳定引用模型来源。  
证据：Schema 单元测试。

### 17.4 颜色语义修订

旧设计：容易把代表色写成商品真实色，且纯色可能被视为异常。  
修订：统一 `image_observed_color_candidate`；合法纯色成功；混合区域降置信度。  
原因：图片受光照、压缩、屏幕和渲染影响，不能在本阶段宣称真实物理颜色。  
证据：纯色、Alpha、混合簇和 lip 色度单元测试。

### 17.5 样本与预算修订

旧设计：未给出可执行清单和真实 unit 数。  
修订：固化 28 图清单、E2 子清单和固定目录，并基于真实 tile 数计算 1/20/64/16 unit。  
原因：图片数不等于 API unit 数，长图必须按 global+tile 计费。  
证据：真实工作区只读 `quick-extract plan`。

### 17.6 验收修订

旧设计：在无 ground truth 时可能暗示精度结论。  
修订：工程结构门禁与准确率门禁分离，精度统一标为 `not_evaluated_without_ground_truth`。  
原因：当前 Stage 2.5 未冻结，不能用未审核模型输出证明 OCR/bbox/ΔE 精度。
