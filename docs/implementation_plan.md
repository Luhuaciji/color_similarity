# 口红/唇部彩妆图片流水线分阶段实施计划

> 编制日期：2026-07-27  
> 修订日期：2026-07-28
> 依据：`docs/repository_audit.md` 与 `docs/codex_lip_color_pipeline_guide.md`  
> 状态：阶段 0.5 与阶段 1 已完成并验收；阶段 1.5 尚未开始
> 原则：先建立可追溯数据底座，再逐步增加模型、颜色计算和知识数据库能力

## 0. 实施状态（2026-07-28）

| 阶段 | 状态 | 证据 |
|---|---|---|
| 0 | `completed` | `docs/repository_audit.md` |
| 0.5 | `passed_with_owner_override` | `docs/stage0_5_security_report.md`、脱敏扫描报告和远端历史重写结果 |
| 1 | `passed` | `docs/stage1_completion_report.md`、运行 `stage1_full_20260728` |
| 1.5–8 | `not_started` | 本轮停止，不自动进入 |

阶段 0.5 的原始目标仍是优先轮换/吊销泄露 Key。仓库所有者在实施时明确要求不再确认供应商侧状态、只从 Git 历史删除，因此实际记录为 `rotation_status=owner_waived_unverified`；这是一项显式例外和残余风险，不把未验证状态改写成“已失效”。

阶段 1 已创建版本化 SQLite + JSONL manifest。它把本次 CSV 快照、当前下载器命名规则、31,511 个物理 occurrence 和 12,386 个内容 SHA 固化为不可覆盖的运行产物，不反向声称历史下载时已经存在成功 manifest。

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
- 把图像中观测到的代表色宣称为未经校准的真实物理颜色；
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

### 2.3 阶段 1 强制 SQLite + JSONL，列式/表格镜像后置

- SQLite：约束、关系、审核状态、迁移和查询；
- JSONL：阶段 1 必需的追加式 manifest、错误和交换输出；
- Parquet：后续阶段的可选宽表、批处理、统计和训练数据导出；
- 全量 CSV 镜像：后续可选兼容导出，不作为规范存储；
- 文件系统：原始模型响应、mask、overlay、工作图和报告；
- 数据库只存派生资产路径、哈希、版本和元数据。

阶段 1 的发布门禁只检查 SQLite 与 JSONL。Parquet 和全量 CSV 镜像均不得成为阶段 1 的阻断条件；如后续生成，必须由同一 `run_id` 和 schema version 可复现。

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
- 与所属分析层的稳定 ID 和 `run_id` 的关系。

### 2.6 模型分析严格拆为内容层与来源上下文层

模型与融合结果分成两个不可混写的层次：

```text
A. content visual analysis
   输入：image_id 对应图像内容，或该内容的全局缩略图/版本化 tile
   禁止输入：当前 SKU、商品目录目标色号、context_shade
   缓存：按内容 SHA256（tile 还包括派生资产哈希）+ 模型/提示词/参数

B. occurrence context fusion
   输入：A 层结果 + image_occurrence_id + folder + CSV + SKU + context_shade
   输出：图片与当前来源商品/色号上下文的关系、冲突和审核状态
   缓存：按 occurrence/source context + A 层版本 + 融合规则版本
```

A 层只描述图片自身可见内容，同一 SHA256 不因出现在不同目录或 SKU 下而重复调用或得到不同“图像事实”。B 层可以对同一内容的不同 occurrence/source record 产生不同关系判断，且上下文变化只重算 B 层。

数据库不创建同时包含 `image_id`、`image_occurrence_id`、角色和上下文结论的单一 `image_roles` 表。A 层使用 `content_visual_analyses`，B 层使用 `occurrence_context_fusions`；两表通过显式外键连接。

### 2.7 颜色字段表示 image-observed representative color

数据库颜色默认语义是**从特定图片像素观测得到的代表色**（`image-observed representative color`），不是未经校准的商品真实物理颜色。统一 sRGB、透明度处理、像素过滤和确定性归一化后的字段使用 `normalized_*`：

- `raw_srgb_*`：从工作图直接计算的观测值；
- `normalized_*`：有版本、参数和证据的标准化/归一化结果；
- `calibrated_*`：仅在存在可验证色卡/设备/照明校准依据时另行增加，不能与 `normalized_*` 混用。

不再使用含义不清的 `corrected_*` 字段。任何导出都必须携带 `color_semantics=image_observed_representative`、来源图片、region、mask、方法和运行版本。

### 2.8 长图必须同时保留全局与局部

长图分析采用“全局缩略图 + 重叠 tile”，不能只把长图切片后丢失整体布局：

1. 生成保留整图纵横关系的全局缩略图；
2. 按版本化策略生成有重叠的 tile；
3. 保存全局布局、阅读轴、tile 原图坐标、重叠范围和缩放/变换；
4. 内容视觉分析同时使用全局布局与局部 tile；
5. OCR 跨 tile 去重，并保留 tile 坐标与原图坐标回映；
6. 合并结果可反向定位到原始图片，且不能重复计数。

### 2.9 性能阈值的状态

本文所有模型/算法性能数字均标记为 `provisional_target`，只是待验证候选值，不是已冻结门禁。只有完成阶段 1.5 Pilot 和阶段 2.5 首轮人工标注后，才能基于分层基线、样本量和误差分析生成版本化的冻结阈值。数据完整性、安全性、原图不变和审计留存要求属于 `hard_gate`，不受此规则影响。

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
| 0.5 | 明文密钥泄露处置 | 阶段 0 | `hard_gate`：密钥轮换/吊销、受跟踪代码清理、环境变量接入和当前树/历史扫描全部闭环；任何后续阶段不得绕过 |
| 1 | 来源清单、稳定 ID 与最小数据库 | 阶段 0.5 | 当前可重建关系固化为不可变 manifest；SQLite + JSONL 门禁通过 |
| 1.5 | 50–100 唯一 SHA256 端到端 VLM Pilot | 阶段 1 | 图片读取到人工核查闭环跑通，A/B 两层隔离和缓存行为通过 |
| 2 | 基础预处理加固与历史产物迁移 | 阶段 1.5 | 严格解码、全局缩略图 + 重叠 tile、派生资产和缓存可复现 |
| 2.5 | 最小人工标注与评估工具 | 阶段 2 | 角色、资格、mask、多色号固定评估集可创建、版本化和复用 |
| 3 | 内容视觉分析与来源上下文融合 | 阶段 2.5 | A/B 分表、固定评估集和冻结阈值版本就绪，模型结果双留存 |
| 4 | OCR、文件夹和源字段信息抽取 | 阶段 3 | 文字、实体候选和冲突均有证据 |
| 5 | 单色图区域与代表色候选 | 阶段 3、4 | 高质量单色图颜色基线达标 |
| 6 | 多色号、长图和文字—色块匹配 | 阶段 4、5 | 清晰多色号图匹配达标 |
| 7 | 实体归一化、多图融合和知识数据库 | 阶段 4–6 | 最终结论均能回溯证据 |
| 8 | 完整人工审核、评估、回归和持续学习 | 阶段 2.5、3–7 | 在最小工具上扩展低置信度闭环和版本化评估 |

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

## 6. 阶段 0.5：明文密钥泄露处置（阻断）

### 目标

在任何 manifest、预处理或模型 Pilot 编码开始前，闭环处置已确认的受跟踪明文 API Key。阶段 0.5 是 `hard_gate`；即使后续模块可离线开发，也不得在旧 Key 未轮换/吊销、当前受跟踪代码未清理或 Git 历史风险未核查处置时宣布进入阶段 1。

> 实施结果（2026-07-28）：受跟踪文件及其本地/远端可达 Git 历史已删除，`.env`/扫描门禁通过；供应商侧失效确认按所有者明确指令豁免，状态为 `passed_with_owner_override`。完整风险说明见 `docs/stage0_5_security_report.md`。

### 输入

- `test_qwen36_vision.py` 中已定位的明文 Key 位置（只用于定位，报告和日志不得复制真实值）；
- 当前 Git 工作树、索引、所有本地可达提交与远程配置；
- 供应商密钥控制台或组织密钥管理流程；
- 当前 `.gitignore` 和运行配置约定。

### 输出

- 已轮换或吊销旧 Key 的脱敏处置记录；
- 从受跟踪代码删除真实值后的安全配置；
- 被 `.gitignore` 排除的本地 `.env` 使用约定，以及只含变量名/占位符的受跟踪 `.env.example`；
- 可重复执行的密钥扫描配置与脱敏扫描报告；
- “是否曾推送远程”的核查结论；
- 若已推送：对远程可达 Git 历史的扫描结果、影响范围和处置决定；如需历史重写，另立经批准的协作方案，不在无协调情况下强推。

### 数据库字段

本阶段不创建业务数据库，不把 Key、Key 哈希或认证头写入 SQLite。安全审计产物至少包含：

```text
security_incident_id
detected_file
detected_line
secret_type
rotation_status
tracked_tree_scan_status
reachable_history_scan_status
remote_push_status
remote_history_scan_status
remediation_status
evidence_paths_json
completed_at
```

所有证据必须脱敏；`remote_push_status=not_pushed` 时可以将远程历史扫描标记为 `not_applicable`，但必须保留判断依据。

### 实施要点

1. 先在供应商侧轮换或吊销暴露的 Key；
2. 从受 Git 跟踪代码中删除真实值；
3. 运行时只从环境变量或密钥管理读取；
4. 本地 `.env` 必须被 `.gitignore` 排除，不提交真实值；
5. `.env.example` 只保存变量名和无效占位符；
6. 扫描工作树、Git 索引和所有本地可达提交；
7. 核查包含泄露提交的分支/tag 是否已推送；若已推送，扫描远程可达历史并记录轮换、通知、历史重写或不重写的决策；
8. 日志、异常、测试 fixture 和扫描报告不得回显 Key。

### 测试方式

- 在无 `.env` 时启动安全失败，错误只显示缺少的变量名；
- 使用测试占位 Key 验证 `.env`/环境变量读取，确认不会写入日志；
- 检查 `.env` 被 Git 忽略、`.env.example` 可安全跟踪；
- 对工作树、索引和所有可达提交执行密钥扫描；
- 验证扫描器能检出专用假密钥 fixture，避免“扫描为零但规则失效”；
- 若已推送远程，核对远程分支/tag 覆盖范围与扫描范围。

### 验收标准

- `hard_gate`：旧 Key 已轮换或吊销；
- `hard_gate`：受跟踪代码、Git 索引和当前工作树不再含真实 Key；
- `hard_gate`：运行配置只接受环境变量/密钥管理，本地 `.env` 不受跟踪；
- `hard_gate`：密钥扫描规则通过正向 fixture，并对当前树与全部本地可达提交完成扫描；
- `hard_gate`：已记录泄露提交是否推送远程；若已推送，远程可达历史已检查并有脱敏处置记录；
- `hard_gate`：日志和报告不包含 Key 或完整认证头；
- 未全部满足前，阶段 1–8 状态必须保持 `blocked_by_stage_0_5`。

## 7. 阶段 1：来源清单、稳定 ID 与最小数据库

### 目标

把“依赖当前 CSV 与当前下载器可重建的路径关系”固化为不可变、版本化的来源血缘。此阶段不做角色分类和颜色提取，也不把当前可重建关系误写为已有长期稳定追溯。

> 实施结果（2026-07-28）：运行 `stage1_full_20260728` 已通过全部计数、SQLite/JSONL、血缘和原图完整性门禁。详见 `docs/stage1_completion_report.md`。

### 输入

- 源 CSV 原始字节；
- 阶段 0.5 的通过记录；
- `download_product_images.py` 的解析/清洗逻辑；
- `downloaded_images/`；
- 现有预处理元数据；
- 审计确认的 16 个目录碰撞组和 9 组品牌别名。

### 输出

- 源 CSV 的 `dataset_snapshot` 记录（哈希、schema、行数和只读源路径；不要求生成全量 CSV 镜像）；
- source record、source image ref、image content、image occurrence manifest；
- SQLite 初始 schema 和迁移版本；
- JSONL manifest；
- 来源映射冲突报告；
- 旧 `image_id` 到新 `image_id`/`image_occurrence_id` 映射表；
- 只读原图完整性基线；
- 可选导出规范说明：Parquet 和全量 CSV 镜像后续按需生成，不属于本阶段输出门禁。

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
9. 成功映射写入不可变 JSONL 和 SQLite，记录 `run_id`、输入哈希、解析/命名规则版本与匹配方法；
10. 后续 CSV 或下载器逻辑变化时创建新快照/新运行，不覆盖旧成功 manifest。

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
- JSONL/SQLite 行数、主键和外键一致性；
- 修改 CSV 快照或命名规则版本后生成新 run，旧 manifest 保持不变；
- Parquet 和全量 CSV 镜像缺失时，本阶段门禁仍可正常通过。

### 验收标准

- `dataset_snapshots=1`，`source_records=2309`；
- 原始 URL 引用 31,513 条全部保留；
- 31,511 个物理 occurrence 全部有记录；
- `image_contents=12,386`，与唯一 SHA256 一致；
- 16 个目录碰撞组和 9 个品牌别名组均未静默合并；
- 每个 source ref 能链接 occurrence，或有明确未匹配原因；
- 每个数据库图片记录可回溯到物理路径和至少一个来源上下文；
- 输入文件抽样前后 SHA256 不变；
- SQLite 与 JSONL 的关键计数、ID 和关系一致；
- 成功映射带 `run_id`、输入快照哈希和规则版本，旧运行不可被覆盖；
- 不要求 Parquet 或全量 CSV 镜像即可通过阶段 1；
- 阶段 0.5 安全门禁仍为通过状态。

## 8. 阶段 1.5：50–100 唯一 SHA256 端到端 VLM Pilot

### 目标

在正式阶段 3 和任何全量模型调用前，用 50–100 个唯一 `image_id` 跑通：

```text
只读图片读取
  → 全局缩略图 + 必要的重叠 tile
  → A 层 image_id/tile 内容视觉分类
  → JSON Schema 校验
  → 原始响应与解析结果双留存
  → SQLite
  → B 层 occurrence 来源上下文融合
  → 人工核查
```

本阶段验证架构、Schema、缓存、审计和人工可核查性，不以少量 Pilot 的性能数字作为正式模型门禁。

### 输入

- 已通过的阶段 0.5；
- 阶段 1 SQLite + JSONL manifest；
- 50–100 个唯一 SHA256 的版本化抽样清单；
- 每个被选中 SHA256 对应的全部抽样 occurrence/source context；
- 八类角色定义和内容层 JSON Schema；
- 长图全局缩略图/重叠 tile 试验配置；
- 安全的 `qwen3.6-plus` 环境变量配置。

抽样必须覆盖：

- 八类核心角色；
- 普通图和整页长图；
- 扩展名/真实格式错配；
- 至少一个有多个 occurrence 的重复内容组；
- 至少一个目录碰撞组，并保留该组涉及的多个 CSV/SKU/context shade。

“50–100”按唯一 SHA256 计数。重复图覆盖通过选取“同一 SHA256 有多个 occurrence”的内容并加载其多个来源上下文实现，不得为同一内容重复付费调用 A 层。

### 输出

- `pilot_selection.jsonl`，含抽样原因、覆盖标签和 occurrence/source context；
- 全局缩略图与必要的重叠 tile 派生资产及坐标；
- 序列化请求、原始模型响应、解析 JSON、Schema 校验结果和错误；
- SQLite 中的模型运行、A 层内容分析和 B 层上下文融合记录；
- 人工核查包及核查结果；
- 缓存命中、唯一付费调用数、延迟、token/成本和失败类型报告；
- Schema/提示词/分层边界问题清单及阶段 3 的 Go/No-Go 结论。

### 数据库表和字段

#### `pilot_samples`

```text
pilot_sample_id
pilot_run_id
image_id
selected_occurrence_ids_json
selected_source_record_ids_json
coverage_tags_json
selection_reason
human_review_status
created_at
```

#### `model_runs`

```text
model_run_id
run_id
analysis_layer
analysis_unit_type
analysis_unit_id
model_name
provider
base_url_alias
prompt_name
prompt_version
schema_version
input_context_policy
generation_parameters_json
request_hash
request_path
raw_response_path
parsed_response_path
response_hash
schema_validation_status
latency_ms
token_usage_json
status
error_json
```

A 层的 `input_context_policy` 必须为 `image_only`；其 `analysis_unit_id` 只能指向 `image_id` 或该内容的版本化全局缩略图/tile，不能指向 SKU、文件夹或 `context_shade`。

#### `content_visual_analyses`（A 层）

```text
content_visual_analysis_id
run_id
image_id
analysis_scope
analysis_asset_id
parent_content_visual_analysis_id
tile_index
tile_bbox_image_json
primary_role
secondary_roles_json
layout_type
global_layout_json
role_confidence
contains_text
contains_multiple_shades
contains_lips
contains_skin_swatch
contains_product_bullet
contains_packaging
depicted_shades_json
representative_color_eligible
eligibility_score
recommended_strategy
rejection_reasons_json
candidate_regions_json
model_run_id
schema_version
```

`analysis_scope` 至少区分 `global_thumbnail`、`tile` 和 `merged_content_summary`。表中禁止出现 `image_occurrence_id`、当前 SKU、商品目录目标色号或 `context_shade`。

#### `occurrence_context_fusions`（B 层）

```text
occurrence_context_fusion_id
run_id
image_occurrence_id
source_record_id
source_ref_id
folder_group_id
content_visual_analysis_id
source_sku_id_raw
folder_context_json
csv_context_json
context_shade_json
depicted_shades_json
relationship_to_context
context_conflicts_json
fusion_method
fusion_version
confidence
review_status
created_at
```

同一 `image_occurrence_id` 若关联多个 source record，应按明确的 occurrence/source context 分别保存 B 层结果，不得覆盖。`relationship_to_context` 的枚举和语义必须版本化。

本计划不创建 `image_roles` 混合表；Pilot 与正式阶段 3 均直接使用上述两表。

### 实施要点

1. 先冻结抽样 JSONL，再发起任何在线调用；
2. 抽样按内容 SHA256 去重，但加载所选内容的多 occurrence/source context；
3. A 层请求构造器设字段白名单，并对请求做禁用上下文字段审计；
4. 长图先提供全局缩略图，再提供重叠 tile；全局总结引用 tile 原图坐标；
5. 原始响应必须先落盘，再解析和校验；
6. Schema 失败保留原始响应，有限修复/重试后写错误状态；
7. A 层缓存键不含 occurrence、文件夹、SKU 或 context shade；
8. B 层在 A 层之后独立运行，规则/上下文变化不使 A 层缓存失效；
9. 人工核查分别评价“图片中看到了什么”和“它与当前来源商品是什么关系”；
10. 若 50 个样本尚未覆盖全部必需切片，可继续补样，但总唯一 SHA256 不超过 100。

### 测试方式

- 请求快照测试：A 层请求中不存在 folder、SKU、source record 或 context shade；
- Schema 单元测试：合法、缺字段、额外字段、非法枚举、越界坐标和非 JSON；
- mock API 端到端测试：读取 → 请求 → 原始响应 → 解析 → SQLite → review；
- 明确开关控制的小批在线测试；
- 同一 SHA256 多 occurrence 时，A 层只调用一次，B 层分别生成上下文结果；
- 长图全局缩略图、tile 重叠、原图坐标回映和合并测试；
- 格式错配文件按实际 MIME 构建 Data URL；
- SQLite/JSONL/文件资产间的路径、哈希和外键一致性；
- 人工核查盲测：内容角色页面默认不显示当前 SKU/目录目标色号。

### 验收标准

- `hard_gate`：Pilot 包含 50–100 个唯一 SHA256；
- `hard_gate`：人工核查后确认八类角色、长图、格式错配、重复内容多 occurrence 和目录碰撞均有覆盖；缺一类即补样或判定 No-Go；
- `hard_gate`：100% 模型尝试都有序列化请求、原始响应、解析/Schema 状态和 SQLite 记录；
- `hard_gate`：A 层请求审计未发现当前 SKU、文件夹目标色号或 `context_shade`；
- `hard_gate`：相同 SHA256/分析资产/模型/提示词/参数组合至多产生一次成功付费调用；
- `hard_gate`：长图同时具有全局缩略图和重叠 tile，且所有 tile/结果可回映原图；
- `hard_gate`：所选重复内容与目录碰撞样本能产生多个独立 B 层上下文结果，不污染 A 层事实；
- 人工核查的角色、资格、失败和上下文关系结果完整留存；
- 所有性能数字只作为 Pilot baseline，状态为 `provisional_target`，不得据此直接扩大到全量。

## 9. 阶段 2：基础预处理加固与历史产物迁移

### 目标

复用现有预处理代码，补齐运行指纹、格式检测、极端尺寸和不可覆盖日志。

### 输入

- 阶段 1 manifest 和 SQLite；
- 阶段 1.5 的 Pilot 资产、错误与人工核查结论；
- `downloaded_images/` 只读图片；
- 现有 `image_preprocessing_output/`；
- `image_preprocessing_pipeline/preprocess_product_images.py`。

### 输出

- 模块化预处理包；
- 版本化运行目录；
- 工作图、Alpha Mask、质量指标和错误记录；
- 历史 1.1.0 产物迁移清单；
- 格式错配、长图、装饰条和超大图片报告；
- 长图全局缩略图、重叠 tile、布局与坐标映射；
- 兼容 JSONL 导出；
- 可选 Parquet/全量 CSV 镜像导出（不属于本阶段或阶段 1 门禁）。

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

#### `long_image_layouts`

```text
long_image_layout_id
image_id
run_id
global_thumbnail_asset_id
original_width
original_height
global_thumbnail_width
global_thumbnail_height
reading_axis
layout_type
global_layout_json
image_to_thumbnail_transform_json
tiling_strategy_version
```

#### `image_tiles`

```text
image_tile_id
long_image_layout_id
image_id
tile_asset_id
tile_index
bbox_image_json
overlap_before_px
overlap_after_px
tile_width
tile_height
image_to_tile_transform_json
tile_to_image_transform_json
transform_fingerprint
```

### 实现要点

- 按魔数/解码结果确定格式和 MIME；
- 把 GIF 纳入支持，明确单帧/多帧策略；
- 对 229 个格式错配文件保留原文件名并显式标记；
- 对长图先生成保留整图比例的全局缩略图，再生成不覆盖原图的重叠 tile 派生资产；
- 保存全局布局/阅读轴；tile 保存原图坐标、重叠范围和双向坐标变换；
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
- 750×1、超长图、全局缩略图与重叠 tile 坐标回映；
- tile 边界目标在相邻重叠区可见，跨 tile 合并后不重复；
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
- 每张进入长图策略的图片同时有全局缩略图和重叠 tile，布局、坐标和变换记录完整；
- 相同输入/配置重复运行结果 hash 一致；
- 配置或实现版本改变时不会误复用旧工作图；
- 单图失败不影响其余记录落库；
- 单元和小型集成测试通过。

## 10. 阶段 2.5：最小人工标注与评估工具

### 目标

在正式阶段 3 前提供可实际使用的最小人工标注与核查工具，创建并冻结第一版角色、代表色资格、mask 和多色号配对评估集。此阶段只覆盖单人/小团队离线标注、版本化和基础质检；完整任务编排、权限、优先级、并发领取、持续学习和生产审核仍保留在阶段 8。

### 输入

- 阶段 1 的 source/content/occurrence manifest；
- 阶段 1.5 Pilot 样本、原始/解析结果和人工核查问题；
- 阶段 2 工作图、全局缩略图、重叠 tile、Alpha 和坐标变换；
- 八类角色与代表色资格标注规范；
- mask/region 标注规范；
- 多色号“文字/色号—区域”配对规范；
- 按 SHA256、产品/系列和碰撞组分组的候选样本。

### 输出

- 最小标注工具及使用说明；
- 版本化角色/资格标注集；
- 版本化 mask/region 标注集，mask 作为只读派生资产保存；
- 版本化多色号区域与色号配对标注集；
- 固定评估集及 train/validation/test 分组清单；
- 标注事件 JSONL、SQLite 记录、冲突/待裁决清单；
- 首轮标注质量和各切片样本量报告；
- 根据阶段 1.5 Pilot 与首轮标注生成的“阈值冻结候选报告”；冻结动作必须产生独立版本记录。

### 数据库表和字段

#### `annotation_sets`

```text
annotation_set_id
name
version
purpose
label_schema_version
selection_rules_json
content_grouping_method
status
created_at
frozen_at
```

#### `annotation_items`

```text
annotation_item_id
annotation_set_id
image_id
image_occurrence_id
global_thumbnail_asset_id
task_types_json
content_context_visibility
coverage_tags_json
group_id
split
status
```

内容角色/资格任务的 `content_context_visibility` 必须为 `image_only`；只有 occurrence 关系任务可以显示 folder、CSV、SKU 和 `context_shade`。

#### `annotation_events`

```text
annotation_event_id
annotation_item_id
annotator_id
annotation_type
role_code
eligibility_label
eligibility_reason_codes_json
region_type
bbox_image_json
polygon_image_json
mask_asset_id
multi_shade_annotation_json
before_json
after_json
supersedes_event_id
created_at
```

标注事件追加写入，修改通过 `supersedes_event_id` 形成链，不覆盖历史。mask 必须登记到 `derived_assets` 并可回映原图。

#### `evaluation_sets`

```text
evaluation_set_id
name
version
source_annotation_set_ids_json
selection_rules_json
content_grouping_method
split_policy_json
metric_schema_version
status
created_at
frozen_at
```

#### `evaluation_set_items`

```text
evaluation_set_item_id
evaluation_set_id
annotation_item_id
image_id
image_occurrence_id
group_id
split
slice_tags_json
ground_truth_version
```

#### `performance_threshold_versions`

```text
threshold_version
metric_name
metric_definition_version
slice_name
operator
target_value
status
pilot_run_id
annotation_set_id
baseline_value
sample_count
rationale
approved_by
created_at
frozen_at
```

`status` 只能从 `provisional_target` 经有证据评审转为 `frozen`。不得直接修改已冻结行；调整时创建新版本。

阶段 8 复用并扩展这些表，不另建语义冲突的评估集表。

### 实施要点

- 角色与资格标注页面默认隐藏目录、SKU 和 context shade，避免污染 A 层真值；
- occurrence 关系标注作为独立任务展示来源上下文；
- 长图同时展示全局缩略图和局部 tile，标注坐标统一保存为原图坐标；
- mask、polygon、bbox 和多色号配对均保存 schema/version；
- 相同 SHA256 不跨 train/validation/test，近重复与同产品系列按组控制泄漏；
- 目录碰撞样本必须保留多个 source context，不能只显示一个 SKU；
- 至少双人复核一小部分样本，用于发现标注规范歧义；不以简单多数投票覆盖分歧；
- 首轮建议规模为至少 400 个唯一 SHA256，但记为 `provisional_sampling_target`，由 Pilot 的小类分布和误差决定最终分层配额；
- Parquet 和全量 CSV 仅为可选导出；SQLite + JSONL 是规范记录。

### 测试方式

- 八类角色和资格标签创建、修改、撤销、追加事件回放；
- polygon/mask 绘制、导入导出、尺寸校验和原图坐标回映；
- 长图全局视图切换到 tile 后坐标一致；
- 多色号区域、文字/色号和配对的往返测试；
- 内容任务上下文隐藏与 occurrence 任务上下文显示权限测试；
- 相同 SHA256/近重复/同产品分组的集合泄漏检测；
- SQLite ↔ JSONL 导出/导入往返及哈希一致；
- 原始图片只读与派生 mask 路径逃逸测试；
- 标注事件历史重放、冲突和裁决测试。

### 验收标准

- `hard_gate`：工具能创建并版本化角色、资格、mask 和多色号四类标注；
- `hard_gate`：固定评估集覆盖八类角色、长图、格式错配、重复内容多 occurrence 和目录碰撞；
- `hard_gate`：相同 SHA256 不跨数据集 split，目录碰撞来源未被静默合并；
- `hard_gate`：角色/资格真值采集时不显示当前 SKU、文件夹目标色号或 `context_shade`；
- `hard_gate`：所有 mask/region 可回映原图且原始图片 SHA256 不变；
- `hard_gate`：标注修改有 annotator、时间、before/after 和 supersedes 链；
- 首轮标注与 Pilot 指标报告齐备，所有性能阈值仍以 `provisional_target` 呈现；
- 正式阶段 3 开始前，须另存经评审的阈值版本及冻结依据；
- 阶段 8 的完整审核系统范围没有被删减或提前宣称完成。

## 11. 阶段 3：内容视觉分析与来源上下文融合

### 目标

把阶段 1.5 验证过的两层架构扩展到正式批次：

- A 层在唯一 `image_id`/tile 上判断八类角色、布局和代表色资格，不接收当前 SKU、folder 目标色号或 `context_shade`；
- B 层在 `image_occurrence_id`/source context 上结合 folder、CSV、SKU、`context_shade` 与 A 层事实，判断图片与当前商品/色号的关系。

两层使用独立表、独立版本和独立缓存失效规则。

### 输入

- 阶段 2 工作图、Alpha、全局缩略图、重叠 tile、布局和质量指标；
- 阶段 2.5 固定人工评估集与已冻结的阈值版本；
- 主指南八类核心角色；
- `qwen3.6-plus` 安全调用配置；
- 仅供 B 层使用的 occurrence、folder、CSV、SKU 和 `context_shade`。

### 输出

- A 层全局缩略图/tile/整图汇总的内容角色、布局、代表色资格和推荐策略；
- B 层 occurrence/source context 关系、`depicted_shades`、冲突和审核状态；
- VLM 原始响应与解析结果；
- 缓存和错误记录；
- 分层分类/资格评估报告、混淆矩阵和抽样 HTML/overlay；
- 低置信度审核任务。

### 数据库表和字段

复用并扩展阶段 1.5 已创建的：

- `model_runs`；
- `content_visual_analyses`（A 层）；
- `occurrence_context_fusions`（B 层）。

正式运行不得新建混合语义的 `image_roles` 表。需要批次信息时增加：

#### `visual_analysis_batches`

```text
visual_analysis_batch_id
run_id
evaluation_set_id
threshold_version
selection_query_json
planned_unique_content_count
planned_tile_count
completed_unique_content_count
cache_hit_count
failed_count
abstained_count
started_at
finished_at
status
```

A 层表只允许内容/派生分析资产字段；B 层表才允许 occurrence/source/folder/SKU/context 字段。若为了兼容旧消费者提供 view，view 名称、字段来源和不可用于训练/缓存的限制必须显式记录，不能重新制造混合写表。

### 实现要点

- 同 SHA256、同分析资产、同模型/提示词/参数只调用一次；
- A 层请求采用白名单构造和自动审计，拒绝 occurrence、folder、SKU、source record 与 context shade；
- B 层读取 A 层结果并另存关系结论，不改变图像事实；同一 occurrence 的多个 source context 分行保存；
- 长图先分析全局缩略图以获取整体布局，再分析重叠 tile，最后按原图坐标汇总；
- `pic_list`/`show_pic` 只允许进入 B 层作为弱来源特征，不进入 A 层；
- JSON 解析、枚举、坐标和置信度强校验；
- 修复/重试次数有限；
- 任何响应都先保存原始值；
- 默认离线测试用 mock/缓存，在线测试显式开启。

### 测试方式

- 至少 400 张固定人工标注图，按品牌、内容 SHA、来源字段、格式、质量和长图分层；
- 覆盖八类角色、组合图、全局长图 + 重叠 tile 和无效装饰条；
- 单元测试 Data URL MIME、A/B 缓存键、Schema、错误分类；
- A 层请求上下文污染测试和 B 层多 source context 测试；
- mock API 集成测试；
- 小批显式在线冒烟测试；
- 回归比较每次提示词变化；
- 按角色/品牌/格式/长图/碰撞组分别报告资格分类 Precision、Recall、F1 和 Coverage。

### 验收标准

- `provisional_target`：角色 Macro-F1 ≥ 0.85；正式门槛以阶段 2.5 冻结的 `threshold_version` 为准；
- `provisional_target`：代表色资格 Precision ≥ 0.90；
- `provisional_target`：代表色资格 Recall、F1、Coverage 在阶段 1.5/2.5 基线后冻结；四项必须同时报告，不能只以 Precision 通过；
- 所有核心角色在评估集中都有足够样本，不以总体准确率掩盖小类；
- `hard_gate`：100% 模型尝试有请求、原始响应、解析/Schema 状态；
- `hard_gate`：Schema 最终失败全部落错误表/审核队列；
- `hard_gate`：A 层请求无 folder/SKU/context shade 污染，相同内容不重复计费；
- `hard_gate`：A/B 两层分别落表，同一内容的不同 occurrence/source context 不覆盖；
- `hard_gate`：长图全局布局和 tile 结果均可回映原图坐标；
- 包装/文字图误入颜色阶段的假阳性按资格 Precision/Recall/F1/Coverage 分层报告。

## 12. 阶段 4：OCR、文件夹和源字段信息抽取

### 目标

建立“原始事实—证据—规范化候选—冲突”的信息抽取层。

### 输入

- 阶段 1 原始 CSV 行和文件夹；
- 阶段 2 工作图/tile；
- 阶段 3 A 层角色/布局/可抽取资格与 B 层来源关系；
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
long_image_layout_id
text_raw
text_normalized
bbox_tile_json
bbox_image_json
source_tile_ids_json
dedupe_group_id
canonical_ocr_span_id
dedupe_method
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
- 长图 OCR 同时利用全局布局和重叠 tile；OCR 框保留 tile 和原图两套坐标；
- 跨 tile OCR 按原图坐标、规范化文本和置信度去重，保留所有来源 span，并选出 canonical span；
- 品牌优先使用 `brand_id_raw` 和别名表；
- 冲突不覆盖，统一进入 conflict group。

### 测试方式

- 目录碰撞组、品牌别名组和重复 `sku_id` 固定 fixture；
- 中英混合、编号、容量、色号名、NBSP 和标点归一化；
- OCR 清晰/模糊/长图 tile；
- 重叠 tile 中同一文字的跨 tile 去重、漏检和原图坐标回映；
- OCR 与目录冲突；
- 同一图片多色号；
- 离线 OCR 回归集和缓存 VLM 响应。

### 验收标准

- `provisional_target`：清晰 shade-code exact match ≥ 0.95；
- `provisional_target`：shade-code CER ≤ 0.05；Unicode、大小写、空白和连字符归一化规则必须版本化；
- `hard_gate`：原始 OCR 框、文本、来源 tile、去重链和引擎版本 100% 留存；
- `hard_gate`：跨 tile 去重后 canonical span 可回到全部原始 span 与原图坐标；
- 16 个目录碰撞组不被错误压成单一色号；
- 9 组品牌目录别名可追溯到源 `brand_id`；
- `sku_color_no=3g/5ml` 等异常不会被写成确认色号；
- 每个规范化候选都有至少一个 source claim；
- 所有冲突都有类型、双方证据和审核状态。

## 13. 阶段 5：单色图区域与 image-observed 代表色候选

### 目标

先处理高价值、边界较清楚的单色试色、膏体和规则色块。输出是特定图片条件下观测到的代表色，不是未经校准的真实物理颜色。

### 输入

- 阶段 2 sRGB 工作图和 Alpha；
- 阶段 3 角色/资格/候选框；
- 阶段 4 OCR 和实体上下文；
- 人工标注 mask/框小样本。

### 输出

- 区域框、polygon、mask；
- 有效像素诊断；
- 带 `image_observed_representative` 语义的 RGB/Lab/LCh/HEX 候选；
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
normalized_srgb_rgb_json
normalized_srgb_hex
normalized_lab_json
normalization_method
normalization_reference_id
normalization_evidence_json
normalization_confidence
color_semantics
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
- 原始 sRGB 观测候选和有证据的 `normalized_*` 候选分开；
- `color_semantics` 固定为 `image_observed_representative`；任何 UI、API 或导出不得省略后把它宣称为真实物理颜色；
- 只有存在可验证的色卡/设备/照明校准链时，才允许另增 `calibrated_*` 字段和校准记录；`normalized_*` 不等同于 calibrated；
- 所有 mask/overlay 版本化。

### 测试方式

- 至少 150 张高质量单色图人工框/mask/颜色基线；
- 透明、白底、黑底、肤色、强高光、裸色、深色；
- 颜色空间和 ΔE00 单元测试；
- mask IoU、背景误选率、高光误选率；
- 相同配置的确定性回归；
- 人工选区与自动选区颜色对比。

### 验收标准

- `provisional_target`：高质量单色图自动 image-observed 代表色与同图人工选区颜色的 Median ΔE00 ≤ 5；
- `provisional_target`：同一评估切片的 P90 ΔE00 ≤ 10；
- 代表色资格假阳性不高于阶段 3 冻结门槛，并同时报告 Precision、Recall、F1、Coverage；
- `provisional_target`：mask IoU ≥ 0.75；
- `hard_gate`：100% 候选带 `color_semantics`、region、mask、有效像素数、算法版本和诊断；
- 包装、背景、肤色和高光误选有独立指标；
- 任何最终颜色都不是仅由 VLM HEX 产生，且不得标为未经证据支持的物理校准颜色。

## 14. 阶段 6：多色号、长图和文字—色块匹配

### 目标

处理多区域、长图、色卡、手臂多色试色和多唇部对比。

### 输入

- 阶段 2 全局缩略图、重叠 tile、全局布局与坐标回映；
- 阶段 3 A 层多色号/布局结果及 B 层当前来源关系；
- 阶段 4 OCR 框和实体候选；
- 阶段 5 区域和颜色算法。

### 输出

- 多个独立色块/膏体/唇部区域；
- 色号—名称—区域匹配；
- 布局、阅读顺序和替代匹配；
- 每色号独立颜色候选；
- 长图全局布局约束下的跨 tile 合并与 OCR 去重结果；
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
long_image_layout_id
global_thumbnail_asset_id
layout_type
estimated_region_count
estimated_text_count
reading_order_json
tile_merge_json
cross_tile_ocr_dedupe_json
image_coordinate_mapping_json
model_run_id
```

### 实现要点

- 同一商品目录内显式区分 `context_shade` 和 `depicted_shades`；
- 检测色块数、文字数和候选 shade 数；
- 规则评分 + 二分图/匈牙利匹配；
- 复杂布局由 VLM 复核，但保留几何证据；
- 长图以全局缩略图确定整体布局/阅读顺序，再在重叠 tile 上细化区域和文字；
- tile 合并要处理重叠区和重复 OCR，保留 canonical span、被合并 span 和去重理由；
- 所有 tile 区域、OCR 和连线统一回映原图坐标后再匹配；
- 色块与文本必须保存坐标和连线；
- 不确定匹配保留替代候选。

### 测试方式

- 至少 100 张清晰多色号图人工配对；
- 包含橘朵 N18/N19/N03/N05 抽样图；
- 网格、横排、竖排、手臂试色、多唇部和长图；
- 色块数/文字数不一致；
- OCR 换行、跨 tile、连接线和裁剪；
- 全局布局约束、跨 tile OCR 去重及合并无重复/漏检回归。

### 验收标准

- `provisional_target`：清晰多色号图色号—色块配对准确率 ≥ 0.90；
- `provisional_target`：多色号整图完全匹配率（所有期望配对正确且无多配/漏配）在阶段 1.5/2.5 基线后冻结；必须作为独立主指标报告；
- `provisional_target`：歧义/数量不一致检测 Recall ≥ 0.90；
- `hard_gate`：100% 匹配保留文字、区域和原图空间坐标；
- `hard_gate`：长图保留全局布局、tile 来源、跨 tile OCR 去重链和原图回映，且无重复计数；
- 低置信度匹配不自动写成确认色号。

## 15. 阶段 7：实体归一化、多图融合和知识数据库

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
- 商品/色号级 image-observed 代表色和多视角颜色；
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
representative_srgb_hex
representative_srgb_rgb_json
representative_lab_json
representative_lch_json
color_semantics
normalization_summary_json
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
- 融合颜色始终标记 `color_semantics=image_observed_representative`，不得表述为未经可验证校准的真实物理颜色；
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
- `provisional_target`：每商品至少一个有效颜色证据的覆盖率在阶段 1.5/2.5 基线后冻结；分母、排除原因和无可用证据商品必须分别报告；
- 完全重复图片不重复增加融合权重；
- 双峰或强离群样本进入审核；
- 数据库无孤立外键和未解释的来源记录；
- 可按品牌/产品/色号导出可复现快照。

## 16. 阶段 8：完整人工审核、评估、回归和持续学习

### 目标

在阶段 2.5 最小工具和固定评估集之上，建立生产级低置信度闭环，保证提示词、算法和 schema 变化可比较。阶段 2.5 没有取代本阶段的任务编排、权限、并发审核、持续评估和回流能力。

### 输入

- 阶段 2.5 标注工具、固定评估集和阈值版本；
- 阶段 3–7 的低置信度、冲突和异常；
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

复用阶段 2.5 的 `annotation_sets`、`annotation_items`、`annotation_events`、`evaluation_sets` 和 `evaluation_set_items`，通过 schema migration 增加权限、分配、裁决和归档字段，不复制语义相同的新表。

#### `evaluation_results`

```text
evaluation_result_id
evaluation_set_id
run_id
metric_name
metric_definition_version
slice_name
metric_value
threshold_version
threshold_status
details_path
created_at
```

### 实现要点

- 审核事件追加，不覆盖旧值；
- 评估集按 SHA256、产品/系列分组，避免重复内容跨集合；
- 每次提示词、阈值、算法、模型或 schema 变化跑回归；
- 所有性能结果关联 `threshold_version`；文档中的候选数字持续标为 `provisional_target`，只有经阶段 1.5/2.5 证据评审的数据库版本可标为 `frozen`；
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
- 只有达到对应 `frozen` 阶段门槛的版本才能扩大批处理范围；`provisional_target` 本身不能授权扩量。

## 17. 横切测试与发布门禁

### 17.1 每阶段通用测试

- 原始文件抽样/全量 SHA256 不变；
- 数据库外键和唯一约束；
- UTF-8、中文、英文、NBSP、特殊标点和 Windows 长路径；
- 相同输入与配置的确定性；
- 单图失败不影响批次；
- 断点续跑不重复已成功且指纹相同的数据；
- 错误必须有阶段、错误码和原始上下文；
- 产物路径不可逃逸输出根目录；
- 日志、配置和版本不可缺失。

### 17.2 安全门禁

- 阶段 0.5 未通过时，所有后续阶段保持阻断；
- 本地 `.env` 被 Git 忽略，受跟踪 `.env.example` 只含无效占位符；
- 当前工作树和索引密钥扫描为零；
- 本地可达 Git 历史扫描已完成；历史发现均关联到已轮换/吊销的 Key，并有是否重写历史的处置记录；
- 若泄露提交已推送，远程可达历史已核查并有处置记录；
- API Key 只从环境变量或密钥管理读取；
- 日志和错误不打印 Key 或完整认证头；
- 原始模型响应中的敏感字段按规则脱敏；
- 付费在线测试默认关闭；
- 全量调用前先报告预计唯一内容调用数和缓存命中率。

### 17.3 数据门禁

- 不按 pHash 自动删除；
- 不按文件名前缀判角色；
- 不把目录色号当图中唯一色号；
- 不把 `sku_color_no` 无条件当真值；
- 不把包装/背景主色当代表色；
- 不把 image-observed representative color 宣称为未经校准的真实物理颜色；
- 不把 folder/SKU/context shade 输入 A 层内容视觉分析；
- 不用单一 `image_roles` 写表混合内容事实和 occurrence 上下文结论；
- 长图不能只做 tile，必须同时保存全局缩略图、布局和原图坐标回映；
- 不把恢复解码等同于严格成功；
- 不把模型自报置信度直接当最终置信度。

### 17.4 必报性能指标与 `provisional_target`

指标定义必须版本化，首轮标注和阶段 1.5 Pilot 完成前不得把候选数字标为 `frozen`：

| 指标 | 定义 | 当前候选 |
|---|---|---|
| 角色 Macro-F1 | 八类角色逐类 F1 的宏平均，另报各类 P/R/F1 | `provisional_target: ≥ 0.85` |
| 资格 Precision | 自动判为可提色中人工真值可提色的比例 | `provisional_target: ≥ 0.90` |
| 资格 Recall | 人工真值可提色中被自动判为可提色的比例 | `provisional_target: TBD_after_stage_1_5_2_5` |
| 资格 F1 | 资格 Precision 与 Recall 的调和平均 | `provisional_target: TBD_after_stage_1_5_2_5` |
| 资格 Coverage | 非 abstain 的自动资格判断数 / 资格评估项总数 | `provisional_target: TBD_after_stage_1_5_2_5` |
| 每商品至少一个有效颜色证据覆盖率 | 至少有一个通过资格、region/mask 与质量门的颜色证据的 in-scope 商品上下文数 / 全部 in-scope 商品上下文数；另报排除原因 | `provisional_target: TBD_after_stage_1_5_2_5` |
| OCR shade-code exact match | 按版本化 Unicode/大小写/空白/连字符规范化后完全相等的 shade code 比例 | `provisional_target: ≥ 0.95` |
| OCR shade-code CER | shade code 的字符编辑距离之和 / 真值字符数之和 | `provisional_target: ≤ 0.05` |
| 颜色 Median ΔE00 | 自动与同一图片人工真值 region/mask 的 image-observed 代表色 ΔE00 中位数 | `provisional_target: ≤ 5` |
| 颜色 P90 ΔE00 | 上述同口径 ΔE00 的第 90 百分位 | `provisional_target: ≤ 10` |
| 多色号配对准确率 | 正确 shade—region 配对数 / 预测配对数 | `provisional_target: ≥ 0.90` |
| 多色号整图完全匹配率 | 一张图的全部期望配对均正确，且无多配/漏配的图片数 / 多色号评估图片数 | `provisional_target: TBD_after_stage_1_5_2_5` |
| mask IoU | 自动 mask 与人工 mask 的交并比，按角色/质量切片报告 | `provisional_target: ≥ 0.75` |
| 歧义检测 Recall | 真值为歧义/数量不一致的图片中被系统标出的比例 | `provisional_target: ≥ 0.90` |

每次报告必须同时给出样本数、置信区间或 bootstrap 区间、分层切片和 abstain 数。阈值冻结后写入 `performance_threshold_versions`；任何修改都创建新版本并重跑固定评估集。

## 18. 迁移和兼容策略

### 18.1 现有预处理元数据

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

### 18.2 主指南数据库语义

- 核心八类角色代码不变；
- `image_id=SHA256` 不变；
- 新增 occurrence/source 层是显式扩展；
- `product_id` 不再直接由原始商品目录生成；
- 原计划混合 `image_roles` 迁移为 A 层 `content_visual_analyses` 与 B 层 `occurrence_context_fusions`，不提供可写混合表；
- `corrected_*` 字段迁移为有明确方法/证据的 `normalized_*`；真正物理校准结果只能使用另行验证的 `calibrated_*`；
- 颜色导出增加 `color_semantics=image_observed_representative`；
- 阶段 1 规范存储为 SQLite + JSONL，Parquet/全量 CSV 镜像后置为可选导出；
- 任何字段重命名都通过迁移表和变更记录完成。

### 18.3 旧设计文档

本轮不批量改写三份旧文档。后续首次实现 schema 时创建 ADR，说明：

- 哪份文档是规范来源；
- 旧表名到新表名的映射；
- 不再采用的“删除损坏图片”等历史描述；
- 对外导出兼容方式。

## 19. 建议的首次实现顺序

严格按以下最小闭环推进：

1. 完成阶段 0.5：轮换/吊销并移除明文密钥，接入 `.env`/环境变量，扫描当前树与 Git 历史，按远程推送状态处置；
2. 完成阶段 1：建立 dataset/source/ref/content/occurrence manifest、SQLite 与 JSONL；
3. 完成阶段 1.5：以 50–100 个唯一 SHA256 跑通 VLM Pilot、A/B 分层、Schema、SQLite 和人工核查；
4. 完成阶段 2：迁移并加固预处理，全局缩略图 + 重叠 tile，不重写原图；
5. 完成阶段 2.5：建立最小标注工具和固定角色/资格/mask/多色号评估集，评审并版本化冻结阈值；
6. 完成阶段 3：扩大 A 层内容视觉分析，再独立运行 B 层 occurrence 来源上下文融合；
7. 达到阶段 3 的 `frozen` 门槛后再进入 OCR、区域、image-observed 颜色和多色号模块；
8. 阶段 8 在最小标注工具之上补齐生产级审核、回归和持续学习。

每个阶段完成后都应停下来生成报告并验收，不自动连续扩展到下一阶段。

## 20. 上一轮文档修订停止点（历史）

上一轮仅修订审计、实施计划和主指南文档，当时没有：

- 创建业务数据库；
- 修改下载器、预处理器或 VLM 脚本；
- 重跑全量预处理；
- 调用付费 API；
- 批量改名、移动或修改原始图片；
- 开始 OCR、分割、颜色提取或模型训练。

特别说明：以上是文档修订轮次的历史停止点。随后在 2026-07-28 的实施轮次中，阶段 0.5 和阶段 1 已完成。

### 20.1 当前实施停止点

当前已完成：

- Git 本地与远端可达历史重写、`.env` 约定和密钥扫描；
- 阶段 1 schema、迁移、构建器、校验器和测试；
- 31,511 张原图的只读 SHA256 manifest 构建；
- SQLite + JSONL 正式运行 `stage1_full_20260728` 和独立验收。

当前没有开始：

- 阶段 1.5 的 50–100 唯一 SHA256 VLM Pilot；
- 阶段 2 预处理加固；
- 阶段 2.5 标注工具；
- OCR、角色/资格分析、mask、颜色提取、实体融合或完整审核系统。

## 21. 文档修订记录

- 2026-07-28：
  - 新增阶段 0.5，将已确认的受跟踪明文 API Key 泄露列为所有后续工作的阻断项，并要求 `.env`/环境变量、密钥扫描及条件性远程 Git 历史核查；
  - 修正来源追溯措辞：当前 CSV 与当前下载器能重建当前路径关系，但没有不可变成功 manifest，不能保证长期稳定追溯；
  - 将模型处理拆成 A 层 `content_visual_analyses` 与 B 层 `occurrence_context_fusions`，删除混合写入 `image_roles` 的设计；
  - 新增阶段 1.5（50–100 唯一 SHA256 Pilot）和阶段 2.5（最小标注/评估工具）；
  - 阶段 1 门禁改为 SQLite + JSONL，Parquet/全量 CSV 镜像后置为可选导出；
  - 所有性能候选值标记 `provisional_target`，补充资格 P/R/F1/Coverage、商品颜色证据覆盖率、OCR exact/CER、颜色 Median/P90 ΔE00 和多色号整图完全匹配率；
  - 明确颜色是 `image-observed representative color`，将 `corrected_*` 改为 `normalized_*`，无可验证校准时不得声称物理真实颜色；
  - 长图策略改为全局缩略图 + 重叠 tile，保存全局布局、tile 坐标、跨 tile OCR 去重与原图回映。
- 2026-07-28（阶段 0.5/1 实施）：
  - 修改原因：用户授权完成阶段 0.5 与阶段 1，并进一步明确“不用确认历史 Key，从 Git 记录里删除 Key即可”；需要把实际执行状态、所有者例外和残余风险与原计划目标区分记录；
  - 实际证据：`origin/main` 由 `fdfdc5804c86f484557e54b3834d1d41dd54fba1` 带租约强制更新为 `90d847c81d0d2354b95ef249781e894b15066805`，目标历史路径在全部本地可达对象中无结果；工作树/索引/历史扫描均为 0 条，正向 fixture 检出 1 条；
  - 阶段 1 证据：`stage1_full_20260728` 生成 1 个数据集快照、2,309 个源记录、31,513 个源引用、31,511 个 occurrence、12,386 个内容 ID 和 31,513 条引用关系；16 个目录碰撞组、9 个品牌别名组均保留；
  - 验证结果：SQLite integrity/foreign key、JSONL SHA/行数/主键、来源闭环、源 CSV SHA、全部原图 stat 和两轮各 100 张 SHA256 抽样均通过；
  - 兼容性影响：原阶段目标和后续阶段设计不变；供应商侧失效未验证，显式记录为 `owner_waived_unverified`，不得解释为已轮换/吊销；本轮停在阶段 1。
- 修改依据：`docs/repository_audit.md` 的路径重放、目录碰撞、重复内容、格式错配、长图和明文 Key 证据，以及本轮用户明确的架构/阶段约束。
