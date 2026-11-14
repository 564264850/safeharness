# SafeHarness

面向工具调用型 Agent 的安全执行、故障恢复与评测工程原型。从本地 Pico 的 Coding Harness 设计出发，独立实现一个控制循环，供 Coding Workspace 和模拟 IT 工单两个环境共用。

后续面向 Agent／AI 应用实习的实施顺序与验收标准见 [求职优化计划](job-oriented-plan.md)。

| 文档 | 内容 |
| --- | --- |
| [Pico 现状与迁移边界](pico-audit.md) | PDF 与源码对应、执行路径、基线和待改造点 |
| [外部组件调研](integrations.md) | 上游版本和许可、接入方式、验收门槛及研究依据 |
| [架构与交付路线](roadmap.md) | 核心契约、故障窗口、分阶段验收和实验设计 |
| [M2 实施记录](m2-implementation.md) | M2.1–M2.5 的实施顺序和验收记录 |
| [Coding 沙箱指南](coding-sandbox.md) | Docker、文件与测试工具、隔离边界和亲手演示 |
| [M3 运行指南](evaluation.md) | 真实 DeepSeek、费用预算、Trace、版本化评测和 OpenJudge |
| [M3 实测结果](m3-results.md) | 脚本/真实模型对照、攻击与恢复证据、语义评分差别 |
| [M4 服务与操作](service.md) | FastAPI、身份、PostgreSQL、审批与两条 HTTP 演示 |
| [M4 实测与验收](m4-results.md) | 120 实例、真实扫描、并发恢复和结果边界 |
| [威胁模型](threat-model.md) | 信任边界、控制措施、已执行证据与限制 |
| [运行正确性 R1](runtime-correctness.md) | 异常状态收敛、未知结果核对、HTTP/CLI 错误诊断和故障演示 |
| [统一调用契约 R2](invocation-contracts.md) | task/run/call/attempt/产物、提交幂等、模型重试与审批恢复关联 |
| [Harness 底层 R3](harness-core.md) | 通用 Agent、Context 预算、私有检查点、Coding 快照和安全续跑 |
| [DeepSeek 网页开发实测](deepseek-program.md) | 真实模型工具调用、网页记账程序、30 项功能检查及失败修复记录 |
| [完整循环审计](loop-audit.md) | 审批与调用保护复测、通用审批续跑和验证记账的缺口 |
| [审批续跑 R4](approval-loop.md) | 多次审批、结果回填、后续工具与最终验收、真实 DeepSeek 演示 |
| [当前项目与简历评审](project-review-r4.md) | 核心贡献、能力边界、验证器新问题与求职展示建议 |
| [验证真实性 R5](verification-truth.md) | 提前退出与伪造测试报告的修复、宿主验收合同及复现演示 |
| [持久调度测试计划](scheduler-test-plan.md) | 分阶段构建、20 个故障场景、自动验收与亲手演示安排 |
| [S1 持久接单](scheduler-s1.md) | 原子入队、幂等兼容、提交故障与 API 重启演示 |
| [S2 独立 worker](scheduler-s2.md) | 短事务领取、多进程竞争、复用 Harness 与双 worker 演示 |
| [S3 租约与心跳](scheduler-s3.md) | 数据库时间续期、长调用、心跳失败和进程死亡演示 |
| [S4 自动恢复](scheduler-s4.md) | 过期扫描、检查点续跑、恢复预算和不确定调用停止 |
| [S5–S7 调度闭环与验收](scheduler-s5-s7.md) | 旧 worker 隔离、批准后自动续跑、业务回执、容量和运行验收 |
| [A.I.G 扫描](scanning.md) | 上游锁定、低权限模型代理、负对照与加固回归 |

## 当前切片

当前包含工单审批恢复、受限 Docker Coding 工作区、脚本/DeepSeek 共用的评测流程，以及带角色与归属检查的 FastAPI 服务和 PostgreSQL 并发持久化。两个环境共用 Gateway 和 Trace；独立合同决定结果，OpenJudge 提供补充评分。A.I.G 的真实 HTTP 扫描已在合成负对照及加固重放上验证。环境使用 Python 3.11。

持久调度切片已支持脚本工单的独立 worker、租约心跳、过期接管、审批后自动续跑、幂等回执核对、容量限制和运行状态查询。真实模型与 Coding 保持显式运行/恢复入口；具体范围和实际验收记录见 [S5–S7 指南](scheduler-s5-s7.md)。

```powershell
uv sync --locked --extra service --extra judge --python 3.11
uv run --no-sync safeharness
uv run --no-sync pytest -m "not acceptance"
uv run --no-sync ruff check src tests scripts
```

示例输出包含 `task_id`、`task_status`、`revision`、`stop_reason` 和按顺序排列的工具事件。任务从 `CREATED` 进入 `RUNNING`。模型提交最终文本后进入 `VERIFYING`，通过独立验证才进入 `SUCCEEDED`。用尽步数或验证失败时进入 `FAILED`。

R1 已补充普通回调异常与非法协议结果的收敛：可确定失败进入 `FAILED`，派发结果不明进入 `NEEDS_REVIEW`；错误与状态同事务保存，并通过 `show`、HTTP 查询及 Trace 提供 `last_runtime_error`。存储不可写会明确报错，已提交状态和原业务回执流程不会被覆盖。通用任务的自动重启扫尾仍需后续实现；R3 已增加检查点的显式续跑。

R2 已统一 task → run → call → attempt → artifact 身份链，关联模型重试、Gateway 派发与审批后的业务恢复。`POST /v1/tasks` 支持按 owner 隔离的 `Idempotency-Key`，相同请求返回原任务，参数冲突返回 409。可用 `show-invocations` 或 HTTP `/invocations` 查询元数据，复现命令见 [R2 指南](invocation-contracts.md)。

## 跨进程读取任务状态

R3 已抽出通用 ToolCallingAgent、受预算的 ContextManager 和可组合 Harness。HTTP、CLI 工单与 Coding 演示保存阶段检查点，支持主动暂停和显式恢复，保留原结果、模型队列、文件快照及累计预算。运行 `python scripts/harness_demo.py` 或加 `--coding` 可复现真实进程退出，见 [底层指南](harness-core.md)。

从项目目录执行以下命令。第一条创建并执行任务，第二条启动新进程读取同一个 ID。

```powershell
$taskRun = .\.venv\Scripts\python.exe -m safeharness | ConvertFrom-Json
.\.venv\Scripts\python.exe -m safeharness show $taskRun.task_id
```

预期返回 `SUCCEEDED` 和 `revision: 3`。任务创建时版本为 0，每次状态转换将版本加一。SQLite 默认文件为当前目录下的 `artifacts/tasks.sqlite3`。需要指定其他路径时，在两个命令的 `-m safeharness` 后都加上 `--db <路径>`。

每次状态更新在独立事务中提交，只有数据库中的旧状态和旧版本仍匹配时才会写入。当前数据库还保存审批、执行意图、调用审计和 Coding 报告。

运行演示时会应用 Alembic 迁移。也可以单独运行 `.\.venv\Scripts\python.exe -m safeharness migrate`。当前迁移版本为 `0017_queued_approvals`，在租约与自动恢复之上增加 WAITING 审批队列和持久唤醒。旧任务不会自动入队；不确定调用交人工核对。升级共享部署前先退出旧版进程，见 [S5–S7 指南](scheduler-s5-s7.md)。旧 CLI 数据不会自动取得 HTTP 身份；HTTP 使用自己的配置目录和任务库。读取命令 `show` 只查询任务。

运行 `.\.venv\Scripts\python.exe -m pytest -q -k "task_persistence or model_claim_without_tool_evidence"`，可以查看跨进程查询、旧快照冲突和缺少工具证据时保存失败状态的测试。

## 工单工具契约

| 工具 | 参数 | 通过参数校验后的结果 |
| --- | --- | --- |
| `read_ticket` v1 | `ticket_id` | 在读取范围内时查询工单 |
| `update_ticket_status` v1 | `ticket_id`、`status`，状态为 `open`、`in_progress` 或 `resolved` | 在写入范围内时返回 `approval_required`，工单保持原状态 |

读取和写入范围由调用方分别配置。模型只能提供工具名和参数。参数中混入 `approved` 等额外字段会被 Schema 拒绝。用 `.\.venv\Scripts\python.exe -m pytest -v -k ticket_update` 可核对有无写权限、非法状态和伪造审批字段的结果。

## 跨进程读取待审批请求

```powershell
$pendingRun = .\.venv\Scripts\python.exe -m safeharness request-update | ConvertFrom-Json
$pendingRun | Select-Object task_status, ticket_status, approval_id
.\.venv\Scripts\python.exe -m safeharness show-approval $pendingRun.task_id
.\.venv\Scripts\python.exe -m safeharness show $pendingRun.task_id
```

`request-update` 使用脚本模型申请把模拟工单 `IT-001` 改为 `resolved`。任务进入 `WAITING_APPROVAL`，工单状态保持申请时的值。后续进程通过任务 ID 读回同一个审批 ID、工具名和版本、规范化参数及其 SHA-256 摘要。

审批记录和任务状态在同一个事务中提交。插入审批记录失败时，任务状态更新也会回滚。

## 审阅并记录审批决定

先读取上一步的审批申请，确认工单 ID 和目标状态。

```powershell
$request = .\.venv\Scripts\python.exe -m safeharness show-approval $pendingRun.task_id | ConvertFrom-Json
$request | ConvertTo-Json -Depth 4
```

确认后执行审批命令。选择拒绝时，将 `approve` 改为 `reject`。

```powershell
.\.venv\Scripts\python.exe -m safeharness decide-approval $request.approval_id approve --arguments-sha256 $request.arguments_sha256
.\.venv\Scripts\python.exe -m safeharness show-approval $request.task_id
```

同一审批 ID 只能记录一次决定。提交时会核对待审批状态和已审阅参数的摘要。批准将审批状态置为 `APPROVED`，任务在 `WAITING_APPROVAL` 等待 `resume` 领取。拒绝将审批状态置为 `REJECTED`，并在同一事务将任务转为 `FAILED`。

本地 CLI 以能操作此数据库的操作员为信任边界，从操作系统取得身份。HTTP 服务另外执行 opaque token、角色、任务归属与自批禁止检查。模型工具参数无法声明审批主体。操作见 [服务指南](service.md)。

用 `.\.venv\Scripts\python.exe -m pytest -v -k "pending_approval or approval_decision"` 运行相关测试。

## 执行、故障注入与恢复

控制库保存审批和执行意图。相邻的 `<控制库文件名>.tickets.sqlite3` 是模拟业务库，工单更新和业务回执在业务库同一个事务提交。操作 ID 复用审批 ID。首次写入前会校验任务状态、审批结果、参数摘要、工具及策略版本和工单范围。批准后的新派发窗口为 24 小时，已提交的回执可在窗口结束后继续核对。

对已批准的任务运行 `resume <task_id>`。以下命令使用当前 PowerShell 会话中的 `$request`，在业务库提交后强制终止执行进程，再启动新进程恢复。

```powershell
.\.venv\Scripts\python.exe -m safeharness resume $request.task_id --simulate-crash-after-write
$LASTEXITCODE
.\.venv\Scripts\python.exe -m safeharness show-execution $request.task_id
.\.venv\Scripts\python.exe -m safeharness show-ticket
.\.venv\Scripts\python.exe -m safeharness resume $request.task_id
.\.venv\Scripts\python.exe -m safeharness show-ticket
```

对首次派发，故障注入的预期退出码为 77，控制面显示 `INTENT`。恢复后应看到 `SUCCEEDED` 和 `reconciled_business_receipt`。对同一个操作，恢复前后 `write_count` 应相等。使用全新的数据库时，首次写入后的计数为 1。已完成任务再次 `resume` 时会返回既有结果。

`cancel <task_id>` 可取消尚未派发的任务。若业务库已有回执，先用 `resume` 核对已提交的写入。业务库缺失、身份变化或回执无法匹配时，恢复会保存 `UNKNOWN` 并将任务转为 `NEEDS_REVIEW`，由操作者查看 `show-execution` 的原因和操作 ID。该原型信任两份本地 SQLite 文件，核对基于模拟服务的事务回执。

用 `.\.venv\Scripts\python.exe -m pytest -v -k execution_recovery` 在临时库跑完整恢复用例。
