import json
import subprocess

import pytest

from superagent.core.model import ChannelRef
from superagent.outbox import PermanentSendError
from superagent.sources.github import GhClient, GhError, GitHubConfig, GitHubSource
from superagent.store import Store

CURSOR = "2026-11-11T09:00:00Z"


class FakeGh:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.posts = []

    def api(self, path, method="GET", body=None):
        if method == "POST":
            self.posts.append((path, body))
            error = self.routes.get(("POST", path.split("/comments")[0]))
            if error:
                raise error
            return {}
        for prefix, value in self.routes.items():
            if isinstance(prefix, str) and path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        return []


def user(login, kind="User"):
    return {"login": login, "type": kind}


def issue(number, login="alice", body="", created=CURSOR, title="Bug", pr=False, state="open", assignees=()):
    item = {"id": 1000 + number, "number": number, "title": title, "body": body, "user": user(login),
            "created_at": created, "updated_at": created, "state": state, "closed_at": None,
            "html_url": f"https://github.com/o/r/issues/{number}", "assignees": [user(a) for a in assignees]}
    if pr:
        item["pull_request"] = {"url": "x"}
    return item


def comment(cid, number, login="alice", body="@agent-bot please fix", created=CURSOR):
    return {"id": cid, "issue_url": f"https://api.github.com/repos/o/r/issues/{number}", "user": user(login),
            "body": body, "created_at": created, "updated_at": created, "html_url": "u"}


def make_source(routes=None, receive_all=None, store=None):
    gh = FakeGh(routes)
    store = store or Store()
    store.kv_set("source:github:cursor", CURSOR)
    config = GitHubConfig(repository="o/r", account="agent-bot", receive_all=receive_all or {})
    return GitHubSource(config, store, gh=gh), gh, store


def test_first_poll_starts_from_now_without_replaying_history():
    store = Store()
    source = GitHubSource(GitHubConfig(repository="o/r", account="agent-bot"), store, gh=FakeGh(),
                          now=lambda: "2026-11-11T10:00:00Z")
    assert source.poll(set()) == []
    source.acknowledge()
    assert store.kv_get("source:github:cursor") == "2026-11-11T10:00:00Z"


def test_mentioning_comment_becomes_an_addressed_trigger():
    source, _, _ = make_source({"repos/o/r/issues/comments": [comment(1, 7)]})
    [trigger] = source.poll(set())
    assert trigger.id == "github:comment:1" and trigger.addressed == "mention"
    assert trigger.threads == ("github:issue:7",) and trigger.channels == (ChannelRef("github", "issue:7"),)


def test_own_messages_and_out_of_scope_messages_are_dropped():
    routes = {"repos/o/r/issues/comments": [comment(1, 7, login="agent-bot"), comment(2, 7, body="just chatting")]}
    source, _, _ = make_source(routes)
    assert source.poll(set()) == []
    assert source.health().counts == {"own_messages": 1, "outside_scope": 1}


def test_unaddressed_comment_on_a_followed_thread_is_received():
    source, _, _ = make_source({"repos/o/r/issues/comments": [comment(2, 7, body="looks good")]})
    [trigger] = source.poll({"github:issue:7"})
    assert not trigger.addressed and not trigger.observed


def test_receive_all_marks_triggers_observed():
    source, _, _ = make_source({"repos/o/r/issues?": [issue(3, body="typo in docs")]}, receive_all={"issues": True})
    [trigger] = source.poll(set())
    assert trigger.kind == "issue" and trigger.observed and trigger.scope == "issues"


def test_mention_must_be_the_exact_account():
    source, _, _ = make_source({"repos/o/r/issues/comments": [comment(1, 7, body="cc @agent-bot2 and me@agent-bot")]})
    assert source.poll(set()) == []


def test_issue_assigned_to_the_agent_addresses_it():
    source, _, _ = make_source({"repos/o/r/issues?": [issue(4, assignees=("agent-bot",))]})
    [trigger] = source.poll(set())
    assert trigger.addressed == "mention"


def test_old_items_with_new_activity_do_not_become_triggers_but_advance_the_cursor():
    old = issue(5, body="@agent-bot hi", created="2026-11-01T00:00:00Z")
    old["updated_at"] = "2026-11-11T09:30:00Z"
    source, _, store = make_source({"repos/o/r/issues?": [old]})
    assert source.poll(set()) == []
    source.acknowledge()
    assert store.kv_get("source:github:cursor") == "2026-11-11T09:30:00Z"


def test_bots_are_flagged():
    item = comment(9, 7, body="@agent-bot bump deps")
    item["user"] = user("dependabot[bot]", "Bot")
    source, _, _ = make_source({"repos/o/r/issues/comments": [item]})
    assert source.poll(set())[0].actor_is_bot


def test_review_comment_replies_go_to_the_review_thread():
    review = {"id": 55, "in_reply_to_id": 50, "pull_request_url": "https://api.github.com/repos/o/r/pulls/8",
              "user": user("alice"), "body": "@agent-bot rename this", "path": "app.py", "line": 3,
              "created_at": CURSOR, "updated_at": CURSOR}
    source, _, _ = make_source({"repos/o/r/pulls/comments": [review]})
    [trigger] = source.poll(set())
    assert trigger.channels == (ChannelRef("github", "pr:8", reply_to="50"),)
    assert trigger.text.startswith("Review comment on app.py:3")


def test_followed_pull_request_close_and_reopen_become_state_triggers():
    closed = issue(8, pr=True, state="closed")
    closed["closed_at"] = CURSOR
    source, _, store = make_source({"repos/o/r/issues?": [closed], "repos/o/r/pulls/8": {"merged": True}})
    [trigger] = [t for t in source.poll({"github:issue:8"}) if t.kind == "pr_closed"]
    assert trigger.meta["merged"] and trigger.actor == ""
    assert [t for t in source.poll({"github:issue:8"}) if t.kind == "pr_closed"] == []
    source.gh.routes["repos/o/r/issues?"] = [issue(8, pr=True, state="open")]
    assert [t.kind for t in source.poll({"github:issue:8"}) if t.kind.startswith("pr_")] == ["pr_reopened"]


def test_failures_count_toward_an_error_after_three_polls():
    source, _, _ = make_source({"repos/o/r/issues?": GhError("boom", 502)})
    for _ in range(2):
        assert source.poll(set()) == []
        assert source.health().ok
    source.poll(set())
    assert not source.health().ok and "boom" in source.health().detail


def test_send_adds_disclosure_and_routes_review_replies():
    source, gh, _ = make_source()
    source.send(ChannelRef("github", "issue:7"), "On it.")
    source.send(ChannelRef("github", "pr:8", reply_to="50"), "Done.")
    assert gh.posts[0] == ("repos/o/r/issues/7/comments", {"body": "This is by an automated agent, not a human.\n\nOn it."})
    assert gh.posts[1][0] == "repos/o/r/pulls/8/comments/50/replies"


def test_send_falls_back_to_the_conversation_when_the_review_comment_is_gone():
    source, gh, _ = make_source({("POST", "repos/o/r/pulls/8"): GhError("not found", 404)})
    source.send(ChannelRef("github", "pr:8", reply_to="50"), "Done.")
    assert gh.posts[-1][0] == "repos/o/r/issues/8/comments"


def test_send_to_a_locked_conversation_is_permanent():
    source, _, _ = make_source({("POST", "repos/o/r/issues/7"): GhError("Issue is locked", 403)})
    with pytest.raises(PermanentSendError):
        source.send(ChannelRef("github", "issue:7"), "x")


def test_resolve_thread_accepts_links_and_numbers():
    source, _, _ = make_source()
    assert source.resolve_thread("#12") == "github:issue:12"
    assert source.resolve_thread("https://github.com/o/r/pull/7/files") == "github:issue:7"
    assert source.resolve_thread("https://github.com/other/repo/pull/7") is None


def test_variables_and_instructions():
    source, gh, _ = make_source({"users/agent-bot": {"id": 42}})
    origin = source.poll(set()) or None
    keys = {v.key: v.value for v in source.variables(None)}
    assert keys["SUPERAGENT_GITHUB_REPOSITORY"] == "o/r"
    assert keys["SUPERAGENT_GITHUB_COMMIT_EMAIL"] == "42+agent-bot@users.noreply.github.com"
    assert origin is None
    assert "This is by an automated agent, not a human." in source.instructions()
    assert "repos/o/r/pulls" in source.instructions()


def test_repository_must_be_owner_slash_name():
    with pytest.raises(ValueError):
        GitHubSource(GitHubConfig(repository="not a repo"), Store(), gh=FakeGh())


def test_gh_client_parses_output_and_http_status():
    def runner(args, input=None, **kwargs):
        if "fail" in args[-1]:
            return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(args, 0, json.dumps({"login": "x", "echo": input}), "")

    client = GhClient(runner=runner)
    assert client.api("user")["login"] == "x"
    assert json.loads(client.api("repos/o/r/issues/1/comments", "POST", {"body": "hi"})["echo"]) == {"body": "hi"}
    with pytest.raises(GhError) as info:
        client.api("fail")
    assert info.value.status == 404
