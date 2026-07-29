# 阶段 2.5 状态报告

> 规范候选运行：`stage2_5_annotation_v2_20260728`  
> annotation set：`annotation_set_de7141ce999057cd97a0e4ced735ca2d`  
> 日期：2026-07-28  
> 状态：`candidate_pool_ready_awaiting_ground_truth`

## 已完成的自动部分

- FastAPI + 原生 HTML/Canvas 最小标注工具；
- 角色、资格、bbox/polygon/mask、多色号配对和追加式修订事件；
- 内容页面匿名化，后端不返回路径、品牌、SKU、folder 或 `context_shade`；
- 640 个唯一 SHA256 候选池；
- 无泄漏组分配与候选 split：train/validation/test=`384/128/128`；
- 候选切片：长图 34、格式错配 27、重复内容多 occurrence 274、目录碰撞 36、装饰条/invalid 13；
- 160 个盲复核候选；
- 初版未标注候选证据保留并标记 `superseded`，没有删除。

首版 split 为 `585/40/15`，不能支持最终 60/20/20。原因是 provisional 近重复边与目录组的传递闭包形成 10,136 图超大无泄漏组。v2 不拆分该组，而是在稳定组哈希后按 split 配额选样。

## 尚未完成的人工硬门禁

当前 640 个候选均为 `pending`，因此阶段 2.5 不能声明完成或冻结。后续必须：

1. 最终接受 480 个唯一 SHA256，八类主角色各 60；
2. 最终 split 恰为 train/validation/test=`288/96/96`；
3. 完成至少 160 个 mask，覆盖 `single_bullet`、`single_swatch`、`lip_effect`、`multi_shade_comparison`、`color_card`；
4. 完成至少 80 个多色号完整配对，覆盖 `multi_shade_comparison` 与 `color_card`；
5. 完成至少 96 个双人盲复核项；
6. 对角色或资格冲突追加裁决事件，不以多数票静默覆盖；
7. 由用户/团队提供 `approved_by`，冻结 annotation/evaluation set。

未完成这些项目时，OCR、颜色 ΔE00 和多色号整图指标保持 `not_evaluated`/`provisional_target`，不得记为 0 或伪造基线。

## 审核入口

本地服务：`http://127.0.0.1:8766/`

应选择 `stage2_5_ground_truth:stage2_5_annotation_v2_20260728`。被替代的 v1 默认不在列表中显示。
