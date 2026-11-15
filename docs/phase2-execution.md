# 阶段 2：真实 DeepSeek 持久调度实施记录

目标：在不削弱现有工具授权、审批、队列租约和回执规则的前提下，让有限范围的 DeepSeek 工单任务可排队、跨进程恢复，并把模型调用的预算和不确定结果持久化。

| 子阶段 | 工作范围 | 验收定义 | 状态 |
| --- | --- | --- | --- |
| 2.1 可信准入配置 | HTTP 仅接受固定 provider/scenario；API 与 worker 使用同一份管理员配置；默认禁用付费 Provider；检查任务配置指纹 | 关闭时拒绝；模型不能选密钥、端点、模型 ID、预算或工具权限 | 完成（模拟 Provider） |
| 2.2 费用与调用持久化 | API 和 worker 共用账本位置及预算身份；保留未知用量；按当前峰时价格做保守预算 | 多进程重启后预算不重置，超限前不派发请求 | 完成（SQLite） |
| 2.3 失权与迟到响应 | 超时/断连不盲目重试；模型结果在调度租约失效后不能驱动工具派发；在途可能计费的调用留下未确认记录 | 故障注入覆盖请求前、请求中、响应迟到、租约过期 | 完成（SQLite/PostgreSQL 故障注入） |
| 2.4 有状态恢复 | 已持久化的模型响应可复用；没有响应证据的模型调用进入待核对；继续累计步数、期限与预算 | 已确认响应不重复付费调用；未知结果不自动重发 | 完成（受限 ticket 队列） |
| 2.5 人工审批闭环 | 队列中的模型写请求暂停等待审批，批准后 worker 自动续跑，拒绝/取消不复活 | 批准前零写入、批准后一次写入并回读、手动续跑次数为零 | 完成（SQLite/PostgreSQL） |
| 2.6 真实 API 验收 | 先用假的 Provider 覆盖故障，再以 DeepSeek 做一个低 token、单票据的端到端演练 | 原始调用、token、预算、审批、回执与验证均可审计；报告标出真实/模拟部分 | 完成（真实 DeepSeek + 本地合成业务） |

## 首轮源码审计（2026-11-15）

- DeepSeek 的显式运行入口已经存在，但 `TaskInput.supported_queue` 和 `canonical_request` 共同将 queued DeepSeek 请求拒绝；`QueueWorker` 创建的 `TaskService` 使用默认 `allow_deepseek=False`，且 CLI worker 没有与 API 共用的付费 Provider 设置。
- DeepSeek 模型和 endpoint 是代码固定值；HTTP task schema 不接受模型 ID、URL、key 或 provider budget 字段。管理员的 `--allow-deepseek` 是目前显式启用边界，但此前只应用于 API 进程。
- `BudgetLedger` 已将每次 attempt 做持久预留；`TaskService` 的 checkpoint fingerprint 记录预算 ID/金额/次数，却没有绑定账本文件位置，多 worker 可能误用不同 SQLite 账本。
- 首轮审计发现 provider 对超时/连接错误和 5xx 会自动重试，存在重复计费风险；2.3 已先收紧自动重试策略，剩余租约过期与迟到响应的恢复/围栏测试仍待完成。
- 官方当前 Flash API 名称为 `deepseek-flash`，支持工具调用。当前官方峰时未命中缓存价为输入 $0.30/百万 tokens、输出 $1.20/百万 tokens。SafeHarness 的预算上界将按 $1 = ¥8 的显式配置上限向上取整，而不是按任意实时汇率声称绝对保证。参考：[DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek 更新说明](https://api-docs.deepseek.com/updates/)。

本阶段真实 API 调用不会用于故障注入。故障矩阵由固定 transport/本地模拟服务完成；真实调用只在协议和费用边界测试通过后运行。

## 2.1–2.3 实施与验证

- queued DeepSeek 仅开放现有 ticket 场景。模型请求仍不能提交 key、endpoint、model、预算或权限字段；默认关闭时 API 返回拒绝，worker 对历史/手工注入的禁用任务也会在加载凭据和建立网络请求之前终止。
- `serve` 与 `worker` 现在接受同一组管理员配置参数。DeepSeek 配置指纹包含固定 endpoint/model、策略版本、token/超时/尝试上限、预算设置和账本路径摘要；不同账本位置无法静默续跑同一个任务。
- 新预算上界使用输入 3、输出 10 微元/token（官方美元峰时价按显式 8 CNY/USD 上限向上取整）。历史已结算 token 记录按新费率重算，未知调用的预留费用保留并按最坏费率比例放大；迁移不删除调用记录。
- 本轮通过 23 项定向测试（SQLite + PostgreSQL + 旧账本迁移），Ruff lint/format 检查通过。测试用假的 HTTP transport 和合成凭据，没有发起真实 API 请求。
- 2.2 覆盖账本跨重启持久化、服务/worker 配置一致性，以及 8 个独立 Python 进程争用同一预算时仅允许 3 次预留成功（其余 5 次在派发前拒绝）。本轮 PostgreSQL 测试连接超时，因此 PostgreSQL 后端专项测试未运行成功；既有 SQLite 测试正常。
- 仓库全量非 acceptance 回归首次结果为 500 通过、2 失败；两项失败是旧准入测试仍期待 DeepSeek 返回 schema 422，而现在默认禁用策略先返回 403。已更新预期并重跑准入/费用/队列定向用例，28 项通过。
- 2.3 初步修正：超时、连接中断和 5xx 均不可自动重试，费用预留保持 UNKNOWN；只有收到明确 HTTP 429 时才可进行原有的有界重试。Provider/queued DeepSeek 定向测试 24 项通过，并强化超时用例检查请求只发出一次且 UNKNOWN 预留仍在。
- 完成上述 2.3 初步修正后，全量非 acceptance 回归为 503 passed、120 deselected，用时 452.96 秒；秘密扫描、Ruff check/format 和 `git diff --check` 复核通过。
- 2.2 多进程测试：8 个 Python spawn 子进程争用同一 SQLite 账本，只 3 个能取得预算预留。当前机器 PostgreSQL 端口 15433 连接超时，专项用例无法在本轮执行。
- 2.4 初步实现：恢复扫描器允许带模型 snapshot 的 DeepSeek checkpoint 进入校验；Provider 按模型/来源/prompt 摘要匹配已成功模型调用，校验 artifact 长度与 SHA-256 后重放，并输出 `model.response_reused` 元数据事件。没有已确认 artifact 时仍走新调用前的 invocation/预算检查；INTENT/UNKNOWN 调用仍会由恢复分类器拦截。
- 阶段 2.3/2.4 定向测试 54 passed、29 skipped。全量非 acceptance 测试 408 passed、97 skipped、120 deselected；另有 2 个 coding sandbox 用例失败，错误为 `sandbox_unavailable`（本机 Docker worker 当前不可用），不是模型恢复用例失败。

## 2.3/2.4 完成验收

本轮 Docker `sandbox-smoke` 隔离与清理检查通过；启动停止的 `safeharness-postgres-m4` 后，PostgreSQL 专项也恢复可用。此前的环境故障已解除。

- 只有 queued 任务以 `resume` 恢复时，Provider 首次请求才允许查找原恢复 checkpoint 之后的响应；以 task/model/provider/完整请求摘要匹配，不对正常运行的相同 prompt 做缓存。
- 原请求已有确认响应但恢复 prompt 不匹配时，停止执行；扫描器对缺失、损坏和未确认响应返回待核对，禁止重复请求。
- 在模型结果落库之后、agent checkpoint 更新之前模拟崩溃：新 worker 复用原响应执行读取，再请求最终答案，总计仅两次 Provider 请求；恢复不会重置原执行截止时间。
- 模型 HTTP 阻塞期间将 worker 围栏置为失效并使数据库租约过期：释放迟到响应后，旧 worker 返回 `lease_lost`，工具调用数量为零，模型 artifact 未写入；扫描器以 `unconfirmed_call` 转人工核对。预算记录仍保留（已返回 usage 可结算，无法确认 usage 则保留预留）。
- SQLite/PostgreSQL 下验证成功响应复用、INTENT、缺失 artifact、摘要损坏、迟到响应等分支。组合测试 101 passed；最终 Provider/queued 专项 38 passed。故障均由合成 transport 注入，未调用真实 API。
- 最终启用 Docker 和 PostgreSQL 的全量非 acceptance 回归：**517 passed、120 deselected、0 skipped**，耗时 460.21 秒。Ruff check/format、秘密扫描和差异检查均通过。120 个 acceptance 用例不属于本轮全量命令，未声称本轮重新执行。

适用范围是现有 ticket 场景。Coding 队列、跨主机费用账本及自动核对 Provider 未知账单仍不在这两个子阶段范围内。

## 2.5/2.6 完成验收（2026-11-15）

- 新增 DeepSeek 协议 transport 的 approve/reject/cancel 参数化队列测试，SQLite 与 PostgreSQL 共 6 项通过。等待期间写入为零，批准后由新 worker 写入一次并回读；拒绝和取消后无模型续调、无业务写入、队列不复活。
- 新增 `scripts/phase2_deepseek_acceptance.py`：显式 `--run-paid` 才调用真实 API。独立 API + worker 进程、真实 DeepSeek 工具调用、本地合成工单；审批者是演练脚本模拟的独立 reviewer 身份，不代表真人手动审批，也不连接企业工单系统。
- 在 `WAITING_APPROVAL` 时终止首个 worker，通过 HTTP 以 reviewer 批准后启动新 worker；任务自动变为 `SUCCEEDED`，generation 从 1 到 2，worker ID 不同。没有调用手动 `/run` 或 `/resume`。自行审批 HTTP 403，重复审批 HTTP 409；写入计数始终只增加一次。
- 验收直接检查业务回执 status 为 SUCCEEDED、operation_id 等于 approval_id；模型的最终文字不能代替这些检查。工具调用记录也确认成功回读。
- 真实演练两次：首轮 4 calls / 2893 input / 264 output / 0.011319 CNY 上界；第二轮 4 calls / 2919 input / 278 output / 0.011537 CNY 上界。两次 unknown_usage_calls 均为 0。费用是按当前账本保守费率计算的估算上界，不是平台实扣账单。
- 第二轮原始本地报告：`artifacts/phase2-acceptance/7a8220217e5d40a983c42e01a7cd5bb7/report.json`。可随代码阅读的脱敏副本见[真实 DeepSeek 审批验收](evidence/phase2/README.md)，包含原报告摘要、源码指纹、调用元数据和业务回执；原始模型内容和凭据仍保留在被忽略的本地目录中。
- 本轮完整相关回归 `tests/test_queued_approval.py tests/test_queued_deepseek.py`：48 passed，SQLite/PostgreSQL 均启用，无跳过。Ruff lint/format、秘密扫描与差异检查通过；本轮没有重复运行上节记录的全量 517 项和 120 个固定 acceptance 用例。

复现命令（会消耗真实 API 余额，每次新建独立工单与账本）：

```powershell
.\.venv\Scripts\python.exe scripts\phase2_deepseek_acceptance.py --run-paid --credentials ..\pico-api.txt
```

单个真实演练是集成冒烟证据，不代表大规模模型可靠性、企业业务上线或跨主机调度验收。阶段 2 的预定 ticket 范围已实现。
