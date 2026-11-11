# 007 Architecture

How the code is organised and why. It builds on [001 Domain model](001-domain.md) through
[006 Deployment](006-deployment.md).

## Modules

```text
superagent/
  core/            the reusable domain; no network, model or file access
    model.py       Trigger, Thread, Eval, Decision, Mandate, Ticket, Input, Assignment, Report
    policy.py      Policy, the gate, admission table, revalidation
    engine.py      Engine: intake, fan-out, evals, decisions, ticket lifecycle, recovery
    stage.py       ticket stages
    ids.py, serde.py
  store.py         SQLite: documents, event journal, key/value, outbox table, command queue
  outbox.py        ordered, retried outgoing messages
  sources/         base.py (the Source port and registry), github.py, inbox.py
  llm/             the model port, the Anthropic client, a scripted client for tests
  agents/          prompts.py, assessor.py, tools.py, worker.py, executor.py,
                   claude_code.py (the Claude Code backend),
                   orchestrator.py and orchestrator_tools.py (the orchestrator's turn and tools)
  orchestrator.py  when the orchestrator wakes and what its update holds
  commands.py      the command queue's request and reply
  bridge.py        MCP server giving Claude Code workers, and the orchestrator, superagent's tools
  cards.py         how Assignments and Inputs read to a worker
  runtime.py       the loop that wires everything together
  panel/           read models, HTTP server, single-page UI
  config.py, cli.py
  prompts/         the built-in prompt parts
```

## Dependency rules

- `core` imports nothing outside `core` except the store and outbox interfaces it writes through. It never calls a
  model, a Source or the Executor.
- Sources know nothing of Tickets; they turn events into Triggers and write to Channels.
- Workers never touch the store. Everything they cause (a reply, a report, a hand-back) is an event on a queue.
- Only the runtime's main thread calls the Engine. Assessments, worker turns and orchestrator turns run on threads
  and report back through the event queue.
- The panel, the CLI and the orchestrator change things only through the command queue, so the Engine checks an
  orchestrator's action exactly like a click in the panel.

## Runtime

Every tick (one second by default) the runtime:

1. applies commands from the panel, the CLI and the orchestrator, storing each outcome for a caller that waits;
2. applies events from workers, assessments and orchestrator turns;
3. polls every Source that is due, passing the Threads live Tickets follow, and records the Triggers;
4. starts Evals and gives their candidates to the orchestrator, or to the assessor when the orchestrator does not
   route;
5. expires Evals past their deadline;
6. launches pending Assignments and delivers waiting Inputs to the Executor;
7. stops Assignments that ended, and publishes which worker sessions run;
8. lets the orchestrator start a turn if something needs it;
9. flushes the outbox.

## Engine

The Engine is the only writer of domain records. Every command runs in one transaction. Commands that need outside
work leave it in the store, not in memory: a pending Assignment, an undelivered Input, a queued message. The runtime
picks that work up on the next tick, which makes a crash between a decision and its effect harmless.

## Executor

The Executor runs worker turns in threads. It keeps, per Ticket, the current Assignment, an inbox of Inputs that
arrived during a turn, and a cancel flag. A scheduler grants slots: `concurrency` normal slots plus one that only a
person's message can take. A turn's Inputs are reported as *delivered* when handed over and as *read* when the model
loop takes them into the conversation.

## Worker loop

The worker loop is written by hand rather than with the SDK's tool runner because it must inject new Inputs between
steps and persist every step. Each step:

1. adds waiting Inputs to the conversation and saves the session;
2. calls the model with the tools, adaptive thinking, the configured effort and prompt caching;
3. appends the response unchanged (thinking blocks included) and runs the tool calls;
4. stops at a terminal Report, a refusal, a step limit or a cancel.

Tool calls cut off by the output limit are not run; the model is told to make them again.

## Claude Code backend

With `backend = "claude-code"` the Executor runs `claude_code_turn` instead of the worker loop. A turn writes the
waiting Input cards to `claude -p` on stdin, appends the worker prompt to Claude Code's own system prompt, and starts
the MCP bridge with `--mcp-config`. The bridge validates each `reply`, `report`, `hand_back` and `follow_thread`
call and appends it to an event file; the turn reads that file twice a second and passes the events on, so replies go
out while the turn still runs. Permissions use `dontAsk` with an allow list of Claude Code's file and shell tools and
the bridge tools; blocked commands become `Bash(...)` deny rules. Variables a parent Claude Code session sets
(`CLAUDECODE`, `CLAUDE_CODE_*`) are removed from the child's environment, so superagent behaves the same when started
from inside Claude Code.

## Store

One SQLite file in WAL mode. Domain records are JSON documents keyed by kind and id, with a small tag column for the
filtered lists the runtime needs. The event journal records every transition and feeds the panel's history and
activity counts.

## Model-facing interfaces

| Role | Input | Output |
| --- | --- | --- |
| Assessor | A candidate card: the new messages, earlier messages for context, hand-back reasons, the Tickets on the Thread | A JSON object constrained by a strict schema: decision, reason, title, scope, name, acknowledgement, note, ticket id, a judgement per enabled rule |
| Orchestrator | An update: operator messages, candidate cards, events since its last turn, running worker sessions | Tool calls that become commands; a closing text for the operator |
| Worker | The assignment card and Input cards | Tool calls; every turn ends with `report` |

## Tests

`pytest` runs the whole suite in a few seconds without network access: the Engine against an in-memory store and a
fake clock, Sources against a fake `gh`, workers and the runtime against a scripted model. The end-to-end tests drive
the real runtime with the inbox Source and with a GitHub Source watching two repositories.

## Differences from the reference prototype

superagent was inspired by DeepSeek's
[dsh-superagent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype), a TypeScript plugin for DeepSeek
Harness. It keeps that design's central ideas (attention as the cost to minimise, a deterministic gate before any
model, immutable Mandates, follow-up ownership, store-first execution) and differs in these ways:

- It is a standalone Python service with no host runtime; workers are Claude tool-use loops it runs itself.
- One deployment watches many repositories, and several GitHub Source instances can carry different allowed lists.
- A persistent orchestrator session routes and coordinates, as the reference's main Session does, with fewer tools
  and none for configuration ([009 Orchestrator](009-orchestrator.md)). With it off, routing is one structured model
  call per Eval.
- The Feishu Source is not included; the local inbox Source takes its place for demos and tests.
- CI results and review approvals are not yet Triggers; see [008 Cookbook](008-cookbook.md) for how to add them.
