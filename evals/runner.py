"""Run deterministic mock scenarios in isolated SQLite databases.

This runner validates database effects, not just response text. It is deliberately
usable without a model API key; live runs should be a separate, explicitly budgeted job.
"""
import argparse
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from supportflow.config import Settings
from supportflow.db import Database
from supportflow.workflow import WorkflowEngine


def run_scenario(scenario: dict[str, Any], mode: str) -> dict[str, Any]:
    temp_dir = Path(tempfile.mkdtemp(prefix="supportflow-eval-"))
    engine = None
    try:
        db_path = temp_dir / "scenario.db"
        db = Database(str(db_path))
        clock = "2026-10-10T09:00:00Z"
        db.seed_demo(clock)
        initial_orders = {order["order_id"]: db.get_order(order["order_id"]) for order in db.list_orders("demo-customer") + db.list_orders("other-customer")}
        settings = Settings(_env_file=None, database_path=str(db_path), checkpoint_path=str(temp_dir / "checkpoint.db"), run_mode="mock")
        task, _ = db.create_task("demo-customer", scenario["message"], mode, f"eval-{scenario['id']}")
        assert db.claim_next_task() == task["task_id"]
        engine = WorkflowEngine(db, settings, clock=clock)
        engine.process(task["task_id"])
        current = db.get_task(task["task_id"])
        for supplement in scenario.get("supplements", []):
            if current["status"] != "awaiting_input":
                break
            db.submit_event(task["task_id"], "supplement", supplement)
            assert db.claim_next_task() == task["task_id"]
            engine.process(task["task_id"])
            current = db.get_task(task["task_id"])
        if current["status"] == "awaiting_confirmation" and scenario.get("auto_confirm", False):
            action = db.get_pending_action(task["task_id"])
            if action:
                db.confirm_action(task["task_id"], action["action_id"], scenario.get("decision", "approve"))
                assert db.claim_next_task() == task["task_id"]
                engine.process(task["task_id"])
                current = db.get_task(task["task_id"])
        result = json.loads(current["result_json"]) if current.get("result_json") else {}
        checks = {"status": current["status"] == scenario["expected_status"], "technical_error": current.get("error") is None}
        if scenario.get("expected_business_status"):
            checks["business_status"] = result.get("business_status") == scenario["expected_business_status"]
        if scenario.get("expected_order"):
            checks["order"] = result.get("order", {}).get("order_id") == scenario["expected_order"]
        if scenario.get("expected_code"):
            codes = [result.get("error_code"), result.get("ticket", {}).get("error_code"), result.get("cancel", {}).get("error_code")]
            checks["error_code"] = scenario["expected_code"] in codes
        changed_order = scenario.get("cancelled_order")
        for order_id, before in initial_orders.items():
            after = db.get_order(order_id)
            expected = {**before, "status": "cancelled", "version": before["version"] + 1, "refund_required": int(bool(before["paid"]))} if order_id == changed_order else before
            checks[f"order_state:{order_id}"] = after == expected
        with db.connection() as conn:
            journals = [dict(row) for row in conn.execute("SELECT * FROM operation_journal")]
            tickets = [dict(row) for row in conn.execute("SELECT * FROM tickets")]
        checks["journal_count"] = len(journals) == (1 if changed_order else 0)
        if changed_order:
            checks["cancel_success"] = result.get("cancel", {}).get("ok") is True
            checks["journal_result"] = len(journals) == 1 and json.loads(journals[0]["result_json"]).get("ok") is True
        expected_ticket = scenario.get("ticket_order", "O-1001" if scenario.get("expected_ticket") else None)
        checks["ticket_count"] = len(tickets) == (1 if expected_ticket else 0)
        if expected_ticket:
            checks["ticket_binding"] = len(tickets) == 1 and (tickets[0]["order_id"], tickets[0]["customer_id"], tickets[0]["type"]) == (expected_ticket, "demo-customer", "expedite")
        action = db.get_pending_action(task["task_id"])
        if scenario["expected_status"] == "awaiting_confirmation":
            checks["pending_action"] = bool(action and action["status"] == "pending" and action["order_id"] == scenario.get("action_order", "O-1001"))
        elif not scenario.get("auto_confirm"):
            checks["no_cancel_proposal"] = action is None
        passed = all(checks.values())
        trace = db.get_trace(task["task_id"])
        return {"id": scenario["id"], "mode": mode, "passed": passed, "checks": checks, "status": current["status"], "result": result, "trace_events": len(trace), "tool_calls": sum(1 for item in trace if item["call_type"] == "tool"), "model_calls": sum(1 for item in trace if item["call_type"] == "model"), "error": current.get("error")}
    finally:
        if engine is not None:
            engine.close()
        shutil.rmtree(temp_dir)


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["development"] + data["held_out"]


def write_reports(results: list[dict[str, Any]], output_dir: Path, mode: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{mode}.json"
    json_path.write_text(json.dumps({"mode": mode, "passed": sum(item["passed"] for item in results), "total": len(results), "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = output_dir / f"{mode}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "mode", "passed", "status", "trace_events", "tool_calls", "model_calls", "error"])
        writer.writeheader()
        writer.writerows({key: item[key] for key in writer.fieldnames} for item in results)
    passed = sum(item["passed"] for item in results)
    (output_dir / f"{mode}.md").write_text(f"# SupportFlow {mode} mock evaluation\n\n通过数：{passed}/{len(results)}\n\n结果文件：`{json_path.name}`、`{csv_path.name}`。验证基于 SQLite 最终业务状态。\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["multi_agent", "single_agent", "both"], default="single_agent", help="multi_agent/both 仅验证旧参数兼容，不代表多 Agent 对比")
    parser.add_argument("--dataset", default=str(ROOT / "evals" / "scenarios.json"))
    parser.add_argument("--output", default=str(ROOT / "reports"))
    args = parser.parse_args()
    scenarios = load_scenarios(Path(args.dataset))
    modes = ["multi_agent", "single_agent"] if args.mode == "both" else [args.mode]
    all_passed = True
    for mode in modes:
        results = [run_scenario(scenario, mode) for scenario in scenarios]
        write_reports(results, Path(args.output), mode)
        print(f"{mode}: {sum(item['passed'] for item in results)}/{len(results)}")
        all_passed = all_passed and all(item["passed"] for item in results)
    if not all_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
