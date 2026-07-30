# 图片观测色相似度 MVP 完成报告

## 1. 结论

Stage 2.6 图片区域观测到色号相似度 Top-K 的本地工程闭环已经实现并通过当前 workspace 验证。

```text
engineering_status = engineering_similarity_baseline_passed
output_semantics = image_observed_color_similarity_baseline
quality_status = not_evaluated_without_ground_truth
```

本次结果只表示图片 swatch 观测的 CIEDE2000 基线，不表示物理真色、实际妆效、商品替代度或正式阶段 3–7 已完成。

最终验证 run：

```text
observed_similarity_mvp_final_20260731
```

另有一次实现收紧前的工程探测 run `observed_similarity_mvp_20260731`。最终下游只应消费带 `_final_` 的 run。

## 2. 实施内容

### 2.1 设计和版本

- 原位修订 `docs/小规模色号数据集无监督相似度计算方案.md`；
- package/pipeline 升级到 `0.4.0`；
- workspace schema 升级为 `observed-similarity-mvp-1`；
- 默认迁移上限和 CLI choices 升级到 8；
- Stage 2.6 的旧测试仍可显式停在迁移 7；
- 新增配置段 `shade_similarity`；
- 输出语义固定为 `image_observed_color_similarity_baseline`。

### 2.2 数据库

新增 `database/migrations/008_observed_similarity_mvp.sql`：

- `shade_similarity_inputs`
- `shade_color_observations`
- `shade_color_profiles`
- `shade_similarity_pairs`
- `shade_similarity_topk`

表约束覆盖 run/来源外键、正式 observation 资格、固定上三角 pair、非自身 Top-K、唯一 candidate 和唯一 rank。

### 2.3 代码

新增：

- `lipcolor_pipeline/color_similarity.py`
  - Hex/RGB 规范化；
  - Lab/LCh 诊断；
  - 向量化和标量 CIEDE2000；
  - 展示分数和未校准距离带。
- `lipcolor_pipeline/shade_similarity.py`
  - source manifest 校验；
  - 色号 token 标准化；
  - 业务/图片内身份解析；
  - 区域资格和全部排除证据；
  - ΔE00 medoid profile；
  - NumPy block pair 和 SQLite 批写；
  - SQL window Top-K；
  - run/resume/export 和原图 SHA 审计。

修改：

- `lipcolor_pipeline/cli.py`
- `lipcolor_pipeline/settings.py`
- `lipcolor_pipeline/workspace.py`
- `lipcolor_pipeline/__init__.py`
- `configs/pipeline.yaml`
- `pyproject.toml`

`shade-similarity plan` 使用只读 workspace 加载器；未迁移到 008 时只提示显式初始化，不隐式写库。

## 3. Canonical 输入

版本化清单：

```text
configs/samples/observed_similarity_mvp_sources_v1.jsonl
```

清单 SHA256：

```text
f36d8b4f2b37c7b92531fac97ee3f48d5a3d98694406f93befceeb76ca18539e
```

来源构成：

| Stage 2.6 run | 图片数 | 用途 |
| --- | ---: | --- |
| `stage2_6_e3_20260729` | 26 | E3 canonical success |
| `stage2_6_e3_recovery_20260729` | 2 | 替代旧失败/partial |
| `stage2_6_e4_20260729` | 11 | 固定完整目录 |
| 合计 | 39 | 唯一 `image_id` |

没有纳入 E2 子集、cache run 或被 recovery 替代的旧结果。

## 4. 最终 run 结果

### 4.1 Run fingerprint

| 项目 | 值 |
| --- | --- |
| run status | `completed` |
| pipeline version | `0.4.0` |
| schema version | `observed-similarity-mvp-1` |
| code fingerprint | `a9804669b23338755cbc6a26fdeaa523e4923918f4c1c58b3eb113e7cbab08be` |
| config hash | `a054bc65a3e4d5c3a97b965fca8ef79c498ad5fc9e52ce05babf24905230f0b7` |
| entity/profile algorithm | `shade-entity-profile-1.0` |
| CIEDE2000 algorithm | `ciede2000-observed-similarity-1.0` |
| Top-K | 10 |
| 最大 ΔE00 | 未设置 |
| model/API calls | 0 |

同一 fingerprint 执行 `--resume` 后返回 `completed_run_reused`，所有计数不变，没有重复写入。

### 4.2 数量门禁

| 指标 | 预期 | 实际 | 状态 |
| --- | ---: | ---: | --- |
| canonical 图片 | 39 | 39 | 通过 |
| 源区域 | 208 | 208 | 通过 |
| 成功颜色 | 207 | 207 | 通过 |
| 正式 swatch observation | 19 | 19 | 通过 |
| 正式 profile | 17 | 17 | 通过 |
| 业务 profile | 11 | 11 | 通过 |
| 图片内临时 profile | 6 | 6 | 通过 |
| 无重复 pair | 136 | 136 | 通过 |
| Top-10 行 | 170 | 170 | 通过 |

17 个 profile 中：

- 16 个为 `single_observation_provisional`；
- 1 个为 `multi_observation_provisional`；
- 11 个为 `business_resolved`；
- 6 个为 `image_local_unmatched`；
- 当前真实样本没有正式 `image_local_ambiguous`，但实现和单元测试已覆盖该分支。

### 4.3 排除诊断

一个区域可以同时有多个排除原因：

| 原因 | 次数 |
| --- | ---: |
| `region_type_not_formal` | 147 |
| `no_linked_shade_code` | 138 |
| `color_confidence_not_accepted` | 108 |
| `missing_association_confidence` | 70 |
| `extraction_status_skipped_ineligible` | 1 |
| `missing_color_payload` | 1 |
| `shade_code_unparseable` | 1 |

不合格区域仍完整保存在 `shade_color_observations`，但没有进入 swatch profile。

### 4.4 正式 Top-K 中的临时候选

正式 Top-K 已同时包含业务和图片内临时候选：

| query → candidate | 行数 |
| --- | ---: |
| 业务 → 业务 | 73 |
| 业务 → 图片内临时 | 37 |
| 图片内临时 → 业务 | 32 |
| 图片内临时 → 图片内临时 | 28 |

临时候选保持 `identity_status=image_local_*`，不填造业务 SKU，也不因跨图代码相同自动合并。

最接近的部分 pair 示例：

| 色号 A | 身份 A | 色号 B | 身份 B | ΔE00 | 展示分数 |
| --- | --- | --- | --- | ---: | ---: |
| V03 | 业务 | N01 | 业务 | 1.0568 | 98.8955 |
| M03 | 业务 | 544 | 图片内临时 | 1.4659 | 97.8964 |
| N01 | 业务 | V07 | 业务 | 2.0833 | 95.8403 |
| 646 | 图片内临时 | 544 | 图片内临时 | 3.7523 | 87.6581 |
| 520 | 图片内临时 | V08 | 业务 | 3.7602 | 87.6125 |

这些示例只验证工程输出和身份混排规则，不是相似度质量 ground truth。

## 5. 导出

目录：

```text
pipeline_output/runs/observed_similarity_mvp_final_20260731/exports
```

| 文件 | 行数/内容 | SHA256 |
| --- | ---: | --- |
| `shade_observations.csv` | 208 | `dd0c41320f1e268c2d793177bd4b1a2214c012aecf10daa848ad28820822f133` |
| `shade_profiles.csv` | 17 | `7a33c09d6726a5427406f9106724f6eae5eba2fcf4ceb7221bca0b229efec3bb` |
| `similarity_pairs.csv` | 136 | `c9b23e16d31773671b77d175e4e0c474f397c641c9150b40008b81176f32cce3` |
| `top_k.csv` | 170 | `7825aee371cb3db30bf83f6e9b5cbec3dcdfb5b6c8cb6e27426cffc20857086f` |
| `similarity_summary.json` | 汇总 | `5e801d94e8193c7bb46f6149de922f7374a435f6cdae9f3888efd2b629895038` |

CSV 由 SQLite 游标流式写出，pair 不会一次全部物化为 Python 字典。

## 6. 测试和审计

### 6.1 自动化测试

最终结果：

```text
49 passed
```

原基线为 43 passed，新增 6 个相似度测试，覆盖：

- 迁移 008；
- 全部 34 组 CIEDE2000 标准参考向量；
- 对称性、非负性、恒等性、零彩度和色相环绕；
- Hex 和色号代码标准化；
- 业务唯一匹配、无匹配和多 SKU 冲突；
- 图片内稳定 ID 和跨图隔离；
- 39 图 manifest 组成；
- 正式区域资格、profile 隔离、medoid 并列规则；
- block pair、plan/run/resume、Top-K 和五类导出；
- plan 数据库零写入和原图 SHA 不变。

唯一警告仍为既有 Starlette `TestClient` deprecation warning，不影响本次结果。

### 6.2 只读 plan

CLI `plan` 前后 workspace 主数据库 SHA256 均为：

```text
aa17f114ae70ab3bdad3e7dd7668c00e829b6ca0335ee665e7f8db1187a613e5
```

说明相似度 `plan` 命令入口没有更新 run 表或 workspace metadata。

### 6.3 SQLite

| 检查 | 结果 |
| --- | --- |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA foreign_key_check` | 0 条违规 |
| 非 swatch 正式 profile | 0 |
| 非 medium/high 正式 profile | 0 |
| pair 顺序或自身 pair 违规 | 0 |
| 重复 pair | 0 |
| Top-K 自身候选 | 0 |
| 重复 Top-K rank | 0 |
| 重复 Top-K candidate | 0 |
| 与该 run 关联的 model run | 0 |

### 6.4 原始图片

最终 run 对 39 个唯一图片、111 个 occurrence 做了运行前后 SHA256 快照：

- 全部 occurrence 的实际 SHA256 等于注册 `image_id`；
- 前后快照内容完全相同；
- 两份快照 SHA256 均为
  `a154b8d4093749fb3291d12274d8b8646dd0279f49ec9c74de2334be5af79b53`。

原图、Stage 1 数据库和历史 Stage 2.6 run 没有被修改。

## 7. 未评估质量

没有人工或业务 ground truth，因此以下内容继续标记：

```text
not_evaluated_without_ground_truth
```

- OCR 色号准确率；
- 色号到 SKU 的实体解析准确率；
- swatch bbox 和代表色准确度；
- CIEDE2000 Top-K 的业务相关性；
- 距离带与真实感知或替代关系。

工程通过不能替代这些质量评估。

## 8. 逻辑回滚

推荐逻辑回滚，不修改历史证据：

1. 下游停止读取 `observed_similarity_mvp_final_20260731`；
2. 恢复 workspace 默认迁移上限前，可显式让旧测试/工具停在迁移 7；
3. 不回写 Stage 2.6 observation 或原图；
4. 如确需清理，只删除明确 run ID 对应的五张 similarity 表记录和该 run 导出目录；
5. 禁止递归删除 workspace 根目录；
6. 身份或算法规则变化时创建新版本 run，不修改本 run。

## 9. 结论边界

本次完成的是可追溯、确定性、纯本地的图片观测色相似度基线。它已经可以把业务色号和无法唯一映射 SKU 的图片内临时候选共同放入正式 Top-K，同时明确隔离两类身份。

下一步应优先建立小规模 ground truth，评估 OCR、实体匹配、代表色和 Top-K 质量，再决定是否进入业务替代度或多模态 rerank。
