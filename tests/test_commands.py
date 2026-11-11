import threading

from superagent.commands import call, is_error, respond
from superagent.store import Store


def test_call_waits_for_the_outcome_the_runtime_stores():
    store = Store()

    def runtime():
        while True:
            commands = store.take_commands()
            if commands:
                kind, args = commands[0]
                respond(store, args, f"done: {kind} {args['ticket']}")
                return

    thread = threading.Thread(target=runtime)
    thread.start()
    assert call(store, "retry", ticket="tk_1", timeout=5, poll=0.01) == "done: retry tk_1"
    thread.join()
    assert store.query("SELECT COUNT(*) AS n FROM kv WHERE key LIKE 'command-reply:%'")[0]["n"] == 0


def test_call_gives_up_when_nobody_answers():
    store = Store()
    ticks = iter(range(100))
    assert call(store, "pause", timeout=3, sleep=lambda _: None, clock=lambda: next(ticks)) is None
    [(kind, args)] = store.take_commands()
    assert kind == "pause" and args["_reply"].startswith("cr_")


def test_respond_without_a_reply_id_stores_nothing_and_errors_are_recognised():
    store = Store()
    respond(store, {"ticket": "tk_1"}, "stopped")
    assert store.query("SELECT COUNT(*) AS n FROM kv")[0]["n"] == 0
    assert is_error("error: the ticket is not open") and is_error(None) and not is_error("stopped")
