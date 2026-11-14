# S2 独立 worker 与原子领取

实施日期：2026-11-13。接续 [S1 持久接单](scheduler-s1.md)。独立进程现在可以领取排队任务，复用现有 TaskService → Harness → Runtime → Gateway → verifier 执行，不依赖 API 进程存活。当前仍只支持 scripted + ticket_read；租约、心跳、失联扫描和自动接管尚待 S3/S4。

后续更新：[S3](scheduler-s3.md) 已增加租约、心跳和失效保护；本页保留 S2 当时的范围。S2 演示在新版中继续可运行。

## 领取与完成

PostgreSQL 使用短事务中的 `FOR UPDATE OF t SKIP LOCKED` 锁定候选任务，登记队列 CLAIMED、worker ID、私有领取凭证及 generation，然后提交。模型/工具运行不在领取事务内。SQLite 开发演示使用 BEGIN IMMEDIATE 串行领取，不据此宣称 PostgreSQL 多进程并发已验证；后者另有真实数据库和进程测试。

worker 在已登记的所有权上下文内调用原服务执行方法。任务状态更新、工具派发和检查点操作校验领取资格；初始检查点沿用领取凭证与 generation，避免建立两套独立的执行所有权。私有凭证不通过 HTTP 快照或 worker 输出公开。API 手动 `/run`、`/continue` 仍不能执行 queued 任务。

任务完成时，任务终态、队列 FINISHED 及审计在同一事务提交。取消既可以发生在入队后，也可以发生在领取后；取消后不能启动新工具。当前任务是只读操作，没有承诺撤销已发出的外部请求。手动任务和已取消任务不会被 worker 领取。

队列状态与任务状态含义不同：

| task_status | scheduling.state | 含义 |
| --- | --- | --- |
| CREATED | QUEUED | 已接单，等待领取 |
| CREATED | CLAIMED | 执行权已提交，Harness 尚未启动的短暂窗口 |
| RUNNING / VERIFYING | CLAIMED | 正在运行或验收 |
| SUCCEEDED | FINISHED | 已完成并通过宿主验收 |
| FAILED / NEEDS_REVIEW | FINISHED | 执行已结束，结果失败或需要核对 |
| CANCELLED | CANCELLED | 已取消 |

FINISHED 只表示此次调度结束，不能单独当作任务成功。worker 若收到非终态结果，会报错并保留领取记录供检查，不报告为已处理，也不盲目重排。

## 亲手做两步演示

从项目目录运行：

```powershell
$s2 = .\.venv\Scripts\python.exe scripts/queue_worker_demo.py --phase prepare | ConvertFrom-Json
$s2 | Select-Object demo_id, task_ids, task_status, queue_state, api_stopped
```

它创建独立 SQLite 演示库，通过真实 HTTP 提交一个 queued 任务，同时准备手动任务和已取消任务作为对照，然后停止 API。此时应为 CREATED/QUEUED。

```powershell
.\.venv\Scripts\python.exe scripts/queue_worker_demo.py --phase work --demo-id $s2.demo_id
```

第二步启动两个真实 CLI worker，API 在执行期间保持关闭。worker 完成后，再启动新的 API 进程，核对任务为 SUCCEEDED/FINISHED、每个任务一次领取与一次 read_ticket 调用、手动任务未动、取消任务未执行、业务写入仍为 0。报告保存在 `artifacts/queue-worker-demos/<id>/report.json`，同目录 service-private 中的凭据与日志不公开。

一条命令执行 8 个任务的批量演示：

```powershell
.\.venv\Scripts\python.exe scripts/queue_worker_demo.py --jobs 8
```

演示不调用付费模型，结束后回收本次 API/worker 进程，保留任务库和公开报告。work 针对新准备的队列执行一次，不作为可重复恢复/重放入口。

## 实际 worker 命令

下面以现有本地 PostgreSQL 服务配置为例，worker 与 API 必须使用相同的 backend 和 config-dir。

```powershell
.\.venv\Scripts\python.exe -m safeharness worker --backend postgres --config-dir artifacts/m4-private --once
```

- `--once`：尝试领取一个任务，输出 processed 或 idle，然后退出。
- `--drain`：持续领取，直到本进程当前看不到可领取任务后退出；其他 worker 可能仍在工作，它不代表全局所有任务已完成。
- 不指定二者：连续轮询，默认空闲等待 1 秒；`--worker-id` 可指定标识。

worker 使用启动时载入的主体配置重查任务 owner、requester 角色和读取范围；这是本地配置下的检查，不是实时身份平台撤权。不存在的 owner、角色被移除或范围不符，不能借排队执行越权操作。

## 验证与边界

新增 `tests/test_queue_worker.py` 覆盖两个真实 worker 进程竞争同一任务、批量排空、手动/取消任务跳过、当前 owner 和权限核对、伪造领取资格拒绝、任务/队列/事件事务回滚，以及 S1 迁移保留旧数据。PostgreSQL 专用用例锁住最早任务，验证另一个 worker 可跳过该锁处理后续任务；另一个检查确认领取提交后不再持有任务行锁。

真实强杀用例还验证了本阶段的明确限制：进程在领取后退出，任务保留 CLAIMED，其他 worker 不会重新领取它。此时任务可能还在 CREATED，也可能已进入 RUNNING。S2 没有租约，也无法确定进程只是变慢还是已经失联，不能手工改回 QUEUED 作为通用恢复方案。

迁移头为 `0014_queue_claims`，在原队列上增加领取信息和状态约束；不会创建新的旧任务队列项。完整回归、固定开发验收、真实演示和安装包记录见 [S2 机器证据](evidence/scheduler-s2.json)。CI 已加入双 worker 演示，远程工作流尚未执行。

下一步 S3 增加独立心跳和有期限的租约，先验证长工具调用不会被误判失联，再在 S4 引入受证据约束的自动接管。
