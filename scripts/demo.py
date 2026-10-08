import json
import time

import httpx


BASE = "http://127.0.0.1:8000"


def wait_for(task_id: str) -> dict:
    for _ in range(100):
        task = httpx.get(f"{BASE}/tasks/{task_id}", timeout=5).json()
        if task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.1)
    raise TimeoutError(task_id)


def main() -> None:
    first = httpx.post(f"{BASE}/tasks", json={"message": "请查询订单 O-1002 的物流", "mode": "multi_agent"}, timeout=10).json()
    print("物流查询:", json.dumps(wait_for(first["task_id"]), ensure_ascii=False, indent=2))
    second = httpx.post(f"{BASE}/tasks", json={"message": "请取消订单 O-1001", "mode": "multi_agent"}, timeout=10).json()
    pending = wait_for(second["task_id"])
    print("等待确认:", json.dumps(pending, ensure_ascii=False, indent=2))
    if pending.get("pending_action"):
        confirmed = httpx.post(
            f"{BASE}/tasks/{second['task_id']}/confirmations",
            json={"action_id": pending["pending_action"]["action_id"], "decision": "approve"},
            timeout=10,
        ).json()
        print("提交确认:", json.dumps(wait_for(confirmed["task_id"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
