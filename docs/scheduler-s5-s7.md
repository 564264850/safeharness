# S5–S7：旧执行者隔离、审批队列与运行验收

实施日期：2026-11-14。实现和验收只针对显式 `queued` 的脚本工单场景。原来的手动模式、DeepSeek 调用和 Docker Coding 入口继续保留。

## 这次补上的闭环

| 阶段 | 新能力 | 主要文件 |
| --- | --- | --- |
| S5 | 备用 worker 接管完成后，旧 worker 恢复运行也不能续心跳、派发工具或覆盖结果；返回 `lease_lost / ownership_changed` | `worker.py`、`scheduling.py`、`task_store.py` |
| S6 | 等待审批时队列进入 WAITING 并释放 worker；批准与 READY 唤醒同事务；新 worker 自动恢复上下文、核对业务回执，再执行后续读取和验证 | `0017_queued_approvals.py`、`checkpoints.py`、`recovery.py` |
| S7 | 数据库就绪检查、审计员运行指标、持久队列容量控制、真实数据库故障演示、混合任务持续运行脚本 | `operations.py`、`service.py`、`scripts/scheduler_soak.py` |

执行路径：

```mermaid
flowchart LR
    A[HTTP 接单并持久入队] --> B[worker 领取及续租]
    B --> C[Harness 提出写操作]
    C --> D[保存检查点与审批申请]
    D --> E[WAITING：释放执行槽]
    E --> F[独立 reviewer 批准]
    F --> G[同事务：审批决定与 READY]
    G --> H[新 worker 领取新一代]
    H --> I[恢复检查点并核对原回执]
    I --> J[后续工具与独立验证]
    J --> K[任务和队列终态提交]
```

`ticket_read`、`ticket_update`、`ticket_update_read` 支持 `provider=scripted, execution_mode=queued`。写入仍须真实的审批决定；模型无法通过参数自行批准。审批等待不消耗执行的 120 秒预算；批准时仅补偿实际等待时长，不重置已经使用的时间、步数或恢复计数。新业务派发仍受原 24 小时审批窗口限制。

拒绝使任务 FAILED，取消使任务 CANCELLED，两者不会被扫描器重新激活。批准后、领取前取消也会停止执行。批准后的检查点损坏或身份配置撤销会进入 NEEDS_REVIEW；自动恢复有次数和期限上限。

## 旧执行者与外部写入的边界

任务、调用、检查点与工具派发均核对队列 token、generation、数据库时间租约和本地心跳失败标记。接管后旧执行者得到明确拒绝。审批等待后原 worker 的收尾可在同一代执行；普通失联扫描产生的 READY 不允许旧 worker 继续收尾或续租。这两种 READY 必须分开判断，回归测试覆盖了这一差别。

已发出的外部请求不能被 generation 撤回。因此新增 `HttpTicketStore` 作为**宿主配置的回环 HTTP mock 适配器**：固定操作 ID、参数摘要、业务原子回执、写入丢响应后查询原回执。查不到或无法确认就停止核对，不直接重发 POST。真实 HTTP 测试让业务提交后关闭 TCP，确认每个逻辑操作只有一次 POST、一次业务写入。

这不等于任意第三方 API 的 exactly-once。当前适配器只允许显式回环 HTTP 地址、有界超时，业务必须提供等价的幂等回执协议；也没有解决所有远程长调用的事务设计。控制面业务恢复路径仍可能在持有任务行锁时做有界业务请求。

API 与 worker 可用相同的 `--business-config <私有JSON路径>` 选择该适配器。私有文件仅含 `base_url` 和 `token`，不能由任务或模型提供；默认继续使用本地/PG 工单业务库。HTTP mock 的接口合同见 `http_ticket.py` 和 `tests/test_http_ticket.py`，它不是通用生产业务连接器。

## 运行接口

| 接口或参数 | 行为 |
| --- | --- |
| `GET /health` | 进程存活；数据库离线时仍可能返回 200 |
| `GET /ready` | 实际执行数据库查询；可用返回 200，数据库错误返回脱敏的 503 |
| `GET /v1/operations` | 仅 auditor；任务/队列状态计数、过期领取、未确认调用、过期与待核对告警 |
| `serve --max-pending 1000` | 限制活跃队列项，含等待审批；满载时新逻辑任务返回 429 |

容量检查与接单在同一事务，PostgreSQL 使用跨进程 advisory lock；相同幂等键重放可在满载时返回原任务。计数针对这个数据库/模式的队列，不限制历史终态、手动任务或所有模型费用；直接调用可信内部 `create()` 不经过 HTTP 接单限额。多 API 进程应使用相同容量配置。

运行指标是轻量查询接口，不是 Prometheus 或告警平台。多条计数查询的在线快照可能跨越并发提交；最终任务守恒在停止接单并排空后核对，不能把在线多表总数称作原子快照。

## 本机亲手复现

在项目目录运行，全程不用模型密钥：

```powershell
.\.venv\Scripts\python.exe scripts\queued_approval_demo.py
.\.venv\Scripts\python.exe scripts\recovery_demo.py --scenario stale
```

第一条通过真实 HTTP 提交写任务，在 API 退出后由独立 worker 进入 WAITING_APPROVAL；重启 API 后由**合成测试 reviewer** 批准，再启动新 worker，自动继续到 SUCCEEDED。应看到 `manual_resume_calls: 0`、`business_writes_added: 1`。该自动审批仅属于新建的演示库，不用于真实任务。

第二条在命名检查点暂停旧 worker 的执行与心跳，等租约过期；独立扫描器和备用 worker 完成接管，再释放仍活着的旧进程，确认它返回 `ownership_changed`，调用账本和终态保持一致。它模拟的是进程失去运行资格后恢复，没有使用 Windows 系统级挂起。

若想把审批前后拆开观察：

```powershell
.\.venv\Scripts\python.exe scripts\queued_approval_demo.py --phase prepare
# 记录输出的 demo_id，再执行输出的 finish_command。
```

持续服务手动审批时，使用 [服务指南](service.md) 的认证方法，创建请求加上 `"execution_mode":"queued"`、场景用 `ticket_update_read`；另开窗口运行：

```powershell
.\.venv\Scripts\python.exe -m safeharness worker --backend postgres
.\.venv\Scripts\python.exe -m safeharness recover-queue --backend postgres --watch
```

批准后不再调用 `/resume`，worker 会自行领取；queued 任务仍禁止手动 `/run` 和 `/resume`。

## 验收方法与证据

针对性测试分别覆盖 SQLite 和真实 PostgreSQL：旧执行者恢复、批准/拒绝/取消、审批与唤醒回滚、写入提交后崩溃及回执续跑、批准后异常收敛、等待预算补偿、容量竞争和 HTTP 角色检查。HTTP 业务丢响应用真实本地 TCP 服务，不是仅替换 Python 函数的返回值。

回归开启真实 Docker/PG；最终数字、源码指纹、XML、120 个开发验收实例、关键窗口重复测试、构建包和独立安装演示的索引见机器证据。四个窗口在两个后端各重复 30 轮，共 240 个重复测试实例，这不是 240 个新的独立场景。

修复消费者后的最终回归为 **483 项通过、零跳过**，另有 **120 个固定开发验收实例通过**。持续运行的最终时长和结果单独记录，不能以回归通过代替。

最终持续运行 **3600.406 秒通过**：按 1→2→4 个 worker 分阶段，接收并完成 **1,165 个任务**，其中成功 583、审批拒绝 291、取消 291，均符合预期。批准写入、实际业务 revision 增量、业务回执和完成的逻辑操作均为 **291**，重复写入为 0。最终无过期领取、未确认调用或待人工核对告警。观测延迟 p50=1,812ms、p95=5,766ms，包含排队和合成审批；它们只是本机轻负载数据，不是生产 SLA。该次正常运行未注入中断，真实故障窗口另由前述演示验证。

```powershell
$env:SAFEHARNESS_POSTGRES_TESTS='1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_stale_worker.py tests/test_queued_approval.py tests/test_http_ticket.py tests/test_operations.py
.\.venv\Scripts\python.exe scripts\database_outage_demo.py
.\.venv\Scripts\python.exe scripts\scheduler_soak.py --duration-seconds 3600
```

数据库故障演示新建独占容器、随机回环端口与凭据，只停止/恢复这个容器。检查心跳失败本地封锁、`/ready=503` 与 `/health=200`、数据库恢复后新代续跑，以及旧执行者迟到结果拒绝。最终核对并清理专属容器，不停止共享 PostgreSQL。

持续运行使用独立 `sh_soak_*` PostgreSQL schema、真实 HTTP API、独立扫描器和 1→2→4 个 worker 进程。任务包含读取、批准后写入读取、拒绝、取消；每个接单做一次幂等重放。报告记录源码指纹、实际持续时长、终态、回执/写入/逻辑操作数、延迟和进程 working set 样本。私有凭据在 `service-private`；公开 JSON 不包含密钥。测试 schema 留存供复查，不自动清理历史报告。

本次负载固定操作 `IT-001`，批准写入的目标状态均为 `open`，每次仍产生真实的 revision 和幂等回执。不同业务写入之间的资源版本冲突、CAS 条件更新及真实业务流量需要另设合同与测试；本轮不据此给出吞吐量或生产 SLA 结论。

短于 3600 秒的运行明确标记 `short_smoke`，不能用它证明一小时稳定性。内存样本只供观察，不能证明无泄漏。本次统计及报告索引保存在 [机器可读交付证据](evidence/scheduler-s5-s7.json)。

Windows venv 的注册 PID 可能是小型启动器；主报告中的 `working_set_bytes` 只代表这些注册 PID。`scripts/monitor_scheduler_memory.py <本次运行目录>` 可以附加采样其解释器子进程树，结果写入同目录 `process-memory.json`，避免把启动器内存误当作整个 worker 的内存。附加采样从启动监控时开始，工作集相加会重复计算共享页，不作为泄漏或容量的结论。

主报告的进程退出码在验收脚本主动停止进程后读取，包含测试清理的终止码；是否运行中异常退出由循环期间的存活检查和 `error_code` 判定。

`scripts/repeat_scheduler_faults.py --repetitions 30` 单独重复四个窗口：旧 worker 恢复、业务提交后控制面崩溃、审批唤醒事务回滚、并发接单争抢一个容量槽。每轮 SQLite/PG 各一例，共 8 例；其他矩阵场景另有覆盖，不宣称全部场景都重复了 30 次。

## 还没有覆盖的生产范围

持久调度首版闭环不代表所有 Harness 任务都已进入调度器。真实 DeepSeek、多轮未知工具及 Coding 容器任务仍通过原有显式运行/恢复入口；没有把它们无条件自动重试。Docker 运行中强杀控制 worker 后的孤儿发现与跨进程接管（计划 F19）需要随 Coding 调度一并实现。

24 小时运行、备份恢复演练、服务自动拉起与优雅退出、TLS/身份与密钥轮换、历史记录保留、完整人工核对处置界面，以及真实线上流量验收仍是后续上线工程。开发合同测试和一小时本机测试均不构成企业生产认证。

常驻 worker 失去某次领取资格后，输出 `lease_lost`，结束这次执行并回到领取循环；新任务使用新的 token，旧任务仍只由扫描器按证据决定恢复。`--once` 和 `--drain` 保留退出码 1。审批后新代恰好抢先完成、旧代尚在收尾时，也执行旧代拒绝策略，不能覆盖新终态或重复写入。数据库完全不可用等启动/领取错误仍会退出，部署仍需进程托管。

持续运行曾在 2046.609 秒因一次心跳失败后消费者退出而中断。该失败报告保留，不计作一小时通过；随后修复常驻消费者的循环行为，增加“失权后处理下一个任务、旧任务由扫描器恢复”的两后端测试，并重新冻结代码开始验收。

`scripts/consumer_fencing_demo.py` 进一步用真实 PG 行锁制造心跳查询超时：旧任务在领取后、启动前的明确窗口等待；心跳失权后，原常驻 worker 继续接单，扫描器恢复旧任务。报告核对同一个 worker 进程存活、两个任务成功、旧任务 generation=2/recovery_count=1，以及每个任务只有一次工具调用。这里替换的是命名等待窗口，没有伪造心跳函数的失败。
