# S1 持久接单

实施日期：2026-11-13。对应 [调度测试计划](scheduler-test-plan.md) 的第一阶段。本阶段建立可持久保存的待办清单；独立 worker、心跳、租约和自动接管属于 S2–S7，尚未实现。

后续更新：[S2](scheduler-s2.md) 已增加独立 worker 和新迁移头。本页保留 S1 当时的范围；S1 演示仍以不启动 worker 验证持久接单。

## 新行为

创建任务时可显式指定：

```json
{"scenario":"ticket_read","execution_mode":"queued"}
```

成功返回的任务仍为 `task_status: CREATED`，另外包含 `scheduling`：

```json
{"mode":"queued","schema_version":1,"state":"QUEUED","created_at":"..."}
```

CREATED 表示任务尚未执行；QUEUED 表示已经保存到数据库的待办清单。它不是执行成功，也不表示后台 worker 已经启动。本阶段仅支持 scripted + ticket_read，拒绝排队的付费模型、Coding 和写入任务，避免调度执行协议尚未完成时提前派发。

未指定 execution_mode 或指定 manual，继续使用已有的手动运行方式。排队任务调用旧 `/run`、`/continue` 会返回 409 `queued_task_requires_worker`；底层状态转换也阻止跳过领取直接运行。通过原取消接口可同时把任务和队列记录置为 CANCELLED，重复提交不会把已取消任务重新排队。

## 持久化与兼容

- 新增 Alembic `0013_durable_queue`，建立 task_queue，按 task_id 唯一并关联已有 service_tasks。
- 任务、归属、入队、Trace、提交幂等键在同一事务提交。中间失败或提交前进程退出，全部回滚。
- 同一 owner、同一 Idempotency-Key、同一请求重试，返回原 task_id 和当前状态；改动 execution_mode 会被视为请求冲突；不同 owner 的键分别隔离。
- 默认/manual 字段不进入保存的请求摘要，保持历史 HTTP 请求的幂等兼容。
- 迁移只建新表，不给旧任务补队列，不会自动启动旧任务。
- 取消任务与取消队列同事务；HTTP 快照通过一次 JOIN 读取任务和队列，避免取消竞争时显示混合状态。
- Trace 新增 scheduling.enqueued / scheduling.cancelled，只保存白名单元数据。

## 亲手做两步实验

在 `C:\Users\25587\Desktop\agent\safeharness` 执行：

```powershell
.\.venv\Scripts\python.exe scripts/durable_submission_demo.py --phase prepare
```

该命令为本次演示建立独立配置与 SQLite 数据库，启动真实 HTTP API，提交排队任务并退出 API。输出中的 api_stopped 应为 true，同时保存 demo_id 和 task_id。输出的 next 是第二条命令，将其中 ID 原样使用：

```powershell
.\.venv\Scripts\python.exe scripts/durable_submission_demo.py --phase check --demo-id <上一步的demo_id>
```

它启动新的 API 进程、读取原任务、用相同幂等键重试提交，并检查仍只有一次入队事件、业务写入为 0。预期 passed/restart_verified 为 true，task_id 不变，任务仍是 CREATED/QUEUED。两步都不调用付费 API，并在结束时停止本次演示服务。

也可一条命令完成全部步骤：

```powershell
.\.venv\Scripts\python.exe scripts/durable_submission_demo.py
```

报告位于 `artifacts/scheduler-demos/<demo_id>/report.json`。同目录的 service-private 存放演示凭据与日志，不应公开。保留数据库用于检查，后续生产 worker 不会自动连接该演示库。

## 自动验证

```powershell
.\.venv\Scripts\python.exe -m safeharness postgres-up
$env:SAFEHARNESS_POSTGRES_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -v tests/test_durable_submission.py
```

新增测试覆盖 SQLite 与专用 PostgreSQL schema：持久化与幂等、模式冲突、同 owner 两独立进程同时提交、提交前后真实进程退出、队列/幂等/Trace 插入失败回滚、取消回滚和旧入口拒绝。HTTP 测试覆盖响应构造失败后的重试、身份/归属和不支持的请求。旧 0012 数据库迁移测试验证历史摘要保持兼容，旧任务没有被排队。

真实强杀测试在完成 queue INSERT 但事务尚未提交的位置调用 os._exit；提交后退出的测试以相同键核对持久结果。演示则使用两个真实 HTTP 服务进程证明 API 重启后的行为。它们尚未覆盖 worker 崩溃接管；这一点由后续阶段实现和验证。

最终回归、固定开发验收、演示与源码指纹保存在 [S1 机器记录](evidence/scheduler-s1.json)。CI 增加了重启演示，远程工作流尚未执行。

下一步 S2：让独立 worker 通过短事务领取队列，再调用已有 Harness；首先验证两个执行进程竞争时，只有一个有效领取者。
