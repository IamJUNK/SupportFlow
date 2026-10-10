"""Executable policies shared by proposal and transaction-time validation."""
from datetime import datetime, timezone
from typing import Any


def utc_datetime(value: str | None = None) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise ValueError("业务时间必须包含时区")
    return result.astimezone(timezone.utc)


def cancel_error(order: Any, customer_id: str, policy_version: str, now: str | None = None, expected_version: int | None = None) -> dict | None:
    if not order or order["customer_id"] != customer_id:
        return failure("ORDER_NOT_FOUND", "订单不存在或不属于当前客户")
    if expected_version is not None and order["version"] != expected_version:
        return failure("ORDER_CHANGED", "等待确认期间订单状态已经变化")
    if order["status"] != "pending":
        return failure("ORDER_SHIPPED" if order["status"] == "shipped" else "ORDER_NOT_CANCELLABLE", f"订单当前状态为 {order['status']}，不能取消")
    if policy_version != "v1":
        return failure("POLICY_UNSUPPORTED", "该政策版本尚未实现，不能执行取消")
    age = (utc_datetime(now) - utc_datetime(order["created_at"])).total_seconds()
    if age < 0:
        return failure("INVALID_ORDER_TIME", "订单创建时间晚于当前时间")
    if age > 86400:
        return failure("CANCEL_WINDOW_EXPIRED", "订单已超过 24 小时取消窗口")
    return None


def failure(code: str, message: str) -> dict:
    return {"ok": False, "error_code": code, "error_message": message}
