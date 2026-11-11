import json

from superagent.core.model import ChannelRef
from superagent.sources.inbox import InboxSource, append_message
from superagent.store import Store


def test_poll_reads_new_lines_once_after_acknowledge(tmp_path):
    inbox = tmp_path / "inbox.jsonl"
    source = InboxSource(Store(), inbox, tmp_path / "replies.jsonl")
    assert source.poll(set()) == []
    append_message(inbox, "alice", "Please add a README", thread="docs")
    [trigger] = source.poll(set())
    assert trigger.actor == "alice" and trigger.addressed == "direct"
    assert trigger.threads == ("inbox:docs",) and trigger.channels == (ChannelRef("inbox", "docs"),)
    assert source.poll(set()) == [trigger]  # not acknowledged yet: read again
    source.acknowledge()
    assert source.poll(set()) == []
    append_message(inbox, "bob", "fyi", addressed=False)
    [second] = source.poll(set())
    assert second.observed and not second.addressed and second.id != trigger.id


def test_partial_and_malformed_lines(tmp_path):
    inbox = tmp_path / "inbox.jsonl"
    inbox.write_text('not json\n{"actor": "alice", "text": "ok"}\n{"actor": "half"')
    source = InboxSource(Store(), inbox, tmp_path / "replies.jsonl")
    [trigger] = source.poll(set())
    assert trigger.text == "ok"
    assert source.health().counts["malformed"] == 1


def test_send_appends_reply(tmp_path):
    source = InboxSource(Store(), tmp_path / "inbox.jsonl", tmp_path / "replies.jsonl")
    source.send(ChannelRef("inbox", "main"), "On it.")
    reply = json.loads((tmp_path / "replies.jsonl").read_text())
    assert reply["thread"] == "main" and reply["text"] == "On it."


def test_resolve_and_listen(tmp_path):
    source = InboxSource(Store(), tmp_path / "i", tmp_path / "r")
    assert source.resolve_thread("inbox:docs") == "inbox:docs"
    assert source.resolve_thread("#4") is None
    assert source.listens("inbox:x") and not source.listens("github:o/r#1")


def test_a_request_can_name_the_repository_it_is_about(tmp_path):
    from superagent.sources.base import SourceRegistry
    from superagent.sources.github import GitHubConfig, GitHubSource

    inbox = tmp_path / "inbox.jsonl"
    source = InboxSource(Store(), inbox, tmp_path / "replies.jsonl")
    append_message(inbox, "me", "Add a CONTRIBUTING.md", thread="docs", repository="acme/web")
    [trigger] = source.poll(set())
    assert trigger.meta == {"repository": "acme/web"} and trigger.title == "docs (acme/web)"

    class NoGh:
        def api(self, *args, **kwargs):
            raise RuntimeError("offline")

    github = GitHubSource(GitHubConfig(repositories=("acme/api", "acme/web"), account="bot"), Store(), gh=NoGh())
    github.commit_email = lambda: ""
    variables = {v.key: v.value for v in SourceRegistry([github, source]).variables(trigger)}
    assert variables["SUPERAGENT_GITHUB_REPOSITORY"] == "acme/web"
    assert variables["SUPERAGENT_GITHUB_REPOSITORIES"] == "acme/api,acme/web"
