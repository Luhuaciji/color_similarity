# 口红/唇部彩妆图片流水线分阶段实施计划

> 编制日期：2026-07-27  
> 依据：`docs/repository_audit.md` 与 `docs/codex_lip_color_pipeline_guide.md`  
> 状态：设计计划，尚未开始实现  
> 原则：先建立可追溯数据底座，再逐步增加模型、颜色计算和知识数据库能力

## 1. 范围与非目标

本计划覆盖：

- 源 CSV、下载引用、物理图片和派生资产的完整血缘；
- 基础预处理、去重和质量检测；
- 图片角色和代表色资格分类；
- OCR、VLM 结构化信息抽取；
- 单色与多色号图片的区域、文字和颜色处理；
- 商品/色号实体归一化、多图融合和知识数据库；
- 人工审核、评估和持续学习数据回流。

本计划不把以下事项塞入第一版：

- 立即全量调用付费 VLM；
- 直接训练专用大模型或 LoRA；
- 在没有固定评估集前调高并发跑全量；
- 覆盖、移动、重命名或删除 `downloaded_images/`；
- 把 VLM 生成的 HEX 当最终代表色；
- 在来源冲突时覆盖原始值；
- 在本轮审计后直接开始实现所有阶段。

## 2. 审计后确定的架构决策

### 2.1 三层图片身份

```text
image_id
    = 完整 SHA256，表示相同字节内容

image_occurrence_id
    = 数据快照 + 相对路径，表示某个物理来源位置

source_ref_id
    = CSV 来源行 + pic_list/show_pic + 序号 + URL，表示业务来源引用
```

关系：

```text
一个 image_id
    ├── 可有多个 image_occurrence_id
    └── 可被多个 source_ref_id 引用
```

现有预处理 `image_id` 不删除，迁移为 `legacy_image_id`。

### 2.2 商品目录只作为分组候选

```text
folder_group_id ≠ product_id ≠ shade_id ≠ source_sku_id
```

- `folder_group_id` 反映当前物理目录；
- `source_record_id` 反映 CSV 行；
- `product_id` 表示归一化后的产品/系列实体；
- `shade_id` 表示归一化后的色号实体；
- `source_sku_id` 保留源 `sku_id`，但源数据出现过重复，不能单独作为数据库主键。

### 2.3 SQLite 是事务主库，Parquet 是批处理快照

- SQLite：约束、关系、审核状态、迁移和查询；
- Parquet：宽表、批处理、统计和训练数据导出；
- JSONL/CSV：兼容现有人工查看和外部交换；
- 文件系统：原始模型响应、mask、overlay、工作图和报告；
- 数据库只存派生资产路径、哈希、版本和元数据。

### 2.4 每个运行都有不可变指纹

最少保存：

```text
run_id
pipeline_version
schema_version
git_commit
config_json
config_hash
dependency_snapshot
dataset_snapshot_id
started_at
finished_at
status
```

派生资产缓存键：

```text
SHA256(
    image_id
    + transform_or_model_name
    + implementation_version
    + config_hash
    + prompt_version
    + model_parameters
)
```

### 2.5 模型结果双留存

每次模型调用必须同时保留：

- 序列化请求或请求哈希；
- 原始响应；
- 解析结果；
- Schema 校验结果；
- 模型、提示词、参数、token、延迟和错误；
- 与 `image_id`、`image_occurrence_id`、`run_id` 的关系。

## 3. 数据库基础约定

所有表至少遵循：

- 主键不使用可变绝对路径；
- 时间使用带时区 ISO-8601/UTC；
- JSON 字段有 `schema_version`；
- 自动结果保存 `method_version` 或 `model_run_id`；
- 低置信度、冲突和人工修改单独留痕；
- 不物理删除原始证据记录；
- 相对路径相对于明确的 root alias，而不是依赖单机绝对路径。

核心基础表：

### `dataset_snapshots`

```text
dataset_snapshot_id
source_type
source_path
source_sha256
row_count
column_schema_json
created_at
```

### `pipeline_runs`

```text
run_id
dataset_snapshot_id
stage
pipeline_version
schema_version
git_commit
config_json
config_hash
dependency_snapshot_json
started_at
finished_at
status
error_summary_json
```

### `source_records`

```text
source_record_id
dataset_snapshot_id
row_number
row_hash
asset_id_raw
sku_id_raw
goods_id_raw
brand_id_raw
brand_name_raw
sku_name_raw
sku_concat_name_raw
sku_color_no_raw
raw_record_json
```

### `source_image_refs`

```text
source_ref_id
source_record_id
source_field
image_index
source_url
source_url_hash
declared_extension
download_status
http_metadata_json
```

### `image_contents`

```text
image_id
sha256
byte_size
detected_format
mime_type
first_seen_at
```

### `image_occurrences`

```text
image_occurrence_id
image_id
folder_group_id
relative_path
filename
extension
extension_mismatch
brand_folder_raw
product_folder_raw
legacy_image_id
source_exists
```

### `source_ref_occurrences`

```text
source_ref_id
image_occurrence_id
match_method
match_confidence
```

### `derived_assets`

```text
derived_asset_id
image_id
image_occurrence_id
run_id
asset_type
relative_path
sha256
width
height
format
transform_name
transform_version
transform_fingerprint
```

### `pipeline_errors`

```text
error_id
run_id
image_id
image_occurrence_id
stage
error_code
error_type
message
details_json
retryable
created_at
```

后续阶段只在这些基础表之上增量增加业务表。

## 4. 阶段总览

| 阶段 | 名称 | 依赖 | 阶段门 |
|---|---|---|---|
| 0 | 仓库与数据审计 | 无 | 本轮已完成 |
| 1 | 来源清单、稳定 ID 与最小数据库 | 阶段 0 | 所有图片和 CSV 引用可追溯 |
| 2 | 基础预处理加固与历史产物迁移 | 阶段 1 | 严格解码、派生资产和缓存可复现 |
| 3 | 图片角色与代表色资格分类 | 阶段 2 | 固定评估集达标，模型结果双留存 |
| 4 | OCR、文件夹和源字段信息抽取 | 阶段 3 | 文字、实体候选和冲突均有证据 |
| 5 | 单色图区域与代表色候选 | 阶段 3、4 | 高质量单色图颜色基线达标 |
| 6 | 多色号、长图和文字—色块匹配 | 阶段 4、5 | 清晰多色号图匹配达标 |
| 7 | 实体归一化、多图融合和知识数据库 | 阶段 4–6 | 最终结论均能回溯证据 |
| 8 | 人工审核、评估、回归和持续学习 | 阶段 3–7 | 低置信度闭环和版本化评估 |

## 5. 阶段 0：仓库与数据审计

### 输入

- `AGENTS.md`
- 主设计文档和历史设计文档
- 代码、依赖和配置
- 源 CSV
- `downloaded_images/`
- `image_preprocessing_output/`

### 输出

- `docs/repository_audit.md`
- `docs/implementation_plan.md`
- 主指南显式变更记录

### 数据库字段

本阶段不创建数据库。计划字段在本文定义。

### 测试方式

- 目录层级和文件集合核对；
- CSV 重放与实际路径集合比较；
- 预处理元数据与源文件集合比较；
- 统计和视觉抽样交叉验证；
- Git 工作树检查。

### 验收标准

- 31,511 张图片全部纳入统计；
- 品牌、商品目录和文件层级有实证；
- 格式、尺寸、失败和每商品图片数有明确口径；
- 原始数据无修改；
- 不启动后续模块实现。

## 6. 阶段 1：来源清单、稳定 ID 与最小数据库

### 目标

先修复数据血缘。此阶段不做角色分类和颜色提取。

### 输入

- 源 CSV 原始字节；
- `download_product_images.py` 的解析/清洗逻辑；
- `downloaded_images/`；
- 现有预处理元数据；
- 审计确认的 16 个目录碰撞组和 9 组品牌别名。

### 输出

- CSV `dataset_snapshot`；
- source record、source image ref、image content、image occurrence manifest；
- SQLite 初始 schema 和迁移版本；
- Parquet/JSONL manifest；
- 来源映射冲突报告；
- 旧 `image_id` 到新 `image_id`/`image_occurrence_id` 映射表；
- 只读原图完整性基线。

### 数据库表和字段

使用第 3 节全部基础表，并增加：

#### `folder_groups`

```text
folder_group_id
dataset_snapshot_id
brand_folder_raw
product_folder_raw
relative_folder_path
source_record_count
image_occurrence_count
collision_status
```

#### `brand_alias_candidates`

```text
alias_candidate_id
brand_id_raw
brand_folder_raw
canonical_brand_id
evidence_json
status
```

### 实现要点

1. 对 CSV 原始文件计算 SHA256；
2. 每行用“快照 + 行号 + 行哈希”生成 `source_record_id`；
3. 对每个图片 URL 生成 `source_ref_id`；
4. 用当前文件名 URL 哈希规则建立 source ref 到 occurrence 的关系；
5. 物理文件完整 SHA256 作为 `image_id`；
6. 同一内容的多个路径全部保留；
7. 16 个目录碰撞组标记为 `multi_source_record`，不自动合并色号；
8. 现有路径相关 ID 保存为 `legacy_image_id`；
9. 轮换泄露的 API Key，并从代码移除；补 `.env.example`，但不保存真实值。

### 测试方式

单元测试：

- 路径清洗；
- URL 列表解析；
- 双斜杠 URL 后缀；
- dataset/source/ref/content/occurrence ID 稳定性；
- 相同内容不同路径；
- 同一路径不同快照；
- 重复 `sku_id`；
- 目录碰撞；
- 品牌别名候选；
- Windows/Unicode/NBSP 文件名。

集成测试：

- 固定 3 个品牌、包含一个目录碰撞组；
- 当前完整 CSV 的只读 manifest 构建；
- SQLite 外键、唯一约束和迁移回滚；
- CSV/Parquet/SQLite 行数一致性。

### 验收标准

- `dataset_snapshots=1`，`source_records=2309`；
- 原始 URL 引用 31,513 条全部保留；
- 31,511 个物理 occurrence 全部有记录；
- `image_contents=12,386`，与唯一 SHA256 一致；
- 16 个目录碰撞组和 9 个品牌别名组均未静默合并；
- 每个 source ref 能链接 occurrence，或有明确未匹配原因；
- 每个数据库图片记录可回溯到物理路径和至少一个来源上下文；
- 输入文件抽样前后 SHA256 不变；
- 密钥扫描不再发现受跟踪明文 Key。

## 7. 阶段 2：基础预处理加固与历史产物迁移

### 目标

复用现有预处理代码，补齐运行指纹、格式检测、极端尺寸和不可覆盖日志。

### 输入

- 阶段 1 manifest 和 SQLite；
- `downloaded_images/` 只读图片；
- 现有 `image_preprocessing_output/`；
- `image_preprocessing_pipeline/preprocess_product_images.py`。

### 输出

- 模块化预处理包；
- 版本化运行目录；
- 工作图、Alpha Mask、质量指标和错误记录；
- 历史 1.1.0 产物迁移清单；
- 格式错配、长图、装饰条和超大图片报告；
- 兼容 CSV/JSONL 导出。

### 数据库表和字段

#### `image_preprocessing_observations`

```text
preprocess_observation_id
image_id
run_id
decode_status
decode_recovered
source_format
source_mode
width
height
frame_count
selected_frame
exif_orientation
orientation_corrected
icc_status
working_color_space
converted_to_srgb
has_alpha
transparent_pixel_ratio
working_asset_id
alpha_asset_id
quality_json
transform_fingerprint
```

#### `duplicate_edges`

```text
duplicate_edge_id
image_id_a
image_id_b
method
distance
threshold_version
confidence_class
run_id
review_status
```

#### `quality_flags`

```text
quality_flag_id
image_id
run_id
flag_code
metric_value
threshold_version
severity
```

### 实现要点

- 按魔数/解码结果确定格式和 MIME；
- 把 GIF 纳入支持，明确单帧/多帧策略；
- 对 229 个格式错配文件保留原文件名并显式标记；
- 对长图生成不覆盖原图的 tile 派生资产；
- tile 保存原图坐标和重叠范围；
- 将 750×1 等文件标记为 `semantic_invalid_candidate`；
- 对 Pillow 安全阈值、自定义硬上限和分析副本阈值统一配置；
- `corrupt`、`policy_rejected` 和 `recovered` 分开；
- transform fingerprint 不匹配时禁止复用旧工作图；
- 日志按 `run_id` 写新文件，不覆盖；
- 旧产物只登记和验证，不静默改写。

### 测试方式

在现有两项测试基础上补：

- sRGB、Display-P3/非 sRGB、无 ICC、损坏 ICC；
- CMYK、灰度、P 模式、RGBA、全透明；
- EXIF 1–8；
- 截断文件严格失败和可配置恢复；
- 100 MP 策略拒绝；
- GIF 伪装 `.jpg`、PNG 伪装 `.jpg`；
- 单帧/多帧；
- 750×1、超长图和 tile 坐标回映；
- 配置变化后缓存失效；
- 同配置重复运行完全复用；
- 日志不覆盖；
- 旧 metadata 迁移。

### 验收标准

- 31,511 个 occurrence 无静默遗漏；
- 当前快照中 31,508 个严格成功、2 个截断、1 个策略拒绝能稳定复现；
- 229 个格式错配全部被标记；
- 原图与阶段 1 SHA256 完全一致；
- 所有派生资产带 hash、run 和 transform fingerprint；
- 相同输入/配置重复运行结果 hash 一致；
- 配置或实现版本改变时不会误复用旧工作图；
- 单图失败不影响其余记录落库；
- 单元和小型集成测试通过。

## 8. 阶段 3：图片角色与代表色资格分类

### 目标

在唯一内容层调用模型，在 occurrence 层保留上下文，形成角色分类最小闭环。

### 输入

- 阶段 2 工作图、Alpha、tile 和质量指标；
- 文件夹和来源字段上下文；
- 主指南八类核心角色；
- 固定人工标注集；
- `qwen3.6-plus` 安全调用配置。

### 输出

- 图片/tile 角色 JSON；
- 代表色资格和推荐策略；
- VLM 原始响应与解析结果；
- 缓存和错误记录；
- 分类评估报告、混淆矩阵和抽样 HTML/overlay；
- 低置信度审核任务。

### 数据库表和字段

#### `model_runs`

```text
model_run_id
run_id
image_id
derived_asset_id
model_name
provider
base_url_alias
prompt_name
prompt_version
schema_version
generation_parameters_json
request_hash
request_path
raw_response_path
parsed_response_path
response_hash
latency_ms
token_usage_json
status
error_json
```

#### `image_roles`

```text
image_role_id
image_id
image_occurrence_id
tile_asset_id
primary_role
secondary_roles_json
layout_type
role_confidence
contains_text
contains_multiple_shades
contains_lips
contains_skin_swatch
contains_product_bullet
contains_packaging
representative_color_eligible
eligibility_score
recommended_strategy
rejection_reasons_json
model_run_id
```

### 实现要点

- 同 SHA256 和同模型/提示词只调用一次；
- occurrence 上下文另存为 evidence，不改变图像事实；
- 长图先 tile，再汇总成整图角色；
- `pic_list`/`show_pic` 仅作来源特征，不作规则真值；
- JSON 解析、枚举、坐标和置信度强校验；
- 修复/重试次数有限；
- 任何响应都先保存原始值；
- 默认离线测试用 mock/缓存，在线测试显式开启。

### 测试方式

- 至少 400 张固定人工标注图，按品牌、内容 SHA、来源字段、格式、质量和长图分层；
- 覆盖八类角色、组合图、长图 tile 和无效装饰条；
- 单元测试 Data URL MIME、缓存键、Schema、错误分类；
- mock API 集成测试；
- 小批显式在线冒烟测试；
- 回归比较每次提示词变化。

### 验收标准

- 角色 Macro-F1 ≥ 0.85；
- 代表色资格 Precision ≥ 0.90；
- 所有核心角色在评估集中都有足够样本，不以总体准确率掩盖小类；
- 100% 模型调用有原始响应和解析结果；
- Schema 最终失败全部落错误表/审核队列；
- 相同内容不重复计费；
- 长图 tile 结果可回映原图坐标；
- 包装/文字图误入颜色阶段的假阳性受控。

## 9. 阶段 4：OCR、文件夹和源字段信息抽取

### 目标

建立“原始事实—证据—规范化候选—冲突”的信息抽取层。

### 输入

- 阶段 1 原始 CSV 行和文件夹；
- 阶段 2 工作图/tile；
- 阶段 3 角色、布局和可抽取资格；
- OCR 引擎；
- VLM 结构化语义结果。

### 输出

- OCR 原始框、文本和置信度；
- 文件夹/`sku_name`/`sku_concat_name` 可追溯解析；
- 品牌、系列、产品、色号、质地等候选；
- 来源冲突和异常报告；
- OCR/实体抽取评估报告。

### 数据库表和字段

#### `ocr_runs`

```text
ocr_run_id
run_id
engine
engine_version
config_hash
raw_response_path
status
error_json
```

#### `ocr_spans`

```text
ocr_span_id
image_id
image_occurrence_id
tile_asset_id
text_raw
text_normalized
bbox_tile_json
bbox_image_json
confidence
language
ocr_run_id
```

#### `folder_parse_runs`

```text
folder_parse_run_id
run_id
parser_name
parser_version
```

#### `evidence_claims`

```text
claim_id
entity_type
entity_candidate_id
field_name
candidate_value_json
source_type
source_id
source_location_json
confidence
method
method_version
status
conflict_group_id
```

### 实现要点

- 所有源字段保留 `_raw`；
- `sku_color_no` 只作为低可信候选，不能覆盖 `sku_name`/OCR；
- 目录名、CSV、OCR 和 VLM 分别建 evidence claim；
- 文件夹 N19 只表示上下文，不证明图中只含 N19；
- OCR 框保留 tile 和原图两套坐标；
- 品牌优先使用 `brand_id_raw` 和别名表；
- 冲突不覆盖，统一进入 conflict group。

### 测试方式

- 目录碰撞组、品牌别名组和重复 `sku_id` 固定 fixture；
- 中英混合、编号、容量、色号名、NBSP 和标点归一化；
- OCR 清晰/模糊/长图 tile；
- OCR 与目录冲突；
- 同一图片多色号；
- 离线 OCR 回归集和缓存 VLM 响应。

### 验收标准

- 清晰色号编号 OCR 准确率 ≥ 0.95；
- 原始 OCR 框、文本和引擎版本 100% 留存；
- 16 个目录碰撞组不被错误压成单一色号；
- 9 组品牌目录别名可追溯到源 `brand_id`；
- `sku_color_no=3g/5ml` 等异常不会被写成确认色号；
- 每个规范化候选都有至少一个 source claim；
- 所有冲突都有类型、双方证据和审核状态。

## 10. 阶段 5：单色图区域与代表色候选

### 目标

先处理高价值、边界较清楚的单色试色、膏体和规则色块。

### 输入

- 阶段 2 sRGB 工作图和 Alpha；
- 阶段 3 角色/资格/候选框；
- 阶段 4 OCR 和实体上下文；
- 人工标注 mask/框小样本。

### 输出

- 区域框、polygon、mask；
- 有效像素诊断；
- RGB/Lab/LCh/HEX 候选；
- 高光、阴影、背景、色偏和离散度；
- overlay、色块和评估报告；
- 低质量审核任务。

### 数据库表和字段

#### `regions`

```text
region_id
image_id
image_occurrence_id
region_type
bbox_json
polygon_json
mask_asset_id
detector
detector_version
segmenter
segmenter_version
detection_confidence
mask_quality_json
```

#### `image_color_candidates`

```text
color_candidate_id
image_id
image_occurrence_id
region_id
source_role
raw_srgb_rgb_json
raw_srgb_hex
raw_srgb_lab_json
corrected_rgb_json
corrected_lab_json
correction_method
correction_confidence
method
method_version
valid_pixel_count
dominant_cluster_ratio
within_cluster_delta_e_p50
within_cluster_delta_e_p95
confidence
diagnostics_json
```

### 实现要点

- VLM 只给语义位置，不给最终权威 HEX；
- 先分割再做颜色计算；
- Alpha Mask 必须参与有效像素判断；
- 裸色/灰调不能用过强饱和度过滤；
- 光泽产品保留主体颜色并单独记录高光；
- 无可靠参考不做强白平衡；
- 原始 sRGB 候选和任何校正候选分开；
- 所有 mask/overlay 版本化。

### 测试方式

- 至少 150 张高质量单色图人工框/mask/颜色基线；
- 透明、白底、黑底、肤色、强高光、裸色、深色；
- 颜色空间和 ΔE00 单元测试；
- mask IoU、背景误选率、高光误选率；
- 相同配置的确定性回归；
- 人工选区与自动选区颜色对比。

### 验收标准

- 高质量单色图自动代表色与人工选区中位数 ΔE00 ≤ 5；
- 代表色资格假阳性不高于阶段 3 门槛；
- mask IoU 达到预先冻结的基线目标，建议首版 ≥ 0.75；
- 100% 候选带 region、有效像素数、算法版本和诊断；
- 包装、背景、肤色和高光误选有独立指标；
- 任何最终颜色都不是仅由 VLM HEX 产生。

## 11. 阶段 6：多色号、长图和文字—色块匹配

### 目标

处理多区域、长图、色卡、手臂多色试色和多唇部对比。

### 输入

- 阶段 2 tile 与坐标回映；
- 阶段 3 多色号/布局结果；
- 阶段 4 OCR 框和实体候选；
- 阶段 5 区域和颜色算法。

### 输出

- 多个独立色块/膏体/唇部区域；
- 色号—名称—区域匹配；
- 布局、阅读顺序和替代匹配；
- 每色号独立颜色候选；
- 长图跨 tile 合并结果；
- 歧义和数量不一致审核任务。

### 数据库表和字段

#### `region_text_links`

```text
region_text_link_id
region_id
ocr_span_id
relation_type
match_score
match_method
match_version
ambiguity
alternative_matches_json
```

#### `image_shade_mentions`

```text
image_shade_mention_id
image_id
image_occurrence_id
shade_code_raw
shade_name_raw
context_type
region_id
linked_ocr_span_ids_json
confidence
status
```

#### `layout_observations`

```text
layout_observation_id
image_id
layout_type
estimated_region_count
estimated_text_count
reading_order_json
tile_merge_json
model_run_id
```

### 实现要点

- 同一商品目录内显式区分 `context_shade` 和 `depicted_shades`；
- 检测色块数、文字数和候选 shade 数；
- 规则评分 + 二分图/匈牙利匹配；
- 复杂布局由 VLM 复核，但保留几何证据；
- 长图 tile 合并要处理重叠区和重复 OCR；
- 色块与文本必须保存坐标和连线；
- 不确定匹配保留替代候选。

### 测试方式

- 至少 100 张清晰多色号图人工配对；
- 包含橘朵 N18/N19/N03/N05 抽样图；
- 网格、横排、竖排、手臂试色、多唇部和长图；
- 色块数/文字数不一致；
- OCR 换行、跨 tile、连接线和裁剪；
- tile 合并无重复/漏检回归。

### 验收标准

- 清晰多色号图色号—色块匹配准确率 ≥ 0.90；
- 完全正确图片比例单独报告；
- 歧义/数量不一致检测召回率达到冻结目标，建议首版 ≥ 0.90；
- 100% 匹配保留文字、区域和空间坐标；
- 长图结果可回映原图且无重复计数；
- 低置信度匹配不自动写成确认色号。

## 12. 阶段 7：实体归一化、多图融合和知识数据库

### 目标

把来源、OCR、图像、区域和颜色候选融合为可审计的品牌/产品/色号知识层。

### 输入

- 阶段 1 source/folder/image 血缘；
- 阶段 4 evidence claims；
- 阶段 5 单图颜色候选；
- 阶段 6 多色号匹配；
- 人工别名和审核结果。

### 输出

- 规范品牌、产品、系列和色号；
- source record 到实体的链接；
- 商品/色号级代表色和多视角颜色；
- 新色号候选；
- 冲突、离群点和置信度；
- 查询与导出接口。

### 数据库表和字段

#### `brands`

```text
brand_id
canonical_name
english_name
aliases_json
status
```

#### `products`

```text
product_id
brand_id
canonical_product_name
product_type
series_name
status
```

#### `shades`

```text
shade_id
product_id
shade_code
shade_name
shade_aliases_json
normalized_descriptor_json
status
```

#### `source_entity_links`

```text
source_entity_link_id
source_record_id
entity_type
entity_id
relation_type
confidence
evidence_summary_json
review_status
```

#### `image_entity_links`

```text
image_entity_link_id
image_id
image_occurrence_id
region_id
entity_type
entity_id
relation_type
confidence
evidence_ids_json
```

#### `shade_representative_colors`

```text
shade_color_id
shade_id
view_type
hex
rgb_json
lab_json
lch_json
fusion_method
fusion_version
evidence_image_count
accepted_image_count
rejected_image_count
cross_image_delta_e_median
cross_image_delta_e_max
confidence
evidence_summary_json
status
```

#### `shade_candidates`

```text
shade_candidate_id
product_id
shade_code_raw
shade_name_raw
evidence_claim_ids_json
confidence
status
```

### 实现要点

- 产品、色号和容量/包装版本分开；
- 文件夹只作为证据；
- 品牌别名通过源 `brand_id` 和人工映射归一化；
- 先按 shade 收集图片，再在 Lab 空间稳健融合；
- 完全重复内容不重复计权；
- 近重复候选默认不自动合并；
- 双峰颜色不强制平均；
- 每个最终字段可反查 evidence claim、图片、区域和运行版本；
- 新色号只进入候选，不能自动确认。

### 测试方式

- 16 个目录碰撞组作为强制回归集；
- 9 个品牌别名组；
- 重复 `sku_id` 两行；
- 完全重复内容不重复计权；
- 同色号多图离群和双峰；
- 数据库约束、迁移、查询和导出；
- 从最终颜色反向追溯到源 CSV 和原图；
- 同输入/版本两次构建结果一致。

### 验收标准

- 100% 最终实体字段有 evidence claim 或人工确认；
- 16 个目录碰撞组不丢失任何源 SKU；
- 9 个品牌别名组归一化后仍保留原始目录名；
- 最终色号颜色可追溯到图片、region、mask、算法和 run；
- 完全重复图片不重复增加融合权重；
- 双峰或强离群样本进入审核；
- 数据库无孤立外键和未解释的来源记录；
- 可按品牌/产品/色号导出可复现快照。

## 13. 阶段 8：人工审核、评估、回归和持续学习

### 目标

建立低置信度闭环，保证提示词、算法和 schema 变化可比较。

### 输入

- 阶段 3–7 的低置信度、冲突和异常；
- 固定评估集；
- overlay、mask、OCR 框、连线图和颜色候选；
- 人工审核结果。

### 输出

- 审核任务包和审核结果；
- 追加式审计日志；
- 版本化标注集；
- 角色/OCR/匹配/颜色回归报告；
- 后续本地模型训练数据。

### 数据库表和字段

#### `review_tasks`

```text
review_task_id
run_id
task_type
entity_type
entity_id
priority
reason_codes_json
payload_json
status
created_at
```

#### `review_events`

```text
review_event_id
review_task_id
reviewer
action
before_json
after_json
comment
created_at
```

#### `evaluation_sets`

```text
evaluation_set_id
name
version
selection_rules_json
content_grouping_method
created_at
```

#### `evaluation_results`

```text
evaluation_result_id
evaluation_set_id
run_id
metric_name
slice_name
metric_value
details_path
created_at
```

### 实现要点

- 审核事件追加，不覆盖旧值；
- 评估集按 SHA256、产品/系列分组，避免重复内容跨集合；
- 每次提示词、阈值、算法、模型或 schema 变化跑回归；
- 报告新增/消失的审核任务；
- 训练集保留来源、许可证/使用范围和版本；
- 高价值品牌可提高审核优先级，但不改变证据标准。

### 测试方式

- 审核创建、领取、确认、修改、拒绝和并发冲突；
- before/after 审计不可变；
- 评估集去重和泄漏检测；
- 历史 run 与新 run 差异；
- 标注导出/回流往返；
- 权限和敏感字段脱敏测试。

### 验收标准

- 所有低置信度和强冲突样本进入审核队列；
- 人工修改 100% 有 reviewer、时间、before/after 和原因；
- 评估集无相同 SHA256 跨 train/validation/test；
- 每次版本变更都有分层指标和差异报告；
- 审核结果能回流但不覆盖原始模型响应；
- 只有达到阶段门槛的版本才能扩大批处理范围。

## 14. 横切测试与发布门禁

### 14.1 每阶段通用测试

- 原始文件抽样/全量 SHA256 不变；
- 数据库外键和唯一约束；
- UTF-8、中文、英文、NBSP、特殊标点和 Windows 长路径；
- 相同输入与配置的确定性；
- 单图失败不影响批次；
- 断点续跑不重复已成功且指纹相同的数据；
- 错误必须有阶段、错误码和原始上下文；
- 产物路径不可逃逸输出根目录；
- 日志、配置和版本不可缺失。

### 14.2 安全门禁

- Git 密钥扫描为零；
- API Key 只从环境变量或密钥管理读取；
- 日志和错误不打印 Key 或完整认证头；
- 原始模型响应中的敏感字段按规则脱敏；
- 付费在线测试默认关闭；
- 全量调用前先报告预计唯一内容调用数和缓存命中率。

### 14.3 数据门禁

- 不按 pHash 自动删除；
- 不按文件名前缀判角色；
- 不把目录色号当图中唯一色号；
- 不把 `sku_color_no` 无条件当真值；
- 不把包装/背景主色当代表色；
- 不把恢复解码等同于严格成功；
- 不把模型自报置信度直接当最终置信度。

## 15. 迁移和兼容策略

### 15.1 现有预处理元数据

迁移映射：

```text
旧 image_id              → legacy_image_id
旧 sha256                → image_contents.image_id / sha256
旧 relative_path         → image_occurrences.relative_path
旧 brand_folder          → image_occurrences.brand_folder_raw
旧 product_folder        → image_occurrences.product_folder_raw
旧 working_image_path    → derived_assets
旧 alpha_mask_path       → derived_assets
旧 quality_warning       → quality_flags + legacy_quality_json
```

旧 CSV/JSONL 保留不覆盖。迁移输出使用新的 schema version 和 run_id。

### 15.2 主指南数据库语义

- 核心八类角色代码不变；
- `image_id=SHA256` 不变；
- 新增 occurrence/source 层是显式扩展；
- `product_id` 不再直接由原始商品目录生成；
- 任何字段重命名都通过迁移表和变更记录完成。

### 15.3 旧设计文档

本轮不批量改写三份旧文档。后续首次实现 schema 时创建 ADR，说明：

- 哪份文档是规范来源；
- 旧表名到新表名的映射；
- 不再采用的“删除损坏图片”等历史描述；
- 对外导出兼容方式。

## 16. 建议的首次实现顺序

严格按以下最小闭环推进：

1. 轮换并移除明文密钥；
2. 建立 dataset/source/ref/content/occurrence manifest；
3. 建立 SQLite 最小 schema 和迁移；
4. 迁移现有预处理 metadata，不重跑全量；
5. 补齐预处理单元测试和运行指纹；
6. 选固定小样本，建立角色标注集；
7. 实现 VLM 安全封装和角色分类；
8. 达到阶段 3 门槛后再进入 OCR 和颜色模块。

每个阶段完成后都应停下来生成报告并验收，不自动连续扩展到下一阶段。

## 17. 本轮停止点

本轮仅交付审计和实施计划。没有：

- 创建业务数据库；
- 修改下载器、预处理器或 VLM 脚本；
- 重跑全量预处理；
- 调用付费 API；
- 批量改名、移动或修改原始图片；
- 开始 OCR、分割、颜色提取或模型训练。
