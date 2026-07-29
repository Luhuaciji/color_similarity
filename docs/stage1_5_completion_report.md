# 阶段 1.5 完成报告

> 运行：`stage1_5_pilot_20260728`  
> 日期：2026-07-28  
> 状态：`passed`

## 结果

- Pilot 最终包含 65 个唯一 SHA256，处于 50–100 硬门禁范围内；
- 65/65 个内容均有 A 层结构化分析，模型请求清单、原始响应、解析结果和 Schema 状态齐全；
- A 层数据库不存在 occurrence、folder、SKU 或 `context_shade` 字段，禁止上下文列检查为 0；
- 模型成功缓存键无重复付费成功，长图均有全局缩略图和重叠 tile；
- B 层共有 192 条 occurrence/source context 机器融合结果；
- 固定策略 `context_review_policy_95159cc1bada5dd086fdafd696d06dda` 的 40 条来源上下文已全部人工审核：38 条与机器一致、2 条分歧；
- 未入固定样本的 152 条关系保持 `machine_prelabel_unreviewed`，未计作人工真值；
- 最新 Gate 为 `go`，`approved_by=repository_owner_instruction_20260728`。

## 色卡补选与审核来源

缺失角色通过真实内容 SHA `da789b0437f65727b406c7d6393a3e0288620368d8a333861cc455c4703c670a` 补足。图片自身显示两个带 `#M03/#G01` 标识的规则数字色块；候选检索和角色审核未使用目录、SKU 或来源上下文。

用户明确指示“继续补选色卡，这部分无需人工审核，然后完成剩下的部分”。因此该项记录为：

- `review_provenance=owner_delegated_agent`；
- scope=`stage1_5_color_card_topup`；
- 角色=`color_card`；
- 资格=`true`，语义为 image-observed digital color evidence；
- delegation=`owner_review_delegation_36b188c09ad257dbb4dd46cece76b80c`。

VLM 对同图的原始结论为 `multi_shade_comparison`，置信度 0.95。模型结论与授权代理审核分别保存，没有互相覆盖。该授权只覆盖此 SHA，不豁免阶段 2.5 的人工固定集。

## 验收证据

- 规范验证报告：`pipeline_output/runs/stage1_5_pilot_20260728/reports/stage1_5_validation.7f01f3b42a917426.json`；
- 补样报告：`pipeline_output/runs/stage1_5_pilot_20260728/reports/pilot_topup.2863346cfa8a8309.json`；
- SQLite/JSONL 导出：`pipeline_output/runs/stage1_5_pilot_20260728/`；
- 自动测试：30 passed；
- 原始图片未修改。
