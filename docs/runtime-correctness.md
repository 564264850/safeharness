# 运行正确性 R1：异常收敛与可信结果

本轮只处理仍存活的执行进程遇到普通异常、协议返回异常及持久化失败时的正确性。现有 Gateway、审批、回执与沙箱继续复用，没有接入付费服务或改变预算。模型上下文恢复、强杀后的通用接管、队列/租约属于后续工作。

## 修复的问题

原来 run_task 在 RUNNING 阶段调用执行器、在 VERIFYING 阶段调用 verifier，非预期异常会直接退出并留下中间状态。完成回调还有可能先构建内存中的成功报告，再因数据库回滚而失败；调用方不能返回这份未提交的成功结果。

现在通过统一的 RuntimeFailure（phase、机器错误码、outcome_unknown）分类，并由 TaskStore 在持有任务锁的事务中保存状态变化与 runtime.error。公开证据不记录原始异常文本、密钥、prompt 或 stack trace。

| 情况 | 处理 |
| --- | --- |
| 模型适配异常或非法模型动作 | FAILED，保存原因，禁止继续派发 |
| 验证器异常或返回非 bool | FAILED，不把 truthy 对象当作通过 |
| 审计/意图保存失败，尚未派发 | FAILED，工具不执行 |
| Gateway 只读适配器抛错 | FAILED，保留安全错误码 |
| 写/执行适配器抛错，或写入结果未能持久审计 | NEEDS_REVIEW，调用不自动重试 |
| 任意 execute 回调抛错或返回非法 ToolResult | 缺乏可信执行证据，保守进入 NEEDS_REVIEW |
| 容器清理无法确认 | NEEDS_REVIEW，停止后续模型和工具调用 |
| 完成回调失败且提交状态不明 | 重新读取数据库；未完成则保守核对，已提交终态保留 |
| 并发取消或其他已提交终态 | 不覆盖、不重开任务；保留诊断 |
| 当前 revision 已改变 | 不终止另一代执行，记录 inspect_current_revision |
| 已有持久工单执行意图 | 继续由原有回执恢复协议负责，不擅自改写意图或重新派发 |
| 连错误记录也无法持久化 | HTTP 503 / CLI 非零退出，返回任务 ID 和 inspect_task_before_retry，不宣称保存成功 |

本轮同时校验最终文本、ToolResult 类型/工具名/状态、审批与原请求的参数绑定、verifier 的严格 bool，以及完成返回的任务 ID、终态和 revision。扫描模型代理也统一经过 Runtime，避免其手写生命周期在异常后留下 RUNNING。

## 代码入口

- failures.py：RuntimeFailure、ToolExecutionError、RuntimePersistenceError 和机器错误码限制。
- runtime.py：有界循环、回调异常边界、协议返回检查和异常收敛。
- execution.py / audit.py：区分派发前失败、只读失败和执行结果不明。
- task_store.py / trace.py：错误诊断与状态的事务一致性，读取最近诊断。
- service.py / __main__.py：HTTP/CLI 返回 last_runtime_error；无法保存时明确报错。
- coding_demo.py / evaluation.py：失败后按持久任务重建结果，避免返回未提交的成功报告。

数据库约束原已包含 NEEDS_REVIEW，因此没有新增迁移，head 仍为 0009_service。Python 状态机新增 VERIFYING → NEEDS_REVIEW，用于验证执行或完成回调结果不明；正常流程不变。

## 诊断字段与操作含义

例如：

```json
{
  "task_status": "NEEDS_REVIEW",
  "last_runtime_error": {
    "phase": "tool",
    "code": "execution_adapter_error",
    "outcome_unknown": true,
    "recorded_status": "NEEDS_REVIEW",
    "recovery_action": "manual_review"
  }
}
```

last_runtime_error 是最近一次异常的历史记录，recorded_status 是记录当时的状态；查询的 task_status 是当前事实。完成回调提交之后才抛错时，当前任务可能已经 SUCCEEDED，此时不会倒退状态或自动重复执行。FAILED 表示任务未完成/未通过验证，不表示此前所有动作均未发生。

如果收到 runtime_failure_not_persisted，数据库可能仍显示 RUNNING/VERIFYING。此时事务没有保存成功，必须先核对同一 task_id，不能通过新建任务或盲目重试来规避未知结果。后续的持久调度/接管模块将处理进程消失与长期失联。

## 亲手验证

```powershell
.\.venv\Scripts\python.exe -m pytest -v tests/test_runtime_failures.py tests/test_runtime_http.py
$env:SAFEHARNESS_POSTGRES_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -v tests/test_postgres.py -k runtime
.\.venv\Scripts\python.exe scripts\runtime_fault_demo.py
```

演示创建独立 artifacts/runtime-fault-demos 批次，不修改原数据库。三例分别为模型异常 → FAILED、验证异常 → FAILED、模拟 Coding 适配器写入后丢失返回 → NEEDS_REVIEW。第三例为明确的宿主侧测试替身，只写该批次目录，没有真实 Docker 或远程服务；模拟写入计数应为 1，展示没有自动重试。演示使用新进程执行 show，确认异常状态和诊断已持久化。

## 本轮验证与限制

故障注入覆盖模型、非法动作/结果、只读/写/执行适配器、派发前/后审计失败、verifier、审批保存前/后失败、完成提交后抛错、并发取消、新 revision、数据库回滚、旧业务意图，以及错误报告重建。SQLite 和真实 PostgreSQL 均验证异常收敛，并复跑原有 Docker、HTTP、审批/恢复与 120 实例矩阵。具体计数、实现摘要和批次链接由本轮 evidence 摘要记录。

最终全量回归 **216/216** 通过（0 跳过），相对原有 164 项新增 52 项；原固定验收矩阵 **120/120** 通过。最终代码摘要与该验收批次一致。新 wheel 在独立环境中通过三类异常状态及真实 Docker isolation smoke，Ruff、锁文件和凭据扫描通过。真实模型账本仍为 61 次、0.276424 CNY，本轮新增付费请求为 0。

证据：[可分享机器摘要](evidence/runtime-correctness-r1.json)、[全量回归 JUnit](../artifacts/runtime-correctness-regression.xml)、[120 实例报告](../artifacts/m4-acceptance/8f9cad43-884f-4078-877f-869600c70404/report.json)、[三例故障演示](../artifacts/runtime-fault-demos/f024033f-54e0-4f5a-b04f-fe7965d9aa7f/report.json)。专项测试与全量回归有重叠，不重复相加。

本轮不会保证数据库不可写时还能保存终态，也不会把 os._exit、断电或 SIGKILL 当作已捕获异常。没有新增自动扫描失联任务的 reaper，没有补齐 Context/Memory/Checkpoint，没有给任意外部 API 承诺 exactly-once；这些仍在后续底座与生产化范围。
