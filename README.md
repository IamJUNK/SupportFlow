# SupportFlow

SupportFlow 是一个订单售后后端 MVP。FastAPI 接收任务，SQLite 保存业务状态、确认方案与执行日志；业务层支持订单查询、政策检索、取消提案和催发货工单。演示客户固定为 `demo-customer`，订单和物流数据均为本地模拟。

## 本地运行

需要 Python 3.10 或更新版本。Windows PowerShell 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
python -m supportflow
```

浏览 `http://127.0.0.1:8000/docs`。默认 `RUN_MODE=mock`，不需要 API Key。

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/tasks `
  -Headers @{"Idempotency-Key"="demo-cancel-1"} `
  -ContentType "application/json" `
  -Body '{"message":"请取消订单 O-1001","execution_mode":"mock","agent_mode":"single_agent"}'
```

任务进入 `awaiting_confirmation` 后，将 `pending_action.action_id` 与 `decision=approve` 或 `reject` 发送到 `/tasks/{task_id}/confirmations`。缺少订单或意图时，任务进入 `awaiting_input`，可通过 `/tasks/{task_id}/events` 补充信息。`completed` 是终态，后续业务请求应创建新任务；技术失败任务可调用 `/tasks/{task_id}/retry` 重试。

首次创建数据库时，演示订单时间以当前 UTC 时间为基准；已有订单不会重新计时。旧数据库中超过 24 小时的订单现在会正确拒绝取消。如需重新演示，设置新的 `DATABASE_PATH` 和 `CHECKPOINT_PATH` 后启动，保留原数据库即可。

## 执行模型

- `execution_mode` 决定如何识别意图：`mock` 使用确定性规则，`live` 使用 OpenAI-compatible tool calling。省略时继承服务端 `RUN_MODE`。
- `agent_mode` 当前只支持 `single_agent`。旧参数 `mode=multi_agent` 仍可用于兼容已有客户端，但会按单 Agent 执行，不能用于多 Agent 效果或成本对比。
- live 模型只提交结构化的意图和订单号。订单归属、政策、查询、确认与所有写操作由后端执行，模型文本不能决定业务成功。
- mock 与 live 共用相同业务状态机和工具层。否定、冲突、未知或多个订单号会进入澄清，不会默认生成取消方案。

## 一致性与恢复

- 取消必须先生成具体方案并取得确认。确认决定与任务重新入队位于同一 SQLite 事务中，相反决定返回冲突。
- 取消执行会在事务内重新检查客户、订单状态、版本、24 小时时限和政策版本；订单更新与 operation journal 原子提交，相同 `action_id` 重放不会重复生效。
- 催发货类型由后端固定为 `expedite`。订单状态检查、唯一业务键和工单写入位于同一事务中。
- 补充事件和入队原子提交；每次执行只确认实际读取的事件。多轮上下文从完整事件历史重建，最新明确修正优先。
- 进程启动时恢复遗留 `running` 任务，也兼容升级前已经写入但未入队的确认事件。LangGraph checkpoint 仅用于诊断快照，业务恢复以业务 SQLite 表和幂等日志为准。
- `/health` 检查数据库和 worker 存活；后台循环异常会记录并退避，不会让执行器静默退出。

## 测试与评测

```powershell
python -m pytest
python evals/runner.py --mode single_agent
```

测试覆盖归属隔离、误取消防护、多轮补充、订单更正、冲突确认、事务回滚、中断恢复、时限与政策复核、工单幂等、模型预算、API 健康检查、旧数据库升级和 checkpoint。

2026-10-10 本地验证：Python 3.12.2，48 项 pytest 测试通过；29 个 mock 场景通过，旧模式参数兼容组同样为 29/29。真实 OpenAI SDK 使用内存 HTTP 替身验证请求、参数和响应解析，没有发送云模型请求。当前依赖会产生一条 LangChain 的未来默认值弃用警告，不影响测试结果。

评测场景会核对 SQLite 最终订单、版本、退款标记、工单和 operation journal，不能仅靠 `completed` 或文本命中判定通过。`--mode both` 只用于验证旧 `multi_agent` 参数兼容；两组结果来自同一单 Agent 路径。

## 当前边界

服务按单进程、单任务串行执行设计，不支持多 worker 协调。退款只记录 `refund_required=true`，没有真实退款流程；认证、真实电商/物流集成、分布式队列和多 Agent 编排不在当前范围内。

`RUN_MODE=live` 需要配置 `LLM_API_KEY` 和 `LLM_MODEL`，可选 `LLM_BASE_URL`。模型端点必须支持 Chat Completions 函数调用以及指定 `tool_choice`。模型调用使用显式截止时间和项目级重试预算。预算按每次后台执行计算，等待用户期间不计时；它不是可强制终止底层同步 I/O 的硬实时保证。返回后和写操作前会再次检查，已经提交的操作通过幂等记录恢复。当前未调用真实云模型，端点兼容性、延迟和费用尚未实测。
