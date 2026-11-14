# S4 过期扫描与有证据的自动恢复

实施日期：2026-11-14。接续 [S3 租约心跳](scheduler-s3.md)。本阶段增加持久化恢复决策、独立扫描进程和备用 worker 续跑。S4 当时仅支持 scripted + ticket_read；后续 [S5–S7](scheduler-s5-s7.md) 增加审批工单的有证据续跑，当前迁移头为 0017。本文验收数字保留为 S4 当时的记录。

`recovery_demo.py` 后续默认包含 safe、unknown、stale 三个场景；只复现原 S4 两项时使用 `--scenario safe --scenario unknown`。

## 恢复流程

扫描器查找 CLAIMED 且租约已过期的记录。PostgreSQL 先用 SKIP LOCKED 领取任务行锁，再锁住队列行，重新读取数据库时间和最新租约；如果一笔在途续期刚刚提交，扫描器会跳过，不以旧快照作决定。SQLite 测试使用写事务串行化。扫描器只做有界数据库检查，不在事务内调用模型或业务工具。

| 证据 | 决策 |
| --- | --- |
| 尚未启动，没有会话或调用 | READY，下一 worker 从头启动 |
| 已进入 RUNNING，但尚未创建会话，且没有调用 | READY，初始化原任务的运行会话 |
| 完整安全检查点，调用记录与快照一致，没有未确认尝试 | READY，固定该检查点并安排续跑 |
| 停在 before_tool、存在 INTENT/UNKNOWN、缺失或损坏检查点、记录不匹配 | NEEDS_REVIEW，不重新派发 |
| 无未确认调用，但原任务期限已耗尽 | FAILED，不重新启动预算 |
| 自动恢复次数用尽 | NEEDS_REVIEW，停止自动安排 |
| 已有终态但队列未收尾 | 保持原结果，只核对队列状态 |

WAITING_APPROVAL 不由本阶段自动处理，不批准、不拒绝，也不绕过审批。审批唤醒与排队写入工作流留给 S6。

即使工具调用可能只是读取，本阶段也不会重放 before_tool 边界。工具结果已经落库、但还没进入完成检查点的情况先交人工核对；没有实现根据任意工具回执补写快照。这是有意限制的恢复范围。

## READY 与执行代次

READY 表示“已经判定可以继续，等待新的执行者”，不是任务成功。扫描器保存恢复模式、原因、次数、固定的检查点 ID 和源会话代次；旧领取者此时就不能继续写入。备用 worker 领取后生成新凭证并增加 generation，例如 1 → 2。

检查点会话使用同一套新凭证与代次。若备用 worker 在领取后、接手检查点前再次退出，后续代次可以继续从最初固定的快照恢复，不强行假设上一队列代次就是会话代次。任务 ID、历史调用 ID 和结果保持关联，正常续跑不重做已经完成的工具。

新代次的心跳计数重新从 0 开始。备用 worker 成功发送心跳后计数增加；扫描器只做核对处置时不启动心跳，计数保持 0，不继承旧 worker 的次数。

新版本首次领取会记录当前服务的 120 秒总执行期限；初始化和恢复都保留该期限，运行会话也不会重新获得完整时间。步骤数沿用快照。默认最多自动重新安排 3 次，恢复计数持久化；达到门槛后的人工核对也记为一次处置，因此 recovery_count 可以显示为 4，但不会继续自动重试。CLI 上限配置限定为 1–10。

恢复决定、队列状态、任务状态/版本和审计同事务提交。并行扫描、重复扫描和进程重启不会对同一过期代次重复增加处置次数。恢复期间取消任务，会取消 READY 记录，不会再次启动。

## 亲手运行

从 `C:\Users\25587\Desktop\agent\safeharness` 执行：

```powershell
.\.venv\Scripts\python.exe scripts/recovery_demo.py
```

演示使用独立 SQLite 数据库和真实进程，依次展示：

1. **安全续跑。** worker 在工具结果已保存到检查点后被终止；租约过期后，独立扫描器和备用 worker 自动处理。最终为 SUCCEEDED/FINISHED，generation 为 2，原工具调用 ID 不变，调用数量仍为 1。
2. **不确定调用。** worker 在工具调用已经登记、结果尚未保存时被终止。扫描器将任务置为 NEEDS_REVIEW，原因是 unconfirmed_call；备用 worker 不重新调用工具，也不补造成功结果。

输出包括 before_worker_exit、lease_expired 和 recovery_decided。末尾 passed: true 表示两个场景都满足预期，不表示人工核对场景也成功执行。报告位于 `artifacts/recovery-demos/<id>/report.json`，私有配置与日志位于各场景的 service-private。所有演示进程仅按本次登记的 PID 清理，不调用付费 API。

## 实际部署命令

以现有 PostgreSQL 本地配置为例，扫描器和 worker 必须使用同一个数据库。先启动备用 worker，再在另一个窗口启动扫描器：

```powershell
.\.venv\Scripts\python.exe -m safeharness worker --backend postgres --config-dir artifacts/m4-private
```

```powershell
.\.venv\Scripts\python.exe -m safeharness recover-queue --backend postgres --config-dir artifacts/m4-private --watch
```

recover-queue 不加 --watch 时只扫描一批，默认最多 100 个候选；--interval-seconds 默认 1，--batch-size 为 1–100，--max-recoveries 默认为 3。扫描器是受信宿主控制进程，不是模型可以调用的工具。它安排 READY 不会授予额外资源权限，实际 worker 仍按当前加载的主体配置核对角色、范围和检查点配置；配置改变或固定快照被更换会停止并转人工核对。

## 迁移与范围

迁移头为 0016_queue_recovery，新增恢复计数、决策、固定快照与原执行期限。已有会话保留实际期限；旧版本遗留且没有会话/期限的领取记录不会被补造新预算，会进入人工核对。升级共享部署前先退出旧版 API/worker/扫描器，再迁移并启动新版。

S3 的演示仍可以在不启动扫描器时观察“过期但尚未接管”；S1/S2 手动与正常执行流程保持原入口。当前固定工具白名单和通用验收合同的边界没有扩大，有限开发集测试也不是生产认证。

新增测试覆盖安全中断窗口、未确认调用、损坏/过深 JSON、恢复预算、二次中断、配置与快照变化、取消、处置回滚、两个真实扫描进程竞争，以及 PostgreSQL 在途续期提交后的重查。实施中修正了领取竞争：锁住任务行后重新读取队列所有权，避免关联查询保留旧队列快照。

完整回归、固定开发验收、安装包和演示证据见 [S4 机器记录](evidence/scheduler-s4.json)。CI 已加入恢复演示，远程工作流尚未执行。

后续 S5 深化接管后旧执行者恢复和在途副作用的隔离；S6 接入排队审批/写操作及业务回执核对；S7 完成长时间运行、容量和运维验收。
