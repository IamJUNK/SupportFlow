import asyncio
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIConnectionError

from supportflow.api import TaskExecutor, create_app
from supportflow.config import Settings
from supportflow.db import ConflictError, Database
from supportflow.model import ModelClient, ModelUnavailable
from supportflow.tools import AFTERSALES_AGENT_TOOLS, ToolContext
from supportflow.workflow import WorkflowEngine

NOW = "2026-10-10T09:00:00Z"


@pytest.fixture
def system(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "app.db"), checkpoint_path=str(tmp_path / "checkpoint.db"))
    db = Database(settings.database_path)
    db.seed_demo(NOW)
    engine = WorkflowEngine(db, settings, clock=NOW)
    yield db, engine, settings
    engine.close()


def new_task(system, message, mode="mock"):
    db, _, _ = system
    return db.create_task("demo-customer", message, mode, None)[0]["task_id"]


def run(system, task_id):
    db, engine, _ = system
    claimed = db.claim_next_task()
    assert claimed == task_id
    engine.process(task_id)
    row = db.get_task(task_id)
    row["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    return row


def stub_decision(monkeypatch, intent, order_id, captured=None, **extra):
    def complete(self, messages, tools, *, deadline):
        if captured is not None:
            captured.extend(messages)
        self.calls += 1
        return {"content": "模型声称成功也不决定业务状态", "tool_calls": [{"id": "decision-1", "name": "submit_decision", "arguments": {"intent": intent, "order_id": order_id, **extra}}]}
    monkeypatch.setattr(ModelClient, "complete_with_retry", complete)


@pytest.mark.parametrize(("message", "status", "intent"), [
    ("O-1001", "awaiting_input", "unknown"),
    ("请查 O-1001", "completed", "query_order"),
    ("不要取消 O-1001，只查询状态", "completed", "query_order"),
    ("不取消 O-1001", "awaiting_input", "unknown"),
    ("查询订单O-1001的物流", "completed", "query_order"),
    ("取消 O-1001 或者催发货", "awaiting_input", "unknown"),
    ("查询 O-1001 和 O-1002", "awaiting_input", "query_order"),
    ("CANCEL O-1001", "awaiting_confirmation", "cancel_order"),
])
def test_routing_is_explicit(system, message, status, intent):
    task_id = new_task(system, message)
    result = run(system, task_id)
    assert (result["status"], result["intent"]) == (status, intent)
    if intent != "cancel_order":
        assert system[0].get_pending_action(task_id) is None
    assert system[0].get_order("O-1001")["status"] == "pending"


def test_three_turn_context_survives_restart(system):
    db, engine, settings = system
    task_id = new_task(system, "帮我处理订单")
    assert run(system, task_id)["status"] == "awaiting_input"
    db.submit_event(task_id, "supplement", "催发货")
    assert run(system, task_id)["intent"] == "create_ticket"
    db.submit_event(task_id, "order_selection", "O-1001")
    assert db.claim_next_task() == task_id
    restarted = WorkflowEngine(db, settings, clock=NOW)
    try:
        restarted.process(task_id)
    finally:
        restarted.close()
    assert db.get_task(task_id)["status"] == "completed"
    assert db.count_tickets("O-1001", "expedite") == 1
    assert db.get_pending_action(task_id) is None


def test_correction_replaces_order_and_terminal_rejects_events(system):
    db, _, _ = system
    task_id = new_task(system, "O-9999")
    assert run(system, task_id)["status"] == "awaiting_input"
    db.submit_event(task_id, "supplement", "订单更正为 O-1001，请查询")
    result = run(system, task_id)
    assert result["result"]["order"]["order_id"] == "O-1001"
    with pytest.raises(ConflictError):
        db.submit_event(task_id, "supplement", "取消")


def test_confirmation_is_atomic_idempotent_and_conflicting(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    action = db.get_pending_action(task_id)
    row = db.confirm_action(task_id, action["action_id"], "approve")
    assert row["status"] == "queued"
    db.confirm_action(task_id, action["action_id"], "approve")
    with pytest.raises(ConflictError):
        db.confirm_action(task_id, action["action_id"], "reject")
    assert len(db.task_events(task_id)) == 1
    result = run(system, task_id)
    assert result["result"]["cancel"]["ok"]
    assert db.get_order("O-1001")["version"] == 2
    assert db.confirm_action(task_id, action["action_id"], "approve")["status"] == "completed"
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_journal").fetchone()[0] == 1


def test_confirmation_transaction_rolls_back_on_queue_failure(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    action = db.get_pending_action(task_id)
    with db.connection() as conn:
        conn.execute("CREATE TRIGGER fail_queue BEFORE UPDATE OF status ON tasks WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.confirm_action(task_id, action["action_id"], "approve")
    assert db.get_pending_action(task_id)["status"] == "pending"
    assert db.task_events(task_id) == []
    assert db.get_task(task_id)["status"] == "awaiting_confirmation"


def test_event_insert_and_queue_roll_back_together(system):
    db, _, _ = system
    task_id = new_task(system, "查询订单")
    run(system, task_id)
    with db.connection() as conn:
        conn.execute("CREATE TRIGGER fail_queue BEFORE UPDATE OF status ON tasks WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.submit_event(task_id, "order_selection", "O-1001")
    assert db.task_events(task_id) == []
    assert db.get_task(task_id)["status"] == "awaiting_input"


def test_concurrent_opposing_confirmations_have_one_winner(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    action_id = db.get_pending_action(task_id)["action_id"]
    def confirm(decision):
        try:
            db.confirm_action(task_id, action_id, decision)
            return decision
        except ConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [item for item in pool.map(confirm, ["approve", "reject"]) if item]
    assert len(winners) == 1
    run(system, task_id)
    assert db.get_order("O-1001")["status"] == ("cancelled" if winners[0] == "approve" else "pending")


def test_acknowledge_only_consumed_events(system):
    db, _, _ = system
    task_id = new_task(system, "查询订单")
    first = db.append_event(task_id, "supplement", "第一条")
    second = db.append_event(task_id, "supplement", "第二条")
    db.mark_events_processed(task_id, [first])
    assert [event["event_id"] for event in db.unprocessed_events(task_id)] == [second]


def test_approved_operation_recovers_after_commit_before_task_result(system, monkeypatch):
    db, engine, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    db.confirm_action(task_id, db.get_pending_action(task_id)["action_id"], "approve")
    original = db.finish_task
    def crash(*args, **kwargs):
        raise SystemExit("simulated process exit")
    monkeypatch.setattr(db, "finish_task", crash)
    with pytest.raises(SystemExit):
        run(system, task_id)
    assert db.get_order("O-1001")["status"] == "cancelled"
    assert db.get_task(task_id)["status"] == "running"
    monkeypatch.setattr(db, "finish_task", original)
    assert db.recover_running_tasks() == 1
    assert run(system, task_id)["result"]["cancel"]["data"]["idempotent_replay"]
    assert db.get_order("O-1001")["version"] == 2


def test_expiry_is_checked_again_at_execution(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    tools = ToolContext(db, task_id, "demo-customer", clock=NOW)
    action = tools.propose_cancel("O-1001").data["pending_action"]
    db.set_action_status(action["action_id"], "approved")
    late = ToolContext(db, task_id, "demo-customer", clock="2026-10-11T09:00:01Z")
    assert late.cancel_order(action["action_id"]).error_code == "CANCEL_WINDOW_EXPIRED"
    assert db.get_order("O-1001")["status"] == "pending"


def test_policy_version_and_ticket_type_are_enforced(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    unsupported = ToolContext(db, task_id, "demo-customer", policy_version="v999", clock=NOW)
    assert unsupported.propose_cancel("O-1001").error_code == "POLICY_UNSUPPORTED"
    tools = ToolContext(db, task_id, "demo-customer")
    assert tools.create_ticket("O-1001").ok
    normalized = tools.invoke("create_ticket", {"order_id": "O-1001", "ticket_type": " EXPEDITE "}, AFTERSALES_AGENT_TOOLS)
    assert normalized.ok and normalized.data["duplicate"]
    assert not tools.create_ticket("O-1001", "refund").ok
    assert db.count_tickets("O-1001", "expedite") == 1


def test_ticket_checks_ownership_and_status_inside_transaction(system):
    db, _, _ = system
    assert db.create_ticket_transaction("O-1002", "demo-customer", "expedite", "催发货")["error_code"] == "TICKET_NOT_NEEDED"
    assert db.create_ticket_transaction("O-1001", "demo-customer", "expedite", "催发货")["ok"]
    assert db.create_ticket_transaction("O-1001", "other-customer", "expedite", "催发货")["error_code"] == "ORDER_NOT_FOUND"


def test_live_reads_supplements_and_rejects_by_policy(system, monkeypatch):
    db, _, _ = system
    captured = []
    task_id = new_task(system, "取消订单", "live")
    stub_decision(monkeypatch, "cancel_order", None)
    assert run(system, task_id)["status"] == "awaiting_input"
    db.submit_event(task_id, "order_selection", "O-1002")
    stub_decision(monkeypatch, "cancel_order", "O-1002", captured)
    result = run(system, task_id)
    assert any(item["content"] == "O-1002" for item in captured)
    assert result["result"]["business_status"] == "rejected_by_policy"
    assert result["result"]["error_code"] == "ORDER_SHIPPED"
    assert db.unprocessed_events(task_id) == []


def test_live_plain_text_cannot_mean_success(system, monkeypatch):
    monkeypatch.setattr(ModelClient, "complete_with_retry", lambda *args, **kwargs: {"content": "已经成功取消", "tool_calls": []})
    task_id = new_task(system, "取消 O-1001", "live")
    assert run(system, task_id)["status"] == "awaiting_input"
    assert system[0].get_order("O-1001")["status"] == "pending"


def test_live_cannot_override_negated_cancellation(system, monkeypatch):
    stub_decision(monkeypatch, "cancel_order", "O-1001")
    task_id = new_task(system, "不要取消 O-1001，只查询状态", "live")
    assert run(system, task_id)["status"] == "awaiting_input"
    assert system[0].get_pending_action(task_id) is None


def test_single_agent_uses_live_when_configured(system, monkeypatch):
    captured = []
    system[2].run_mode = "live"
    stub_decision(monkeypatch, "query_order", "O-1001", captured)
    task_id = new_task(system, "查询 O-1001", "single_agent")
    assert run(system, task_id)["status"] == "completed"
    assert captured and system[0].get_task(task_id)["execution_mode"] == "live"


def test_deadline_prevents_post_response_side_effects(system, monkeypatch):
    system[2].task_timeout_seconds = 0.03
    def slow(*args, **kwargs):
        time.sleep(0.04)
        return {"tool_calls": [{"id": "1", "name": "submit_decision", "arguments": {"intent": "create_ticket", "order_id": "O-1001"}}]}
    monkeypatch.setattr(ModelClient, "complete_with_retry", slow)
    task_id = new_task(system, "催发货 O-1001", "live")
    assert run(system, task_id)["status"] == "failed"
    assert system[0].count_tickets("O-1001", "expedite") == 0


def test_model_retries_respect_call_budget(monkeypatch):
    client = ModelClient(Settings(_env_file=None, max_model_calls=1, max_model_retries=3))
    def fail(messages, tools, *, timeout):
        client.calls += 1
        raise APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))
    monkeypatch.setattr(client, "complete", fail)
    with pytest.raises(ModelUnavailable):
        client.complete_with_retry([], [], deadline=time.monotonic() + 10)
    assert client.calls == 1


def test_example_configuration_loads_without_price_values():
    config = Settings(_env_file=Path(__file__).parents[1] / ".env.example")
    assert config.llm_input_price_per_1m is None
    assert config.llm_output_price_per_1m is None
    assert Settings(_env_file=None, llm_input_price_per_1m="").llm_input_price_per_1m is None


def wait_task(client, task_id):
    for _ in range(150):
        result = client.get(f"/tasks/{task_id}").json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    pytest.fail("background task did not finish")


def test_api_full_confirmation_and_health(tmp_path):
    settings = Settings(_env_file=None, database_path=str(tmp_path / "api.db"), checkpoint_path=str(tmp_path / "checkpoint.db"))
    app = create_app(settings)
    assert not Path(settings.database_path).exists()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        payload = {"message": "取消 O-1001", "execution_mode": "mock", "agent_mode": "single_agent"}
        created = client.post("/tasks", json=payload, headers={"Idempotency-Key": "api-1"})
        assert created.status_code == 202
        task_id = created.json()["task_id"]
        assert client.post("/tasks", json=payload, headers={"Idempotency-Key": "api-1"}).json()["task_id"] == task_id
        pending = wait_task(client, task_id)
        action = pending["pending_action"]["action_id"]
        assert client.post(f"/tasks/{task_id}/confirmations", json={"action_id": action, "decision": "approve"}).status_code == 202
        final = wait_task(client, task_id)
        assert final["result"]["cancel"]["ok"]
        assert client.post(f"/tasks/{task_id}/confirmations", json={"action_id": action, "decision": "reject"}).status_code == 409
        assert client.post(f"/tasks/{task_id}/events", json={"content": "查询 O-1002"}).status_code == 409
        app.state.executor.last_error = "injected"
        app.state.executor.stop_event.set()
        assert client.get("/health").status_code == 503


def test_executor_survives_one_database_failure():
    class FlakyDatabase:
        calls = 0
        def recover_running_tasks(self):
            return 0
        def claim_next_task(self):
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("locked")
            return None
    async def exercise():
        worker = TaskExecutor(FlakyDatabase(), object())
        await worker.start()
        for _ in range(50):
            if worker.db.calls > 1:
                break
            await asyncio.sleep(0.02)
        assert worker.alive and worker.db.calls > 1 and worker.last_error is None
        await worker.stop()
    asyncio.run(exercise())


def test_rejected_action_allows_a_new_task(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    db.confirm_action(task_id, db.get_pending_action(task_id)["action_id"], "reject")
    assert run(system, task_id)["result"]["business_status"] == "rejected_by_user"
    following = new_task(system, "查询 O-1002 物流")
    assert run(system, following)["result"]["order"]["order_id"] == "O-1002"


def test_retry_uses_saved_decision_after_ticket_commits(system, monkeypatch):
    db, _, _ = system
    captured = []
    stub_decision(monkeypatch, "create_ticket", "O-1001", captured)
    task_id = new_task(system, "催发货 O-1001", "live")
    original = db.finish_task
    def fail_once(task_id, status, *args, **kwargs):
        if status == "completed":
            raise RuntimeError("result write failed after ticket commit")
        return original(task_id, status, *args, **kwargs)
    monkeypatch.setattr(db, "finish_task", fail_once)
    assert run(system, task_id)["status"] == "failed"
    assert db.count_tickets("O-1001", "expedite") == 1
    with pytest.raises(ConflictError):
        db.submit_event(task_id, "supplement", "改为取消")
    monkeypatch.setattr(db, "finish_task", original)
    def forbidden(*args, **kwargs):
        pytest.fail("retry must not replan a persisted operation")
    monkeypatch.setattr(ModelClient, "complete_with_retry", forbidden)
    db.retry_task(task_id)
    result = run(system, task_id)
    assert result["status"] == "completed"
    assert result["result"]["ticket"]["data"]["duplicate"]
    assert db.count_tickets("O-1001", "expedite") == 1


def test_legacy_confirmation_event_recovers_after_enqueue_gap(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    action_id = db.get_pending_action(task_id)["action_id"]
    db.append_event(task_id, "confirmation", json.dumps({"action_id": action_id, "decision": "approve"}))
    db.recover_running_tasks()
    assert db.get_pending_action(task_id)["status"] == "approved"
    assert run(system, task_id)["result"]["cancel"]["ok"]


def test_legacy_conflicting_confirmations_require_fresh_decision(system):
    db, _, _ = system
    task_id = new_task(system, "取消 O-1001")
    run(system, task_id)
    action_id = db.get_pending_action(task_id)["action_id"]
    for decision in ["approve", "reject"]:
        db.append_event(task_id, "confirmation", json.dumps({"action_id": action_id, "decision": decision}))
    db.recover_running_tasks()
    assert db.get_task(task_id)["status"] == "awaiting_confirmation"
    assert db.claim_next_task() is None
    db.confirm_action(task_id, action_id, "reject")
    run(system, task_id)
    assert db.get_order("O-1001")["status"] == "pending"


def test_default_clock_uses_real_time_and_seed_never_refreshes_existing_orders(tmp_path):
    db = Database(str(tmp_path / "old.db"))
    db.seed_demo("2020-01-01T00:00:00Z")
    before = db.get_order("O-1001")
    db.seed_demo()
    assert db.get_order("O-1001") == before
    task, _ = db.create_task("demo-customer", "取消 O-1001", "mock", None)
    assert ToolContext(db, task["task_id"], "demo-customer").propose_cancel("O-1001").error_code == "CANCEL_WINDOW_EXPIRED"


def test_schema_upgrade_is_additive_and_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,status TEXT NOT NULL,mode TEXT NOT NULL,message TEXT NOT NULL,request_hash TEXT NOT NULL,idempotency_key TEXT,intent TEXT,result_json TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
        conn.execute("INSERT INTO tasks VALUES('old','demo-customer','completed','mock','original','hash',NULL,NULL,NULL,NULL,'old','old')")
    db = Database(path)
    db.initialize()
    row = db.get_task("old")
    assert row["message"] == "original" and row["status"] == "completed"
    assert row["agent_mode"] == "single_agent" and row["context_json"] is None


def test_evaluation_rejects_completed_without_cancellation(monkeypatch):
    from evals import runner
    class BrokenEngine:
        def __init__(self, db, settings, clock=None):
            self.db = db
        def process(self, task_id):
            self.db.update_task(task_id, status="completed", result_json=json.dumps({"business_status": "accepted"}))
        def close(self):
            pass
    monkeypatch.setattr(runner, "WorkflowEngine", BrokenEngine)
    scenario = {"id": "false-positive", "message": "取消 O-1001", "expected_status": "completed", "auto_confirm": True, "cancelled_order": "O-1001"}
    result = runner.run_scenario(scenario, "single_agent")
    assert not result["passed"]
    assert not result["checks"]["order_state:O-1001"]
    assert not result["checks"]["journal_count"]


def test_real_sdk_uses_project_retry_budget_and_structured_decision(monkeypatch):
    import openai
    from supportflow.workflow import DECISION_TOOL
    real_client = openai.OpenAI
    requests = []
    options = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "submit_decision", "arguments": json.dumps({"intent": "query_order", "order_id": "O-1001"})}}]}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    transport = httpx.MockTransport(handler)
    def factory(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, http_client=httpx.Client(transport=transport))
    monkeypatch.setattr(openai, "OpenAI", factory)
    client = ModelClient(Settings(_env_file=None, llm_api_key="test-only", llm_model="test-model"))
    result = client.complete_with_retry([{"role": "user", "content": "查询 O-1001"}], [DECISION_TOOL], deadline=time.monotonic() + 2)
    assert client.calls == 1 and options[0]["max_retries"] == 0
    assert 0 < options[0]["timeout"] <= 2
    assert requests[0]["tool_choice"]["function"]["name"] == "submit_decision"
    assert result["tool_calls"][0]["arguments"]["order_id"] == "O-1001"
    assert result["usage"]["total_tokens"] == 15


def test_live_unknown_input_cannot_create_a_ticket(system, monkeypatch):
    stub_decision(monkeypatch, "create_ticket", "O-1001")
    task_id = new_task(system, "O-1001", "live")
    assert run(system, task_id)["status"] == "awaiting_input"
    assert system[0].count_tickets("O-1001", "expedite") == 0


def test_live_without_configuration_is_technical_failure(system):
    system[2].llm_api_key = None
    system[2].llm_model = None
    task_id = new_task(system, "查询 O-1001", "live")
    assert run(system, task_id)["status"] == "failed"
    assert any(row["call_type"] == "model" and row["error_type"] for row in system[0].get_trace(task_id))
