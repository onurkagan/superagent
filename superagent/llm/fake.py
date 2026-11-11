"""A scripted model client for tests and offline demos."""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from typing import Any

from superagent.llm.client import ModelResponse

_ids = itertools.count(1)

Step = ModelResponse | Exception | Callable[[dict[str, Any]], ModelResponse]


def text(value: str) -> ModelResponse:
    return ModelResponse(content=[{"type": "text", "text": value}], stop_reason="end_turn")


def tool_call(name: str, **arguments: Any) -> ModelResponse:
    return tool_calls((name, arguments))


def tool_calls(*calls: tuple[str, dict[str, Any]]) -> ModelResponse:
    content = [{"type": "tool_use", "id": f"toolu_{next(_ids):04d}", "name": n, "input": a} for n, a in calls]
    return ModelResponse(content=content, stop_reason="tool_use")


class ScriptedModelClient:
    """Return the given responses in order. A step may be an exception to raise or a function of the request."""

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps: list[Step] = list(steps or [])
        self.requests: list[dict[str, Any]] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

    def create(self, **request: Any) -> ModelResponse:
        self.requests.append(copy.deepcopy(request))
        if not self.steps:
            raise AssertionError("the scripted model client ran out of responses")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step) and not isinstance(step, ModelResponse):
            return step(request)
        return step
