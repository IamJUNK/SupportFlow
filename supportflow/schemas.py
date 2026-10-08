from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class TaskCreate(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    mode: Literal["mock", "live", "multi_agent", "single_agent"] = "multi_agent"


class EventCreate(BaseModel):
    type: Literal["user_message", "order_selection", "supplement"] = "supplement"
    content: str = Field(min_length=1, max_length=8000)


class ConfirmationCreate(BaseModel):
    action_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject"]


class PendingAction(BaseModel):
    action_id: str
    task_id: str
    order_id: str
    operation: str
    parameters: dict[str, Any]
    order_version: int
    policy_version: str
    status: str
    created_at: str


class AgentDecision(BaseModel):
    intent: Literal["query_order", "cancel_order", "create_ticket", "unknown"] = "unknown"
    next_agent: Literal["order", "aftersales", "clarify", "finish", "handoff"] = "clarify"
    order_id: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    reason_summary: str = ""


class TaskView(BaseModel):
    task_id: str
    customer_id: str
    status: str
    mode: str
    message: str
    intent: str | None = None
    pending_action: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class HealthView(BaseModel):
    ok: bool
    run_mode: str
    live_configured: bool
    worker: str


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
