"""Convert domain dataclasses to JSON-ready dicts and back.

The store keeps every record as JSON; these helpers rebuild nested dataclasses, lists, tuples and optionals from the
type hints of the target class, so model classes need no hand-written (de)serialization code.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from functools import cache
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")


def to_dict(obj: Any) -> Any:
    """Return ``obj`` as plain JSON-compatible data."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    return obj


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def from_dict(cls: type[T], data: dict[str, Any]) -> T:
    """Build ``cls`` from ``data``; unknown keys are ignored and missing keys take the field defaults."""
    hints = _hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in data:
            kwargs[f.name] = _convert(hints[f.name], data[f.name])
    return cls(**kwargs)


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    args = get_args(tp)
    if origin in (Union, types.UnionType):
        options = [a for a in args if a is not type(None)]
        for option in options:
            if dataclasses.is_dataclass(option) and isinstance(value, dict):
                return from_dict(option, value)
        return _convert(options[0], value) if len(options) == 1 else value
    if dataclasses.is_dataclass(tp) and isinstance(value, dict):
        return from_dict(tp, value)
    if origin is list:
        return [_convert(args[0], v) for v in value] if args else list(value)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_convert(args[0], v) for v in value)
        if args:
            return tuple(_convert(a, v) for a, v in zip(args, value))
        return tuple(value)
    if origin is dict:
        return {k: _convert(args[1], v) for k, v in value.items()} if args else dict(value)
    if origin is typing.Literal:
        return value
    return value
