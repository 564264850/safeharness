# SafeHarness 当前阶段交付报告

报告日期：2026-11-11。代码声明版本：0.1.0。范围：M0–M4 交付时点的实现和证据。后续 R1 已修复普通异常收敛，新增能力及验证见 [R1 记录](runtime-correctness.md)；下文原型成绩与其他未交付能力保持原交付口径。

## 交付结论

当前可以交付为“具备安全执行、工单操作恢复和评测能力的 Harness 工程原型”。不能交付为成熟企业产品，也不建议承接真实客户与真实外部副作用的生产流量。

原型计划的 M0–M4 已完成，但后续评审已发现未覆盖的异常状态问题；原型范围验收通过不等于生产准入。当前适合代码评审、教学、受控演示、机制验证和后续生产化建设。M5 尚未实施，预生产演练尚未完成。

## 目标、边界与来源

SafeHarness 为工具调用型 Agent 提供控制平面：决定哪些动作可以执行、有副作用的动作如何批准、如何核对执行结果，以及怎样留下可检查的证据。模型负责提出动作，Harness 管理权限、执行与任务判定。

立项时以本地 Pico 为设计参照；根据当时源码与许可调研，SafeHarness 在独立目录实现，不将 Pico 的全部能力计为本项目功能。当前实现支持受限 Coding 文本工作区与模拟 IT 工单两个环境。实际入口是 CLI 与 FastAPI；没有完整管理控制台。

## 已完成阶段

| 阶段 | 已交付内容 | 当前边界 |
| --- | --- | --- |
| M0 | Pico 现状及组件调研、架构路线、Python 工程、最小可运行任务 | 独立工程原型起点 |
| M1 | 任务状态机、SQLite 持久化、工单读写范围、审批与摘要、执行意图、业务回执、取消和崩溃恢复 | 恢复保证依赖支持幂等回执的模拟工单适配器 |
| M2 | 统一 Gateway、Docker worker、文件工具、pytest、独立 Coding 验证、限额和清理 | 小型文本快照；无任意宿主仓库挂载和完整 Coding 崩溃续跑 |
| M3 | DeepSeek Provider、持久预算、Trace/JSONL、版本化评测、OpenJudge | 真实模型只完成小规模开发验证 |
| M4 | FastAPI 身份/角色/归属、PostgreSQL 及并发、A.I.G 实扫与回归、120 实例、CI 配置、部署和演示文档、包验证 | 本机验证；远程 CI、持续运行和生产发布未验证 |

## 已交付架构和组件

控制平面持有身份、任务、审批、策略和凭据。模型通过 Model Adapter 提出结构化请求。Runtime 驱动有界循环，Tool Gateway 执行参数和范围检查。允许的 Coding 操作进入隔离 worker，工单更新先保存审批请求。批准后的业务操作使用稳定操作 ID 和业务回执核对。独立 verifier 判定任务结果，状态和关键事件保存至数据库。

| 组件 | 职责 | 主要实现 |
| --- | --- | --- |
| Task / Runtime | 状态转换、步数边界、模型/工具循环和验证阶段 | task.py、runtime.py |
| Gateway / ToolSpec | 可信工具注册、严格参数、资源范围、审批判定和调用审计 | execution.py、audit.py |
| Approval / Execution | 参数摘要绑定审批、不可重复决定、意图、回执、恢复与取消 | approval.py、task_store.py、ticket_store.py、pg_ticket_store.py |
| Sandbox / Workspace | 私有文本快照、Docker 派发、资源/输出/时限控制和回收 | sandbox.py、coding.py、process.py、sandbox_assets |
| Persistence / Trace | SQLite/PG、迁移、并发版本/行锁、脱敏事件和导出 | database.py、migrations、trace.py |
| Provider / Budget | 脚本与 DeepSeek 接入，协议和重试边界、持久费用预留 | provider.py、budget.py |
| Evaluation / Verifier | 用例重置、独立合同、统计与报告、补充模型评分 | dataset.py、evaluation.py、coding_demo.py、judging.py |
| Service / Scanning | HTTP 任务/审批/Trace、角色和归属、低权限扫描与模型代理 | service.py、scanning_service.py |

自主工程核心是执行控制、审批/回执协议、恢复、并发控制、独立验证和证据关联。Docker、PostgreSQL、FastAPI、DeepSeek、OpenJudge、A.I.G 为外部依赖。当前没有原创算法或新分布式理论的实证结论。

## 典型工作流程

### 模拟工单

1. requester 创建任务并运行，Gateway 检查工具参数和写范围。
2. 保存待审批请求，任务进入 WAITING_APPROVAL，业务状态不变。
3. 独立 approver 审阅参数与摘要并决定；禁止自批、重复决定和伪造主体字段。
4. owner 续跑；执行前再次检查批准摘要、工具/策略、有效期与当前写范围。
5. 持久化执行意图，业务更新与回执在业务事务中共同提交，再完成控制状态。
6. 重试使用相同操作 ID 核对回执；已提交时不再写入，证据无法确认时进入 NEEDS_REVIEW。

用户已复跑 task `36d899b0-83ae-40f9-95fe-fdae502a0a47`：WAITING_APPROVAL → SUCCEEDED，业务 open → resolved，write_count 0 → 1；脚本重复 resume 后仍只写一次。本次自动演示由脚本分别使用 requester 与 reviewer token，并非人工在界面审批。

### Coding

读取私有工作区 → pytest 检出初始 bug → Gateway 授权写入修复 → pytest 通过 → 独立合同验证 → SUCCEEDED。

用户已复跑 task `3db00eca-49f8-4401-b903-b2fd7c1b4556`：Trace 保存首次 tests_failed、write_file、再次 tests_passed、_verify_coding_contract 和最终成功。工具实际运行于 Docker。这两条演示使用脚本模型和独立 SQLite 服务目录，不消耗模型费用；它们本身不验证 PostgreSQL。

### 评测与外部扫描

确定性 verifier 依据工具结果和环境观测判定合同；OpenJudge 补充工具选择、调用准确性和循环等评分。评分不能替代业务证据和独立合同。

A.I.G 使用独立 venv 和固定上游提交扫描实际本机 HTTP 目标。scanner 与 model_proxy 分权，真实 Provider 密钥留在宿主。实验在只含随机合成 canary 的负对照中故意放宽 IT-002 读取范围，确认一次发现；同一请求在加固配置中重放，未泄露 canary，业务写入为 0。确认请求另固化为离线回归。

## 安全与可靠性保证的范围

- 工具参数、未知字段、读写范围及审批在派发前检查，关键意图先落库。
- 审批绑定规范化参数摘要、工具/策略版本；首次新派发有效期为 24 小时。既有回执可在之后继续核对。
- 工单恢复已验证 SQLite 与真实 PG 的提交后进程退出、并发审批/续跑、取消竞争及证据缺失。
- Coding 使用非 root、只读根、无网络、无宿主挂载/凭据/Docker socket 的一次性容器，并限制 CPU、内存、PID、磁盘、时间和输出。
- 当前工作区最多 32 个文件、单文件 8 KiB、总内容 32 KiB。API 默认 Coding fixture 为 clamp，不是通用大型仓库开发平台。
- 工单幂等性不等于对任意外部服务的 exactly-once；Docker 共享内核，也不等于 VM 安全边界。

## 本机验证结果

| 验证 | 结果 | 口径 |
| --- | --- | --- |
| 已保存全量回归 | 163 passed，0 failures/errors/skipped | artifacts/m4-regression.xml |
| 后续受影响专项 | 27 passed | 包含新增 1 个目标工单绑定反例；与全量有重叠，不相加 |
| Coding 验收 | 30/30 | 10 函数 × 3 bug，脚本执行、真实 Docker |
| IT 验收 | 30/30 | 10 工作流 × 3 初始状态 |
| 攻击验收 | 40/40 合同通过 | 5 类边界，未观察到越权影响 |
| 故障验收 | 20/20 合同通过 | 10 个可恢复、10 个预期安全停止 |
| 合法请求误拒绝 | 0/117 | 合法模型 Gateway 请求，排除预期拒绝和控制面验证器 |
| 可恢复实例 | 10/10 | 安全停止不进入恢复成功分母 |
| NEEDS_REVIEW | 2 | 属于正确安全停止，不是恢复成功 |
| 重复业务写入 | 0 | 受控测试操作及回执边界 |
| PG 网络并发 | 通过 | 两个 HTTP 进程同时批准只接受一次；四个并发续跑只写一次 |
| 独立安装包验证 | 通过 | wheel 内资产、CLI、service-init、真实 sandbox-smoke |

M4 固定数据为 m4-development-v1，120 实例均为合成脚本开发验收，不是 120 个真实模型任务。M3 小批量真实 DeepSeek 的正常合同为 5/5，5 个攻击任务未观察到目标达成，其中实际提出目标攻击操作的只有 2 个；其余由模型拒绝。6 个控制面合同由可信测试驱动完成，不归功于模型自主恢复。

0/40 攻击的 Wilson 95% 区间约为 [0, 8.76%]，10/10 恢复为 [72.25%, 100%]。没有保留集、Pass³、生产 SLO 或持续负载成绩。新增审查发现的问题说明：现有测试全部通过也不代表覆盖所有异常与生产故障。

包括 M3 和 M4 扫描在内，费用账本最后验证为累计 61 次请求、费用上界 0.276424 CNY，1 次未知用量继续保留预留预算，累计限额为 0.50 CNY/80 次。此为保守估算及预留口径，不是服务商账单。

## 已交付资产

| 类别 | 位置与状态 |
| --- | --- |
| 源码和依赖 | src/safeharness、pyproject.toml、uv.lock |
| 数据库 | SQLite/PG 支持，Alembic head 0009_service |
| 迁移验证 | 旧库只读备份从 0003/0006 升级，分别保留 3/1 个任务；原库未被修改或覆盖 |
| worker | 固定基础镜像摘要、依赖哈希、源摘要预检和实际镜像 ID 核对 |
| 评测与脚本 | tests、scripts/m4_acceptance.py、m4_demo.py、m4_scan.py、install_aig.py、verify_migrations.py、check_secrets.py |
| 对外摘要 | docs/evidence/m4-summary.json |
| 设计与操作文档 | roadmap.md、service.md、threat-model.md、scanning.md、m4-results.md |
| 发布包 | artifacts/m4-dist/*.whl 与 *.tar.gz，已进行独立环境安装 smoke |
| CI 和部署配置 | .github/workflows/ci.yml、deploy/compose.postgres.yml；远程 CI 未运行，Compose 仅验证配置，本机使用 bootstrap PG |
| 许可与来源 | THIRD_PARTY_NOTICES.md，固定 A.I.G 提交及 124 文件来源校验清单 |

当前目录仍未初始化 Git。现有发布包和结果以文件/源码/数据摘要追踪，没有正式 Git tag、远程流水线或生产发布记录。配置、密钥与原始私有扫描日志应留在忽略目录，分享时只选择公开证据。

## 尚未完成和未修复的内容

| 项目 | 当前事实 | 影响 |
| --- | --- | --- |
| 未预期异常的收敛 | M4 交付后确认的问题已由 R1 修复，见 runtime-correctness.md；强杀恢复与失联接管仍未实现 | 普通异常已修，通用恢复仍为生产阻断 |
| 通用持久执行 | /run 同步，缺持久调度、worker 租约/接管，Coding 和完整模型上下文恢复未实现 | 生产阻断 |
| 端到端幂等与外部协议 | 同操作 ID 有保障；重复提交创建新任务、无回执的真实外部服务未覆盖 | 不能承诺任意业务副作用可靠性 |
| 正式身份与租户隔离 | 本地静态 token、角色与归属；缺 token 生命周期及租户全链路隔离 | 未达到正式多租户服务要求 |
| 任务和产物产品接口 | 固定场景；HTTP Coding 缺完整持久补丁/验证报告；NEEDS_REVIEW 无处置工作台 | 尚未形成完整使用与运营闭环 |
| 长期运营与上线 | 缺运行指标/告警、长稳压测、备份恢复、升级与回滚演练 | 缺生产准入证据 |
| 外部有效性 | 无独立保留集和广泛真实模型/外部基准验证 | 不能从开发集成绩推导泛化 |
| 可选优化 | M5 反思、上下文优化与消融尚未实施 | 不计入本次交付 |

本报告对应的 M4 时点仅复现了审查问题；后续 R1 已交付普通异常收敛修复。整体仍属于“可演示、可回归、具备自主核心的原型阶段交付”，成熟产品验收与生产准入仍不通过；其他底座与预生产能力继续待补。

证据入口：[M4 实测](m4-results.md)、[机器摘要](evidence/m4-summary.json)、[服务指南](service.md)、[威胁模型](threat-model.md)、[上线准备评审](production-readiness-review.md)。
