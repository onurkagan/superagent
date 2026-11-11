# 002 Authorization

Authorization decides who may start work and how far that work may go. Code enforces it; messages, comments, quoted
documents and prompt overrides are inputs to a decision and never change a permission. It builds on
[001 Domain model](001-domain.md).

## Two paths for a Trigger

1. **Fan-out.** The Trigger reaches every live Ticket subscribed to its Threads. A person's message on a Thread a
   Ticket follows (not context-only) is *owned* by that Ticket: its worker decides what to do with it and no Eval
   runs. Fan-out grants no new authority; the Ticket stays inside its Mandate.
2. **Eval.** A Trigger that no Ticket owns (any bot or CI message, a person's message on a Thread without a live
   Ticket, a message a worker handed back) passes the gate. When the gate admits it, it waits on its Thread for the
   next Eval.

## The gate

The gate is deterministic and runs before any model sees the message. It answers whether the message may become a
candidate, and with what **strength**:

| Case | Strength |
| --- | --- |
| A person on the Source's allowed list addresses the agent (mention, assignment, direct message) | explicit |
| A person on the list writes on a Thread that has a Ticket (a follow-up that reached Eval) | explicit |
| Someone not on the list follows up on a Thread that has a Ticket | inferred |
| A bot on the Source's bot list addresses the agent, or writes where the Source receives everything | inferred |
| Anything the admission table admits (below) | the table's cell |
| Any other bot, a message without a sender, anything the table ignores | not admitted |

An inferred candidate is never admitted while the accept mode is `off`.

## Admission table

Each Source has an admission table for human messages that do not come from an allowed person addressing the agent:

| Cell | Choices | Default |
| --- | --- | --- |
| `listed_unaddressed`: on the list, not addressing the agent | inferred, ignore | inferred |
| `unlisted_addressed`: not on the list, addressing the agent | explicit, inferred, ignore | ignore |
| `unlisted_unaddressed`: not on the list, not addressing the agent | inferred, ignore | ignore |

The unaddressed cells apply only where the Source receives every message, such as a GitHub scope whose
`receive_all` switch is on. Setting `unlisted_addressed = "explicit"` lets anyone who can comment on the repository ask
the agent for work; use it only for repositories where that is what you want.

## Accept mode and rules

| Mode | Effect on inferred candidates |
| --- | --- |
| `off` | They are not admitted at all |
| `observe` (default) | They are assessed; a positive assessment is recorded as `ignore` marked *would accept* |
| `auto` | A positive assessment accepts them |

Explicit requests work in every mode. An inferred candidate can be accepted only when the assessment matches at least
one enabled rule; the Engine drops any rule the assessment invents. The Ticket's Mandate records the matched rules
and its worker reads their instructions. Start new rules in `observe`, read the recorded decisions in the panel, and
switch to `auto` when they look right.

## The assessment proposes, the Engine decides

The assessor is a model call with a strict output schema. It proposes `accept`, `attach`, `stop` or `ignore`. The
Engine then checks the proposal:

- `accept` of an inferred candidate needs an enabled matched rule and `auto` mode.
- `attach` and `stop` need an explicit candidate; `stop` also needs the Ticket to be on the candidate's Thread.
- A proposal made under an older Policy version is discarded and the batch is queued again.

## Policy versions

The accept mode, the rules and every Source's allowed list, bot list and admission table form one Policy version.
When the version changes, running Evals are discarded and their Triggers queued again, and every live Ticket the change
bears on receives a notice: a matched rule changed or was disabled, the requester was removed from the list, or
proactive work is no longer accepted. Live Tickets are not cancelled; half-finished work is worse than a notice.

## Resume and stop

An ended Ticket keeps its Mandate. A person's message on one of its own Threads resumes it without a new Eval, after
the Mandate is checked against today's Policy:

- an allowlist Mandate needs the requester to still be on the list;
- an inferred Mandate needs `auto` mode and its rules still enabled;
- a bot Mandate needs the bot to still be on the bot list;
- an admission-table Mandate needs the table to still grant at least its original strength.

If the check fails, the Ticket stays ended and the message is treated as reaching no Ticket.

Three operations change a Mandate: an accept creates it, a person's stop withdraws it, a failed check revokes it.

- **A person asking the agent to stop in a conversation** is an ordinary message. The worker that owns it hands it
  back with the reason "asks to stop", and the Eval decides `stop`. A worker cannot stop its own Ticket on a
  person's behalf.
- **Stop in the panel or CLI** is a command and acts at once.
- A stopped Ticket receives nothing more; new work on its Thread needs a new accept.

## When an Eval does not finish

An assessment that fails, is refused by the model, or passes its deadline (`eval_timeout_seconds`) is retried twice.
The third failure records `unavailable`, which the panel shows as a problem with an *Evaluate again* action.
