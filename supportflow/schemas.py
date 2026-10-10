from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=8000)
    execution_mode: Literal["mock", "live"] | None = None
    agent_mode: Literal["single_agent"] = "single_agent"
    mode: Literal["mock", "live", "multi_agent", "single_agent"] | None = None

    @model_validator(mode="after")
    def consistent_modes(self):
        if self.mode in {"mock", "live"} and self.execution_mode and self.mode != self.execution_mode:
            raise ValueError("mode 与 execution_mode 冲突")
        return self

    def resolved_execution_mode(self, default: str) -> str:
        return self.execution_mode or (self.mode if self.mode in {"mock", "live"} else default)


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["user_message", "order_selection", "supplement"] = "supplement"
    content: str = Field(min_length=1, max_length=8000)


class ConfirmationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    intent: Literal["query_order", "cancel_order", "create_ticket", "unknown"] = "unknown"
    order_id: str | None = Field(default=None, pattern=r"^O-[0-9]{4}$")
    missing_fields: list[Literal["intent", "order_id"]] = Field(default_factory=list)
    reason_summary: str = Field(default="", max_length=500)


class TaskView(BaseModel):
    task_id: str
    customer_id: str
    status: str
    mode: str
    execution_mode: str | None = None
    agent_mode: str = "single_agent"
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
    worker_alive: bool = False
    database_ok: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
