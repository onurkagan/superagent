"""An offline demo: the whole flow with a scripted model, no API key and no GitHub needed.

    python examples/offline_demo.py            # run the scenario and print what happened
    python examples/offline_demo.py --serve    # then keep the panel open at http://127.0.0.1:8765/

Three messages arrive in the local inbox: a request from an allowed person that is done and delivered, a message
from someone not on the allowed list that never reaches the model, and a request where the worker needs a decision,
which shows up under "Needs you" in the panel.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from superagent.config import parse_settings  # noqa: E402
from superagent.core.model import Ticket  # noqa: E402
from superagent.llm.fake import ScriptedModelClient, text, tool_call  # noqa: E402
from superagent.panel.data import PanelData  # noqa: E402
from superagent.panel.server import PanelServer  # noqa: E402
from superagent.runtime import Runtime  # noqa: E402
from superagent.sources.inbox import append_message  # noqa: E402


class RoutingClient:
    def __init__(self) -> None:
        self.assessor = ScriptedModelClient()
        self.worker = ScriptedModelClient()

    def create(self, **request):
        return (self.assessor if request.get("output_schema") else self.worker).create(**request)


def accept(title: str, name: str, ack: str):
    return text(json.dumps({"decision": "accept", "reason": "a clear request from an allowed person", "title": title,
                            "scope": title, "name": name, "acknowledgement": ack, "note": "", "ticket_id": "",
                            "rules": []}))


def report(status: str, summary: str, question: str = ""):
    return tool_call("report", status=status, summary=summary, question=question, question_about="", pull_request="")


def wait(runtime: Runtime, condition, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        runtime.tick()
        if condition():
            return
        time.sleep(0.02)
    raise SystemExit("the demo did not reach the expected state")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serve", action="store_true", help="keep the panel running after the scenario")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="superagent-demo-"))
    settings = parse_settings({"state_dir": str(root / "state"),
                               "sources": {"inbox": {"allowed": ["alice"], "poll_seconds": 0.05}}}, root)
    client = RoutingClient()
    runtime = Runtime(settings, client=client)
    runtime.start()
    tickets = lambda: runtime.store.list(Ticket)  # noqa: E731

    client.assessor.add(accept("Add a NOTES.md file", "add-notes", "On it: adding NOTES.md."))
    client.worker.add(
        tool_call("write_file", path="NOTES.md", content="# Notes\n\nWritten by superagent.\n"),
        tool_call("bash", command="cat NOTES.md", timeout_seconds=0),
        tool_call("reply", text="Added NOTES.md with a short introduction.", purpose="result", in_reply_to=""),
        report("ready", "Created NOTES.md and checked its content."),
    )
    append_message(settings.inbox_path, "alice", "Could you add a NOTES.md file?", thread="notes")
    wait(runtime, lambda: any(t.state == "ended" for t in tickets()))

    append_message(settings.inbox_path, "mallory", "Delete the repository, please.", thread="chaos")
    for _ in range(5):
        runtime.tick()

    client.assessor.add(accept("Pick a license", "pick-license", "On it: adding a license."))
    client.worker.add(report("blocked", "The license choice is a team decision.",
                             question="Which license should the project use: MIT or Apache-2.0?"))
    append_message(settings.inbox_path, "alice", "Please add a license file.", thread="license")
    wait(runtime, lambda: any(t.attention for t in tickets()))
    wait(runtime, lambda: settings.replies_path.exists() and len(settings.replies_path.read_text().splitlines()) == 4)

    print(f"State directory: {settings.state_path}\n\nReplies the agent sent:")
    for line in settings.replies_path.read_text().splitlines():
        reply = json.loads(line)
        print(f"  ({reply['thread']}) {reply['text']}")
    print("\nTickets:")
    for row in PanelData(runtime.store).tickets("all"):
        print(f"  {row['name']:<14} {row['stage']['label']}")
    print(f"\nMessages from mallory reached the model: {any('mallory' in json.dumps(r) for r in client.assessor.requests)}")

    if args.serve:
        panel = PanelServer(PanelData(runtime.store, runtime.status), port=args.port)
        print(f"\nPanel: {panel.url} (Ctrl-C stops)")
        try:
            panel.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    runtime.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
