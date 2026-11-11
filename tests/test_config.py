import tomllib
from pathlib import Path

import pytest

from superagent.config import CONFIG_TEMPLATE, ConfigError, GitHubSettings, InboxSettings, load_settings, parse_settings

MINIMAL = {"sources": {"inbox": {"allowed": ["me"]}}}


def parse(data):
    return parse_settings(data, Path("/srv/agent"))


def test_minimal_config_uses_defaults():
    settings = parse(MINIMAL)
    assert settings.model.worker == "claude-opus-5-5" and settings.model.fallbacks == "default"
    assert settings.policy.mode == "observe"
    assert settings.state_path == Path("/srv/agent/.superagent")
    assert settings.work_root == Path("/srv/agent/.superagent/work")
    assert settings.inbox_path == Path("/srv/agent/.superagent/inbox.jsonl")
    assert settings.replies_path == Path("/srv/agent/.superagent/replies.jsonl")
    assert list(settings.sources()) == ["inbox"]


def test_template_parses_with_several_repositories():
    settings = parse(tomllib.loads(CONFIG_TEMPLATE))
    [github] = settings.github_sources
    assert github.name == "github" and github.repositories == ("OWNER/NAME", "OWNER/OTHER")
    assert settings.policy.rules[0].id == "dependency-updates" and not settings.policy.rules[0].enabled
    assert "$SUPERAGENT_GITHUB_REPOSITORY" in settings.execution.prepare
    assert list(settings.sources()) == ["github"]


def test_named_instances_have_their_own_kind_and_policy():
    data = {"sources": {
        "backend": {"kind": "github", "repositories": ["acme/api", "acme/worker"], "allowed": ["alice"]},
        "frontend": {"kind": "github", "repositories": ["acme/web"], "allowed": ["bob"]},
        "lab": {"kind": "inbox", "allowed": ["me"]},
    }}
    settings = parse(data)
    assert [s.name for s in settings.github_sources] == ["backend", "frontend"]
    assert isinstance(settings.source_configs["lab"], InboxSettings)
    assert settings.inbox_files(settings.inbox) == (Path("/srv/agent/.superagent/lab.jsonl"),
                                                    Path("/srv/agent/.superagent/lab-replies.jsonl"))
    policy = settings.build_policy()
    assert policy.sources["backend"].allowed == ("alice",) and policy.sources["frontend"].allowed == ("bob",)


def test_policy_is_built_from_enabled_sources():
    data = {"policy": {"mode": "auto", "rules": [{"id": "r", "name": "R", "instruction": "Do r."}]},
            "sources": {"github": {"repositories": ["o/r"], "allowed": ["alice"], "bots": ["dependabot[bot]"],
                                   "admission": {"unlisted_addressed": "inferred"}},
                        "inbox": {"enabled": False}}}
    settings = parse(data)
    policy = settings.build_policy()
    assert policy.mode == "auto" and list(policy.sources) == ["github"]
    assert policy.sources["github"].admission.unlisted_addressed == "inferred"
    assert policy.rules[0].instruction == "Do r."
    assert isinstance(settings.source_configs["github"], GitHubSettings) and settings.inbox is None


@pytest.mark.parametrize("data,message", [
    ({**MINIMAL, "modle": {}}, "unknown setting"),
    ({**MINIMAL, "policy": {"mode": "yolo"}}, "must be one of"),
    ({**MINIMAL, "model": {"worker_effort": "extreme"}}, "must be one of"),
    ({**MINIMAL, "execution": {"concurrency": 0}}, "at least 1"),
    ({"sources": {"github": {"repositories": ["nope"]}}}, "owner/name"),
    ({"sources": {"github": {"repositories": []}}}, "at least one repository"),
    ({"sources": {"github": {"repositories": ["o/r", "O/R"]}}}, "twice"),
    ({"sources": {"a": {"kind": "github", "repositories": ["o/r"]}, "b": {"kind": "github", "repositories": ["o/r"]}}},
     "belongs to one source"),
    ({"sources": {"github": {"repository": "o/r"}}}, "unknown setting"),
    ({"sources": {"mystery": {"allowed": []}}}, "kind must be one of"),
    ({"sources": {"Bad_Name": {"kind": "inbox"}}}, "lowercase"),
    ({"sources": {"inbox": {"enabled": False}}}, "enable at least one source"),
    ({"sources": {"inbox": {"admission": {"listed_unaddressed": "explicit"}}}}, "cannot be explicit"),
    ({**MINIMAL, "policy": {"rules": [{"id": "a", "instruction": "x"}, {"id": "a", "instruction": "y"}]}},
     "duplicate"),
    ({**MINIMAL, "policy": {"rules": [{"id": "a"}]}}, "instruction"),
    ({"sources": {"inbox": {"allowed": "me"}}}, "list of strings"),
])
def test_invalid_settings_are_rejected_with_a_clear_message(data, message):
    with pytest.raises(ConfigError) as info:
        parse(data)
    assert message in str(info.value)


def test_load_settings_reports_missing_and_invalid_files(tmp_path):
    with pytest.raises(ConfigError, match="superagent init"):
        load_settings(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_settings(bad)
    good = tmp_path / "superagent.toml"
    good.write_text('[sources.inbox]\nallowed = ["me"]\n')
    assert load_settings(good).base_dir == tmp_path.resolve()


def test_backend_and_turn_timeout_settings():
    settings = parse({**MINIMAL, "model": {"backend": "claude-code", "claude_command": "/opt/claude"},
                      "execution": {"turn_timeout_seconds": 900}})
    assert settings.model.backend == "claude-code" and settings.model.claude_command == "/opt/claude"
    assert settings.execution.turn_timeout_seconds == 900
    assert parse(MINIMAL).model.backend == "anthropic"
    with pytest.raises(ConfigError, match="must be one of"):
        parse({**MINIMAL, "model": {"backend": "carrier-pigeon"}})
