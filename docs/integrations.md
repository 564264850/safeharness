# 外部组件调研

2026-11-11 接入更新：M3 已将 `py-openjudge==0.2.4` 锁入 `judge` 可选依赖组，验证 ToolSelection、ToolCallAccuracy、ActionLoopDetection 的合同。模型型 Grader 通过预算受控的真实 DeepSeek 调用完成正反例评分；见 [M3 运行指南](evaluation.md) 和 [实测结果](m3-results.md)。M4 已完成固定提交的 A.I.G 实际 HTTP 扫描，合成负对照发现及加固重放关联到 Trace 和业务证据；见 [扫描指南](scanning.md)。

核对日期为 2026-11-10。根据 SafeHarness V1.0 列出的候选，检查官方仓库、根许可证、包元数据、接入文档和相关论文。纳入可对应到安全、恢复或评测接口的能力。以下上游快照是调研证据，实际集成时再锁定可复现的包版本或镜像摘要。代码许可与数据集、镜像、外部服务的条款分别核对。

| 候选 | 选型与验证门槛 |
| --- | --- |
| OpenJudge · Apache-2.0 · P0 | 作为评测进程的可选安装组，属于正式交付项。PyPI 名为 `py-openjudge`，核对到 0.2.4，元数据声明 Python ≥3.10，快速入门写的是 ≥3.10 且 <3.13，因此工程和适配测试统一在 3.11。先安装锁定版本，核对 grader 签名并用固定样本冒烟，再用真实 Judge 跑少量样本。 [PyPI](https://pypi.org/project/py-openjudge/0.2.4/) · [快速入门](https://agentscope-ai.github.io/OpenJudge/get_started/quickstart/) · [源码快照](https://github.com/agentscope-ai/OpenJudge/tree/eb34c3b94c908d875eda025e7863bc765c44ad76) |
| 腾讯 AI-Infra-Guard · Apache-2.0 · P0 | 独立 Agent-Scan CLI/容器，HTTP Provider 调受限测试入口。接入验收先证明 scanner 收到非空 `reply`，再证明一条攻击结果能关联目标 run 与状态证据。锁定提交并保留项目要求的署名与适用通知。 [Agent-Scan 文档](https://github.com/Tencent/AI-Infra-Guard/tree/acddd07bfcab5c0c4a6eeefa487164ba34261c39/agent-scan) · [许可与署名](https://github.com/Tencent/AI-Infra-Guard#-license--attribution) |
| Docker Worker · P0 | 复用 Linux 容器隔离。先在 Windows 的 WSL2 / Docker Desktop 环境验证引擎、网络和资源限制。OpenHands SDK 的 MIT 实现提供 Workspace 与确认机制设计参考，本期由 SafeHarness 的执行适配器调度受限 Docker Worker。 [Docker 运行参数](https://docs.docker.com/engine/containers/run/) · [资源限制](https://docs.docker.com/engine/containers/resource_constraints/) · [OpenHands SDK 源码快照](https://github.com/OpenHands/software-agent-sdk/tree/79844cebdce44ac4b27dcd2929436cb6c363174a) · [SDK 架构文档](https://github.com/OpenHands/docs/blob/main/sdk/arch/overview.mdx) |
| OpenTelemetry · P1 | 内部事件先固化版本，再映射模型与工具 span。GenAI 语义约定处于演进中，导出版本和内部事件版本分别记录。验收包括同一运行中的模型、策略、工具和恢复事件可关联，以及敏感值不会默认进入 span。 [GenAI 语义约定](https://opentelemetry.io/docs/specs/semconv/gen-ai/) |
| OPA · Apache-2.0 · P1 条件接入 | 保留独立策略接口，先用本地确定性策略满足单用户、工单角色与资源约束。出现规则需要独立发布或策略显著复杂时试接 OPA Data API，沿用同一 Policy 测试集验证结果、决策版本和服务故障时的默认拒绝。 [OPA 仓库](https://github.com/open-policy-agent/opa) · [REST API](https://www.openpolicyagent.org/docs/rest-api) |
| Claw-Eval · MIT · P2 | 引入少量可隔离、依赖可复现的任务，先验证数据许可、工具环境和证据通道。借鉴独立 Trace、审计与环境快照三路证据。保留原始任务定义或标记为衍生子集。 [源码快照](https://github.com/claw-eval/claw-eval/tree/5680b8b11ff2ee5dd2b07b89086a29a5c5c984d7) · [论文 v3](https://arxiv.org/abs/2604.06132v3) |
| PawBench · Apache-2.0 · P2 | 备选的 Model × Harness 对照。官方列 QwenPaw、OpenClaw、Hermes，SafeHarness 需自建适配器，维持任务 prompt、workspace、timeout、transcript 和结果格式。其部分任务来自 Claw-Eval 等上游，避免把重叠场景当作独立证据。 [源码快照](https://github.com/agentscope-ai/PawBench/tree/0f794a8bb6c27aa9ee4091b2691fa30e4ed9cc8f) |
| Reflexion · 方法参考 · P2 | 基于独立反馈生成可丢弃的文本修正建议，采用任务内限次、限 token 的实验。只考虑可纠正的规划、工具选择或参数理解错误。权限拒绝、临时基础设施故障和副作用未确认继续走各自恢复路径。 [NeurIPS 论文](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html) · [作者实现](https://github.com/noahshinn/reflexion) |
| Langfuse、Mem0、ReMe | Langfuse 待本地 Trace 和 OTel 跑通后再评估展示与实验管理，部署时区分开源与企业部分。长期记忆只在本地验证有明确收益后评估。 [Langfuse 许可](https://github.com/langfuse/langfuse/blob/main/LICENSE) |

## OpenJudge 适配契约

本轮在独立的 Windows CPython 3.11.18 临时环境安装 `py-openjudge==0.2.4`，四个计划使用的 grader 均可导入。用单次模拟工单工具调用构造 user、assistant tool call、tool 和 assistant 四条消息，`ActionLoopDetectionGrader.aevaluate(messages=...)` 返回 `GraderScore(score=1.0)`。这个样例验证消息格式和本地评分路径。评测适配器与需要模型的三项语义评分留待 M3 联调。

- 从已脱敏的 Trace 重建轮次与工具回合，提供工具定义、调用参数和 `tool_call_id`。映射层只读取评测输入并输出可选的 `grader_id`、分数、理由、用量和错误。业务状态和越权副作用由确定性 verifier 裁决。
- 优先验证 `ActionLoopDetectionGrader` 的消息格式，再接 `ToolSelectionGrader`、`ToolCallAccuracyGrader`、`TrajectoryAccuracyGrader`。有参考路径的用例验证相应 grader 对参考调用的要求。允许等价工具序列，严格参数合同优先由代码判定。 [Agent Graders](https://agentscope-ai.github.io/OpenJudge/built_in_graders/agent_graders/)
- Judge 对比固定模型、版本、rubric 和 prompt，保留评分失败及人工抽样复核记录。对模型轨迹中的注入文本做数据边界标识，限制送出的字段，使用合成 fixture 验证映射。Judge 故障单独记为评分不可用。

## A.I.G 适配契约

- 固定版本后用 `--agent_provider` 指向 HTTP YAML，将 `{{prompt}}` 放入请求的消息字段，并设置 `transform_response: reply`。可选结构化 JSON 输出供导入。测试入口建议绑定回环或容器私有网络，scanner 与目标模型各用独立凭据。
- 目标入口由可信配置选定低权限主体和隔离 fixture，通过普通运行时路径执行。扫描请求无审批权。定义测试回合的会话隔离及重置规则，目标输出关联 run ID，接入层另存扫描批次和目标 run 的映射。
- 用工具调用、策略结果、沙箱结果以及模拟 sink 或数据库的观测区分尝试、拦截、实际影响。将确认的攻击固化为无网络回归。可先接本地记录型 sink 测越权外发，再用受控测试网络验证真实 egress。扫描结果为候选风险，测试覆盖范围以本地验收集为准。 [Agent-Scan 配置](https://github.com/Tencent/AI-Infra-Guard/tree/main/agent-scan) · [DeepSeek Harness 安全评估 v2](https://arxiv.org/abs/2608.16393v2) · [OWASP Agentic Applications 风险分类](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/)

## 研究依据

- OpenHands SDK 论文与源码给出远端工作区、执行、确认和生命周期的边界划分。SafeHarness 借鉴边界划分，自己控制任务状态与工具授权。 [论文](https://arxiv.org/abs/2511.03690)
- Claw-Eval 论文将完成、安全和鲁棒性分别评分，并区分多次尝试的至少一次通过与次次通过。SafeHarness 将复用这种指标区分，另在固定故障窗口下测业务恢复。 [论文 v3](https://arxiv.org/abs/2604.06132v3)
- Ying 等用受控的输入通道与记录型 sink 评估间接注入。SafeHarness 借鉴合成攻击载体、独立副作用证据与分离判分。 [论文 v2](https://arxiv.org/abs/2608.16393v2)
- Shinn 等的语言反馈机制发表于 NeurIPS 2023。论文的任务收益只能作为选题依据，SafeHarness 用相同预算重新比较。DOI 已对照会议页和 Crossref 元数据。 [会议页](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html) · [DOI](https://doi.org/10.52202/075280-0377)

本轮是工程选型调研。检索词围绕项目名、`Agent-Scan HTTP Provider`、`Agent graders`、`workspace sandbox` 和 `harness benchmark`，以官方仓库与文档确认接口，以 arXiv、会议论文和 Crossref 核对方法与书目。端到端联调按后续里程碑验收。
