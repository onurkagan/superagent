# 008 Cookbook: adding a Source

A Source connects superagent to a stream of events, such as a chat system, an issue tracker or CI. This recipe shows
what a Source must do; `superagent/sources/inbox.py` is the smallest complete example and
`superagent/sources/github.py` the fullest.

## 1. Map the events onto the domain

Decide, for your system:

- **Trigger:** which events count, and a stable id for each, so reading the same event twice is harmless.
- **Thread:** which events form one conversation. The key starts with the Source instance's name, such as
  `slack:C024BE91L/1700000000.000100`.
- **Channel:** where a reply to a Trigger goes, as an `address` (and `reply_to` when replies thread under a message).
- **Addressed:** when a message speaks to the agent (`mention` or `direct`).
- **Bots:** how to recognise automated senders.

## 2. Implement the port

```python
class MySource:
    name = "mysource"           # set by the runtime from the [sources.<name>] table
    poll_seconds = 30.0

    def poll(self, listened: set[str]) -> list[Trigger]: ...
    def acknowledge(self) -> None: ...
    def send(self, channel: ChannelRef, text: str) -> None: ...
    def resolve_thread(self, ref: str) -> str | None: ...
    def listens(self, thread_key: str) -> bool: ...
    def variables(self, origin: Trigger | None) -> list[Variable]: ...
    def instructions(self) -> str: ...
    def health(self) -> SourceHealth: ...
```

- `poll` returns new Triggers since the last acknowledged position. It always reads the Threads in `listened`
  (followed by live Tickets) and drops the agent's own messages and anything outside what it is configured to receive.
- `acknowledge` persists the read position, after the runtime recorded the Triggers. Store it with
  `store.kv_set(f"source:{self.name}:...")`.
- `send` raises `PermanentSendError` when the conversation can never receive the message (deleted, locked, archived),
  and any other exception for failures worth retrying.
- `resolve_thread` maps links and references people and workers use to Thread keys of this Source.
- `variables` returns execution variables; with an `origin` Trigger, include the values of that conversation.
- `instructions` returns prompt text for workers, such as how to use your system's CLI, or an empty string.
- `health` reports whether recent polls failed; report an error only after several failures in a row.

## 3. Mark what was received only by receive-all

If your Source can receive every message in a scope (not only those that address the agent), set `observed=True` on
Triggers received that way and `scope` to the scope's name. The gate applies the admission table's unaddressed cells
only to observed Triggers.

## 4. Configure it

Add a kind to `SOURCE_KINDS` and a settings class in `superagent/config.py`, parse its table, and construct the Source
in `build_sources` in `superagent/runtime.py`. Its allowed list, bot list and admission table come from the common
source settings, so the Policy covers it without further work.

## 5. Test it

- Unit-test the Source against a fake client: Trigger translation, own-message and scope filtering, cursors, replies
  and permanent errors (see `tests/test_github_source.py`).
- Add an end-to-end test that drives the runtime with your Source and a scripted model (see
  `tests/test_runtime.py`).

## Example: CI results as Triggers

To let workers react to failing checks on their pull requests, extend the GitHub Source: for each pull request Thread
in `listened`, read `repos/<repo>/commits/<head sha>/check-runs`, and emit a Trigger with an empty `actor` (a system
event) when a check run concludes. The Trigger fans out to the Ticket as context; it never starts work by itself,
because the gate refuses events without a sender.

## Checklist

- [ ] Trigger ids are stable and globally unique within the Source.
- [ ] Thread keys start with the instance name and are what `resolve_thread` returns for the same conversation.
- [ ] The agent's own messages never become Triggers.
- [ ] A restart neither replays nor loses events.
- [ ] `send` distinguishes permanent from passing failures.
- [ ] Tests cover translation, filtering, cursors and replies.
