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
    try:
        db_path = temp_dir / "scenario.db"
        db = Database(str(db_path))
        db.seed_demo()
        settings = Settings(database_path=str(db_path), checkpoint_path=str(temp_dir / "checkpoint.db"), run_mode="mock")
        task, _ = db.create_task("demo-customer", scenario["message"], mode, f"eval-{scenario['id']}")
        db.update_task(task["task_id"], status="running")
        engine = WorkflowEngine(db, settings)
        engine.process(task["task_id"])
        current = db.get_task(task["task_id"])
        if current["status"] == "awaiting_confirmation" and scenario.get("auto_confirm", False):
            action = db.get_pending_action(task["task_id"])
            if action:
                db.append_event(task["task_id"], "confirmation", json.dumps({"action_id": action["action_id"], "decision": "approve"}))
                db.update_task(task["task_id"], status="running")
                engine.process(task["task_id"])
                current = db.get_task(task["task_id"])
        result = json.loads(current["result_json"]) if current.get("result_json") else {}
        passed = current["status"] == scenario.get("expected_status")
        if scenario.get("expected_order"):
            passed = passed and scenario["expected_order"] in json.dumps(result)
        if scenario.get("expected_code"):
            passed = passed and scenario["expected_code"] in json.dumps(result)
        if scenario.get("expected_ticket"):
            passed = passed and db.count_tickets("O-1001", "expedite") == 1
        trace = db.get_trace(task["task_id"])
        return {"id": scenario["id"], "mode": mode, "passed": passed, "status": current["status"], "result": result, "trace_events": len(trace), "tool_calls": sum(1 for item in trace if item["call_type"] == "tool"), "model_calls": sum(1 for item in trace if item["call_type"] == "model"), "error": current.get("error")}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    parser.add_argument("--mode", choices=["multi_agent", "single_agent", "both"], default="both")
    parser.add_argument("--dataset", default=str(ROOT / "evals" / "scenarios.json"))
    parser.add_argument("--output", default=str(ROOT / "reports"))
    args = parser.parse_args()
    scenarios = load_scenarios(Path(args.dataset))
    modes = ["multi_agent", "single_agent"] if args.mode == "both" else [args.mode]
    for mode in modes:
        results = [run_scenario(scenario, mode) for scenario in scenarios]
        write_reports(results, Path(args.output), mode)
        print(f"{mode}: {sum(item['passed'] for item in results)}/{len(results)}")


if __name__ == "__main__":
    main()
