import json
import logging
import re
import time

from .config import Settings
from .db import Database
from .graph import CheckpointManager
from .model import ModelClient
from .schemas import AgentDecision, ToolResult
from .tools import ToolContext

logger = logging.getLogger(__name__)
ORDER_RE = re.compile(r"(?<![A-Za-z0-9_-])O-[0-9]{4}(?![A-Za-z0-9_-])", re.IGNORECASE)
NEGATION = r"(?:不(?:要|想|需要|必|用|再)?|无需|不能|无法|别|勿|do not|don't|never)"
OPERATIONS = {
    "cancel_order": r"取消|cancel",
    "create_ticket": r"催发货|催发|人工工单|工单|ticket|expedite",
    "query_order": r"查询|请查|查一下|帮我查|物流|状态|哪里|tracking|query|track",
}
DECISION_TOOL = {"type": "function", "function": {
    "name": "submit_decision",
    "description": "提交当前用户意图及明确指定的订单；不确定时填写 missing_fields，不能自行认定业务成功",
    "parameters": AgentDecision.model_json_schema(),
}}


class WorkflowEngine:
    """Mock and live select decisions; the same backend executes every business transition."""

    def __init__(self, db: Database, settings: Settings, clock: str | None = None):
        self.db = db
        self.settings = settings
        self.clock = clock
        self.checkpoints = CheckpointManager(settings.checkpoint_path)

    def close(self) -> None:
        self.checkpoints.close()

    def process(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if not task or task["status"] != "running":
            return
        started = time.monotonic()
        deadline = started + self.settings.task_timeout_seconds
        events = self.db.task_events(task_id)
        event_ids = [event["event_id"] for event in events if not event["processed"]]
        context = self._resolve_context(task, events)
        self._save_checkpoint(task)
        try:
            execution = task.get("execution_mode") or (task["mode"] if task["mode"] in {"mock", "live"} else self.settings.run_mode)
            # Persist mode before execution so restart/config changes cannot switch an existing task.
            self.db.update_task(task_id, execution_mode=execution, error=None, result_json=None)
            tools = ToolContext(self.db, task_id, task["customer_id"], self.settings.policy_version, self.clock)
            action = self.db.get_pending_action(task_id)
            if action:
                self._resume_action(task, action, context, event_ids, tools, deadline)
                return
            saved = json.loads(task.get("context_json") or "{}")
            # Persisted decisions make replay after a committed write deterministic.
            revision = [event["event_id"] for event in events]
            if saved.get("decision_ready") and saved.get("event_revision") == revision:
                context = saved
            elif execution == "live":
                decision = self._live_decision(task, events, context, deadline)
                context.update(decision.model_dump())
            context.update(decision_ready=True, event_revision=revision)
            self.db.update_task(task_id, context_json=json.dumps(context, ensure_ascii=False), intent=context["intent"])
            self._execute_decision(task, context, event_ids, tools, deadline)
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            # No event is acknowledged on technical failure; durable tools remain replayable.
            self.db.finish_task(task_id, "failed", None, context.get("intent"), context, [], str(exc))
        finally:
            self._trace(task_id, "workflow", "system", "workflow", "process", {}, {"elapsed_ms": int((time.monotonic() - started) * 1000)})
            latest = self.db.get_task(task_id)
            if latest:
                self._save_checkpoint(latest)

    def _save_checkpoint(self, task: dict) -> None:
        try:
            state = {key: task.get(key) for key in ("task_id", "customer_id", "status", "intent", "error", "updated_at")}
            state["context"] = json.loads(task.get("context_json") or "{}")
            state["result"] = json.loads(task["result_json"]) if task.get("result_json") else None
            state["pending_action"] = self.db.get_pending_action(task["task_id"])
            self.checkpoints.save(state)
        except Exception:
            logger.exception("Checkpoint snapshot failed for %s", task["task_id"])

    def _trace(self, *args, **kwargs) -> None:
        try:
            self.db.trace(*args, **kwargs)
        except Exception:
            logger.exception("Trace write failed")

    @staticmethod
    def _check_budget(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("任务超过总执行时间预算")

    def _tool(self, tools: ToolContext, deadline: float, name: str, **arguments) -> ToolResult:
        self._check_budget(deadline)
        if tools.tool_calls >= self.settings.max_tool_calls:
            raise RuntimeError("超过工具调用预算")
        result = tools.invoke(name, arguments, {name})
        if result.error_code in {"TOOL_ERROR", "INVALID_TOOL_ARGUMENTS"}:
            raise RuntimeError(result.error_message)
        self._check_budget(deadline)
        return result

    def _finish(self, task: dict, context: dict, event_ids: list[str], status: str, result: dict, deadline: float) -> None:
        self._check_budget(deadline)
        self.db.finish_task(task["task_id"], status, result, context["intent"], context, event_ids)

    def _resume_action(self, task, action, context, event_ids, tools, deadline):
        context.update(intent="cancel_order", order_id=action["order_id"], missing_fields=[])
        if action["status"] == "pending":
            self._finish(task, context, event_ids, "awaiting_confirmation", {"business_status": "awaiting_confirmation", "message": "请确认取消方案", "pending_action": self._action_view(action)}, deadline)
        elif action["status"] == "rejected":
            self._finish(task, context, event_ids, "completed", {"business_status": "rejected_by_user", "cancel_executed": False}, deadline)
        elif action["status"] == "approved":
            result = self._tool(tools, deadline, "cancel_order", action_id=action["action_id"])
            self._finish(task, context, event_ids, "completed", {"business_status": "accepted" if result.ok else "rejected_by_policy", "cancel": result.model_dump()}, deadline)
        else:
            raise RuntimeError("无法恢复未知操作状态")

    def _execute_decision(self, task, context, event_ids, tools, deadline):
        intent, order_id = context["intent"], context.get("order_id")
        missing = list(context.get("missing_fields", []))
        if intent == "unknown" and "intent" not in missing:
            missing.append("intent")
        if not order_id and "order_id" not in missing:
            missing.append("order_id")
        context["missing_fields"] = missing
        self._trace(task["task_id"], "decision", "single_agent", "decision", "route", {}, context)
        if missing:
            result = {"business_status": "awaiting_input", "missing_fields": missing, "message": "请明确一个需求：查询、取消或催发货" if "intent" in missing else "请补充或选择一个订单号"}
            if "order_id" in missing:
                orders = self._tool(tools, deadline, "list_my_orders")
                result["orders"] = orders.data["orders"]
            self._finish(task, context, event_ids, "awaiting_input", result, deadline)
            return
        order_result = self._tool(tools, deadline, "get_order", order_id=order_id)
        if not order_result.ok:
            self._finish(task, context, event_ids, "completed", {"business_status": "rejected_by_policy", "error_code": order_result.error_code, "reason": order_result.error_message}, deadline)
            return
        if intent == "query_order":
            order = order_result.data["order"]
            tracking = self._tool(tools, deadline, "get_tracking", order_id=order_id) if order["status"] == "shipped" else None
            result = {"business_status": "accepted", "order": order, "tracking": tracking.data if tracking else None}
            self._finish(task, context, event_ids, "completed", result, deadline)
        elif intent == "create_ticket":
            ticket = self._tool(tools, deadline, "create_ticket", order_id=order_id, ticket_type="expedite", description="用户明确请求催发货")
            self._finish(task, context, event_ids, "completed", {"business_status": "accepted" if ticket.ok else "rejected_by_policy", "ticket": ticket.model_dump()}, deadline)
        elif intent == "cancel_order":
            policy = self._tool(tools, deadline, "search_policy", query="取消订单 未发货 24 小时", category="cancel")
            proposal = self._tool(tools, deadline, "propose_cancel", order_id=order_id)
            if proposal.ok:
                result = {"business_status": "awaiting_confirmation", "policy": policy.data, "pending_action": self._action_view(proposal.data["pending_action"])}
                self._finish(task, context, event_ids, "awaiting_confirmation", result, deadline)
            else:
                self._finish(task, context, event_ids, "completed", {"business_status": "rejected_by_policy", "error_code": proposal.error_code, "reason": proposal.error_message, "policy": policy.data}, deadline)
        else:
            raise ValueError("不支持的业务意图")

    def _live_decision(self, task, events, context, deadline) -> AgentDecision:
        messages = [{"role": "system", "content": "你负责识别售后需求。根据完整用户消息提交 submit_decision：最新明确修正优先，缺少订单或意图要澄清，否定取消不能解释为取消，多需求冲突应澄清。只能使用用户明确指定的订单号，不得猜测。你不执行业务，不宣告成功，也不能改变客户身份。"}, {"role": "user", "content": task["message"]}]
        messages.extend({"role": "user", "content": event["content"]} for event in events if event["event_type"] != "confirmation")
        client = ModelClient(self.settings)
        try:
            response = client.complete_with_retry(messages, [DECISION_TOOL], deadline=deadline)
        except Exception as exc:
            self._trace(task["task_id"], "decision", "single_agent", "model", self.settings.llm_model, {"message_count": len(messages)}, {"attempts": client.calls}, error_type=type(exc).__name__)
            raise
        self._check_budget(deadline)
        self._trace(task["task_id"], "decision", "single_agent", "model", self.settings.llm_model, {"message_count": len(messages)}, {"tool_calls": response.get("tool_calls", [])}, response.get("elapsed_ms", 0), response.get("retry", 0), usage=response.get("usage"), cost=self._cost(response.get("usage")))
        calls = response.get("tool_calls", [])
        if not calls:
            return AgentDecision(intent="unknown", order_id=context.get("order_id"), missing_fields=["intent"], reason_summary="模型未提交结构化决策，请明确需求")
        if len(calls) != 1 or calls[0]["name"] != "submit_decision":
            raise ValueError("模型必须提交且只能提交一个结构化决策")
        decision = AgentDecision.model_validate(calls[0]["arguments"])
        # The backend binds orders and honors explicit negation even if a model ignores it.
        if context.get("order_id") != decision.order_id or context.get("ambiguous"):
            decision.order_id = context.get("order_id")
            if not decision.order_id:
                decision.missing_fields = sorted(set(decision.missing_fields + ["order_id"]))
        if context.get("ambiguous") or decision.intent in context.get("denied_intents", []):
            decision.intent = "unknown"
            decision.missing_fields = sorted(set(decision.missing_fields + ["intent"]))
        if context["intent"] != "unknown" and decision.intent not in {context["intent"], "unknown"}:
            decision.intent = "unknown"
            decision.missing_fields = sorted(set(decision.missing_fields + ["intent"]))
        if decision.intent in {"cancel_order", "create_ticket"} and context["intent"] != decision.intent:
            decision.intent = "unknown"
            decision.missing_fields = sorted(set(decision.missing_fields + ["intent"]))
        return decision

    def _cost(self, usage):
        if not usage or self.settings.llm_input_price_per_1m is None or self.settings.llm_output_price_per_1m is None:
            return {"value": None, "currency": self.settings.price_currency, "status": "unknown"}
        value = ((usage.get("prompt_tokens", 0) or 0) * self.settings.llm_input_price_per_1m + (usage.get("completion_tokens", 0) or 0) * self.settings.llm_output_price_per_1m) / 1_000_000
        return {"value": value, "currency": self.settings.price_currency, "status": "calculated"}

    @classmethod
    def _resolve_context(cls, task, events):
        context = {"intent": "unknown", "order_id": None, "missing_fields": [], "denied_intents": [], "ambiguous": False}
        texts = [task["message"]] + [e["content"] for e in events if e["event_type"] != "confirmation"]
        for text in texts:
            ids = list(dict.fromkeys(ORDER_RE.findall(text.upper())))
            if ids:
                context["order_id"] = ids[0] if len(ids) == 1 else None
            intent, denied, ambiguous = cls._classify(text)
            if intent != "unknown" or denied or ambiguous:
                context.update(intent=intent, denied_intents=denied, ambiguous=ambiguous)
        return context

    @staticmethod
    def _classify(message):
        text = message.lower()
        if re.search(r"如果|否则|若|(?<![a-z])if(?![a-z])", text) and re.search(OPERATIONS["cancel_order"], text) and re.search(OPERATIONS["create_ticket"], text):
            return "unknown", [], True
        denied = [intent for intent, pattern in OPERATIONS.items() if re.search(NEGATION + r"[^，。；,;]{0,12}?(?:" + pattern + r")", text)]
        hits = [intent for intent, pattern in OPERATIONS.items() if re.search(pattern, text) and intent not in denied]
        # Reading words around one explicit operation are context, not a second write request.
        writes = [intent for intent in hits if intent != "query_order"]
        if len(writes) > 1:
            return "unknown", denied, True
        return (writes[0] if writes else ("query_order" if "query_order" in hits else "unknown")), denied, False

    @classmethod
    def _intent(cls, message):
        return cls._classify(message)[0]

    @staticmethod
    def _extract_order_id(message):
        ids = list(dict.fromkeys(ORDER_RE.findall(message.upper())))
        return ids[0] if len(ids) == 1 else None

    @staticmethod
    def _action_view(action):
        return {key: action[key] for key in ("action_id", "task_id", "order_id", "operation", "parameters", "order_version", "policy_version", "status", "created_at")}
