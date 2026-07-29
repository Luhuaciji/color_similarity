# Stage 2.6 Quick Extraction MVP 完成报告

日期：2026-07-29  
实现版本：0.3.0  
Workspace Schema：`stage2-6-1`  
数据库迁移：007  
最终工程状态：`engineering_mvp_passed`

## 1. 结论

Stage 2.6 MVP 已完成并通过工程验收：

```text
Stage 2 图片资产
  → Qwen 角色/文字/bbox/关联/风险
  → 原图坐标回映与长图本地合并
  → Stage 2 working/alpha 本地取色
  → SQLite + JSONL + CSV
```

最终输出语义为：

```text
image_observed_color_candidate
```

本结论只表示工程闭环、审计、缓存、恢复、取色和导出可用，不表示真实物理色识别或正式阶段 3–6 已完成。

## 2. 实现清单

### 2.1 新增

- `database/migrations/007_stage2_6_quick_extraction.sql`
  - `quick_extraction_units`
  - `quick_image_extractions`
  - `quick_text_items`
  - `quick_color_regions`
- `lipcolor_pipeline/quick_extract_schemas.py`
  - 严格 Pydantic Schema；
  - scope、ID、bbox、关联和单资产列表上限；
  - fence 去除和明确 bbox 归一化；
- `lipcolor_pipeline/color_extraction.py`
  - sRGB → Lab D65；
  - 固定 seed K-means；
  - 实际像素 medoid；
  - Alpha/近白/近黑过滤；
  - lip 高色度簇规则；
- `lipcolor_pipeline/quick_extract.py`
  - 真实 Stage 2 资产选择；
  - 普通图 preview；
  - 调用、缓存、重试和硬预算；
  - 中断 artifact recovery；
  - 长图本地合并；
  - 本地取色；
  - SQLite 和四类导出；
- `configs/prompts/quick_extract_v1.txt`
- `configs/samples/stage2_6_mvp_anchor_v1.jsonl`
- `configs/samples/stage2_6_mvp_e2_v1.jsonl`
- `configs/samples/stage2_6_mvp_e3_recovery_v1.jsonl`
- `tests/test_quick_extract.py`

### 2.2 修改

- `configs/pipeline.yaml`
- `lipcolor_pipeline/settings.py`
- `lipcolor_pipeline/workspace.py`
- `lipcolor_pipeline/cli.py`
- `lipcolor_pipeline/__init__.py`
- `pyproject.toml`
- `docs/stage2_6_quick_extraction_codex_plan.md`

### 2.3 CLI

已提供：

```text
quick-extract plan
quick-extract run
quick-extract export
quick-extract recover
```

`plan/run` 强制且只能使用一个选择器：

```text
--image-id
--selection-manifest
--folder-group-id
--limit
```

在线调用必须同时提供：

```text
--execute-online --max-calls N
```

## 3. 实际资产路径

实现未使用不存在的 Stage 2 preview。

普通图：

- 来源：`image_preprocessing_observations.working_asset_id`；
- Alpha：`alpha_asset_id`；
- VLM 输入：确定性 `quick_vlm_preview`；
- 最大长边 2,048；
- JPEG quality 92；
- 小于 11 px 的边使用白色居中 padding。

长图：

- global：复用 `long_image_layouts.global_thumbnail_asset_id`；
- tile：复用有序 `image_tiles.tile_asset_id`；
- 不重新切图。

最终颜色始终从 Stage 2 working/alpha 裁剪，不从 preview/global/tile JPEG 取色。

## 4. 样本清单

28 图锚点配额：

| 角色 | 数量 |
|---|---:|
| single_bullet | 4 |
| single_swatch | 4 |
| lip_effect | 4 |
| multi_shade_comparison | 4 |
| color_card | 1 |
| packaging | 4 |
| text_promo | 3 |
| invalid | 4 |

清单包含：

- 4-tile；
- 6-tile；
- GIF/格式错配；
- Alpha；
- 多 occurrence；
- folder collision；
- 1 px 最小边；
- 业务无效长图切片。

唯一 color-card 的标签来源是 `owner_delegated_agent`。

完整目录：

```text
fg_97d2a18d19f65a7ca04bc6695b5854d4
```

数据库复核：

- 11 个唯一内容；
- 5 个 source record；
- 10 个普通图；
- 1 张 5-tile 长图；
- 16 个模型 unit。

## 5. 离线测试

最终结果：

```text
43 passed, 1 warning
```

warning 是原有 Starlette/httpx deprecation warning，不是 Stage 2.6 失败。

原有 30 个基线测试全部通过。

新增测试覆盖：

- 迁移 007；
- Schema extra/ID/关联/scope/上限；
- bbox 确定性归一化；
- 真实 Stage 2 long asset 选择；
- preview 和坐标变换；
- 缓存键；
- 跨 run cache materialization；
- 中断 artifact recovery；
- 普通图 Mock；
- global + 多 tile Mock；
- 跨 tile 文字/区域去重；
- 部分 tile 失败；
- SQLite 和四类导出；
- NFKC/空白/拉丁大写；
- Lab/K-means/medoid；
- Alpha/背景过滤；
- lip 高色度规则；
- 合法纯色成功；
- 原图 SHA 不变。

## 6. 在线验证

### 6.1 调用总账

所有 Stage 2.6 真实供应商尝试：

```text
94 / 100
```

剩余未使用：

```text
6
```

状态总计：

| model run 状态 | 数量 | 是否计入真实尝试 |
|---|---:|---|
| succeeded | 85 | 是 |
| schema_failed | 7 | 是 |
| request_failed | 2 | 是，按中断请求保守计数 |
| cache_hit | 32 | 否 |
| budget_exhausted | 1 | 否，未访问供应商 |

真实尝试：

```text
85 + 7 + 2 = 94
```

### 6.2 分阶段结果

| 阶段/run | 图片 | unit | 新调用 | 缓存 | 结果 |
|---|---:|---:|---:|---:|---|
| `stage2_6_e1_20260729` | 1 | 1 | 2 | 0 | 关联置信度 Schema 失败，保留证据 |
| `stage2_6_e1_retry1_20260729` | 1 | 1 | 1 | 0 | 1 success |
| `stage2_6_e1_cache_20260729` | 1 | 1 | 0 | 1 | 零调用缓存成功 |
| `stage2_6_e2_20260729` | 10 | 14 | 13 | 1 | 10 success |
| `stage2_6_e3_20260729` | 28 | 64 | 55 | 14 | 恢复后 26 success / 1 partial / 1 failed |
| `stage2_6_e3_recovery_20260729` | 2 | 7 | 7 | 0 | 两张补偿图全部 success |
| `stage2_6_e4_20260729` | 11 | 16 | 16 | 0 | 11 success |
| `stage2_6_e4_cache_20260729` | 11 | 16 | 0 | 16 | 零调用缓存成功 |

### 6.3 Prompt 修订证据

Prompt 1.0.0：

- 模型给出非空 `linked_text_item_ids`，但关联置信度为空；
- 严格 Schema 拒绝；
- 没有本地伪造置信度。

Prompt 1.0.1：

- 明确非空关联必须有 0–1 置信度；
- 品牌/宣传语不得错误关联颜色；
- E1/E2 通过。

E3 基础 run：

- 两个高文字密度 unit 输出 21–23 条；
- 严格单资产 20 条上限拒绝；
- 没有本地截断冒充模型原始结果。

Prompt 1.0.2：

- 明确最多 20，候选更多时只返回最高置信度的 20；
- 补偿 run 的普通图严格返回 20；
- 5-tile 长图每 tile 不超过 20，整图合并后合法得到 65 条 canonical 文字；
- 7/7 unit 首次通过。

## 7. 中断与恢复

E3 外层执行达到 10 分钟命令上限时：

- request：69；
- raw：67；
- parsed：62；
- 当时 SQLite 已有 14 个 cache hit。

最初批执行是在所有 future 收齐后统一写 SQLite，暴露了中断窗口。实现随后修复为：

- 每个完成 future 立即写 model run 和 unit；
- `quick-extract recover` 扫描不可变 artifact；
- raw+parsed 恢复为成功；
- raw 无 parsed 重新做本地严格解析；
- request 无 raw 保守记为 `request_failed` 和一次真实尝试；
- 重新聚合 canonical 结果；
- 重新检查原图 SHA；
- final pipeline 状态和 recovery 报告落库。

E3 实际恢复：

- 48 succeeded；
- 5 schema_failed；
- 2 request_failed；
- 55 次真实尝试全部记账；
- unmatched artifact：0。

该事件没有丢失 raw/parsed，也没有重复计算已恢复成功的模型结果。

## 8. 结果与示例

### 8.1 E2

- 10/10 图片成功；
- 65 条 canonical 文字；
- 32 个 canonical 区域；
- 31 个颜色成功；
- 1 个 `skipped_ineligible`。

色号示例：

```text
image_id:
62286e921786098226430b62a563d9118b0b6290a2a9ae4560e4ac9ff77463b1

visible shade code: N01
lip observed candidate:    #C8695C / [200,105,92]
swatch observed candidate: #CE7357 / [206,115,87]
```

同一可见色号在 lip 与 swatch 区域得到不同图片像素候选，说明本阶段没有错误宣称“真实统一物理色”。

### 8.2 E3

基础 run：

- 413 条 canonical 文字；
- 154 个区域；
- 153 个颜色成功；
- 1 个 `skipped_ineligible`。

补偿 run：

- 2/2 图片成功；
- 85 条 canonical 文字；
- 19 个区域；
- 19 个颜色成功。

### 8.3 E4 完整目录

- 11/11 图片成功；
- 73 条 canonical 文字；
- 38 个区域；
- 38 个颜色成功；
- 5 条 `shade_code` 文字；
- 1 张 5-tile 长图完成回映和去重。

长图示例：

```text
image_id:
206d36e728f515e14e01d92c446fdc1e058d448d51f947f2ce2b412d00d8f70d

shade code/name: 510 / LADY BUG
original-image bbox:
[0.0, 5713.92, 180.0, 6164.48)
observed candidate:
#C3292C
sources:
tile 3 + tile 4
```

该 canonical 区域保留两个 tile 的模型 region、文字关联、bbox 和风险证据。

E3 补偿 + E4 中有 32 个颜色因混合程度被标为 `low` confidence，但颜色和诊断均保留。

## 9. 审计

数据库/文件终审：

- migration max：7；
- 成功或缓存 model run：117；
- request/raw/parsed 完整三件套：117/117；
- 失败/预算记录：10；
- 有明确 error JSON：10/10；
- request manifest 白名单违规：0；
- Stage 2.6 文件中的 API key 命中：0；
- 最终验证涉及唯一图片：39；
- 重算原图 occurrence：111；
- 原图缺失：0；
- 原图 SHA 不一致：0。

缓存复跑：

- E1：1 cache hit，0 调用；
- E4：16 cache hit，0 调用；
- E4 缓存结果与原 run 的图片/文字/区域/颜色计数一致。

## 10. 导出

每个完成 run 生成：

- `image_results.jsonl`
- `text_items.csv`
- `color_regions.csv`
- `occurrence_results.csv`

E4 导出：

| 文件 | 行语义/数量 | SHA256 |
|---|---:|---|
| `image_results.jsonl` | 11 images | `faa6ba993c3e093c00f8dc583760d34519b9386c05f3c0ecae86f2320a6c8efd` |
| `text_items.csv` | 73 text items | `e78780034cee11192a165970796d7c7523ce0aa0becaeb0899cd2bf299230007` |
| `color_regions.csv` | 38 regions | `501729778eafe16accb9075735806a8ec075469cfcd1c1d172a421ca8833680c` |
| `occurrence_results.csv` | 15 occurrences | `ae2113b3544ff124b6b6e4960be4046dcdc7e36b1a01ec7830981577d4be65b9` |

E3 基础 run 也已导出：

- 28 images；
- 413 text items；
- 154 color regions；
- 96 occurrences。

补偿 run 单独导出两张严格上限修复后的完整结果。

## 11. MVP 验收对照

| 验收项 | 结果 |
|---|---|
| 成功尝试有 request/raw/parsed | 通过，117/117（含缓存） |
| 失败有明确状态 | 通过，10/10 |
| 缓存复跑零重复调用 | 通过 |
| 有文字示例 | 通过 |
| 有色号示例 | 通过 |
| 有多区域示例 | 通过 |
| 有颜色成功示例 | 通过 |
| 有长图回映示例 | 通过 |
| 原图零漂移 | 通过，111/111 |
| 四类导出 | 通过 |
| 真实 API 总数 ≤ 100 | 通过，94 |

最终状态：

```text
engineering_mvp_passed
```

## 12. 未评估与限制

以下项目没有冻结 ground truth，因此为：

```text
not_evaluated_without_ground_truth
```

- OCR 字符准确率；
- 色号准确率；
- bbox IoU 精度；
- 文字—区域配对准确率；
- mask 精度；
- HEX/RGB/Lab 对真实物理色的精度；
- ΔE；
- 角色分类准确率。

已知限制：

1. bbox 是矩形，不是分割 mask；
2. 反光、阴影、渐变、肤色或背景仍可能进入 crop；
3. K-means/medoid 是图片像素代表，不是仪器测色；
4. 模型可能误读可见文字；
5. 单模型资产最多 20 条文字和 20 个区域；
6. 长图整图合并结果可以超过 20；
7. 低 confidence 颜色需要未来 ground truth 或人工评估；
8. 本阶段不自动进入阶段 3–6。

## 13. 回滚

本实现没有修改原始图片。

回滚时：

1. 停止调用 `quick-extract`；
2. 下游停止读取四张 Stage 2.6 表；
3. 保留 `pipeline_output/runs/stage2_6_*` 作为审计证据；
4. 如需清理，只针对明确 run ID 和该 run 的 `quick_vlm_preview`；
5. 不删除 Stage 1–2.5 run；
6. 不删除或覆盖 Stage 2 working/alpha/global/tile；
7. 不对 workspace 根目录执行递归删除。

迁移 007 只新增表和索引，不改写既有业务表。
