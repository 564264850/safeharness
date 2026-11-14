# 真实 DeepSeek 网页开发演示

后续 [R4](approval-loop.md) 已将独立验证接入统一台账，并将新开发任务默认预算提高到 5 元、80 次请求；下文前四轮实验数字保留原始历史。

2026-11-13 的 [R5 修复](verification-truth.md) 已将最终验收替换为宿主侧观测合同；普通 pytest 只作为开发反馈。下文的私有测试和 14 项独立检查描述历史实现，不能作为抵抗恶意候选的保证。本次修复使用已有交付程序验证，没有新增付费生成实验。

2026-11-12，使用当前 Harness + DeepSeek Flash + Docker Coding 工具构建 BudgetBook。它是本地单用户网页记账程序，包含金额校验、JSON 持久化、分类/月度筛选、汇总和 CSV 导出。

## 分工与运行链

控制面提供需求、公开只读契约测试、私有独立测试、文件权限和预算。DeepSeek 自行调用 read_file、write_file、run_tests 编写 Python 后端、网页、补充测试及 README。程序没有调用 Pico 或第三方 Agent 循环。

实际调用链为 `DeepSeekClient → ToolCallingAgent/ContextManager → Harness/Runtime → ToolGateway → CodingWorkspace → Docker`。首个工具结果持久保存后，演示丢弃内存适配器并重建，再通过 CheckpointSession 续跑；使用同一费用账本。这里验证的是适配器重建和数据库恢复，没有模拟本轮开发任务中的进程强杀。

最终模型任务 `7b8aa880-e294-42d1-a25f-02c0d2ee5b43` 为 SUCCEEDED。公开契约 10 项、模型补充 6 项、控制面独立检查 14 项，交付目录在隔离容器中合计 **30/30** 通过。浏览器实际验证添加、错误提示、刷新持久化、筛选和重置。

本轮 Harness 回归 **251/251** 通过，包含真实 Docker；选用 `not acceptance and not postgres`，未重跑既有 120 个合成验收与 12 个 PostgreSQL 用例。新增 3 项回归覆盖 ASCII/中文文件大小反馈及总工作区超限不派发、不破坏快照。Ruff、格式检查及 254 个公开源码/证据文件的凭据扫描通过。

## 可运行交付

```powershell
Set-Location C:\Users\25587\Desktop\agent\safeharness
.\.venv\Scripts\python.exe apps\budgetbook\server.py --port 8765
```

浏览器打开 http://127.0.0.1:8765 。数据默认在 `apps/budgetbook/book.json`，可用 `--data PATH` 指定。服务不需要 API Key，只有重新让模型生成代码才调用付费 API。运行依赖为 Python 标准库，pytest 只用于测试。

交付的 Python 文件仅做 Ruff 格式整理及添加说明边界行为的 lint 注释；原始模型输出保存在 artifacts 的各次构建中。`apps/budgetbook/PROVENANCE.json` 记录来源及整理前后的摘要，Python AST 对比验证整理不改变语法树。`tests/test_contract.py` 和 `tests/test_independent.py` 来自控制面，`tests/test_extra.py` 来自模型。

## 失败与修复

本轮经历 4 个任务，前 3 个保留 FAILED，不重开终态任务；新任务可导入前一次已成功写入的文件快照。

| 构建目录 ID | 结果 | 发现 |
| --- | --- | --- |
| fb0e9dc8-5e3a-4d85-971c-1d7c536f53f0 | FAILED | 单文件超过 8192 UTF-8 字节，反复被拒，最终预算不足 |
| 96afe172-e479-4cd8-b69a-9941afcb5251 | FAILED | 部分源码完成；批量读取大文件后无法容纳完整近期消息组 |
| 0354f7be-a032-4155-97f7-c8d8729b1077 | FAILED | 页面及测试完成；加入文档时超过工作区总字节预算 |
| acbfd88f-d27f-4c44-8a9b-05004586fb3a | SUCCEEDED | 导入已有源码，补齐 README，重新测试并独立验证 |

修复包括：工具 Schema 明示 UTF-8 字节上限，超限反馈包含实际/允许字节数且不回显源码；CodingWorkspace 在派发前验证整个候选快照，已知超限保持原快照及可继续状态。本地任务采用较大的 48,000 字节上下文，避免多份源码消息组挤满默认预算；单条工具结果上限 8192。需求通过任务输入传入，不再重复存入受限代码工作区。没有放宽 Docker 的隔离配置或文件/总字节限制。

共 **28 次真实模型请求**，全部取得 usage；按账本峰值保守价格合计 **0.519452 元费用上界**（含失败重试，不是供应商账单实扣）。原 m3-validation-v1 账本未清零；后续开发/修复使用独立明确预算，修复任务间继续同一账本，配置也绑定预算身份。定价核对来源为 [DeepSeek 官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，工具协议参考 [官方 Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)。

## 自己再运行一次生成流程

只检查依赖、不调用模型：

```powershell
.\.venv\Scripts\python.exe scripts\deepseek_program.py --prepare-only
```

重新从空文件生成是一个新的付费实验，模型可能失败。下面明确创建新预算，额度最多 5 元、最多 80 次请求；同一预算 ID 不能通过重复执行清零：

```powershell
.\.venv\Scripts\python.exe scripts\deepseek_program.py --budget-id my-budgetbook-01 --budget-cny 5.00 --max-calls 80
```

凭据从环境变量或现有 `../pico-api.txt` 读取，不复制到项目。原始过程与结果在 `artifacts/deepseek-builds/<ID>/`。`--seed-build <ID>` 从既有导出文件开启新任务；`--build-id <ID>` 仅用于同配置 PAUSED 会话的续跑，不能重开 FAILED/SUCCEEDED。

## 范围

这是可实际运行的本地桌面浏览器演示，不是部署验收。它没有登录、多用户并发写、数据库服务、CSRF/认证边界及公网部署配置。月度汇总覆盖当月全部分类，分类筛选作用于列表；窄屏原始页面隐藏操作列，完整删除交互适合桌面。功能测试属于开发契约，不是企业生产认证或保留集成绩。

证据见 `docs/evidence/deepseek-program.json`，最终构建原始报告在 `artifacts/deepseek-builds/acbfd88f-d27f-4c44-8a9b-05004586fb3a/report.json`，交付代码独立重测在同目录 `delivered-tests.json`，截图为 `browser-demo.png`。宿主只在源码审阅和容器测试后启动交付程序；DeepSeek 的全部工具执行均位于隔离 Docker 中。
