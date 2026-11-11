# superagent

A proactive coding agent for your team's GitHub repositories, powered by Claude.

superagent watches any number of repositories. When someone on the team mentions it in an Issue or pull request, it
decides whether that is a request for work, opens a ticket, works on it in its own directory with its own Claude
session, and answers in the original conversation, usually with a pull request. Many tickets run at once, and one
**orchestrator** session oversees all of them: it routes new requests, tells workers when their work overlaps,
restarts what failed for a passing reason, and answers you when you ask what is going on. A small web panel shows
only what needs a person, and lets you talk to the orchestrator.

It is built around one rule: **take as little human attention as possible, and give as much value as possible.**
The agent acts only when asked or when a rule the team wrote allows it, confirms a request once, stays quiet while it
works, and speaks again only with a result, one concrete question, or an honest failure.
[docs/000-philosophy.md](docs/000-philosophy.md) explains the principle.

> **Status:** an early prototype. Review it before pointing it at repositories that matter, start with a short
> allowed list, and keep proactive rules in `observe` mode until their recorded decisions look right.

## How it works

```mermaid
flowchart LR
  GH[GitHub repositories] -->|Triggers| T[Threads]
  T -->|deterministic gate| O[Orchestrator: one persistent Claude session]
  T -->|follow-ups| K
  O -->|accept with a Mandate| K[Tickets]
  O -->|notes, status requests, retries| K
  K -->|Assignments| W[Workers: one Claude session per ticket, in parallel]
  W -->|reply / report| K
  K -->|reports| O
  K -->|comments, questions| GH
  You -->|superagent chat, panel| O
```

- **One orchestrator over every session.** A persistent Claude session sees every ticket, worker session and
  conversation across all repositories. It routes new requests, sends a worker a note when another ticket changes the
  same files, asks idle workers for their status, retries passing failures, and answers you in `superagent chat` and
  the panel. It acts only through commands the Engine checks; see [docs/009-orchestrator.md](docs/009-orchestrator.md).

- **A gate before any model.** Who may ask for work is checked by code from each Source's allowed list, bot list and
  admission table. No message, comment or prompt can widen it.
- **Mandates.** An accepted request becomes a Ticket with an immutable Mandate: its scope, its requester, the rules it
  relied on and the policy version. Wider scope needs a new Ticket.
- **Follow-ups go to the work they belong to.** A person's comment on a Ticket's Issue or pull request reaches that
  Ticket's worker directly; the worker handles it or hands it back. Ended Tickets resume when a person writes again.
- **Many repositories, many sessions, one agent.** One deployment watches any number of repositories. Tickets run in
  parallel worker sessions (`execution.concurrency`), each with the right repository in its environment. Groups of
  repositories can have different allowed lists.
- **Store first, act after.** Decisions, launches, Inputs and outgoing messages are stored before they happen, so a
  restart loses and repeats nothing.
- **A panel that answers three questions:** what needs me, is the system healthy, and what happened to one piece of
  work.

## Quick start

### No API key needed

superagent runs on the `claude` command line you already use: its workers, routing and the orchestrator all go
through your Claude Code login. You need:

- Python 3.11 or later;
- [Claude Code](https://code.claude.com), logged in (run `claude` once and log in);
- the [GitHub CLI](https://cli.github.com), logged in: `gh auth login`.

```sh
git clone https://github.com/onurkagan/superagent.git && cd superagent
python -m venv .venv && . .venv/bin/activate
pip install -e .

superagent init --repo YOU/PROJECT     # repeat --repo for more repositories
superagent doctor                      # checks claude, gh and access to the repositories
superagent run                         # leave it running; the panel is at http://127.0.0.1:8765/
```

In a second terminal, give it work and talk to it:

```sh
superagent say "Add a CONTRIBUTING.md" --repo YOU/PROJECT
superagent replies                     # what the agent answered
superagent chat "What are you working on?"
```

Each `say` becomes its own ticket with its own worker session, and several run at once. The worker clones the
repository, makes the change on a branch and opens a pull request; the answer appears in `superagent replies` and in
the panel. To add to a request later, pass the `--thread` that `say` printed.

`superagent init` writes `backend = "claude-code"` whenever `ANTHROPIC_API_KEY` is not set. Check that your Claude
plan allows unattended use: every worker turn and every orchestrator turn counts against it.

### Just look around

```sh
python examples/offline_demo.py --serve   # no Claude, no GitHub: a scripted model runs the whole flow
```

### For a team: ask on GitHub

Give the agent a GitHub account of its own (a machine user, logged in with `gh auth login`) and teammates can ask in
any Issue or pull request of a watched repository, for example `@my-agent please add a CONTRIBUTING.md`; their review
comments on its pull requests go straight to the worker that opened them. Run `superagent init` without `--repo` for
the full template and fill in `repositories` and `allowed`. While the agent works as your own account, it ignores
your own comments on GitHub, so use `superagent say` for your requests.

### With an API key

Set `ANTHROPIC_API_KEY` (or run `ant auth login`) before `superagent init` and it writes `backend = "anthropic"`:
workers then run superagent's own tool loop over the Messages API, and a message reaches a busy worker at its next
step instead of its next turn. *Backends* in [docs/004-prompts.md](docs/004-prompts.md) compares the two.

## Configuration

```toml
[model]
backend = "claude-code"     # or "anthropic" with an API key
worker = "claude-opus-5-5"
worker_effort = "high"

[execution]
concurrency = 3

[orchestrator]
enabled = true
prepare = 'gh repo clone "$SUPERAGENT_GITHUB_REPOSITORY" "$SUPERAGENT_NAME" -- --quiet && git -C "$SUPERAGENT_NAME" switch -c "$SUPERAGENT_NAME"'

[policy]
mode = "observe"

# One source watching several repositories with one allowed list.
[sources.github]
repositories = ["acme/api", "acme/web", "acme/docs"]
allowed = ["alice", "bob"]
bots = ["dependabot[bot]"]

# Another group of repositories with a different allowed list.
[sources.infra]
kind = "github"
repositories = ["acme/terraform"]
allowed = ["carol"]
```

Every setting is described in [docs/006-deployment.md](docs/006-deployment.md). Behaviour the team describes in words,
such as when to stop with a draft or what a pull request must contain, lives in editable prompt sections:
`superagent prompts eject team/delivery`.

## Command line

| Command | What it does |
| --- | --- |
| `superagent init [--repo owner/name]` | Write a configuration: ready to run with `--repo`, a full template without |
| `superagent doctor` | Check the configuration, `claude` or the API key, `gh` and repository access |
| `superagent run` | Run the agent and its panel |
| `superagent status` | What needs you, problems, the last 24 hours |
| `superagent chat ["..."]` | Talk to the orchestrator: ask what runs, which tickets overlap, or tell it to act |
| `superagent tickets [--all]`, `show <id>` | List tickets / show one in detail |
| `superagent stop <id>`, `retry <id>` | Stop a ticket / retry a blocked or failed one |
| `superagent pause`, `resume`, `reevaluate <trigger>` | Control processing |
| `superagent say "..." --repo owner/name`, `replies` | Give the agent a request / read its answers |
| `superagent prompts list\|show\|eject <part>` | Inspect and customise prompts |

## Documentation

Read the numbered documents in order; each builds only on the ones before it.

- [000 Philosophy](docs/000-philosophy.md)
- [001 Domain model](docs/001-domain.md)
- [002 Authorization](docs/002-authorization.md)
- [003 Execution](docs/003-execution.md)
- [004 Prompts](docs/004-prompts.md)
- [005 Panel](docs/005-panel.md)
- [006 Deployment](docs/006-deployment.md)
- [007 Architecture](docs/007-architecture.md)
- [008 Cookbook: adding a Source](docs/008-cookbook.md)
- [009 Orchestrator](docs/009-orchestrator.md)

## Development

```sh
pip install -e ".[dev]"
pytest
```

The suite runs in seconds without network access: the domain against an in-memory store, Sources against a fake
`gh`, and the runtime end to end against a scripted model. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

The design is inspired by DeepSeek's
[dsh-superagent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype) and its write-up on super-agent
design. superagent is an independent Python implementation; [docs/007-architecture.md](docs/007-architecture.md)
lists how it differs.

## License

[MIT](LICENSE)
