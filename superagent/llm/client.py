"""Calling Claude through the Anthropic SDK.

The rest of superagent depends only on :class:`ModelClient`, so tests use a scripted client and another provider can
be plugged in by implementing the same method. Messages and content blocks are plain dicts in the Messages API
shape; responses are converted to dicts so they can be stored and replayed unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelError(Exception):
    """A model call failed in a way retrying will not fix, such as an invalid request or missing credentials."""


class TransientModelError(ModelError):
    """A passing failure: a rate limit, an overloaded or failing server, or a network problem."""


@dataclass
class ModelResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, Any] = field(default_factory=dict)
    refusal_category: str = ""

    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def json(self) -> Any:
        """Parse the text of a response that was constrained by an output schema."""
        return json.loads(self.text())


class ModelClient(Protocol):
    def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        effort: str = "high",
        max_tokens: int = 16000,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelResponse: ...


class AnthropicModelClient:
    """A :class:`ModelClient` backed by the official ``anthropic`` SDK.

    It uses adaptive thinking, the effort level the caller asks for, prompt caching of the system prompt and the
    conversation, and, unless ``fallbacks`` is empty, the server-side refusal fallback so a declined request is
    continued by a fallback model inside the same call.
    """

    def __init__(self, base_url: str = "", fallbacks: str = "default", timeout: float = 600.0,
                 max_retries: int = 2) -> None:
        import anthropic  # imported lazily so the domain and tests do not need the SDK

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(base_url=base_url or None, timeout=timeout, max_retries=max_retries)
        self.fallbacks = fallbacks

    def create(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        effort: str = "high",
        max_tokens: int = 16000,
        output_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        output_config: dict[str, Any] = {"effort": effort}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            params["tools"] = tools
        anthropic = self._anthropic
        try:
            if self.fallbacks:
                response = self._client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks=self.fallbacks)
            else:
                response = self._client.messages.create(**params)
        except anthropic.APIConnectionError as exc:
            raise TransientModelError(f"cannot reach the model API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            message = f"model API returned {exc.status_code}: {exc.message}"
            if exc.status_code in (408, 409, 429) or exc.status_code >= 500:
                raise TransientModelError(message) from exc
            raise ModelError(message) from exc
        except anthropic.AnthropicError as exc:
            raise ModelError(str(exc)) from exc
        data = response.to_dict(mode="json")
        details = data.get("stop_details") or {}
        return ModelResponse(
            content=list(data.get("content") or []),
            stop_reason=str(data.get("stop_reason") or ""),
            usage=dict(data.get("usage") or {}),
            refusal_category=str(details.get("category") or "") if isinstance(details, dict) else "",
        )
