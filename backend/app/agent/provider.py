from __future__ import annotations

import json

import httpx

from app.agent.tools import tool_definitions
from app.config import Settings


class ModelUnavailable(RuntimeError):
    def __init__(
        self, message: str, *, reason: str = "model_unavailable", retryable: bool = False
    ):
        super().__init__(message)
        self.reason = reason
        self.retryable = retryable


class OpenAICompatibleProvider:
    """Small OpenAI Chat Completions adapter; the model chooses every tool call."""

    provider_name = "openai_compatible"

    def __init__(self, settings: Settings):
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise ModelUnavailable(
                "Configure LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL for real-model mode."
            )
        self.settings = settings
        self.client = httpx.Client(
            timeout=httpx.Timeout(
                settings.llm_timeout_seconds,
                connect=min(30, settings.llm_timeout_seconds),
            )
        )

    def complete(self, messages: list[dict], *, allow_tools: bool = True) -> dict:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.settings.llm_model, "messages": messages, "temperature": 0.15}
        if self.settings.llm_model.casefold().startswith("qwen3.8-"):
            payload["enable_thinking"] = self.settings.llm_enable_thinking
            payload["max_completion_tokens"] = self.settings.llm_max_completion_tokens
        if allow_tools:
            payload["tools"] = tool_definitions()
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = True
        try:
            for connect_attempt in range(2):
                try:
                    response = self.client.post(
                        url,
                        headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                        json=payload,
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if connect_attempt == 1:
                        raise
            response.raise_for_status()
            body = response.json()
            choices = body.get("choices") if isinstance(body, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise ValueError("The response does not contain a valid choice.")
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError("The response choice does not contain a valid message.")
            return {
                "message": message,
                "finish_reason": choice.get("finish_reason", ""),
                "usage": body.get("usage", {}),
            }
        except httpx.TimeoutException as exc:
            raise ModelUnavailable(
                "The model endpoint timed out.", reason="model_timeout", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error_code = ""
            try:
                body = exc.response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(error, dict):
                    error_code = str(error.get("code", "")).casefold()
            except ValueError:
                pass
            if error_code in {
                "arrearage",
                "insufficient_quota",
                "billing_hard_limit_reached",
            }:
                reason, retryable = "model_quota_exhausted", False
            elif status == 429:
                reason, retryable = "model_rate_limited", True
            elif status in {408, 425} or status >= 500:
                reason, retryable = "model_server_error", True
            elif status in {401, 403}:
                reason, retryable = "model_auth_error", False
            else:
                reason, retryable = "model_request_error", False
            raise ModelUnavailable(
                f"The model endpoint returned HTTP {status}.", reason=reason, retryable=retryable
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(
                "The model endpoint could not be reached.",
                reason="model_network_error",
                retryable=True,
            ) from exc
        except (ValueError, KeyError, IndexError) as exc:
            raise ModelUnavailable(
                "The configured model endpoint returned an invalid response.",
                reason="model_invalid_response",
            ) from exc

    def close(self) -> None:
        self.client.close()


class DemoProvider:
    """Clearly labeled offline stand-in. It is never claimed as LLM-selected behavior."""

    provider_name = "demo"

    def __init__(self, articles: list[dict], subscription: dict):
        self.articles = articles
        self.subscription = subscription
        self.done = False

    def complete(self, messages: list[dict], *, allow_tools: bool = True) -> dict:
        if not self.articles:
            return {
                "message": {"role": "assistant", "content": self._final_json()},
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }
        if self.done or not allow_tools:
            return {
                "message": {"role": "assistant", "content": self._final_json()},
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }
        self.done = True
        query = (
            " ".join(self.subscription.get("topics", []) + self.subscription.get("keywords", []))
            or "AI"
        )
        call = {"name": "search_news", "arguments": {"query": query}}
        return {
            "message": {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "demo-call-1",
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
            "usage": {},
        }

    def _final_json(self) -> str:
        if not self.articles:
            result = {
                "status": "no_match",
                "title": "今天没有匹配的演示素材",
                "summary": "当前体验数据没有可用于本次偏好的内容。",
                "items": [],
            }
        else:
            result = {
                "status": "published",
                "title": "AI 每日简报（离线演示）",
                "summary": "下列内容为本地演示素材，用于验证简报页面和工具路径；不是实时新闻。",
                "items": [
                    {
                        "article_id": article["id"],
                        "summary": article.get("summary", ""),
                        "reason": "演示素材：用于检查简报条目、来源链接和阅读状态。",
                        "certainty": "demo",
                    }
                    for article in self.articles[: self.subscription["item_limit"]]
                ],
            }
        return json.dumps(result, ensure_ascii=False)
