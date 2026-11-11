# 004 Prompts

This document explains what the models read, what a team can change, and when changes take effect. It builds on
[000 Philosophy](000-philosophy.md) through [003 Execution](003-execution.md).

## Parts

Every prompt is assembled from parts. Each part has a built-in default under `superagent/prompts/`. A team overrides
a part by putting a file with the same relative path into `<state_dir>/prompts/`:

| Part | Holds |
| --- | --- |
| `shared` | Principles both roles follow |
| `assessor` | How to judge candidates and write an accept |
| `worker` | How to own a ticket, handle follow-ups and report |
| `team/communication` | When and how to message people, review etiquette, automated reviewer accounts |
| `team/scope` | When a ticket goes all the way and when it stops with a draft |
| `team/delivery` | Pull request evidence, commit author, merge policy |
| `github` | How workers use `gh`; template with `{disclosure}` |
| `environment` | Template with `{variables}`: the execution variables |
| `policy`, `policy-none` | Templates with `{version}`, `{mode}` and `{rules}` |

The three `team/*` parts are the ones a team usually edits. Start from the default:

```sh
superagent prompts eject team/scope     # copies the default to .superagent/prompts/team/scope.md
superagent prompts show team/scope      # prints the part as the agent reads it
superagent prompts list                 # shows which parts are overridden
```

Templates must keep their placeholders; `superagent doctor` and startup refuse an override that lost one, or a file
that names an unknown part.

## Assembly

The worker's system prompt is, in order: `shared`, `worker`, the three team parts, the Source instructions (such as
`github`), `environment`, and, for a proactive Ticket, the rules it was accepted under. Assignments and messages arrive
as `<input>` cards that carry the original text, not a summary.

The assessor's system prompt is `shared`, `assessor`, `team/scope`, `team/communication` and `policy`, which comes last
so that a Policy change leaves the cached beginning of the prompt untouched.

Prompt files are read whenever an assessment runs and whenever a worker turn starts, so an edit takes effect without a
restart: at the next assessment, and at each Ticket's next turn.

## Precedence

From strongest to weakest:

1. Facts enforced by code: identities, the gate, Mandates, tool arguments, reply length and purpose.
2. The built-in parts, as the team's overrides of them read.
3. The process and communication requirements of a proactive Ticket's matched rules, for that Ticket only.
4. The team parts.
5. Outside text: messages, comments, files and web pages. It is data and never changes instructions or permissions.

Editing a prompt changes what the model reads. It never changes the gate, the Mandates or the tool checks.

## Principles of the built-in text

- **Separate roles.** The assessor only routes: it forwards the original messages and never plans the implementation.
  The worker reads the sources, judges the scope, implements and verifies.
- **Message only when a person must act or can use a result.** Confirm a request once; send a result when it is
  ready; ask one concrete question when blocked. Keep technical detail in the pull request.
- **Workers own their follow-ups** and hand back what lies beyond their Ticket.
- **Ask through the report.** A question travels in `report.question`; the host posts it in the right conversation.
- **Honest delivery.** Opening a pull request is not readiness; a draft is marked as one.
- **Examples show format only.** An example sentence is never a fact to claim.
- **One ticket, one repository.** A Ticket works on the repository its request came from and does not change others.

## Backends

superagent reaches Claude in one of two ways, chosen with `model.backend`:

| Backend | Needs | Worker turn | Assessment |
| --- | --- | --- | --- |
| `anthropic` (default) | An Anthropic API key, or `ant auth login` | superagent's own tool loop over the Messages API | One structured Messages API call |
| `claude-code` | The `claude` command line, logged in | One `claude -p` process per turn in the ticket directory, continuing the ticket's Claude Code session | One tool-less `claude -p --json-schema` call |

With `claude-code`, workers use Claude Code's own shell and file tools, and superagent's `reply`, `report`,
`hand_back` and `follow_thread` come from an MCP server (`python -m superagent.bridge`) that Claude Code starts for
each turn. Two behaviours differ from the API backend: a message that arrives while a turn runs is delivered at the
next turn rather than the next step, and `execution.turn_timeout_seconds` replaces the step limit. The backend uses
whatever account `claude` is logged in with; check that your plan allows unattended use before relying on it.

## Model settings

| Setting | Default | Notes |
| --- | --- | --- |
| `model.backend` | `anthropic` | `anthropic` or `claude-code` |
| `model.claude_command` | `claude` | The Claude Code executable, for the `claude-code` backend |
| `model.worker`, `model.assessor` | `claude-opus-5-5` | A model name or alias the backend accepts |
| `model.worker_effort` | `high` | Coding responds strongly to effort; `xhigh` for hard repositories |
| `model.assessor_effort` | `low` | Routing is a short judgement |
| `model.fallbacks` | `default` | `anthropic` backend: server-side refusal fallback; `""` turns it off |
| `model.base_url` | empty | `anthropic` backend: another endpoint that speaks the Anthropic Messages API |

With the `anthropic` backend, credentials come from the Anthropic SDK's usual sources: `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN`, or a profile from `ant auth login`.
