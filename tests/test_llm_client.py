import types

import anthropic
import httpx2
import pytest

from superagent.llm.client import AnthropicModelClient, ModelError, ModelResponse, TransientModelError
from superagent.llm.fake import ScriptedModelClient, text, tool_call


class FakeMessage:
    def __init__(self, data):
        self.data = data

    def to_dict(self, mode="python"):
        return self.data


def client_with(create):
    client = AnthropicModelClient.__new__(AnthropicModelClient)
    client._anthropic = anthropic
    client.fallbacks = "default"
    client._client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=types.SimpleNamespace(create=create)),
                                           messages=types.SimpleNamespace(create=create))
    return client


def test_request_uses_adaptive_thinking_effort_caching_and_fallbacks():
    seen = {}

    def create(**params):
        seen.update(params)
        return FakeMessage({"content": [{"type": "text", "text": "{\"ok\": true}"}], "stop_reason": "end_turn",
                            "usage": {"input_tokens": 3}})

    response = client_with(create).create(model="claude-opus-5-5", system="sys", messages=[], effort="low",
                                          output_schema={"type": "object"})
    assert response.json() == {"ok": True}
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["fallbacks"] == "default" and seen["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tools" not in seen


def _status_error(status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request)
    return anthropic.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize("status,kind", [(429, TransientModelError), (529, TransientModelError),
                                         (400, ModelError)])
def test_status_errors_are_classified(status, kind):
    def create(**params):
        raise _status_error(status)

    with pytest.raises(kind) as info:
        client_with(create).create(model="m", system="s", messages=[])
    assert (info.type is TransientModelError) == (kind is TransientModelError)


def test_refusal_category_is_read_from_stop_details():
    def create(**params):
        return FakeMessage({"content": [], "stop_reason": "refusal", "stop_details": {"category": "cyber"}})

    assert client_with(create).create(model="m", system="s", messages=[]).refusal_category == "cyber"


def test_scripted_client_replays_steps_and_records_requests():
    client = ScriptedModelClient([tool_call("bash", command="ls"), text("done")])
    first = client.create(model="m", system="s", messages=[{"role": "user", "content": "hi"}])
    assert first.tool_uses()[0]["input"] == {"command": "ls"}
    assert client.create(model="m", system="s", messages=[]).text() == "done"
    assert client.requests[0]["messages"][0]["content"] == "hi"
    with pytest.raises(AssertionError):
        client.create(model="m", system="s", messages=[])


def test_model_response_helpers():
    response = ModelResponse(content=[{"type": "thinking", "thinking": ""}, {"type": "text", "text": "hi"}],
                             stop_reason="end_turn")
    assert response.text() == "hi" and response.tool_uses() == []
