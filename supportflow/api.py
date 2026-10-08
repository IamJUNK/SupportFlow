import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query

from .config import Settings, get_settings
from .db import ConflictError, Database
from .schemas import ConfirmationCreate, EventCreate, HealthView, TaskCreate, TaskView
from .workflow import WorkflowEngine


DEMO_CUSTOMER = "demo-customer"


class TaskExecutor:
    def __init__(self, db: Database, engine: WorkflowEngine):
        self.db = db
        self.engine = engine
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.db.recover_running_tasks()
        self.task = asyncio.create_task(self._run(), name="supportflow-executor")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            await self.task

    async def _run(self) -> None:
        while not self.stop_event.is_set():
            task_id = await asyncio.to_thread(self.db.claim_next_task)
            if task_id:
                await asyncio.to_thread(self.engine.process, task_id)
            else:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.15)
                except asyncio.TimeoutError:
                    pass


def _task_view(db: Database, row: dict) -> TaskView:
    pending = db.get_pending_action(row["task_id"])
    result = json.loads(row["result_json"]) if row.get("result_json") else None
    if pending and pending["status"] == "pending":
        pending_view = {key: pending[key] for key in ("action_id", "task_id", "order_id", "operation", "parameters", "order_version", "policy_version", "status", "created_at")}
    else:
        pending_view = None
    return TaskView(task_id=row["task_id"], customer_id=row["customer_id"], status=row["status"], mode=row["mode"], message=row["message"], intent=row.get("intent"), pending_action=pending_view, result=result, error=row.get("error"), created_at=row["created_at"], updated_at=row["updated_at"])


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path)
    db.seed_demo()
    engine = WorkflowEngine(db, settings)
    executor = TaskExecutor(db, engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await executor.start()
        try:
            yield
        finally:
            await executor.stop()
            engine.close()

    app = FastAPI(title="SupportFlow", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.executor = executor

    @app.get("/health", response_model=HealthView)
    async def health() -> HealthView:
        return HealthView(ok=True, run_mode=settings.run_mode, live_configured=settings.live_configured, worker="single-process")

    @app.post("/tasks", response_model=TaskView, status_code=202)
    async def create_task(payload: TaskCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> TaskView:
        mode = payload.mode
        if mode == "multi_agent" and settings.run_mode.lower() == "mock":
            mode = "multi_agent"
        try:
            row, _ = db.create_task(DEMO_CUSTOMER, payload.message, mode, idempotency_key)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_view(db, row)

    @app.get("/tasks", response_model=list[TaskView])
    async def list_tasks(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> list[TaskView]:
        return [_task_view(db, row) for row in db.list_tasks(limit, offset)]

    @app.get("/tasks/{task_id}", response_model=TaskView)
    async def get_task(task_id: str) -> TaskView:
        row = db.get_task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _task_view(db, row)

    @app.post("/tasks/{task_id}/events", response_model=TaskView, status_code=202)
    async def add_event(task_id: str, payload: EventCreate) -> TaskView:
        row = db.get_task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        if row["status"] not in ("awaiting_input", "failed", "completed"):
            raise HTTPException(status_code=409, detail=f"当前状态 {row['status']} 不接受补充事件")
        db.append_event(task_id, payload.type, payload.content)
        db.update_task(task_id, status="queued", error=None)
        return _task_view(db, db.get_task(task_id))

    @app.post("/tasks/{task_id}/confirmations", response_model=TaskView, status_code=202)
    async def confirm(task_id: str, payload: ConfirmationCreate) -> TaskView:
        row = db.get_task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        action = db.get_pending_action(task_id, payload.action_id)
        if not action:
            raise HTTPException(status_code=404, detail="待确认方案不存在或不属于当前任务")
        if action["status"] != "pending":
            return _task_view(db, row)
        db.append_event(task_id, "confirmation", json.dumps(payload.model_dump(), ensure_ascii=False))
        db.update_task(task_id, status="queued")
        return _task_view(db, db.get_task(task_id))

    @app.get("/tasks/{task_id}/trace")
    async def trace(task_id: str) -> list[dict]:
        if not db.get_task(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        return db.get_trace(task_id)

    return app


app = create_app()

