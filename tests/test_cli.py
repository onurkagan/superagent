import json

import pytest

from superagent.cli import main
from superagent.store import Store

CONFIG = """
state_dir = "state"
[sources.inbox]
allowed = ["me"]
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "superagent.toml"
    path.write_text(CONFIG)
    return path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_init_writes_a_template_once(tmp_path, capsys):
    path = tmp_path / "superagent.toml"
    assert run(capsys, "-c", str(path), "init")[0] == 0
    assert "[sources.github]" in path.read_text()
    assert run(capsys, "-c", str(path), "init")[0] == 1


def test_missing_config_is_a_clear_error(tmp_path, capsys):
    code, _, err = run(capsys, "-c", str(tmp_path / "nope.toml"), "status")
    assert code == 2 and "superagent init" in err


def test_say_appends_to_the_inbox(config, capsys):
    assert run(capsys, "-c", str(config), "say", "please", "add", "a", "README", "--as", "me")[0] == 0
    line = json.loads((config.parent / "state" / "inbox.jsonl").read_text())
    assert line["text"] == "please add a README" and line["actor"] == "me" and line["addressed"]


def test_status_and_tickets_on_an_empty_state(config, capsys):
    code, out, _ = run(capsys, "-c", str(config), "status")
    assert code == 0 and "Needs you (0)" in out
    assert "No tickets." in run(capsys, "-c", str(config), "tickets")[1]
    assert json.loads(run(capsys, "-c", str(config), "status", "--json")[1])["needs_person"] == []


def test_commands_are_queued_for_the_runtime(config, capsys):
    assert run(capsys, "-c", str(config), "stop", "tk_1")[0] == 0
    assert run(capsys, "-c", str(config), "pause")[0] == 0
    store = Store(config.parent / "state" / "superagent.db")
    assert store.take_commands() == [("stop", {"ticket": "tk_1"}), ("pause", {})]


def test_prompts_list_show_and_eject(config, capsys):
    assert "team/scope" in run(capsys, "-c", str(config), "prompts", "list")[1]
    assert "Ticket scope" in run(capsys, "-c", str(config), "prompts", "show", "team/scope")[1]
    assert run(capsys, "-c", str(config), "prompts", "eject", "team/scope")[0] == 0
    assert (config.parent / "state" / "prompts" / "team" / "scope.md").exists()
    assert "overridden" in run(capsys, "-c", str(config), "prompts", "list")[1]
    assert run(capsys, "-c", str(config), "prompts", "show", "bogus")[0] == 1


def test_show_unknown_ticket(config, capsys):
    assert run(capsys, "-c", str(config), "show", "tk_missing")[0] == 1


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "superagent" in capsys.readouterr().out


def test_run_reports_a_busy_panel_port_clearly(config, capsys):
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        config.write_text(CONFIG + f"\n[panel]\nport = {port}\n")
        code, _, err = run(capsys, "-c", str(config), "run")
    assert code == 1 and "free port" in err
