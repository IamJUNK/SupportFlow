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

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.calls += 1
        if not self.settings.live_configured:
            raise ModelUnavailable("live 模式需要 LLM_API_KEY 和 LLM_MODEL；当前请使用 RUN_MODE=mock")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelUnavailable("未安装 openai 客户端") from exc
        started = time.perf_counter()
        client = OpenAI(api_key=self.settings.llm_api_key, base_url=self.settings.llm_base_url, timeout=self.settings.model_timeout_seconds)
        response = client.chat.completions.create(model=self.settings.llm_model, messages=messages, tools=tools or [])
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

    def complete_with_retry(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = max(0, self.settings.max_model_retries) + 1
        for attempt in range(attempts):
            try:
                response = self.complete(messages, tools)
                response["retry"] = attempt
                return response
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(0.25 * (2**attempt), 1.0))
        raise ModelUnavailable(f"模型请求失败（已尝试 {attempts} 次）：{last_error}") from last_error
