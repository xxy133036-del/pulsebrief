import httpx
import pytest
from app.agent.provider import ModelUnavailable, OpenAICompatibleProvider
from app.config import Settings


def _settings() -> Settings:
    return Settings(
        demo_mode=False,
        auth_secret="test-secret-not-for-production-0123456789",
        llm_api_key="test-key",
        llm_model="test-model",
        llm_base_url="https://llm.example.test/v1",
    )


def test_provider_enables_parallel_tool_calls(monkeypatch):
    requests = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "done"}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, _url, **kwargs):
            requests.append(kwargs["json"])
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)
    OpenAICompatibleProvider(_settings()).complete([{"role": "user", "content": "test"}])

    assert requests[0]["tool_choice"] == "auto"
    assert requests[0]["parallel_tool_calls"] is True


def test_provider_retries_connection_failure_before_request_is_sent(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "done"}}]}

    class FakeClient:
        def __init__(self, **_kwargs):
            self.attempts = 0

        def post(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.ConnectTimeout("transient connect failure")
            return FakeResponse()

        def close(self):
            return None

    clients = []

    def create_client(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr("app.agent.provider.httpx.Client", create_client)
    provider = OpenAICompatibleProvider(_settings())

    result = provider.complete([{"role": "user", "content": "test"}])

    assert result["message"]["content"] == "done"
    assert clients[0].attempts == 2


@pytest.mark.parametrize(
    ("response", "expected_reason"),
    [
        ({"choices": []}, "model_invalid_response"),
        ({"choices": [{"message": None}]}, "model_invalid_response"),
    ],
)
def test_provider_rejects_malformed_success_responses(monkeypatch, response, expected_reason):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return response

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(_settings())

    with pytest.raises(ModelUnavailable) as error:
        provider.complete([{"role": "user", "content": "test"}])

    assert error.value.reason == expected_reason


def test_provider_reports_timeouts_with_safe_reason(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("private request details")

        def close(self):
            return None

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(_settings())

    with pytest.raises(ModelUnavailable) as error:
        provider.complete([{"role": "user", "content": "test"}])

    assert error.value.reason == "model_timeout"
    assert error.value.retryable is True
    assert "private request details" not in str(error.value)


def test_provider_classifies_rate_limits_without_exposing_response_body(monkeypatch):
    class FakeResponse:
        status_code = 429

        def raise_for_status(self):
            request = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")
            response = httpx.Response(429, request=request, text="private vendor details")
            raise httpx.HTTPStatusError("rate limit", request=request, response=response)

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(_settings())

    with pytest.raises(ModelUnavailable) as error:
        provider.complete([{"role": "user", "content": "test"}])

    assert error.value.reason == "model_rate_limited"
    assert error.value.retryable is True
    assert "private vendor details" not in str(error.value)


@pytest.mark.parametrize(
    ("status", "body", "reason", "retryable"),
    [
        (503, {"error": {"code": "server_error"}}, "model_server_error", True),
        (401, {"error": {"code": "invalid_api_key"}}, "model_auth_error", False),
        (
            429,
            {"error": {"code": "insufficient_quota"}},
            "model_quota_exhausted",
            False,
        ),
        (400, {"error": {"code": "Arrearage"}}, "model_quota_exhausted", False),
    ],
)
def test_provider_classifies_transient_permanent_and_quota_errors(
    monkeypatch, status, body, reason, retryable
):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://llm.example.test/v1/chat/completions")
            response = httpx.Response(status, request=request, json=body)
            response.raise_for_status()
            return response

        def close(self):
            return None

    monkeypatch.setattr("app.agent.provider.httpx.Client", FakeClient)
    provider = OpenAICompatibleProvider(_settings())

    with pytest.raises(ModelUnavailable) as error:
        provider.complete([{"role": "user", "content": "test"}])

    assert error.value.reason == reason
    assert error.value.retryable is retryable
