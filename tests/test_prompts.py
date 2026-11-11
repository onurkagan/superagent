import pytest

from superagent.agents.prompts import PARTS, PromptError, PromptLibrary, fill
from superagent.core.policy import Rule
from superagent.sources.base import Variable
from tests.conftest import make_policy


def test_every_default_part_exists_and_keeps_its_placeholders():
    library = PromptLibrary()
    for key in PARTS:
        assert library.default(key)
    assert "{variables}" in library.default("environment")
    assert "{rules}" in library.default("policy")


def test_override_replaces_a_part(tmp_path):
    (tmp_path / "team").mkdir()
    (tmp_path / "team" / "scope.md").write_text("Only touch docs.")
    library = PromptLibrary(tmp_path)
    assert library.part("team/scope") == "Only touch docs."
    assert library.is_overridden("team/scope")
    assert "Only touch docs." in library.worker_system([])


def test_override_must_keep_placeholders(tmp_path):
    (tmp_path / "environment.md").write_text("no placeholder here")
    with pytest.raises(PromptError):
        PromptLibrary(tmp_path).part("environment")
    assert PromptLibrary(tmp_path).validate()


def test_validate_reports_unknown_parts(tmp_path):
    (tmp_path / "mystery.md").write_text("?")
    assert "unknown prompt part" in PromptLibrary(tmp_path).validate()[0]


def test_worker_system_lists_variables_and_proactive_rules():
    text = PromptLibrary().worker_system(
        [Variable("SUPERAGENT_DIR", "/work/fix", "The ticket directory")],
        source_instructions=["# Working with X"],
        rules=[Rule("typos", "Fix typos", "Fix obvious typos.")],
    )
    assert "`SUPERAGENT_DIR` = `/work/fix`" in text
    assert "# Working with X" in text
    assert "- Fix typos (typos): Fix obvious typos." in text
    assert text.index("Your role: worker") < text.index("Delivery requirements") < text.index("# Environment")


def test_assessor_system_ends_with_policy():
    policy = make_policy(rules=[Rule("typos", "Fix typos", "Fix obvious typos.")])
    text = PromptLibrary().assessor_system(policy)
    assert text.rstrip().endswith("- Fix typos (typos): Fix obvious typos.")
    assert policy.version in text


def test_policy_without_rules_uses_the_none_template():
    assert "No proactive rule is enabled" in PromptLibrary().policy(make_policy())


def test_fill_leaves_other_braces_alone():
    assert fill("{a} and {b} and {c}", a="1", b="2") == "1 and 2 and {c}"
