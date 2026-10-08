# SupportFlow

SupportFlow 是一个订单售后后端 MVP：FastAPI 接收任务，SQLite 保存业务状态与轨迹，业务工具执行订单查询、政策检索、取消提案和催发货工单。演示客户固定为 `demo-customer`，所有订单数据均为本地模拟。

## 本地运行

需要 Python 3.10 或更新版本。Windows PowerShell 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
python -m supportflow
```

浏览 `http://127.0.0.1:8000/docs`。数据文件写入 `data/`，默认 mock 模式无需 API Key。另一个终端可运行 `python scripts/demo.py` 演示查物流和确认取消。

创建任务：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/tasks `
  -Headers @{"Idempotency-Key"="demo-cancel-1"} `
  -ContentType "application/json" `
  -Body '{"message":"请取消订单 O-1001","mode":"multi_agent"}'
```

任务进入 `awaiting_confirmation` 后，将返回的 `pending_action.action_id` 与 `decision=approve` 或 `reject` 发送到 `/tasks/{task_id}/confirmations`。支持的接口包括 `/health`、`/tasks`、`/tasks/{task_id}`、`/events`、`/confirmations` 和 `/trace`。

## 当前行为

- `list_my_orders`、`get_order`、`get_tracking`、`search_policy`、`propose_cancel`、`cancel_order` 和 `create_ticket` 都由本地业务服务执行。订单归属从服务端演示上下文注入，模型与用户内容均不能切换客户。
- 取消方案在批准前不能执行。批准时服务会重新检查客户归属、订单状态和版本；取消更新与 operation journal 位于同一个 SQLite `BEGIN IMMEDIATE` 事务中。相同 `action_id` 重放不会重复生效。
- 工单业务键由订单号与工单类型确定，重复催发货只创建一张工单。已发货订单会拒绝取消或催发货工单。已付款订单取消会返回 `refund_required=true`；退款没有实现。
- FastAPI lifespan 启动单进程数据库队列执行器。进程启动时会将遗留的 `running` 任务重新排队；已批准但尚未取消的操作可在恢复时继续。确认与补充等待期间不占执行槽。
- `RUN_MODE=mock` 不调用模型，仍运行真实的 API、数据库、业务工具、确认和幂等代码。当前 mock 的路由是可重复的规则响应，用来验证工程链路，不代表语言模型 Agent 能力。
- `RUN_MODE=live` 需要 `LLM_API_KEY`、`LLM_MODEL`，可选 `LLM_BASE_URL`。live 已实现 OpenAI-compatible 原生 tool-calling 循环：模型只能看到查询、政策、取消提案和催发货工具，后端仍负责客户归属、版本复核和确认后的写操作。没有 API Key 时不会伪造业务成功；模型失败会将任务标为技术失败。当前环境没有 API Key，因此 live 真实云模型行为尚未验证。
- LangGraph 与 SQLite checkpoint 已接入 `WorkflowEngine`，每次流程开始和结束保存任务状态，`CheckpointManager.save/load` 已通过本地 smoke 测试。HTTP 执行器的业务恢复仍依靠业务 SQLite 表和事件重放，checkpoint 用于保存 LangGraph 状态快照，不替代业务事务。
- `single_agent` 和 `multi_agent` 接受相同业务安全边界；目前两者仍使用本地规则 mock 路径，只有轨迹标签不同，不能据此比较真实模型效果或成本。

## 测试与评测

```powershell
python -m pytest
```

`tests/` 覆盖订单归属、取消确认、版本复核、取消 journal 幂等、工单唯一性、API 请求幂等、批准后中断恢复、checkpoint 读写和模型工具权限校验。`evals/scenarios.json` 固定了 12 个开发场景与 8 个留出场景；`python evals/runner.py --mode both` 已在本地无模型环境生成 `reports/`，当前 mock 结果为 multi-agent `20/20`、single-agent `20/20`。两种模式共用确定性业务路径，只有轨迹标签不同，因此这组结果只能证明工程保护和评测 harness，不能作为真实 Agent 能力或成本结论。Mock 测试不能作为简历中的真实 Agent 能力指标。

## 技术边界与 Roadmap

当前服务按单进程、单任务串行执行设计，不支持多 worker 协调。SQLite 使用 WAL、busy timeout、短连接；不依赖 Docker、向量数据库或本地推理。已在 Python 3.12.14 运行时安装并验证：FastAPI 0.142.4、Uvicorn 0.54.0、LangGraph 0.6.11、langgraph-checkpoint-sqlite 2.0.11、pytest 9.1.1、OpenAI 1.109.1。原有 pytest 8 项测试通过；本轮新增测试场景已通过逐项 smoke，FastAPI TestClient 已验证 `/health`、查询任务后台完成、取消确认、Idempotency-Key 重放和 live 缺少密钥失败；LangGraph 图构建和 SQLite checkpoint 读写均已验证。当前环境没有 API Key，尚未进行真实云模型 live smoke、真实 usage/cost 记录和内存峰值测量。

Roadmap：使用真实云模型完成小规模 live smoke，核对不同模型的 tool-call 参数格式、延迟和 usage/cost 记录；再扩展同数据集的真实 single-agent 对比。生产认证、退款、真实电商/物流、向量检索和分布式队列不在本 MVP 范围内。
