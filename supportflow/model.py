import json
import time
from typing import Any

from .config import Settings


class ModelUnavailable(RuntimeError):
    pass


class ModelClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        if not self.settings.live_configured:
            raise ModelUnavailable("live 模式需要 LLM_API_KEY 和 LLM_MODEL；当前请使用 RUN_MODE=mock")
        if self.calls >= self.settings.max_model_calls:
            raise ModelUnavailable("超过模型调用预算")
        self.calls += 1
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelUnavailable("未安装 openai 客户端") from exc
        started = time.perf_counter()
        with OpenAI(api_key=self.settings.llm_api_key, base_url=self.settings.llm_base_url, timeout=timeout or self.settings.model_timeout_seconds, max_retries=0) as client:
            response = client.chat.completions.create(model=self.settings.llm_model, messages=messages, tools=tools or [], tool_choice={"type": "function", "function": {"name": "submit_decision"}})
        choice = response.choices[0].message
        return {
            "content": choice.content or "",
            "tool_calls": [
                {"name": call.function.name, "arguments": json.loads(call.function.arguments or "{}"), "id": call.id}
                for call in (choice.tool_calls or [])
            ],
            "usage": getattr(response, "usage", None).model_dump() if getattr(response, "usage", None) else None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    def complete_with_retry(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, *, deadline: float | None = None) -> dict[str, Any]:
        deadline = deadline if deadline is not None else time.monotonic() + self.settings.task_timeout_seconds
        last_error: Exception | None = None
        attempts = min(self.settings.max_model_retries + 1, self.settings.max_model_calls - self.calls)
        if attempts <= 0:
            raise ModelUnavailable("超过模型调用预算")
        attempted = 0
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelUnavailable("任务超过总执行时间预算")
            try:
                attempted += 1
                response = self.complete(messages, tools, timeout=min(self.settings.model_timeout_seconds, remaining))
                if time.monotonic() >= deadline:
                    raise ModelUnavailable("任务超过总执行时间预算")
                response["retry"] = attempt
                return response
            except Exception as exc:
                last_error = exc
                from openai import APIConnectionError, APIStatusError
                retryable = isinstance(exc, APIConnectionError) or (isinstance(exc, APIStatusError) and (exc.status_code in {408, 409, 429} or exc.status_code >= 500))
                if not retryable or attempt + 1 >= attempts:
                    break
                delay = min(0.25 * (2**attempt), 1.0)
                if delay >= deadline - time.monotonic():
                    break
                time.sleep(delay)
        raise ModelUnavailable(f"模型请求失败（已尝试 {attempted} 次）：{last_error}") from last_error
