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

Prompt files are read on every model request: an edit takes effect at the next step without a restart.

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

## Model settings

| Setting | Default | Notes |
| --- | --- | --- |
| `model.worker`, `model.assessor` | `claude-opus-5-5` | Any model the Messages API offers |
| `model.worker_effort` | `high` | Coding responds strongly to effort; `xhigh` for hard repositories |
| `model.assessor_effort` | `low` | Routing is a short judgement |
| `model.fallbacks` | `default` | Server-side refusal fallback; set to `""` to turn it off |
| `model.base_url` | empty | Another endpoint that speaks the Anthropic Messages API |

Credentials come from the Anthropic SDK's usual sources: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or a profile
from `ant auth login`.
