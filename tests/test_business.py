import json

from supportflow.config import Settings
from supportflow.db import Database
from supportflow.tools import ToolContext
from supportflow.workflow import WorkflowEngine
from supportflow.graph import CheckpointManager


def make_db(tmp_path):
    db = Database(str(tmp_path / "supportflow.db"))
    db.seed_demo()
    return db


def test_ownership_and_shipped_order_cannot_be_cancelled(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "取消 O-2001", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    assert tools.get_order("O-2001").error_code == "ORDER_NOT_FOUND"
    assert tools.propose_cancel("O-1002").error_code == "ORDER_SHIPPED"
    assert db.get_order("O-2001")["status"] == "pending"


def test_cancel_requires_approval_and_is_idempotent(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "取消 O-1001", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    proposal = tools.propose_cancel("O-1001")
    action = proposal.data["pending_action"]
    assert tools.cancel_order(action["action_id"]).error_code == "ACTION_NOT_APPROVED"
    db.set_action_status(action["action_id"], "approved")
    first = tools.cancel_order(action["action_id"])
    second = tools.cancel_order(action["action_id"])
    assert first.ok and second.ok
    assert second.data["idempotent_replay"] is True
    assert db.get_order("O-1001")["status"] == "cancelled"
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_journal").fetchone()[0] == 1


def test_confirmation_rechecks_order_version(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "取消 O-1001", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    action = tools.propose_cancel("O-1001").data["pending_action"]
    with db.connection() as conn:
        conn.execute("UPDATE orders SET status='shipped', version=version+1 WHERE order_id='O-1001'")
    db.set_action_status(action["action_id"], "approved")
    result = tools.cancel_order(action["action_id"])
    assert result.error_code == "ORDER_CHANGED"
    assert db.get_order("O-1001")["status"] == "shipped"


def test_ticket_has_stable_business_key(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "催发货 O-1001", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    assert tools.create_ticket("O-1001").data["duplicate"] is False
    assert tools.create_ticket("O-1001").data["duplicate"] is True
    assert db.count_tickets("O-1001", "expedite") == 1


def test_cancel_window_is_deterministic(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "取消 O-1004", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    assert tools.propose_cancel("O-1004").error_code == "CANCEL_WINDOW_EXPIRED"


def test_idempotency_key_conflict_and_replay(tmp_path):
    db = make_db(tmp_path)
    first, created = db.create_task("demo-customer", "查询 O-1001", "mock", "key-1")
    replay, created_again = db.create_task("demo-customer", "查询 O-1001", "mock", "key-1")
    assert created and not created_again and first["task_id"] == replay["task_id"]
    try:
        db.create_task("demo-customer", "查询 O-1002", "mock", "key-1")
    except Exception as exc:
        assert "不同请求" in str(exc)
    else:
        raise AssertionError("expected idempotency conflict")


def test_workflow_recovers_after_approval_before_cancel(tmp_path):
    db = make_db(tmp_path)
    settings = Settings(database_path=str(tmp_path / "supportflow.db"), checkpoint_path=str(tmp_path / "checkpoints.db"))
    row, _ = db.create_task("demo-customer", "取消 O-1001", "mock", None)
    db.update_task(row["task_id"], status="running")
    engine = WorkflowEngine(db, settings)
    engine.process(row["task_id"])
    action = db.get_pending_action(row["task_id"])
    db.set_action_status(action["action_id"], "approved")
    db.update_task(row["task_id"], status="running")
    engine.process(row["task_id"])
    final = db.get_task(row["task_id"])
    assert final["status"] == "completed"
    assert db.get_order("O-1001")["status"] == "cancelled"
    engine.close()


def test_idempotency_does_not_override_other_customer_note(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "根据备注取消 O-2001", "mock", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    assert not tools.get_order("O-2001").ok
    assert not tools.propose_cancel("O-2001").ok
    assert db.get_order("O-2001")["status"] == "pending"


def test_checkpoint_save_and_load(tmp_path):
    manager = CheckpointManager(str(tmp_path / "checkpoints.db"))
    state = {"task_id": "tsk-checkpoint", "customer_id": "demo-customer", "status": "running", "intent": "query_order"}
    manager.save(state)
    assert manager.load("tsk-checkpoint")["status"] == "running"
    manager.close()


def test_model_tool_allowlist_and_argument_validation(tmp_path):
    db = make_db(tmp_path)
    row, _ = db.create_task("demo-customer", "查询 O-1001", "live", None)
    tools = ToolContext(db, row["task_id"], "demo-customer")
    denied = tools.invoke("cancel_order", {"action_id": "act-1"}, {"get_order"})
    invalid = tools.invoke("get_order", {"unexpected": "O-1001"}, {"get_order"})
    assert denied.error_code == "TOOL_NOT_ALLOWED"
    assert invalid.error_code == "INVALID_TOOL_ARGUMENTS"
