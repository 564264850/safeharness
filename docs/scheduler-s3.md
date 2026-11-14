# S3 租约、独立心跳与过期保护

实施日期：2026-11-13。接续 [S2](scheduler-s2.md)。本阶段增加“执行资格何时失效”的规则和独立续期机制，自动扫描、接管和恢复仍由 S4 实现。排队任务范围仍是 scripted + ticket_read。

后续更新：[S4](scheduler-s4.md) 已接入独立过期扫描和安全恢复；本页保留 S3 当时的范围，演示不启动扫描器，仍可单独观察租约失效。

## 工作方式

worker 领取任务时，数据库记录 lease_seconds、last_heartbeat_at、lease_expires_at 和 heartbeat_count。PostgreSQL 使用 clock_timestamp，SQLite 使用其数据库时间表达式；宿主提供的 claimed_at 展示时间不决定租约是否有效。有效条件为当前数据库时间严格小于到期时间，恰好到期即失效。

每个正在执行的任务拥有一个独立心跳线程和数据库连接。默认租约 30 秒、心跳间隔 5 秒，间隔要求至少 0.05 秒且不超过租约的三分之一。每次续期原子检查任务 ID、领取凭证、generation、CLAIMED 状态及尚未到期，成功后把到期时间延至数据库当前时间加该租期。过期、变更所有者、已完成和已取消均不能重新续活。

心跳连接的连接超时为 2 秒，SQL 超时为 1 秒；SQLite 写锁等待上限为 0.25 秒。心跳写入失败时，本地执行资格立即被标记失效，不在后台无限重试。结束任务时停止并有界等待心跳线程。心跳只表示进程仍能续期，不证明模型或工具正在取得有效进展。

## 失效后的行为

任务状态变更、工具派发、工具结果/调用账本、检查点保存和验证事件都核对当前执行资格。心跳失败或租约过期后，在途调用可能仍会返回，但迟到结果不能被写成成功；检查点不能被旧执行者清为正常完成。已经在有效资格下提交的终态，允许按同一凭证清理运行会话，不会因随后的租约时间流逝被改判。

worker 能查询数据库时，发现失去租约会输出 `status: lease_lost` 和安全的原因代码。S3 当时均退出码 1；后续 [S5–S7](scheduler-s5-s7.md) 将常驻消费者改为结束这次领取后继续接单，`--once/--drain` 仍退出码 1。无法查询数据库时明确报错，不能假定已保存失败状态。旧任务保留原状态和未确认调用证据；S3 阶段不自动改回 QUEUED、不派发第二份执行，也不自动声称 FAILED 或 SUCCEEDED。

失效保护不撤销已经发到外部的请求，也不能强制终止任意 Python 回调。当前排队任务只读；后续写操作必须配合业务幂等回执核对。S4/S5 仍需覆盖接管、旧执行者恢复和在途副作用的完整边界。

## 查询字段

领取后的 scheduling 增加：

| 字段 | 含义 |
| --- | --- |
| lease_seconds | 本次领取的租约时长 |
| last_heartbeat_at | 数据库记录的上次成功心跳时间，UTC Unix 秒 |
| lease_expires_at | 执行资格到期时间，UTC Unix 秒 |
| heartbeat_count | 已提交的成功续期次数 |
| lease_status | ACTIVE、EXPIRED 或 RELEASED |

ACTIVE 只表示数据库租约尚未到期，不保证 worker 当前健康。心跳写入失败后，本地 worker 可能已经停止执行，而数据库仍暂时显示 ACTIVE，直到原到期时间过去。EXPIRED 不等于已接管，队列状态此时仍为 CLAIMED；RELEASED 表示任务已结束或取消。

尚未领取的 QUEUED 记录不显示这些字段，保持 S1 接单和重启演示的兼容性。心跳更新计数及时间，不逐次追加 Trace，避免心跳造成持续事件膨胀；实际任务调用与终态仍保留原审计链。

## 亲手观察

在 `C:\Users\25587\Desktop\agent\safeharness` 执行：

```powershell
.\.venv\Scripts\python.exe scripts/lease_demo.py
```

演示使用独立的 SQLite 任务库，租约缩短为 1.5 秒、心跳 0.1 秒，依次展示三种情形：

1. **长工具调用。** 主执行线程在工具回调中阻塞 2.2 秒，超过最初租约；heartbeat_count 增长、到期时间向后延长，任务最终 SUCCEEDED/FINISHED。
2. **心跳写入失败。** 在工具尚未返回时注入心跳传输失败，本地 worker 放弃资格；释放回调后，迟到结果没有成功记录，输出 lease_lost，之后数据库显示 EXPIRED。
3. **真实进程退出。** 独立子进程运行实际 QueueWorker 和心跳，到达工具窗口后仅终止本次演示登记的 PID。心跳计数停止，到期后显示 EXPIRED，竞争者不能盲目重做。

可单独运行一个场景：

```powershell
.\.venv\Scripts\python.exe scripts/lease_demo.py --scenario long-tool
.\.venv\Scripts\python.exe scripts/lease_demo.py --scenario heartbeat-failure
.\.venv\Scripts\python.exe scripts/lease_demo.py --scenario process-death
```

脚本逐行输出观测，末尾给出 passed 和报告路径。报告位于 `artifacts/lease-demos/<id>/report.json`，service-private 保存本次私有配置和日志。它不调用付费 API；长调用和传输失败来自宿主侧演示夹具，没有给正式模型工具增加注入故障的入口。

## 正式 worker 参数

```powershell
.\.venv\Scripts\python.exe -m safeharness worker --backend postgres --config-dir artifacts/m4-private --once --lease-seconds 30 --heartbeat-seconds 5
```

worker 与 API 仍须使用相同数据库和配置目录。调小租约时也须相应调小心跳间隔。演示的小租约用于加速观察，不能直接当作已验证的生产配置。

## 迁移与验证

迁移头为 `0015_queue_leases`。升级共享部署前先退出旧版 API/worker，再迁移和启动新版，避免旧代码不理解新租约规则。迁移保留所有任务与队列；旧 S2 的 CLAIMED 记录会立即成为过期记录，不凭空获得新租约，也不自动重新排队。已完成记录不会被补造历史心跳。

新增 `tests/test_queue_leases.py` 包含可控数据库时间的到期边界、不可复活、身份篡改、宿主展示时间偏移、终态停止续期、迟到结果/账本/检查点/验证事件拒绝、心跳失败旗标的并发顺序、独立心跳支持长工具和长模型回调、真实锁等待超时、真实进程死亡及 S2 迁移。并发和数据库行为分别在 SQLite、真实 PostgreSQL 中验证；SQLite 演示不能替代 PostgreSQL 结论。

最终测试、固定开发验收、源码指纹和安装包证据见 [S3 机器记录](evidence/scheduler-s3.json)。CI 已加入租约演示；远程工作流尚未执行。

下一步 S4：扫描过期记录，依据检查点和业务证据分类，再以新的执行代次安排接管；不确定的在途调用必须先核对或交人工处理。
