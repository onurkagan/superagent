import pytest

from superagent.core.model import ChannelRef, Mandate, Subscription, Thread, Ticket, Trigger
from superagent.store import Store


def make_ticket(tid="tk_1", state="open"):
    return Ticket(id=tid, name="fix", title="Fix", decision_id="d", origin_thread="t", origin_trigger="x",
                  mandate=Mandate("p", "explicit", "allowlist", "scope", "alice", "github"),
                  channels=[ChannelRef("github", "issue:1")], state=state)


def test_put_and_get_round_trip(tmp_path):
    store = Store(tmp_path / "state.db")
    trigger = Trigger(id="github:c:1", source="github", kind="comment", actor="alice", text="hi",
                      threads=("github:o/r#1",), channels=(ChannelRef("github", "issue:1"),))
    store.put(trigger)
    assert store.get(Trigger, "github:c:1") == trigger
    assert store.get(Trigger, "missing") is None


def test_thread_is_keyed_by_key_and_tagged_active_when_queued():
    store = Store()
    store.put(Thread(key="github:o/r#1", source="github", subscriptions=[Subscription("tk_1")]))
    store.put(Thread(key="github:o/r#2", source="github", queued=["t1"]))
    assert [t.key for t in store.list(Thread, tag="active")] == ["github:o/r#2"]
    assert store.get(Thread, "github:o/r#1").subscriptions == [Subscription("tk_1")]


def test_tickets_are_listed_by_state():
    store = Store()
    store.put(make_ticket("tk_1", "open"))
    store.put(make_ticket("tk_2", "ended"))
    assert [t.id for t in store.list(Ticket, tag="open")] == ["tk_1"]
    assert store.count(Ticket) == 2


def test_atomic_rolls_back_on_error():
    store = Store()
    with pytest.raises(RuntimeError):
        with store.atomic():
            store.put(make_ticket("tk_1"))
            raise RuntimeError("boom")
    assert store.get(Ticket, "tk_1") is None


def test_nested_atomic_commits_once():
    store = Store()
    with store.atomic():
        store.put(make_ticket("tk_1"))
        with store.atomic():
            store.put(make_ticket("tk_2"))
    assert store.count(Ticket) == 2


def test_kv_and_journal():
    store = Store()
    assert store.kv_get("cursor", "none") == "none"
    store.kv_set("cursor", {"since": "2026-01-01"})
    assert store.kv_get("cursor") == {"since": "2026-01-01"}
    store.journal("ticket.created", {"ticket": "tk_1"})
    assert store.events()[0]["type"] == "ticket.created"


def test_command_queue_is_drained_in_order():
    store = Store()
    store.enqueue_command("stop", ticket="tk_1")
    store.enqueue_command("pause")
    assert store.take_commands() == [("stop", {"ticket": "tk_1"}), ("pause", {})]
    assert store.take_commands() == []
