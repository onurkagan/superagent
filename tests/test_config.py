import tomllib
from pathlib import Path

import pytest

from superagent.config import CONFIG_TEMPLATE, ConfigError, load_settings, parse_settings

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
    assert list(settings.sources()) == ["inbox"]


def test_template_parses_once_placeholders_are_filled():
    data = tomllib.loads(CONFIG_TEMPLATE)
    settings = parse(data)
    assert settings.github.repository == "OWNER/NAME"
    assert settings.policy.rules[0].id == "dependency-updates" and not settings.policy.rules[0].enabled
    assert "git clone" in settings.execution.prepare


def test_policy_is_built_from_enabled_sources():
    data = {"policy": {"mode": "auto", "rules": [{"id": "r", "name": "R", "instruction": "Do r."}]},
            "sources": {"github": {"repository": "o/r", "allowed": ["alice"], "bots": ["dependabot[bot]"],
                                   "admission": {"unlisted_addressed": "inferred"}},
                        "inbox": {"enabled": False}}}
    policy = parse(data).build_policy()
    assert policy.mode == "auto" and list(policy.sources) == ["github"]
    assert policy.sources["github"].admission.unlisted_addressed == "inferred"
    assert policy.rules[0].instruction == "Do r."


@pytest.mark.parametrize("data,message", [
    ({**MINIMAL, "modle": {}}, "unknown setting"),
    ({**MINIMAL, "policy": {"mode": "yolo"}}, "must be one of"),
    ({**MINIMAL, "model": {"worker_effort": "extreme"}}, "must be one of"),
    ({**MINIMAL, "execution": {"concurrency": 0}}, "at least 1"),
    ({"sources": {"github": {"repository": "nope"}}}, "owner/name"),
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
