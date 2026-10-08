from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from .db import Database
from .schemas import ToolResult


class ToolContext:
    def __init__(self, db: Database, task_id: str, customer_id: str, policy_version: str = "v1", clock: str = "2026-10-08T09:00:00Z"):
        self.db = db
        self.task_id = task_id
        self.customer_id = customer_id
        self.policy_version = policy_version
        self.clock = clock
        self.tool_calls = 0

    def invoke(self, name: str, arguments: dict[str, Any], allowed_tools: set[str] | None = None) -> ToolResult:
        """Invoke a model-selected tool only after role-level allow-list and schema validation."""
        self.tool_calls += 1
        if allowed_tools is not None and name not in allowed_tools:
            return ToolResult(ok=False, error_code="TOOL_NOT_ALLOWED", error_message=f"工具 {name} 不在当前 Agent 权限范围内")
        schema = TOOL_ARGUMENTS.get(name)
        if schema is None or name not in TOOL_HANDLERS:
            return ToolResult(ok=False, error_code="UNKNOWN_TOOL", error_message=f"未知工具 {name}")
        try:
            parsed = schema.model_validate(arguments or {})
            return getattr(self, TOOL_HANDLERS[name])(**parsed.model_dump())
        except Exception as exc:
            return ToolResult(ok=False, error_code="INVALID_TOOL_ARGUMENTS", error_message=str(exc))

    def _call(self, name: str, **kwargs: Any) -> ToolResult:
        try:
            fn = getattr(self, f"_{name}")
            result = fn(**kwargs)
        except Exception as exc:
            result = ToolResult(ok=False, error_code="TOOL_ERROR", error_message=str(exc))
        self.db.trace(self.task_id, "tool", "tool", "tool", name, kwargs, result.model_dump())
        return result

    def list_my_orders(self) -> ToolResult:
        return self._call("list_my_orders")

    def _list_my_orders(self) -> ToolResult:
        return ToolResult(ok=True, data={"orders": self.db.list_orders(self.customer_id)})

    def get_order(self, order_id: str) -> ToolResult:
        return self._call("get_order", order_id=order_id)

    def _get_order(self, order_id: str) -> ToolResult:
        order = self.db.get_order(order_id)
        if not order or order["customer_id"] != self.customer_id:
            return ToolResult(ok=False, error_code="ORDER_NOT_FOUND", error_message="订单不存在或不属于当前客户")
        order.pop("customer_id", None)
        return ToolResult(ok=True, data={"order": order})

    def get_tracking(self, order_id: str) -> ToolResult:
        return self._call("get_tracking", order_id=order_id)

    def _get_tracking(self, order_id: str) -> ToolResult:
        order = self.db.get_order(order_id)
        if not order or order["customer_id"] != self.customer_id:
            return ToolResult(ok=False, error_code="ORDER_NOT_FOUND", error_message="订单不存在或不属于当前客户")
        tracking = self.db.get_tracking(order_id)
        return ToolResult(ok=True, data={"tracking": tracking or {"status": "not_available"}})

    def search_policy(self, query: str, category: str | None = None) -> ToolResult:
        return self._call("search_policy", query=query, category=category)

    def _search_policy(self, query: str, category: str | None = None) -> ToolResult:
        return ToolResult(ok=True, data={"policies": self.db.search_policies(query, category)})

    def propose_cancel(self, order_id: str) -> ToolResult:
        return self._call("propose_cancel", order_id=order_id)

    def _propose_cancel(self, order_id: str) -> ToolResult:
        order = self.db.get_order(order_id)
        if not order or order["customer_id"] != self.customer_id:
            return ToolResult(ok=False, error_code="ORDER_NOT_FOUND", error_message="订单不存在或不属于当前客户")
        if order["status"] != "pending":
            return ToolResult(ok=False, error_code="ORDER_SHIPPED" if order["status"] == "shipped" else "ORDER_NOT_CANCELLABLE", error_message=f"订单当前状态为 {order['status']}，不能取消")
        created = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        now = datetime.fromisoformat(self.clock.replace("Z", "+00:00"))
        if (now - created).total_seconds() > 24 * 3600:
            return ToolResult(ok=False, error_code="CANCEL_WINDOW_EXPIRED", error_message="订单已超过 24 小时取消窗口")
        action = self.db.create_pending_action(self.task_id, order_id, "cancel_order", {"order_id": order_id}, order["version"], self.policy_version)
        return ToolResult(ok=True, data={"executable": True, "pending_action": action, "message": "可以取消，请确认此具体操作方案"})

    def cancel_order(self, action_id: str) -> ToolResult:
        return self._call("cancel_order", action_id=action_id)

    def _cancel_order(self, action_id: str) -> ToolResult:
        action = self.db.get_pending_action(self.task_id, action_id)
        if not action:
            return ToolResult(ok=False, error_code="ACTION_NOT_FOUND", error_message="待确认方案不存在或不属于当前任务")
        if action["status"] != "approved":
            return ToolResult(ok=False, error_code="ACTION_NOT_APPROVED", error_message="操作尚未获得确认")
        result = self.db.cancel_order_transaction(action_id, action["order_id"], self.customer_id, action["order_version"])
        if "data" in result:
            data = {**result["data"], "idempotent_replay": result.get("idempotent_replay", False)}
            return ToolResult(ok=result["ok"], data=data)
        return ToolResult(ok=result["ok"], error_code=result.get("error_code"), error_message=result.get("error_message"), data={"idempotent_replay": result.get("idempotent_replay", False)})

    def create_ticket(self, order_id: str, ticket_type: str = "expedite", description: str = "用户请求催发货") -> ToolResult:
        return self._call("create_ticket", order_id=order_id, ticket_type=ticket_type, description=description)

    def _create_ticket(self, order_id: str, ticket_type: str = "expedite", description: str = "用户请求催发货") -> ToolResult:
        order = self.db.get_order(order_id)
        if not order or order["customer_id"] != self.customer_id:
            return ToolResult(ok=False, error_code="ORDER_NOT_FOUND", error_message="订单不存在或不属于当前客户")
        if order["status"] != "pending":
            return ToolResult(ok=False, error_code="TICKET_NOT_NEEDED", error_message="订单已经发货，不创建无意义的催发货工单")
        return ToolResult(**self.db.create_ticket_transaction(order_id, self.customer_id, ticket_type, description))


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=32)


class PolicyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    category: str | None = None


class CancelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: str = Field(min_length=1, max_length=100)


class TicketArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str = Field(min_length=1, max_length=32)
    ticket_type: str = "expedite"
    description: str = Field(default="用户请求催发货", max_length=1000)


TOOL_ARGUMENTS: dict[str, type[BaseModel]] = {
    "list_my_orders": EmptyArgs,
    "get_order": OrderArgs,
    "get_tracking": OrderArgs,
    "search_policy": PolicyArgs,
    "propose_cancel": OrderArgs,
    "cancel_order": CancelArgs,
    "create_ticket": TicketArgs,
}

TOOL_HANDLERS = {
    "list_my_orders": "list_my_orders",
    "get_order": "get_order",
    "get_tracking": "get_tracking",
    "search_policy": "search_policy",
    "propose_cancel": "propose_cancel",
    "cancel_order": "cancel_order",
    "create_ticket": "create_ticket",
}

TOOL_DEFINITIONS = [
    {"type": "function", "function": {"name": "list_my_orders", "description": "列出当前客户的订单摘要", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_order", "description": "查询当前客户指定订单", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_tracking", "description": "查询当前客户订单物流", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "search_policy", "description": "检索取消或物流政策", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "category": {"type": ["string", "null"]}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "propose_cancel", "description": "检查取消条件并生成待确认方案，不能直接取消订单", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "create_ticket", "description": "为未发货订单创建催发货工单", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "ticket_type": {"type": "string"}, "description": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}},
]

ORDER_AGENT_TOOLS = {"list_my_orders", "get_order", "get_tracking"}
AFTERSALES_AGENT_TOOLS = {"search_policy", "propose_cancel", "create_ticket"}
