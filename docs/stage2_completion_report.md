# 阶段 2 完成报告

> 运行：`stage2_full_20260728`  
> 日期：2026-07-28  
> 状态：`passed`

## 历史产物迁移

- 31,511/31,511 条旧元数据成功映射到当前 occurrence；
- 31,508 个成功 working-image 哈希全部与旧元数据一致；
- 12,383 个规范 working 资产和 751 个规范 Alpha 资产完成登记；
- 13,134 个资产均使用同盘硬链接，未修改旧文件；
- 同一内容 SHA 的 working-image 哈希不一致数为 0；
- 映射错误、working 哈希错误均为 0。

## 全量预处理

| 项目 | 结果 |
|---|---:|
| 唯一内容 | 12,386 |
| 严格成功 | 12,383 |
| 损坏/截断失败 | 2 |
| 100MP 策略拒绝 | 1 |
| occurrence | 31,511 |
| occurrence 成功 | 31,508 |
| 格式错配 occurrence | 229 |
| 长图全局布局 | 172 |
| 重叠 tile | 1,330 |
| 近重复边 | 43,481 |

长图同时保存全局缩略图、阅读轴、原图布局、半开 tile 坐标、重叠范围和双向坐标变换；没有只生成 tile。

## 最终门禁

- 内容数、内容状态、occurrence 数、occurrence 状态：通过；
- 229 个格式错配复现：通过；
- 长图全局资产/tile 完整：通过；
- 31,511 个原图 SHA 全量复核：零漂移；
- 派生资产：零缺失、零哈希不一致；
- 原始图片和阶段 1 SQLite 未修改。

规范报告位于：

- `pipeline_output/runs/stage2_full_20260728/reports/legacy_migration_summary.f338bc4b95445242.json`；
- `pipeline_output/runs/stage2_full_20260728/reports/stage2_execution_summary.276514a001d5bc69.json`；
- `pipeline_output/runs/stage2_full_20260728/reports/stage2_validation.42ca0dac59763532.json`。
