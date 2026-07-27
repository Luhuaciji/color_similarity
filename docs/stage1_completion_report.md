# 阶段 1 完成与验收报告

> 完成时间：2026-07-28（Asia/Shanghai）
> 正式运行：`stage1_full_20260728`
> Schema：`stage1-1`
> 状态：`passed`

## 1. 范围和停止点

本轮只完成阶段 0.5 与阶段 1，没有启动阶段 1.5 VLM Pilot，也没有进行 OCR、角色分类、mask、颜色提取、模型调用或全量预处理重跑。`downloaded_images/` 未被写入、改名、移动或删除。

阶段 0.5 的供应商侧失效确认按仓库所有者明确指令被豁免；处置状态是 `passed_with_owner_override`，详见 `docs/stage0_5_security_report.md`。本报告不把该豁免表述为 Key 已轮换或吊销。

## 2. 实现内容

### 安全与配置

- `.gitignore`：忽略 `.env`/`.env.*`，允许 `.env.example`，并停止忽略历史泄露脚本名；
- `.env.example`：只含变量名、空值和公共 Base URL；
- `lipcolor_pipeline/config.py`：无第三方依赖的 `.env` 读取和缺失变量安全失败；
- `lipcolor_pipeline/security.py`、`scripts/scan_secrets.py`：工作树、索引和全部可达 Git blob 的脱敏扫描，含正向假密钥自测。

### 来源清单与数据库

- `database/migrations/001_stage1.sql`：阶段 1 SQLite schema、外键、唯一约束和索引；
- `lipcolor_pipeline/stage1_manifest.py`、`scripts/build_stage1_manifest.py`：全量只读构建器；
- `lipcolor_pipeline/stage1_validate.py`、`scripts/validate_stage1_manifest.py`：独立发布后校验器；
- `tests/`：环境变量、扫描脱敏、迁移回滚、稳定 ID、Unicode/NBSP、目录碰撞、品牌别名、重复内容、SQLite/JSONL 和原图不变测试。

复用了现有 `download_product_images.py` 的 `parse_image_urls`、`sanitize_component`、`url_extension` 和 `output_filename`，没有复制一套可能漂移的下载命名规则。旧预处理元数据只读加载；旧路径相关 `image_id` 显式写入 `legacy_image_id` 和 `legacy_id_mappings`，没有覆盖历史 CSV/JSONL。

## 3. ID 和来源语义

| ID | 实际语义 |
|---|---|
| `dataset_snapshot_id` | `ds_` + 源 CSV 完整 SHA256 |
| `source_record_id` | UUIDv5（快照、CSV 行号、规范行哈希） |
| `source_ref_id` | UUIDv5（源行、字段、序号、URL） |
| `folder_group_id` | UUIDv5（快照、原始相对商品目录） |
| `image_id` | 原始图片字节的完整 SHA256 |
| `image_occurrence_id` | UUIDv5（快照、root alias、原始相对路径） |
| `legacy_image_id` | 历史预处理脚本生成的路径相关 ID，仅作迁移兼容 |

相对路径使用 `root_alias=raw_images`；数据库和 JSONL 不依赖本机绝对原图路径。相同字节内容只创建一个 `image_contents`，但每个物理路径都保留独立 `image_occurrences`；一个 occurrence 可以连接多个来源引用。

## 4. 正式运行产物

运行目录：

```text
stage1_output/runs/stage1_full_20260728/
├── manifest.sqlite
├── jsonl/
│   ├── dataset_snapshots.jsonl
│   ├── pipeline_runs.jsonl
│   ├── folder_groups.jsonl
│   ├── source_records.jsonl
│   ├── source_image_refs.jsonl
│   ├── image_contents.jsonl
│   ├── image_occurrences.jsonl
│   ├── source_ref_occurrences.jsonl
│   ├── brand_alias_candidates.jsonl
│   ├── legacy_id_mappings.jsonl
│   ├── derived_assets.jsonl
│   └── pipeline_errors.jsonl
├── integrity_baseline.jsonl
├── lineage_conflicts.jsonl
├── stage0_5_evidence.json
├── stage1_summary.json
└── stage1_validation.json
```

`stage1_output/` 是可重建生成物目录，已被 Git 忽略。每次正式构建必须使用新的 `run_id`，已有运行目录不会被覆盖。

本次数据集快照：

```text
dataset_snapshot_id =
ds_a80cb6df2a15f8b5774dbd2a7b6e2219ea2608e02eeeb2247538260497bdcd1c
```

## 5. 全量计数

| 表/指标 | 实际值 | 验收值 | 结果 |
|---|---:|---:|---|
| `dataset_snapshots` | 1 | 1 | 通过 |
| `source_records` | 2,309 | 2,309 | 通过 |
| `source_image_refs` | 31,513 | 31,513 | 通过 |
| 唯一预测目标路径 | 31,511 | 31,511 | 通过 |
| `image_occurrences` | 31,511 | 31,511 | 通过 |
| `image_contents` | 12,386 | 12,386 | 通过 |
| `source_ref_occurrences` | 31,513 | 31,513 | 通过 |
| `legacy_id_mappings` | 31,511 | 31,511 | 通过 |
| 目录碰撞组 | 16（涉及 48 个源行） | 16 | 通过 |
| 品牌别名组 | 9（18 个候选目录名） | 9 | 通过 |
| `pipeline_errors` | 0 | 0 个阶段 1 读/血缘错误 | 通过 |

补充交叉验证：

- 完全重复的额外 occurrence 为 19,125，与审计一致；
- 229 个 occurrence 的扩展名和文件魔数格式不一致，已单独记录 `extension_mismatch=1`；
- 单个 occurrence 最多连接 3 个源 URL 引用，证明不能把来源和物理文件强制写成一对一；
- `download_failures.csv` 被识别为非图片文件，不进入 `image_occurrences`。

`pipeline_errors=0` 只表示阶段 1 的字节读取和来源映射没有错误，不代表所有图片都能通过后续严格解码。审计中 3 个解码失败/策略拒绝文件仍由阶段 2 处理，未被删除。

## 6. SQLite 与 JSONL 验证

独立校验器已验证：

- SQLite `integrity_check` 通过；
- SQLite `foreign_key_check` 为 0 条；
- 12 张规范表的 JSONL 文件 SHA256、行数和主键集合与 SQLite 一致；
- 未匹配 source ref：0；
- 没有来源链接的 occurrence：0；
- 没有来源上下文的 content：0；
- `image_id != sha256`：0；
- legacy SHA256 冲突：0；
- 16 个目录碰撞组和 9 个品牌别名组均保留，没有静默合并。

Parquet 和全量 CSV 镜像均未生成，符合阶段 1 “SQLite + JSONL 强制、其余可选”的门禁。

## 7. 原始数据完整性

构建器读取全部 31,511 张原图并计算完整 SHA256，随后执行：

- 源 CSV 完整 SHA256 再检查：通过；
- 原图文件集合前后比较：通过；
- 31,511 个文件的大小和 `mtime_ns` 前后比较：通过；
- 分层确定性抽样 100 张再次计算 SHA256：通过。

独立校验器又重复执行一次全部文件 stat 和 100 张 SHA256 抽样，结果仍通过。完整逐文件基线保存在 `integrity_baseline.jsonl`。

## 8. 测试

使用捆绑 Python 3.12.13 和标准库 `unittest` 执行 10 项测试，全部通过。测试覆盖：

- 扫描器正向 fixture 和脱敏输出；
- `.env` 读取及缺失变量不泄露；
- 下载器 URL/路径规则复用；
- Windows 保留名、Unicode 和 NBSP；
- 稳定 ID 与快照隔离；
- 文件魔数和扩展名分离；
- SQLite 迁移失败回滚；
- 重复 `sku_id`、目录碰撞、品牌别名；
- 同内容多路径；
- SQLite/JSONL 镜像、旧 ID 映射、运行不可覆盖和原始 fixture 不变。

## 9. 已知边界

- 阶段 1 manifest 固化的是本次 CSV 快照、当前命名规则、当前物理路径和当前内容哈希；它不反向声称过去下载时已存在不可变成功 manifest；
- 历史下载器没有保存成功 HTTP 状态、ETag 或响应头，因此 `http_metadata_json` 在本次重建中是空对象，不能伪造网络下载证据；
- 文件格式在本阶段按魔数记录，严格解码、尺寸、ICC、Alpha 和质量观察仍属于阶段 2；
- `folder_group_id` 不是最终 `product_id` 或 `shade_id`；
- 供应商侧历史 Key 是否失效仍未验证，风险接受见阶段 0.5 报告。

## 10. 重跑与校验命令

```powershell
python scripts/scan_secrets.py `
  --repo . `
  --json-output stage1_output/security/secret_scan.json

python scripts/build_stage1_manifest.py `
  --enforce-audit-counts `
  --workers 4 `
  --run-id <新的唯一运行ID>

python scripts/validate_stage1_manifest.py `
  stage1_output/runs/<新的唯一运行ID> `
  --source-csv data/dim_pub_sku_20260513_115554_口红唇膏唇蜜唇釉.csv `
  --raw-root downloaded_images `
  --sample-count 100 `
  --enforce-audit-counts

python -m unittest discover -s tests -v
```

构建器拒绝覆盖已有 `run_id`。命令不需要 Parquet、pytest 或付费 API。

## 11. 验收结论

阶段 1 的 SQLite + JSONL、稳定 ID、多对多来源追溯、旧 ID 映射、目录/品牌冲突保留和原图完整性门禁全部通过。实施在此停止；下一阶段是独立的阶段 1.5 Pilot，未在本轮启动。
