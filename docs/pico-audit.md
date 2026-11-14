# Pico 现状与迁移边界

调研日期为 2026-11-10。以本地 `2-pico.pdf`、`pico/` 工作树和 SafeHarness V1.0 计划报告为输入。PDF 用于理解设计，当前行为以本地源码和复跑为准。

## 基线身份

| 对象 | 观察 |
| --- | --- |
| `2-pico.pdf` | 167 页，SHA-256 `4745c587af579203ed76d69ed9736e3823ef6bd6b305a3828cc877bd451ee16f` |
| 本地工作树 | `main` 基于 `473de5dd05512efff651ecfa15a278f9a22614c8`，14 个已跟踪文件有未提交修改，涉及 Provider、CLI、Workspace、评测及测试 |
| 本地来源 | `origin` 指向 Gitee，PDF 第 3 页也指向该仓库。PowerShell 下 `git diff --binary \| git hash-object --stdin` 得到 `5e4eabde9cbc51e2d61cc87849d20d0299e75190` |
| 计划引用的 GitHub 上游 | 当次核对的 `main` 为 `14d780de2d55d0c3b0f40bd880effa597d8923d5`，与本地 HEAD 不同 |
| 授权证据 | 本地跟踪文件未找到许可证，GitHub 树无顶层 LICENSE/NOTICE 且许可证端点返回 404，Gitee 的 `main/LICENSE` 原始路径返回 404。当前证据不足以确认可复制或再分发，SafeHarness 独立实现，Pico 仅用作本地对照 |

来源包括 [Gitee 仓库](https://gitee.com/htxoffical/pico)、[GitHub 上游快照](https://github.com/Litshp/Pico-harness/tree/14d780de2d55d0c3b0f40bd880effa597d8923d5) 和 [GitHub 仓库许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)。

## 从请求到结果

`pico.cli:main` 装配 Provider、工作区和会话 → `Pico.ask` → `AgentLoop.run` 创建运行并控制模型轮次 → `ContextManager` 组装分区 prompt → Provider `complete` → `Pico.parse` 归一化文本协议 → `Pico.execute_tool` → `ToolExecutor.execute` → 工具结果写回会话、状态与 Trace → 创建 Checkpoint → 完成时写 Report。评测另行执行 verifier。PDF 第 16、38、49、83、92 页分别是这些主题的入口。

| 模块 | 本地证据及现状 | SafeHarness 迁移决策 |
| --- | --- | --- |
| CLI、Provider 和循环 | `cli.py`、`providers/clients.py`、`runtime.py`、`agent_loop.py`。本地提供 Fake、Ollama、OpenAI Responses/Chat 兼容、Anthropic 兼容。文本解析允许 XML 等形态。模型轮次与工具步数有各自上限 | 保留单一循环和脚本模型思想。内部用显式 `ToolRequest` / `FinalAnswer`，在适配器统一 Provider 消息、调用 ID、用量及错误。首版只接一种真实 Provider |
| 工作区与上下文 | PDF 第 31、58、74 页。 `workspace.py` 建仓库快照，`prompt_prefix.py` 管稳定前缀，`context_manager.py` 分区、压缩历史和裁剪。历史结果按字符计量 | 先做有预算的最小上下文，优先保留当前任务、待审批状态和必要工具回执，再按 Provider 的 token 计量和比较。文件、SOP、工具输出一律作为不可信数据 |
| Memory | PDF 第 66 页。 `features/memory.py` 管结构化工作记忆、相关笔记及 freshness，`runtime.py` 负责更新与晋升 | 第一版只保存任务内证据。记忆治理放到可复现实验后。此次在 PDF 提取文本、本地 Python 和 Markdown 中未定位到名为 Jev 的实现，先登记为待核实扩展 |
| 工具与审批 | PDF 第 49 页。 `tool_executor.py:45` 集中检查 allowlist、参数、重复调用和风险审批。 `runtime.py:633` 的审批是同步判断，缺少持久化请求及参数绑定 | 保留集中入口，改为严格 Schema、独立三态策略和持久化审批。工具名、参数、资源、任务及策略版本全部纳入授权依据 |
| 主机和间接执行 | `tools.py:213` 在宿主机以 `shell=True` 启动命令，工作目录和环境过滤属于约束手段。 `tools.py:183` 的搜索也启动外部进程。 `runtime.py:612` 起的直接工具包装可绕开统一入口，属于可调用代码路径，当前模型主循环走统一执行器。 `evaluation/evaluator.py:135` 另起 verifier 子进程 | 普通文件工具和命令都只能操作受控工作区。Coding 中的 pytest、Git 等走隔离 worker，评测 verifier 通过独立可信入口启动另一套隔离执行，模型无权修改评分逻辑或触发宿主进程 |
| 状态、Checkpoint、Trace | PDF 第 83 页。 `task_state.py` 管单轮运行状态，`checkpoint.py:60` 检查版本、文件 freshness 和运行身份。 `agent_loop.py:221` 执行完工具后才写工具事件、Checkpoint，`session_store.py:15` 与 `run_store.py` 分别落文件 | 会话续聊与同一任务的续接分开。通过执行意图、业务回执和核对流程覆盖崩溃窗口，Checkpoint 保存游标与引用。审计事件按字段白名单导出 |
| 评测 | PDF 第 92–100 页。 `evaluation/evaluator.py` 支持固定任务、独立 verifier 与 fixture 指纹。`metrics.py` 做聚合和消融。当前 `benchmarks/coding_tasks.json` 有 12 条，包含修改、拒绝后恢复、Checkpoint 和记忆场景 | 保留固定任务加独立验证的原则。增加工单、授权攻击、故障注入和版本矩阵，分开报告脚本模型与真实模型 |

PDF 第 2–3 页列的是不同版本与实验口径。本地 `benchmarks/results/main-resume-repro-2026-06-07/DATA_PROVENANCE.md` 将工作区漂移的历史结果具体限定为 2 个场景各重复 3 次，6/6 检出。它与进程崩溃后核对远端写入是两类验证。

## 本轮复跑范围

基线使用本地工作树、其既有虚拟环境 CPython 3.13.5 和仓内预设脚本模型。用临时工作区运行固定 benchmark，评测产物也写入临时目录。此基线用于证明已有合同的可复现性。后续对照需要为两套 Harness 分配独立复位的同版本 fixture，并使用共享任务或等价适配。Pico 没有 IT 工单能力的任务单列。

| 验证 | 本轮结果 |
| --- | --- |
| 从 `pico/` 执行 `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` | 137 通过，1 跳过，216.23 秒。跳过分支为 Windows 符号链接创建权限不足 |
| `run_fixed_benchmark(artifact_path=临时路径)` | 12/12 通过，12/12 在预算内，12/12 verifier 通过。模型为 `FakeModelClient`，配置为 `scripted-deterministic` |
| 临时 fixture 单次读取 | `run_started → prompt_built → model_requested → model_parsed → tool_executed → checkpoint_created → prompt_built → model_requested → model_parsed → checkpoint_created → run_finished`。执行 `read_file` 为 `ok`，任务为 `completed` |
| 宿主环境 | WSL2 Ubuntu 可用。Docker Desktop 客户端已安装，Linux Engine 命名管道当时不可连接，Coding 容器隔离留待 M2 预检 |
