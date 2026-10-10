import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Response

from .config import Settings, get_settings
from .db import ConflictError, Database
from .schemas import ConfirmationCreate, EventCreate, HealthView, TaskCreate, TaskView
from .workflow import WorkflowEngine

DEMO_CUSTOMER = "demo-customer"
logger = logging.getLogger(__name__)


class TaskExecutor:
    def __init__(self, db: Database, engine: WorkflowEngine):
        self.db, self.engine = db, engine
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_error: str | None = None

    @property
    def alive(self) -> bool:
        return self.task is not None and not self.task.done() and not self.stop_event.is_set()

    async def start(self) -> None:
        self.stop_event.clear()
        await asyncio.to_thread(self.db.recover_running_tasks)
        self.task = asyncio.create_task(self._run(), name="supportflow-executor")

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task:
            await self.task

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _run(self) -> None:
        failures = 0
        unresolved_task = None
        while not self.stop_event.is_set():
            task_id = None
            try:
                if unresolved_task:
                    await asyncio.to_thread(self.db.update_task, unresolved_task, status="failed", result_json=None, error="执行器异常，请检查日志并重试")
                    unresolved_task = None
                task_id = await asyncio.to_thread(self.db.claim_next_task)
                if task_id:
                    await asyncio.to_thread(self.engine.process, task_id)
                self.last_error, failures = None, 0
                if not task_id:
                    await self._wait(0.15)
            except Exception as exc:
                self.last_error = type(exc).__name__
                failures += 1
                logger.exception("Executor iteration failed for %s", task_id)
                if task_id:
                    try:
                        await asyncio.to_thread(self.db.update_task, task_id, status="failed", result_json=None, error="执行器异常，请检查日志")
                    except Exception:
                        unresolved_task = task_id
                        logger.exception("Could not record task failure")
                await self._wait(min(0.25 * 2 ** min(failures - 1, 4), 3))


def _task_view(db: Database, row: dict) -> TaskView:
    pending = db.get_pending_action(row["task_id"])
    result = json.loads(row["result_json"]) if row.get("result_json") else None
    pending_view = WorkflowEngine._action_view(pending) if pending and pending["status"] == "pending" else None
    return TaskView(task_id=row["task_id"], customer_id=row["customer_id"], status=row["status"], mode=row["mode"], execution_mode=row.get("execution_mode"), agent_mode=row.get("agent_mode", "single_agent"), message=row["message"], intent=row.get("intent"), pending_action=pending_view, result=result, error=row.get("error"), created_at=row["created_at"], updated_at=row["updated_at"])


def create_app(settings: Settings | None = None) -> FastAPI:
    # Importing the module never seeds data or opens long-lived database handles.
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or get_settings()
        config.ensure_dirs()
        db = Database(config.database_path)
        db.seed_demo()
        engine = WorkflowEngine(db, config)
        executor = TaskExecutor(db, engine)
        app.state.settings, app.state.db, app.state.executor = config, db, executor
        try:
            await executor.start()
            yield
        finally:
            try:
                await executor.stop()
            finally:
                engine.close()

    app = FastAPI(title="SupportFlow", version="0.2.0", lifespan=lifespan)

    @app.get("/health", response_model=HealthView)
    def health(response: Response) -> HealthView:
        config, executor = app.state.settings, app.state.executor
        try:
            with app.state.db.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            database_ok = True
        except Exception:
            database_ok = False
        ok = database_ok and executor.alive and executor.last_error is None
        if not ok:
            response.status_code = 503
        return HealthView(ok=ok, run_mode=config.run_mode, live_configured=config.live_configured, worker="single-process", worker_alive=executor.alive, database_ok=database_ok)

    @app.post("/tasks", response_model=TaskView, status_code=202)
    def create_task(payload: TaskCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=200)) -> TaskView:
        config, db = app.state.settings, app.state.db
        execution = payload.resolved_execution_mode(config.run_mode)
        mode = payload.mode or payload.agent_mode
        try:
            row, _ = db.create_task(DEMO_CUSTOMER, payload.message, mode, idempotency_key, execution_mode=execution)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_view(db, row)

    @app.get("/tasks", response_model=list[TaskView])
    def list_tasks(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0)) -> list[TaskView]:
        db = app.state.db
        return [_task_view(db, row) for row in db.list_tasks(limit, offset)]

    @app.get("/tasks/{task_id}", response_model=TaskView)
    def get_task(task_id: str) -> TaskView:
        db = app.state.db
        row = db.get_task(task_id)
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _task_view(db, row)

    @app.post("/tasks/{task_id}/events", response_model=TaskView, status_code=202)
    def add_event(task_id: str, payload: EventCreate) -> TaskView:
        db = app.state.db
        try:
            row = db.submit_event(task_id, payload.type, payload.content)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_view(db, row)

    @app.post("/tasks/{task_id}/confirmations", response_model=TaskView, status_code=202)
    def confirm(task_id: str, payload: ConfirmationCreate) -> TaskView:
        db = app.state.db
        try:
            row = db.confirm_action(task_id, payload.action_id, payload.decision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="方案不存在或不属于当前任务") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_view(db, row)

    @app.post("/tasks/{task_id}/retry", response_model=TaskView, status_code=202)
    def retry(task_id: str) -> TaskView:
        db = app.state.db
        try:
            row = db.retry_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _task_view(db, row)

    @app.get("/tasks/{task_id}/trace")
    def trace(task_id: str) -> list[dict]:
        db = app.state.db
        if not db.get_task(task_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        return db.get_trace(task_id)

    return app


app = create_app()
