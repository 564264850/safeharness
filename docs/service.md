# M4 服务与亲手操作

当前迁移头为 `0017_queued_approvals`。[S1](scheduler-s1.md)–[S4](scheduler-s4.md) 提供队列、worker、租约和恢复；[S5–S7](scheduler-s5-s7.md) 增加旧执行者隔离、审批自动续跑和运行验收。默认 manual 保留本页操作方式。

服务运行于宿主控制平面，默认只监听 `127.0.0.1`。PostgreSQL 和不可信 Coding worker 使用不同容器；API 进程不在接收 Docker socket 的 Web 容器内运行。需要 Python 3.11、Linux Docker Engine，以及 service 可选依赖。

```powershell
Set-Location C:\Users\25587\Desktop\agent\safeharness
.\.tools\uv.exe sync --locked --extra service --extra judge --python 3.11
.\.venv\Scripts\python.exe -m safeharness service-init
.\.venv\Scripts\python.exe -m safeharness postgres-up
.\.venv\Scripts\python.exe -m safeharness sandbox-build
.\.venv\Scripts\python.exe -m safeharness serve --backend postgres
```

最后一条命令在前台提供服务，默认端口 `18081`；保持窗口打开。`postgres-up` 只管理带 SafeHarness 所有权标签的 `safeharness-postgres-m4`、15433 端口和独立命名卷，不操作其他项目的 PostgreSQL。生成的数据库密码、身份 token 和哈希配置位于忽略目录 `artifacts/m4-private`。Windows 文件权限继承宿主账号策略，Python chmod 不等于完整 Windows ACL 隔离；不要共享这个目录。

在第二个 PowerShell 窗口逐条执行。凭据从文件读入变量，不输出到终端或命令参数：

```powershell
Set-Location C:\Users\25587\Desktop\agent\safeharness
$tokens = Get-Content artifacts\m4-private\service-tokens.json -Raw | ConvertFrom-Json
$owner = @{ Authorization = "Bearer $($tokens.alice)" }
$reviewer = @{ Authorization = "Bearer $($tokens.reviewer)" }
$base = 'http://127.0.0.1:18081'
$created = Invoke-RestMethod "$base/v1/tasks" -Method Post -Headers $owner -ContentType application/json -Body '{"scenario":"ticket_update"}'
$taskId = $created.task_id
Invoke-RestMethod "$base/v1/tasks/$taskId/run" -Method Post -Headers $owner
Invoke-RestMethod "$base/v1/ticket" -Headers $owner
$approval = Invoke-RestMethod "$base/v1/tasks/$taskId/approval" -Headers $reviewer
$approval | ConvertTo-Json -Depth 5
```

此时应为 `WAITING_APPROVAL`，业务写入计数未增加。确认工单和参数后，由 reviewer 记录决定：

```powershell
$decision = @{ decision = 'approve'; arguments_sha256 = $approval.arguments_sha256 } | ConvertTo-Json
Invoke-RestMethod "$base/v1/approvals/$($approval.approval_id)/decision" -Method Post -Headers $reviewer -ContentType application/json -Body $decision
Invoke-RestMethod "$base/v1/tasks/$taskId/resume" -Method Post -Headers $owner
Invoke-RestMethod "$base/v1/tasks/$taskId/resume" -Method Post -Headers $owner
Invoke-RestMethod "$base/v1/ticket" -Headers $owner
Invoke-RestMethod "$base/v1/tasks/$taskId/trace" -Headers $owner | ConvertTo-Json -Depth 6
```

两次 resume 都应为 `SUCCEEDED`，相对初始计数只新增一次写入。默认配置中 alice/bob 为 requester，reviewer 为 approver，auditor 为只读审计角色；即使给 requester 同时授予 approver，也不能批准自己的任务。模型工具 Schema 不含这些身份权限。

| API | 权限和用途 |
| --- | --- |
| `POST /v1/tasks` | requester 创建；scenario 为 ticket_read、ticket_update、ticket_update_read、coding_fix；provider 默认 scripted |
| `POST /v1/tasks/{id}/run` | 归属者启动 CREATED 任务，显式、有界执行 |
| `GET /v1/tasks/{id}` | 归属者查询，approver/auditor 可审阅 |
| `GET /v1/tasks/{id}/approval` | 读取规范化参数和摘要 |
| `POST /v1/approvals/{id}/decision` | 独立 approver；摘要、写范围和未决定状态再次校验 |
| `POST /v1/tasks/{id}/resume` | 归属者续跑，重查当前写范围，再由存储层核对审批与业务证据 |
| `POST /v1/tasks/{id}/cancel` | 归属者取消；已提交写入必须核对回执，不能伪装成撤销 |
| `GET /v1/tasks/{id}/trace[.jsonl]` | 归属者或审阅者导出元数据，不含原始模型内容和凭据 |
| `GET /v1/ticket` | 范围内读取模拟工单状态和 write_count |
| `GET /health`、`GET /ready` | 进程存活和数据库就绪；后者数据库故障返回 503 |
| `GET /v1/operations` | auditor 读取队列计数、过期租约和待核对告警 |

`/run` 是同步请求。任务执行并发信号量每进程为 2，数据库行锁负责跨进程写入一致性。R3/R4 支持模型上下文、Coding 快照和审批后的显式续跑。queued 任务不能使用手动入口运行，当前支持 scripted 和显式启用的 DeepSeek + ticket_read/ticket_update/ticket_update_read；批准后自动唤醒 worker，不需 `/resume`。`serve --max-pending` 限制数据库活跃队列容量，默认 1000，满载新接单返回 429，幂等重放仍可返回原任务。取消 Coding 会停止后续派发，正在执行的沙箱 worker 会被回收；正在等待的模型 HTTP 请求可能到达其有界超时后才返回。

不需要启动常驻服务也能运行两条演示，每次创建独立数据库和临时服务，自动保存报告并停止服务：

```powershell
.\.venv\Scripts\python.exe scripts\m4_demo.py ticket
.\.venv\Scripts\python.exe scripts\m4_demo.py coding
```

Coding 演示经过真实 Docker、文件写入、pytest 和控制平面的独立合同。演示默认 scripted，不消耗 API 额度。开启 DeepSeek 必须显式给 serve 和 worker 都加 `--allow-deepseek`，并在任务中设置 provider。API 和每个 worker 必须使用相同的 `--budget-db`、`--budget-id`、`--budget-cny`、`--max-provider-calls` 与 provider 配置，否则配置指纹会阻止任务继续。账本路径会以摘要绑定进任务配置，不会写入 trace。

下面是两个 PowerShell 窗口的最小启动示例。凭据文件只由进程从本地受保护路径读取，不要把 API key 放进命令行参数、任务 JSON 或仓库：

```powershell
# 窗口 1：API
.\.venv\Scripts\python.exe -m safeharness serve --allow-deepseek --credentials ..\pico-api.txt --budget-db artifacts\provider-budget.sqlite3 --budget-id service-deepseek-v2 --budget-cny 5.00 --max-provider-calls 80

# 窗口 2：worker（参数必须与 API 完全一致）
.\.venv\Scripts\python.exe -m safeharness worker --allow-deepseek --credentials ..\pico-api.txt --budget-db artifacts\provider-budget.sqlite3 --budget-id service-deepseek-v2 --budget-cny 5.00 --max-provider-calls 80
```

当前价格预算按照官方峰时未命中缓存价格，以 1 USD ≤ 8 CNY 的运营假设向上取整。该汇率上限是显式估算参数，不是汇率保证；DeepSeek 官方价格可能变更，升级前需复核。详见[阶段 2 实施记录](phase2-execution.md)。

`deploy/compose.postgres.yml` 提供另一种 PostgreSQL 部署方式，使用相同的固定镜像摘要、资源上限和回环端口。它与 `postgres-up` 是两种启动方式，不能同时占用 15433；本机已使用后者，Compose 仅做过配置验证，不要另外启动它覆盖现有服务。数据库迁移版本为 `0017_queued_approvals`。升级前先停止旧 API/worker/扫描器，对共享库备份，再由单一进程迁移并启动同版本服务。生产部署仍需 TLS、身份与密钥生命周期、涵盖手动任务与费用的容量控制、完整在途副作用协议、备份与保留策略；详见威胁模型。
