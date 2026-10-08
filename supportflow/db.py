import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="milliseconds") + "Z"


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


class ConflictError(Exception):
    pass


class Database:
    """Small SQLite repository. Each operation owns a short-lived connection."""

    def __init__(self, path: str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._init_lock, self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    paid INTEGER NOT NULL DEFAULT 1,
                    refund_required INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tracking (
                    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policies (
                    policy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(policy_id, version)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    message TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    intent TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idempotency
                    ON tasks(customer_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_actions (
                    action_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    order_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    order_version INTEGER NOT NULL,
                    policy_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_task_open
                    ON pending_actions(task_id) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS operation_journal (
                    operation_key TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    business_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trace_events (
                    trace_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    node TEXT NOT NULL,
                    role TEXT,
                    call_type TEXT NOT NULL,
                    name TEXT,
                    input_summary TEXT,
                    output_summary TEXT,
                    duration_ms INTEGER,
                    retry INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT,
                    usage_json TEXT,
                    cost_json TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trace_events)")}
            if "usage_json" not in columns:
                conn.execute("ALTER TABLE trace_events ADD COLUMN usage_json TEXT")
            if "cost_json" not in columns:
                conn.execute("ALTER TABLE trace_events ADD COLUMN cost_json TEXT")

    def seed_demo(self) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO orders(order_id,customer_id,status,created_at,version,paid,note) VALUES(?,?,?,?,?,?,?)",
                    [
                        ("O-1001", "demo-customer", "pending", "2026-10-07T09:00:00Z", 1, 1, "普通订单"),
                        ("O-1002", "demo-customer", "shipped", "2026-10-01T09:00:00Z", 2, 1, "已交给承运商"),
                        ("O-1003", "demo-customer", "cancelled", "2026-10-02T09:00:00Z", 2, 1, "用户已取消"),
                        ("O-1004", "demo-customer", "pending", "2026-10-05T09:00:00Z", 1, 1, "超过取消窗口"),
                        ("O-2001", "other-customer", "pending", "2026-10-07T10:00:00Z", 1, 1, "另一位客户订单"),
                    ],
                )
                conn.executemany(
                    "INSERT INTO tracking(order_id,status,updated_at) VALUES(?,?,?)",
                    [("O-1001", "not_shipped", "2026-10-07T09:00:00Z"), ("O-1002", "in_transit", "2026-10-06T12:00:00Z")],
                )
            if conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO policies(policy_id,version,category,content) VALUES(?,?,?,?)",
                    [
                        ("cancel-v1", "v1", "cancel", "订单尚未发货且创建后 24 小时内可以取消；已付款订单取消只标记 refund_required，退款流程尚未实现。"),
                        ("shipping-v1", "v1", "shipping", "未发货订单可以创建一次催发货工单；已发货订单不创建催发货工单。"),
                    ],
                )
            conn.commit()

    def create_task(self, customer_id: str, message: str, mode: str, idempotency_key: str | None) -> tuple[dict[str, Any], bool]:
        request_hash = hashlib.sha256(json_text({"message": message, "mode": mode}).encode()).hexdigest()
        task_id = "tsk-" + uuid.uuid4().hex[:16]
        now = utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE customer_id=? AND idempotency_key=?",
                    (customer_id, idempotency_key),
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise ConflictError("Idempotency-Key 已用于不同请求")
                    return dict(row), False
            conn.execute(
                "INSERT INTO tasks(task_id,customer_id,status,mode,message,request_hash,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, customer_id, "queued", mode, message, request_hash, idempotency_key, now, now),
            )
            conn.commit()
            return dict(conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()), True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))]

    def update_task(self, task_id: str, **fields: Any) -> None:
        fields["updated_at"] = utc_now()
        allowed = {"status", "intent", "result_json", "error", "updated_at"}
        fields = {key: value for key, value in fields.items() if key in allowed}
        if not fields:
            return
        sql = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as conn:
            conn.execute(f"UPDATE tasks SET {sql} WHERE task_id=?", (*fields.values(), task_id))

    def claim_next_task(self) -> str | None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                conn.commit()
                return None
            conn.execute("UPDATE tasks SET status='running', updated_at=? WHERE task_id=? AND status='queued'", (utc_now(), row["task_id"]))
            conn.commit()
            return row["task_id"]

    def recover_running_tasks(self) -> int:
        with self.connection() as conn:
            cur = conn.execute("UPDATE tasks SET status='queued', updated_at=? WHERE status='running'", (utc_now(),))
            return cur.rowcount

    def append_event(self, task_id: str, event_type: str, content: str) -> str:
        event_id = "evt-" + uuid.uuid4().hex[:16]
        with self.connection() as conn:
            conn.execute("INSERT INTO task_events(event_id,task_id,event_type,content,created_at) VALUES(?,?,?,?,?)", (event_id, task_id, event_type, content, utc_now()))
        return event_id

    def unprocessed_events(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM task_events WHERE task_id=? AND processed=0 ORDER BY created_at", (task_id,)).fetchall()
            return [dict(row) for row in rows]

    def mark_events_processed(self, task_id: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE task_events SET processed=1 WHERE task_id=?", (task_id,))

    def create_pending_action(self, task_id: str, order_id: str, operation: str, parameters: dict[str, Any], order_version: int, policy_version: str) -> dict[str, Any]:
        existing = self.get_pending_action(task_id)
        if existing and existing["status"] == "pending":
            return existing
        action_id = "act-" + uuid.uuid4().hex[:16]
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO pending_actions(action_id,task_id,order_id,operation,parameters_json,order_version,policy_version,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (action_id, task_id, order_id, operation, json_text(parameters), order_version, policy_version, "pending", utc_now()),
            )
        return self.get_pending_action(task_id)  # type: ignore[return-value]

    def get_pending_action(self, task_id: str, action_id: str | None = None) -> dict[str, Any] | None:
        with self.connection() as conn:
            if action_id:
                row = conn.execute("SELECT * FROM pending_actions WHERE task_id=? AND action_id=?", (task_id, action_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM pending_actions WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["parameters"] = json.loads(result.pop("parameters_json"))
            return result

    def set_action_status(self, action_id: str, status: str) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE pending_actions SET status=?, decided_at=? WHERE action_id=?", (status, utc_now(), action_id))

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            return dict(row) if row else None

    def list_orders(self, customer_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT order_id,status,created_at,version FROM orders WHERE customer_id=? ORDER BY created_at DESC", (customer_id,))]

    def get_tracking(self, order_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM tracking WHERE order_id=?", (order_id,)).fetchone()
            return dict(row) if row else None

    def search_policies(self, query: str, category: str | None = None) -> list[dict[str, Any]]:
        tokens = [token.lower() for token in query.split() if token]
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM policies WHERE (? IS NULL OR category=?)", (category, category)).fetchall()
            scored = []
            for row in rows:
                content = row["content"].lower()
                score = sum(1 for token in tokens if token in content)
                scored.append((score, dict(row)))
            return [item for _, item in sorted(scored, key=lambda item: item[0], reverse=True)[:3]]

    def trace(self, task_id: str, node: str, role: str | None, call_type: str, name: str | None, input_summary: Any, output_summary: Any, duration_ms: int = 0, retry: int = 0, error_type: str | None = None, usage: Any = None, cost: Any = None) -> None:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO trace_events(trace_id,task_id,node,role,call_type,name,input_summary,output_summary,duration_ms,retry,error_type,usage_json,cost_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("trc-" + uuid.uuid4().hex[:16], task_id, node, role, call_type, name, json_text(input_summary)[:2000], json_text(output_summary)[:4000], duration_ms, retry, error_type, json_text(usage) if usage is not None else None, json_text(cost) if cost is not None else None, utc_now()),
            )

    def get_trace(self, task_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM trace_events WHERE task_id=? ORDER BY created_at", (task_id,))]

    def operation_result(self, operation_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT result_json FROM operation_journal WHERE operation_key=?", (operation_key,)).fetchone()
            return json.loads(row[0]) if row else None

    def cancel_order_transaction(self, operation_key: str, order_id: str, customer_id: str, expected_version: int) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            journal = conn.execute("SELECT result_json FROM operation_journal WHERE operation_key=?", (operation_key,)).fetchone()
            if journal:
                conn.commit()
                result = json.loads(journal[0])
                result["idempotent_replay"] = True
                return result
            order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not order or order["customer_id"] != customer_id:
                result = {"ok": False, "error_code": "ORDER_NOT_FOUND", "error_message": "订单不存在或不属于当前客户"}
            elif order["version"] != expected_version:
                result = {"ok": False, "error_code": "ORDER_CHANGED", "error_message": "等待确认期间订单状态已经变化"}
            elif order["status"] != "pending":
                result = {"ok": False, "error_code": "ORDER_SHIPPED" if order["status"] == "shipped" else "ORDER_NOT_CANCELLABLE", "error_message": f"订单当前状态为 {order['status']}，不能取消"}
            else:
                refund_required = bool(order["paid"])
                conn.execute("UPDATE orders SET status='cancelled', version=version+1, refund_required=? WHERE order_id=? AND version=?", (int(refund_required), order_id, expected_version))
                result = {"ok": True, "data": {"order_id": order_id, "status": "cancelled", "refund_required": refund_required}, "idempotent_replay": False}
            conn.execute("INSERT INTO operation_journal(operation_key,operation_type,parameters_json,result_json,created_at) VALUES(?,?,?,?,?)", (operation_key, "cancel_order", json_text({"order_id": order_id, "customer_id": customer_id, "expected_version": expected_version}), json_text(result), utc_now()))
            conn.commit()
            return result

    def create_ticket_transaction(self, order_id: str, customer_id: str, ticket_type: str, description: str) -> dict[str, Any]:
        business_key = f"{order_id}:{ticket_type}"
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM tickets WHERE business_key=?", (business_key,)).fetchone()
            if existing:
                conn.commit()
                return {"ok": True, "data": {"ticket_id": existing["ticket_id"], "duplicate": True}}
            order = conn.execute("SELECT customer_id FROM orders WHERE order_id=?", (order_id,)).fetchone()
            if not order or order["customer_id"] != customer_id:
                conn.commit()
                return {"ok": False, "error_code": "ORDER_NOT_FOUND", "error_message": "订单不存在或不属于当前客户"}
            ticket_id = "tic-" + uuid.uuid4().hex[:16]
            conn.execute("INSERT INTO tickets(ticket_id,order_id,customer_id,type,description,status,business_key,created_at) VALUES(?,?,?,?,?,?,?,?)", (ticket_id, order_id, customer_id, ticket_type, description[:1000], "open", business_key, utc_now()))
            conn.commit()
            return {"ok": True, "data": {"ticket_id": ticket_id, "duplicate": False}}

    def count_tickets(self, order_id: str, ticket_type: str) -> int:
        with self.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM tickets WHERE order_id=? AND type=?", (order_id, ticket_type)).fetchone()[0]

