from dataclasses import dataclass, field
from typing import Literal

from superagent.core.serde import from_dict, to_dict


@dataclass(frozen=True)
class Ref:
    source: str
    address: str


@dataclass
class Record:
    id: str
    refs: tuple[Ref, ...] = ()
    items: list[Ref] = field(default_factory=list)
    lookup: dict[str, Ref] = field(default_factory=dict)
    maybe: Ref | None = None
    kind: Literal["a", "b"] = "a"
    count: int = 0


def test_round_trip_nested_dataclasses():
    record = Record(
        id="r1",
        refs=(Ref("github", "issue:1"),),
        items=[Ref("inbox", "main")],
        lookup={"x": Ref("github", "pr:2")},
        maybe=Ref("github", "issue:3"),
        kind="b",
        count=4,
    )
    data = to_dict(record)
    assert data["refs"] == [{"source": "github", "address": "issue:1"}]
    assert from_dict(Record, data) == record


def test_missing_keys_take_defaults_and_unknown_keys_are_ignored():
    record = from_dict(Record, {"id": "r2", "unknown": True})
    assert record == Record(id="r2")


def test_none_stays_none():
    assert from_dict(Record, {"id": "r3", "maybe": None}).maybe is None
