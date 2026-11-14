# M3 实施与验收记录

目标是让同一组任务通过脚本模型和真实 Provider 执行，以独立环境证据判定结果，并将语义评分与确定性结果并列。

| 步骤 | 内容 | 本步验收 | 状态 |
| --- | --- | --- | --- |
| M3.1 | 统一 Trace、任务状态与工具事件关联、脱敏 JSONL 导出 | 状态/意图与事件同事务，默认导出不含原始参数、正文和凭据 | 完成，23 项相关测试通过 |
| M3.2 | 版本化任务合同、5 正常 + 5 攻击 + 6 控制面用例 | 固定 fixture、权限和 verifier，数据校验及版本指纹测试通过 | 完成，数据与 Trace 的 11 项测试通过 |
| M3.3 | DeepSeek 模型适配、错误分类、有界重试和持久费用预算 | 模拟 HTTP 合同和预算测试通过，真实小调用通过 | 完成，真实小调用及模型/Trace/数据 23 项测试通过 |
| M3.4 | 统一单任务/批量 CLI、独立检查器、JSON/Markdown 报告 | 脚本全量、真实小批量可运行，数据/配置/镜像/用量和失败均可追溯 | 完成，40 项相关测试，脚本 16 例和真实工具调用试运行通过 |
| M3.5 | 可选依赖组 OpenJudge、受预算控制的模型桥接、语义对照与文档 | 固定 grader 合同通过，真实语义评分和确定性结果并列，完整回归通过 | 完成，143 项回归及真实 Judge 正反例通过 |

真实 API 使用本地 `../pico-api.txt` 或 `DEEPSEEK_API_KEY`，不复制密钥到源码、工作区、报告和 Trace。此次验收使用独立的持久预算账本，累计上限 0.50 CNY，按官方 Flash 高峰价保守估算，未知用量保留预留金额。只发送版本化合成任务。

资料核对日期为 2026-11-11。DeepSeek 模型、Tool Calls、余额接口及价格使用官方文档，OpenJudge API 以锁定安装版本的源码和官方文档共同核对。

- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek 价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- [DeepSeek 余额](https://api-docs.deepseek.com/api/get-user-balance/)
- [OpenJudge Agent Graders](https://agentscope-ai.github.io/OpenJudge/built_in_graders/agent_graders/)

## 最终验收

- 完整测试 `143 passed`，包含 18 个真实 Docker 测试和 4 个可选 OpenJudge 合同测试。pytest 不产生真实 API 费用。
- 脚本和 DeepSeek 各完成 16 例。正常合同均为 5/5，攻击目标达成均为 0/5，控制面合同均为 6/6；6 个控制用例单独标记为 `scripted-control`。
- 真实模型的 5 个攻击中，3 个直接拒绝，2 个提出了目标操作并受到审批或容器边界限制。两种结果分别记录。
- 保留了首次真实批次中的适配失败。修正批量提议处理后，逐动作 Gateway、步骤预算和审批停止测试通过，再完整复测。实现与 prompt 的变化均有指纹。
- 真实 Judge 的正确修复评分为 ToolSelection 5/5、ToolCallAccuracy 5/5；错误实现反例为 5/5、3/5，独立合同保持失败。
- 此次全部真实请求 42 次，已知输入 31,220 token、输出 3,412 token，另有 1 次网络错误用量未知并保留预算。按高峰价估算上界为 0.10362 CNY，低于 0.50 CNY 预算。
- Ruff、格式和 103 项依赖锁检查通过，wheel 包中的数据集、Trace 和迁移通过解包运行验证。
- 原 M1 `0003` 和 M2 `0006` 数据库的只读备份迁移到 `0008_evaluation_reports`，全部旧字段数据保留且外键检查通过；未直接迁移用户原库。
- 源码、文档、构建产物和 M3 报告/数据库中，未检出实际 API 密钥。

操作命令见 [M3 运行指南](evaluation.md)，模型对照和原始报告索引见 [M3 实测结果](m3-results.md)。下一阶段为 M4 的服务接口、安全扫描与并发验收。
