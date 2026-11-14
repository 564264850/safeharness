# 统一调用契约 R2

本轮落实“统一调用逻辑”：Runtime、模型请求、Gateway、人工审批及业务回执恢复使用同一套持久身份链，HTTP 提交增加幂等键。这里的统一既包含记录，也包含派发前登记、身份校验、结果不可改写与恢复关联。它为后续 Context、Checkpoint 和持久调度提供基础；这些后续模块尚未实现。

## 身份的含义

| 标识 | 含义 | 何时改变 |
| --- | --- | --- |
| `task_id` | 一项用户任务 | 创建新任务时 |
| `run_id` | 一段执行阶段 | 首次 Agent 执行、审批后业务阶段各生成一个；同一业务阶段的回执恢复复用它 |
| `call_id` | 一次逻辑模型请求、工具调用或隐藏验证调用 | 新的逻辑调用时；同一模型请求的重试、同一审批的执行与恢复保持不变 |
| `attempt_id` | 一次已登记的执行、提议或恢复尝试 | 每次尝试都生成新 ID |
| `operation_id` | 需要业务去重的操作标识 | 工单场景沿用审批 ID，恢复时绝不换成 attempt ID |
| `artifact_id` | 已保存的输出产物 | 每个尝试的每类输出生成一个，内容摘要固定 |

`run_id` 代表阶段，不代表 Python 进程或 HTTP 请求；本轮没有引入 worker lease 或 fencing。`logical_calls.run_id` 指向逻辑调用首次创建的阶段，`call_attempts.run_id` 指向实际尝试所在阶段，因此审批后两者可以不同，但必须属于同一 task。

例如，一次更新工单先进入人工审批，业务写入提交后进程退出，然后恢复：

```text
task T
  run R1（Agent，WAITING_APPROVAL）
    call C（update_ticket_status，operation_id = approval_id）
      attempt A1（proposal，WAITING_APPROVAL）
  run R2（business，恢复后 SUCCEEDED）
    call C
      attempt A2（已登记，进程退出；后来由回执核对为 RECONCILED）
      attempt A3（恢复，SUCCEEDED，保存业务回执摘要）
```

上例中有 1 个 task、2 个 run、1 个 call、3 个 attempt，业务只写入 1 次。`RECONCILED` 表示该未完成尝试的逻辑结果已有业务回执证实，并不证明具体哪个进程完成了写入。并发领取但复用已完成结果的尝试可以标为 `REUSED`；终态任务再次 `resume` 不创建额外尝试。

模型响应中的 `tool_call_id` 只用于满足模型对话协议，不能指定宿主的 call、attempt 或业务幂等键。宿主忽略回调返回的伪造身份，绑定自己在派发前生成的身份。

## 实施路径与代码

| 步骤 | 落实内容 | 主要入口 |
| --- | --- | --- |
| R2.1 身份和数据库 | 运行、逻辑调用、尝试、产物、审批关联、提交键六张表；复合外键、状态及唯一约束 | `contracts.py`、`migrations/versions/0010_invocations.py` |
| R2.2 提交幂等 | owner + key 摘要 + 规范请求摘要；并发提交不产生孤儿任务 | `TaskStore.submit`、`POST /v1/tasks` |
| R2.3 工具调用 | Runtime 派发前登记，Gateway 复用身份并绑定策略，结果与审计关联 | `InvocationLedger.invoke_tool`、`AuditLog.begin/finish` |
| R2.4 重试和恢复 | 模型一次逻辑请求对应多个尝试；审批提议、业务执行与恢复共享 call；输出产物保存与校验 | `DeepSeekClient.complete`、`TaskStore.pause_for_approval/resume` |
| R2.5 验收与可读性 | HTTP/CLI 查询、回归、真实进程退出演示、旧库备份迁移 | `show-invocations`、`scripts/invocation_demo.py` |

`invocations.py` 负责身份、登记和结算；实际工具仍由现有 Gateway 和业务适配器执行。审批与业务执行沿用原有权限校验、参数摘要、策略版本、审批有效期和业务回执核对。

不配置 TaskStore 的纯内存 `run` / `run_task` 仍用于单测和最小循环示例，不提供持久台账；HTTP、CLI 演示及评测的产品执行路径均使用 TaskStore。

同一个 attempt 只能登记一次 Gateway 派发；状态检查和 `gateway_events(attempt_id, phase)` 唯一约束共同保证。已结算的状态、错误码或产物内容不能被替换。普通工具结果不明时进入人工核对，本轮没有给写文件、命令执行或任意外部 API 添加自动重试。

模型重试继续遵守原重试次数与持久预算：每次实际 API 尝试使用独立 attempt ID 预留费用，超时/连接错误保留未知用量，不当作免费。Provider 调用通过同一个 call 关联；脱离 Agent 生命周期的评测/模型调用使用独立 provider run，自己关闭，不修改任务终态。

## HTTP 提交幂等

```http
POST /v1/tasks
Authorization: Bearer <本地配置中的 token>
Idempotency-Key: internship-demo-001
Content-Type: application/json

{"scenario":"ticket_read","provider":"scripted"}
```

| 请求情况 | 返回 |
| --- | --- |
| 当前 owner 首次使用该 key | 201，`Idempotency-Replayed: false` |
| 同 owner、同 key、同一规范请求 | 200，`Idempotency-Replayed: true`，返回原 task 的当前状态 |
| 同 owner、同 key、不同请求 | 409，不创建新任务 |
| 不同 owner 使用同 key | 各自独立，沿用原有权限与归属检查 |
| 未传 key | 每次提交创建新任务 |
| 无身份、无角色、key 格式非法 | 401 / 403 / 422，在创建任务之前拒绝 |

key 接受 1–128 位 ASCII 字母、数字、`.`、`_`、`:`、`-`。服务先按 TaskInput 填入默认值，再计算请求摘要；省略默认 `provider` 与显式 `scripted` 视为同一请求。幂等表保存 key 的 SHA-256，不保存原 key，不设置自动过期。SQLite 使用写事务，PostgreSQL 使用事务级 advisory lock 和唯一约束；提交记录、任务、owner 与创建 Trace 在同一事务提交。

提交只创建任务，不运行。`POST /v1/tasks/{id}/run` 保持原有状态与 revision 抢占规则；任务已执行后，重复 run 不会重开执行。新 key 表示新的用户意图，不能替用户判断两项不同任务是否业务等价。

## 查询与亲手演示

在项目目录执行，整个演示使用脚本模型，不需要 DeepSeek 或 Docker：

```powershell
.\.venv\Scripts\python.exe scripts\invocation_demo.py
```

它依次提交两次同一请求、验证参数冲突、申请审批、批准、在业务提交后使子进程退出（exit 77），然后由重新创建的 HTTP 服务核对回执。只使用本次新建的 `artifacts/invocation-demos/<随机ID>` 目录。

拿输出中的 `task_id` 与报告同目录的 `control.sqlite3`：

```powershell
.\.venv\Scripts\python.exe -m safeharness --db artifacts\invocation-demos\<目录ID>\control.sqlite3 show-invocations <task_id>
```

输出为 `runs`、`calls`、`attempts` 和 `artifacts` 四组元数据。HTTP 等价入口为 `GET /v1/tasks/{task_id}/invocations`，执行与 Trace 相同的身份、角色与任务归属检查。未授权的其他 owner 不能枚举这些数据。

产物正文保存在控制数据库的私有表中。公开查询只给出种类、媒体类型、SHA-256、字节数和关联 ID，不返回正文。每份产物最多 256 KiB UTF-8，超过上限拒绝结算，保留结果不明语义，不静默截断。数据库操作员依然拥有读取正文的能力；本轮未提供对象存储、加密、留存清理或内容下载接口。

Trace / Gateway 新事件版本为 2，记录真实 run/call/attempt 身份。旧记录迁移后相关列保留 NULL；不会补造历史执行。旧审批在实际恢复时才建立 `approval_import` 关联，不虚构过去的模型或工具尝试。老数据库先显式 `migrate` 或由写入入口初始化后再使用新查询。

## 测试和边界

本轮新增测试覆盖提交并发与 owner 隔离、HTTP 重放及参数冲突、Runtime/Gateway/Trace 身份一致、伪造返回 ID、重复派发拦截、审批取消/拒绝、模型重试、输出不可修改、产物上限、跨任务关联拒绝及故障后的回执恢复。PostgreSQL 包含两个独立 HTTP 进程同时提交同 key，以及真实子进程退出后的恢复。

```powershell
$env:SAFEHARNESS_POSTGRES_TESTS = '1'
$env:SAFEHARNESS_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q -m "not acceptance"
.\.venv\Scripts\python.exe scripts\m4_acceptance.py
```

最新执行证据见 `docs/evidence/invocation-contracts-r2.json`。历史 M4 与 R1 报告保持原样。

2026-11-11 本地验收：完整回归 **239/239** 通过（较 R1 新增 23 项，0 跳过），固定合成开发验收 **120/120** 通过；本轮新增付费 API 调用为 0。新 wheel 在独立虚拟环境中通过 HTTP 幂等提交、真实进程退出和回执恢复演示，48 个 Python 源文件与 wheel 一致。Ruff、锁文件检查、公开证据凭据扫描通过。两个原始旧数据库仅迁移了只读备份，4 条任务快照保持一致。

这一步完成了统一调用契约与提交幂等，并未完成通用 Checkpoint、上下文重建、持久队列、worker lease/fencing、服务重启后的自动扫尾或任意工具的 exactly-once。当前只有模拟工单适配器具备原子业务回执与去重能力；接入真实外部系统时仍须验证其幂等键、查询回执与补偿能力。
