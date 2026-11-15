# 阶段 3：应用注册与受控 MCP

2026-11-15。阶段 2 已提交为 `87cd62b`，并通过[远端 CI](https://github.com/564264850/safeharness/actions/runs/43685338842)。本阶段继续复用 Runtime、Checkpoint、Gateway、InvocationLedger 和原生写审批；不新建另一套执行循环。

| 步骤 | 交付 | 验收 |
| --- | --- | --- |
| 3.1 宿主应用注册 | ApplicationRegistry、Application、ReadOnlyBinding | 应用 ID、版本、工具 Schema、验证合同和授权配置可声明；重复注册及覆盖原生工具被拒绝 |
| 3.2 Gateway 扩展 | 宿主选定原生工具及只读适配器 | 参数严格校验、资源授权在派发前执行，调用 ID 与策略审计复用现有账本 |
| 3.3 受控 MCP | 有界本地 stdio tools 客户端和合成文档服务 | initialize → initialized → tools/list → tools/call，Schema 与宿主声明一致才调用 |
| 3.4 故障边界 | 限制输入/输出/总时限，回收直接子进程，最小环境变量 | 未知工具、越权、Schema 错误、超时、断连、协议错误和输出超限均有独立子进程用例 |
| 3.5 组合与恢复 | 原生 read_ticket + MCP kb_read_document 演示 | 同一个 Harness 完成并独立核对结果；暂停后恢复不重复读取，配置变化拒绝恢复 |

## 注册接口

`src/safeharness/applications.py` 中的 Application 声明应用 ID/版本、native_tools、只读 bindings、verify 及 verifier_version。配置摘要包含工具版本/Schema、资源白名单、策略版本、适配器配置摘要和执行预算；将 `app.configuration` 传入 Harness，沿用现有 checkpoint 配置指纹。

应用注册是受信任的 Python 宿主入口。它不从模型文本、HTTP 任务参数或服务器工具描述加载命令。`document_app.py` 展示一个可复用适配器：DocumentArgs 负责严格参数校验，resources 提取文档 ID，allowed_resources 定义授权，dispatch 适配 MCP 返回值；应用提供独立 verify 合同。新增只读业务使用这些接口，不修改 Runtime。

示例调用：

```python
app = document_application()
registry.register(app)
gateway = ToolGateway(..., application=registry.get("ticket-docs"), audit=audit)
# 模型使用 app.tool_schemas()；Harness 使用 app.verify 和 app.configuration。
```

本轮没有把动态 app_id 加入已有 HTTP/queued API。现有 API 仍只接受已经验证的 ticket/coding 场景；阶段 4 的应用入口可基于宿主注册来扩展，不能直接把任意字符串当 Python 导入路径。

## MCP 支持范围

`mcp_readonly.py` 明确实现 MCP **2025-11-25** 的 tools-only stdio 子集，不声称支持最新协议全部能力。参考官方 [生命周期](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/lifecycle.mdx)、[stdio 传输](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/transports.mdx)和[工具协议](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/server/tools.mdx)。

- 宿主配置绝对命令、cwd、适配器版本和预算，不用 shell。每次调用创建独立会话，结束或失败后回收直接子进程。
- 不继承 DeepSeek 等应用凭据；仅传递启动 Python 必要的少量环境变量。不输出服务器 stderr 或协议错误详情。
- 只允许宿主注册的远端工具名；服务器 inputSchema 必须与宿主模型完全匹配。工具 descriptions、instructions 和 annotations 都不授予权限。
- 首版只支持单页、最多 32 项工具发现和文本结果。分页、服务端请求、notifications、多媒体、sampling、远程 HTTP、MCP 写工具均不支持；收到不支持内容时安全失败，不退回任意派发。
- 对协议输入、累计 stdout 字节数及整个会话时限做限制。工具错误与传输错误返回稳定代码；调用链仍由 Gateway 记录。

stdio 服务是**宿主信任的代码**。资源白名单限制的是 SafeHarness 派发，无法阻止已启动的恶意程序自行读取磁盘或联网；该进程没有宣称受到 Docker 沙箱隔离。`demo_mcp_server.py` 是自建、合成数据服务，不表示通过任意第三方 MCP 互操作验收。

MCP 写工具保持关闭；现有工单写入继续走原生审批、参数摘要、幂等操作和业务回执。

## 运行与验证

```powershell
.\.venv\Scripts\python.exe scripts\application_demo.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_applications_mcp.py
```

演示使用 scripted planner 和真实本地 MCP stdio 子进程，原生工单及文档均为合成数据，不调用付费 API。报告保存于 `artifacts/application-demos/<run>/report.json`。最终成功由合同检查工单状态、文档 ID、来源和精确内容，不能由模型最后一句话决定。

20 项针对性测试覆盖原生/MCP 共用调用账本、越权及非法参数在启动服务前拒绝、注册配置指纹、跨 Harness 恢复，以及独立协议 fixture 的超时/断连/Schema 变化/重复工具/输出超限/协议错误/工具错误/版本错误/分页/不支持内容。异常测试检查直接子进程已退出。CI 新增此演示并归档其报告。

本地组合演示为 `SUCCEEDED`，可公开的报告与原始报告摘要见[阶段 3 演示证据](evidence/phase3/README.md)。

阶段收尾：启用 Docker 和 PostgreSQL 的全量非 acceptance 回归为 **543 passed、120 deselected、0 skipped**，耗时 477.82 秒。Ruff、格式检查和秘密扫描通过。120 个固定验收用例由远端 CI 单独执行，不混入本轮本地回归数量。
