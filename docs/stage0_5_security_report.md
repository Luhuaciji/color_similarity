# 阶段 0.5 安全处置报告

> 完成时间：2026-07-28（Asia/Shanghai）
> 处置状态：`passed_with_owner_override`

## 1. 结果

受跟踪的 `test_qwen36_vision.py` 已从当前树、Git 索引、全部本地可达历史和远端 `origin/main` 的可达历史中移除。远端 `main` 已通过 `--force-with-lease` 从旧提交 `fdfdc5804c86f484557e54b3834d1d41dd54fba1` 更新为重写后的 `90d847c81d0d2354b95ef249781e894b15066805`。

仓库所有者在本轮明确要求“不用确认历史 Key，从 Git 记录里删除 Key 即可”。因此本报告不声称供应商侧 Key 已轮换、吊销或失效；该项记录为 `rotation_status=owner_waived_unverified`。这是一项显式风险接受，不是对实施计划原始安全目标的静默修改。

## 2. 已完成处置

- 重写所有本地可达提交，删除整个 `test_qwen36_vision.py` 历史路径；
- 删除 `filter-branch` 备份引用、stash 旧引用和 reflog，并立即回收不可达对象；
- 使用带明确旧提交租约的强制推送更新 `origin/main`，避免覆盖并发远端更新；
- 从 `.gitignore` 删除针对 `test_qwen36_vision.py` 的忽略规则，使同名文件未来重新出现时可被 Git 和扫描器发现；
- 将 `.env`、`.env.*` 设为忽略，同时通过否定规则允许安全的 `.env.example`；
- 创建只含变量名和空值/公共端点的 `.env.example`，本地 `.env` 不受跟踪；
- 增加 `scripts/scan_secrets.py`，覆盖工作树、索引和全部本地可达 Git blob；
- 扫描输出只保留规则、位置和行号，不保存或打印秘密值、认证头或秘密指纹。

## 3. 验证证据

2026-07-28 执行结果：

| 检查 | 结果 |
|---|---:|
| 专用合成假密钥正向 fixture | 通过，检出 1 条 |
| 当前工作树扫描 | 0 条发现 |
| Git 索引扫描 | 0 条发现 |
| 全部本地可达历史扫描 | 0 条发现 |
| `git log --all -- test_qwen36_vision.py` | 无结果 |
| `git rev-list --all --objects` 中目标路径 | 无结果 |
| `.env` | 被 `.gitignore` 排除 |
| `.env.example` | 可跟踪 |
| 远端重写 | Git 返回 `main -> main (forced update)` |

脱敏机器可读证据位于 `stage1_output/security/secret_scan.json` 和 `stage1_output/security/security_remediation.json`；该目录是生成物目录，不受 Git 跟踪。

## 4. 剩余风险与协作要求

- 供应商侧 Key 是否仍有效没有验证，不能据此报告推断为“已失效”；
- GitHub fork、既有 clone、本地备份、CI 缓存或第三方日志中的历史副本不会因主分支重写自动消失；
- 已拉取旧历史的协作者应重新克隆，或明确将本地分支迁移到新的 `main`；不应把旧提交再次合并或推回远端；
- 如后续恢复供应商侧处置，应在不记录秘密值的前提下新增轮换/吊销证据，不覆盖本报告。

## 5. 结论

按仓库所有者本轮的明确处置范围，阶段 0.5 以 `passed_with_owner_override` 完成，可进入阶段 1。原实施计划中“优先轮换/吊销”的安全目标仍保留；本次例外及其风险已显式记录。
