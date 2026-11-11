"""The ``superagent`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from superagent import __version__
from superagent.agents.prompts import PARTS, PromptLibrary
from superagent.commands import call, is_error
from superagent.config import (
    CONFIG_TEMPLATE,
    ConfigError,
    Settings,
    detect_backend,
    load_settings,
    personal_config,
    with_backend,
)
from superagent.core.ids import slug
from superagent.core.ids import new_id
from superagent.orchestrator import chat_log
from superagent.panel.data import PanelData
from superagent.sources.inbox import append_message
from superagent.store import Store

DEFAULT_CONFIG = "superagent.toml"


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.config)


def _store(settings: Settings) -> Store:
    return Store(settings.db_path)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


# -- commands --------------------------------------------------------------------------------------------------


def _gh_login() -> str:
    gh = shutil.which("gh")
    if not gh:
        return ""
    try:
        result = subprocess.run([gh, "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists; use --force to overwrite it", file=sys.stderr)
        return 1
    backend = detect_backend() if args.backend == "auto" else args.backend
    how = "your `claude` login, no API key" if backend == "claude-code" else "an Anthropic API key"
    if args.repo:
        login = _gh_login()
        text = personal_config(args.repo, login or "YOUR-LOGIN", backend)
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path}: backend {backend} ({how}), repositories {', '.join(args.repo)}.")
        if not login:
            print("Could not read your GitHub login; log in with `gh auth login` and put it in allowed.")
        print("Next: `superagent doctor`, then `superagent run`, then in another terminal "
              f'`superagent say "..." --repo {args.repo[0]}`.')
        return 0
    path.write_text(with_backend(CONFIG_TEMPLATE, backend), encoding="utf-8")
    print(f"Wrote {path} with backend {backend} ({how}). Edit the repositories and the allowed list, then run "
          "`superagent doctor`.")
    return 0


def _check(ok: bool, label: str, hint: str = "") -> bool:
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"\n       {hint}" if hint and not ok else ""))
    return ok


def cmd_doctor(args: argparse.Namespace) -> int:
    try:
        settings = _settings(args)
    except ConfigError as exc:
        _check(False, "configuration", str(exc))
        return 1
    healthy = _check(True, f"configuration {args.config}")
    problems = PromptLibrary(settings.prompts_dir).validate()
    healthy &= _check(not problems, "prompt overrides", "; ".join(problems))
    if settings.model.backend == "claude-code":
        claude = shutil.which(settings.model.claude_command)
        version = subprocess.run([claude, "--version"], capture_output=True, text=True) if claude else None
        healthy &= _check(bool(version and version.returncode == 0),
                          f"Claude Code CLI ({settings.model.claude_command})",
                          "install Claude Code and log in with `claude`, or set [model] claude_command")
    else:
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if not has_key and shutil.which("ant"):
            has_key = subprocess.run(["ant", "auth", "status"], capture_output=True).returncode == 0
        healthy &= _check(has_key, "model credentials",
                          'set ANTHROPIC_API_KEY, log in with `ant auth login`, or use backend = "claude-code"')
    if settings.github_sources:
        gh = shutil.which("gh")
        healthy &= _check(bool(gh), "GitHub CLI installed", "install it from https://cli.github.com")
        if gh:
            auth = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
            healthy &= _check(auth.returncode == 0, "GitHub CLI logged in", "run `gh auth login` as the agent account")
        for source in settings.github_sources:
            for repository in source.repositories if gh else ():
                repo = subprocess.run([gh, "api", f"repos/{repository}", "--jq", ".full_name"],
                                      capture_output=True, text=True)
                healthy &= _check(repo.returncode == 0, f"[sources.{source.name}] access to {repository}",
                                  (repo.stderr or "").strip()[:200])
            healthy &= _check(bool(source.allowed), f"[sources.{source.name}] allowed list is not empty",
                              f"add the people who may ask for work to [sources.{source.name}] allowed")
    print("All checks passed." if healthy else "Some checks failed.")
    return 0 if healthy else 1


def cmd_run(args: argparse.Namespace) -> int:
    from superagent.panel.server import PanelServer
    from superagent.runtime import Runtime

    settings = _settings(args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    runtime = Runtime(settings)
    panel = None
    if settings.panel.enabled and not args.no_panel:
        try:
            panel = PanelServer(PanelData(runtime.store, runtime.status), settings.panel.host, settings.panel.port,
                                settings.panel.token)
        except OSError as exc:
            print(f"cannot serve the panel on {settings.panel.host}:{settings.panel.port}: {exc.strerror or exc}. "
                  "Set [panel] port to a free port, or run with --no-panel.", file=sys.stderr)
            runtime.shutdown()
            return 1
    runtime.start()
    if panel:
        panel.start()
        print(f"Panel: {panel.url}")
    stop = threading.Event()

    def handle(signum: int, frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    sources = ", ".join(s.name for s in runtime.registry)
    print(f"superagent {__version__} running with {sources}; policy {runtime.engine.policy.version}. Ctrl-C stops.")
    runtime.run_forever(stop, interval=args.interval)
    if panel:
        panel.stop()
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    from superagent.panel.server import PanelServer

    settings = _settings(args)
    panel = PanelServer(PanelData(_store(settings)), settings.panel.host, settings.panel.port, settings.panel.token)
    print(f"Panel (read from {settings.db_path}): {panel.url}")
    try:
        panel.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _ticket_line(row: dict[str, Any]) -> str:
    return f"{row['id']}  {row['stage']['label']:<22} {row['name']:<30} {row['title']}"


def cmd_status(args: argparse.Namespace) -> int:
    data = PanelData(_store(_settings(args)))
    overview = data.overview()
    if args.json:
        _print_json(overview)
        return 0
    print("Processing is paused." if overview["paused"] else "Processing is running.")
    print(f"\nNeeds you ({len(overview['needs_person'])}):")
    for row in overview["needs_person"]:
        print("  " + _ticket_line(row))
        if row["attention"]:
            print(f"      question: {row['attention']}")
    if not overview["needs_person"]:
        print("  nothing")
    if overview["problems"]:
        print(f"\nProblems ({len(overview['problems'])}):")
        for problem in overview["problems"]:
            print(f"  [{problem['kind']}] {problem['detail']}")
    activity = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in overview["activity"].items() if v)
    print(f"\nLast 24 hours: {activity or 'no activity'}")
    workers = overview["executor"]
    if workers:
        print(f"Worker sessions: {workers['running']} of {workers['concurrency']} running, "
              f"{workers['queued']} waiting for a slot")
    orchestrator = overview["orchestrator"]
    if orchestrator.get("enabled"):
        state = f"in a turn since {orchestrator['since']}" if orchestrator.get("busy") else "idle"
        print(f"Orchestrator: {state}, {orchestrator.get('turns', 0)} turns since start"
              + (f"; last error: {orchestrator['last_error']}" if orchestrator.get("last_error") else ""))
    return 0


def cmd_tickets(args: argparse.Namespace) -> int:
    rows = PanelData(_store(_settings(args))).tickets("all" if args.all else "open")
    if args.json:
        _print_json(rows)
    for row in [] if args.json else rows:
        print(_ticket_line(row))
    if not rows and not args.json:
        print("No tickets.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    detail = PanelData(_store(_settings(args))).ticket(args.ticket)
    if detail is None:
        print(f"no ticket {args.ticket}", file=sys.stderr)
        return 1
    _print_json(detail)
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    data = PanelData(_store(_settings(args)))
    if args.command in ("stop", "retry"):
        data.command(args.command, ticket=args.ticket)
    elif args.command == "reevaluate":
        data.command("reevaluate", trigger=args.trigger)
    else:
        data.command(args.command)
    print(f"Queued {args.command}; the running agent applies it on its next tick.")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    settings = _settings(args)
    if settings.inbox is None:
        print("no inbox source is enabled; add [sources.inbox] to the configuration", file=sys.stderr)
        return 1
    text = " ".join(args.text)
    watched = {r.lower(): r for source in settings.github_sources for r in source.repositories}
    repository = ""
    if args.repo:
        repository = watched.get(args.repo.lower(), "")
        if not repository:
            print(f"{args.repo} is not a watched repository; watched: {', '.join(watched.values()) or 'none'}",
                  file=sys.stderr)
            return 1
    elif len(watched) > 1:
        print(f"Several repositories are watched; add --repo with one of: {', '.join(watched.values())}",
              file=sys.stderr)
        return 1
    thread = args.thread or f"{slug(text)[:32].strip('-') or 'request'}-{os.urandom(2).hex()}"
    append_message(settings.inbox_path, args.actor, text, thread=thread, bot=args.bot,
                   addressed=not args.unaddressed, repository=repository)
    print(f"Sent as conversation {thread!r}. Follow up with --thread {thread}; see answers with `superagent replies`.")
    return 0


def cmd_replies(args: argparse.Namespace) -> int:
    path = _settings(args).replies_path
    if not path.exists():
        print("No replies yet.")
        return 0
    for line in path.read_text(encoding="utf-8").splitlines()[-args.limit:]:
        reply = json.loads(line)
        print(f"[{reply['at']}] ({reply['thread']}) {reply['text']}")
    return 0


def _print_chat(entry: dict[str, Any]) -> None:
    who = {"operator": "you", "orchestrator": "orchestrator"}.get(entry["role"], entry["role"])
    print(f"[{entry['at'][11:16]}] {who}: {entry['text']}")


def _answer(store: Store, message_id: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while True:
        for entry in reversed(chat_log(store)):
            if entry["role"] == "orchestrator" and message_id in (entry.get("reply_to") or []):
                return entry
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def _chat_once(store: Store, text: str, timeout: float) -> int:
    message_id = new_id("ch")
    outcome = call(store, "chat", text=text, id=message_id, timeout=10)
    if outcome is None:
        print("superagent is not running. The message stays queued and the orchestrator reads it once superagent "
              "runs; see `superagent chat --history` then.")
        return 1
    if is_error(outcome):
        print(outcome, file=sys.stderr)
        return 1
    answer = _answer(store, message_id, timeout)
    if answer is None:
        print("No answer yet; the orchestrator may still be working. See `superagent chat --history`.")
        return 0
    print(f"orchestrator: {answer['text']}")
    return 1 if answer.get("error") else 0


def cmd_chat(args: argparse.Namespace) -> int:
    store = _store(_settings(args))
    if args.history:
        entries = chat_log(store, args.limit)
        for entry in entries:
            _print_chat(entry)
        if not entries:
            print("No conversation yet.")
        return 0
    if args.reset:
        outcome = call(store, "orchestrator_reset", timeout=10)
        print(outcome or "superagent is not running; the reset waits in the queue.")
        return 1 if is_error(outcome) else 0
    if args.message:
        return _chat_once(store, " ".join(args.message), args.timeout)
    print("Talking to the orchestrator. An empty line does nothing; Ctrl-D ends.")
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if line:
            _chat_once(store, line, args.timeout)


def cmd_prompts(args: argparse.Namespace) -> int:
    settings = _settings(args)
    library = PromptLibrary(settings.prompts_dir)
    if args.action == "list":
        for key in PARTS:
            print(f"{key:<22} {'overridden' if library.is_overridden(key) else 'default'}")
        return 0
    if not args.part or args.part not in PARTS:
        print(f"name a part: {', '.join(PARTS)}", file=sys.stderr)
        return 1
    if args.action == "show":
        print(library.part(args.part))
        return 0
    target = settings.prompts_dir / f"{args.part}.md"
    if target.exists():
        print(f"{target} already exists", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(library.default(args.part) + "\n", encoding="utf-8")
    print(f"Wrote {target}; edit it and the agent reads it on the next model request.")
    return 0


# -- parser ----------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superagent", description="A proactive, attention-frugal coding agent.")
    parser.add_argument("--version", action="version", version=f"superagent {__version__}")
    parser.add_argument("-c", "--config", default=os.environ.get("SUPERAGENT_CONFIG", DEFAULT_CONFIG),
                        help="configuration file (default: superagent.toml or $SUPERAGENT_CONFIG)")
    sub = parser.add_subparsers(dest="name", required=True)

    p = sub.add_parser("init", help="write a configuration")
    p.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME",
                   help="write a ready-to-run configuration for these repositories (repeat for several)")
    p.add_argument("--backend", choices=("auto", "claude-code", "anthropic"), default="auto",
                   help="auto: anthropic when ANTHROPIC_API_KEY is set, otherwise claude-code (no API key)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("doctor", help="check configuration, credentials and GitHub access").set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run the agent and its panel")
    p.add_argument("--no-panel", action="store_true")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between ticks")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)

    sub.add_parser("panel", help="serve the panel from the state database only").set_defaults(func=cmd_panel)

    p = sub.add_parser("status", help="what needs you, problems and recent activity")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tickets", help="list tickets")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tickets)

    p = sub.add_parser("show", help="show one ticket in detail")
    p.add_argument("ticket")
    p.set_defaults(func=cmd_show)

    for name, help_text in (("stop", "stop a ticket and withdraw its mandate"),
                            ("retry", "start a new assignment for a blocked or failed ticket")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("ticket")
        p.set_defaults(func=cmd_command, command=name)
    for name, help_text in (("pause", "pause evaluation, delivery and replies"), ("resume", "resume processing")):
        sub.add_parser(name, help=help_text).set_defaults(func=cmd_command, command=name)
    p = sub.add_parser("reevaluate", help="send a trigger to evaluation again")
    p.add_argument("trigger")
    p.set_defaults(func=cmd_command, command="reevaluate")

    p = sub.add_parser("say", help="give the agent a request through the local inbox source")
    p.add_argument("text", nargs="+")
    p.add_argument("--repo", default="", metavar="OWNER/NAME",
                   help="the watched repository the request is about (needed when several are watched)")
    p.add_argument("--thread", default="", help="continue this conversation; by default each request starts one")
    p.add_argument("--as", dest="actor", default="me")
    p.add_argument("--bot", action="store_true")
    p.add_argument("--unaddressed", action="store_true")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("replies", help="print the latest replies of the local inbox source")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_replies)

    p = sub.add_parser("chat", help="talk to the orchestrator that oversees every ticket")
    p.add_argument("message", nargs="*", help="one message; without it, an interactive conversation")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for an answer")
    p.add_argument("--history", action="store_true", help="print the conversation so far")
    p.add_argument("--limit", type=int, default=30, help="entries --history prints")
    p.add_argument("--reset", action="store_true", help="start the orchestrator's next turn in a new session")
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("prompts", help="list, show or eject prompt parts for editing")
    p.add_argument("action", choices=("list", "show", "eject"))
    p.add_argument("part", nargs="?")
    p.set_defaults(func=cmd_prompts)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
