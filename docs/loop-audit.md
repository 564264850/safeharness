# 完整循环与既有保护审计（2026-11-12）

后续修复见 [审批续跑 R4](approval-loop.md)：新增 Agent 审批续跑、独立验证记账和基础重复失败停止策略。下文保留审计当时的复现结果。

结论：已有 Runtime、Gateway、调用身份、工单审批及业务回执恢复能够执行各自的既定契约；通用、多步骤、审批后的 Agent 循环尚未闭合。BudgetBook 的交付经历控制方调整配置、导入部分源码及开启新任务，不能描述为单次任务从失败到成功的全自动恢复。

本轮未修改生产源码，未请求付费模型。检查当前实现，运行 146 项针对性回归，并新增独立的离线行为探针。

## 仍然生效的能力

| 能力 | 核对结果 |
| --- | --- |
| 工具参数、路径和资源范围校验 | Gateway 派发前处理；无效或越权调用不会因此取得权限 |
| 调用身份与台账 | Runtime/Gateway 共用宿主生成的 task/run/call/attempt；回调伪造 ID 不具权威，已结算结果不可改写 |
| 人工审批 | 工单写操作暂停模型和普通 verifier，审批批准前不修改工单；角色、范围、参数摘要、决定不可覆盖和执行时效检查保留 |
| 提交幂等与业务恢复 | 真实子进程退出演示再次通过；审批后业务提交再崩溃，恢复后仍然只写 1 次，1 个 call 关联 3 个 attempt |
| 通用检查点 | 已保存结果可以继续；配置变化拒绝恢复；未确认的工具边界进入 NEEDS_REVIEW，不能盲重放 |

Coding 的 write_file 不需要人工审批，这是当前 `ToolSpec.requires_approval` 的明确策略：仅工单业务写要求审批。受限代码文件写依靠范围授权和 Docker 隔离。现有审批没有覆盖任意自定义工具、宿主写入或未来外部系统。

## 本轮复现的缺口

1. **审批后缺少通用 Agent 续跑。** 离线模型一次提出“更新工单，然后读取工单”。Runtime 正确暂停为 WAITING_APPROVAL；人工批准并执行现有 `TaskStore.resume` 后，工单业务进入 SUCCEEDED，但队列仍有 1 项读取未执行，通用 verifier 调用次数为 0。业务恢复自身执行了回执核对，问题在于它按单步工单流程收尾，未回到原 Agent 做剩余任务与最终验收。对声称支持审批后多步骤任务的通用 Harness，这是 P0 完整性缺口。
2. **新开发脚本的独立验证未进入统一调用台账。** `scripts/deepseek_program.py` 直接构造独立 CodingWorkspace 并调用 execute；14 项独立测试确实运行且通过，仍在 Docker 内，但没有经过 Gateway/Audit/InvocationLedger。最终台账只含 4 个模型调用、2 个普通测试工具调用和 1 个写文档调用，没有独立验证调用。隔离保留，追溯链不完整；需优先统一此入口，不能据此宣称所有执行都已完整记账。
3. **缺少无进展检测。** 模型连续提出无效工具时，探针得到 4 次相同拒绝，最后因 step_limit 失败；没有更早识别重复错误并调整计划或停止。现有步数、时限、费用预算提供上限，不能代替纠错策略。
4. **Coding 环境可用性存在待定位的不稳定。** 146 项回归首轮 144 通过、2 失败；两个失败均在首个 list_files 得到 sandbox_unavailable。随后 preflight 与 isolation smoke 通过，两个用例单独重跑也通过。工作区将具体的 preflight 检查结果归为笼统的 sandbox_unavailable，现有证据不足以确定首轮是启动延迟、超时还是其他环境条件，不能将它当作已修复。

Context 超限时拒绝请求、工具结果不明时停止核对属于当前边界策略；但它们尚未组合成自动拆分任务、读取历史和安全重调度。进程故障后的显式恢复仍由操作方发起，没有自动 reaper。

## 本轮证据

- 目标回归：`artifacts/loop-audit-regression.xml`，146 项，首轮 144 passed / 2 failed。
- Coding 两项复测：`artifacts/loop-audit-coding-rerun.xml`，2 passed。
- 幂等、审批及真实进程退出：`artifacts/invocation-demos/aa0d6dc5-001a-4ba7-8233-de80b3036a1b/report.json`，business_write_count=1。
- 离线探针：`artifacts/loop-audits/d4218997-bdb7-4cbc-b420-aa943e8a61e4/report.json`。
- 汇总：`docs/evidence/loop-audit.json`。

建议先统一控制面验证调用，再设计审批结果回填 Context、恢复后续工具队列、最终 verifier 的通用续跑；随后加无进展检测及环境就绪诊断。保留现有单步工单业务回执兼容路径，不能把通用续跑实现成无条件重放已批准写操作。
