# 验证真实性修复 R5

日期：2026-11-13。修复对象是“候选程序提前退出，pytest 退出码为 0，但没有执行断言仍可能使任务成功”。历史复现见 [R4 评审](project-review-r4.md)。本次使用已有程序和脚本模型验证，没有调用付费模型。

## 修复后的边界

| 路径 | 用途 | 判定规则 |
| --- | --- | --- |
| `run_tests` | 提供开发反馈 | 检查 session 完成、实际收集数、通过/失败/跳过数；没有报告、0 用例、跳过或提前退出均不返回通过 |
| `ToolGateway.verify_tests` | 旧通用最终验收入口 | 返回 `trusted_contract_required`，不执行候选程序，调用仍保留审计 |
| `observe_book` → `evaluate_book` | 记账程序最终验收 | 沙箱收集观测，宿主逐项核对预期，缺失或不符即失败 |
| 现有工单回执、clamp/固定函数合同 | 已有专用验收 | 继续由宿主核对业务证据或预期输出，未改为信任 pytest 状态 |

`run_tests` 的结果统一标记为 `development_only`。候选代码与 pytest 同进程运行，能够修改对象、退出进程，甚至向原始输出管道伪造完整报告。因此增加计数和完成标记只能改善开发反馈，不能建立最终可信验收。回归特意保留了一个伪造成功的攻击：开发工具显示 `tests_passed`，最终任务仍为 `FAILED`。

## 记账程序的新验收

宿主模块 `src/safeharness/verification.py` 保存合同版本、输入和预期结果；不会导入候选模块。`sandbox_assets/book_observer.py` 在受限容器内运行候选程序，返回实际值和异常类型；它是**不可信的观测适配器**，没有决定成功的权限。期望结果不随请求发入容器，也不再将私有 pytest 断言复制到该进程执行。

当前合同包含 25 个场景、97 个操作，覆盖金额/日期校验、持久化与重新加载、筛选汇总、损坏存储、返回值修改隔离、删除与 ID、中文 CSV、40 条数据累计，以及真实本地 HTTP 的新增、查询、汇总、导出、删除和错误处理。HTTP 服务仅在容器内部访问，没有向宿主映射端口。

宿主检查进程结果、协议版本、场景总数、顺序与 ID、数据类型和值。空结果、`{"passed": true}`、伪造的 pytest 计数和部分结果均不能作为成功证据。最终判定带合同摘要，观测调用通过统一 Gateway 保存 call/attempt 与审计信息；容器仍要求回收确认。

`scripts/deepseek_program.py` 已迁移到新路径：模型结束后，宿主从文件快照创建独立工作区，只提交观测请求，核对合同，再决定任务终态。模型最后一次写文件后仍须运行开发测试，该要求用于开发流程完整性，最终成功还必须通过宿主合同。

## 亲手复现

在 `C:\Users\25587\Desktop\agent\safeharness` 执行：

```powershell
.\.venv\Scripts\python.exe -m safeharness sandbox-build
.\.venv\Scripts\python.exe -m safeharness sandbox-smoke
.\.venv\Scripts\python.exe scripts/verifier_demo.py
```

演示不调用 API，使用真实 Docker 和 Harness，按顺序运行三个独立任务：

| 场景 | 开发反馈 | 最终任务 |
| --- | --- | --- |
| 已交付的 BudgetBook | `tests_passed` | `SUCCEEDED` |
| 导入时 `os._exit(0)` | `tests_failed` | `FAILED` |
| 伪造完整 pytest 成功报告后退出 | `tests_passed`，不可信 | `FAILED` |

输出指向 `artifacts/verifier-demos/<id>/report.json`，包含合同摘要、审计身份及回收结果。自动化用例在 `tests/test_verification_truth.py`，同时覆盖 SystemExit、缺失/重复/错误观测、数值与布尔混淆、空测试、跳过及模型调用私有入口。最终回归与固定开发集结果见 [机器记录](evidence/verification-truth-r5.json)。CI 配置加入此演示；本地运行不等于远程 CI 已运行。

## 适用范围与迁移

这解决了“把退出码或测试成功声明直接当作最终成功”的错误。有限功能合同不能证明任意恶意程序的所有行为正确。观测适配器与候选同处沙箱，候选仍可能伪造观测或针对固定输入硬编码答案；宿主只能判断返回值是否符合所列合同。当前场景集是可见、固定的开发集，没有宣称保留集成绩、不可作弊或生产安全认证。

新类型程序需要由可信调用方提供对应的输入、观测适配器与宿主验收规则，不能直接调用旧 `verify_tests` 取得通用成功认证。后续可增加按场景隔离的保留集与随机输入，并将浏览器/HTTP 等可从候选进程外观测的检查分离；这些尚未实现。

本次工作镜像资产和开发脚本配置版本已改变。先重新构建镜像，再创建新任务；已有 Coding 检查点绑定旧镜像/配置，不能在新版本中无条件恢复。数据库迁移头仍为 `0012_approval_loop`，历史报告与任务终态保持原记录。
