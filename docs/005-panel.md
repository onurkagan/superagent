# 005 Panel

The panel is a small web page served by `superagent run` (by default at <http://127.0.0.1:8765/>). It builds on
[000 Philosophy](000-philosophy.md) through [004 Prompts](004-prompts.md).

## Three questions

Opening the panel answers, in this order:

1. **Does anything need me now?** *Needs you* lists every open Ticket in a `person` stage, with its question or last
   report and the actions that fit: Details, Retry (for undelivered, blocked or failed Tickets) and Stop.
2. **Is the system healthy and doing the right things?** *System problems* appears only when there is one: a message
   that failed three times, a Source failing its polls, or an Eval recorded `unavailable`. *Last 24 hours* counts
   Triggers, Decisions by kind, Tickets created and ended, and messages sent.
3. **What is the history of one piece of work?** The tabs below show Tickets (open, ended, all), Decisions with their
   reasons and matched rules, Threads with each Trigger and what happened to it, and the event journal. A Ticket's
   details show its Mandate, the Decision that created it, its Reports, the messages it sent, every Input and its
   Assignments.

Thread keys name the repository (`github:acme/api#12`), so Tickets of every watched repository share one list.
The header shows how many worker sessions run out of `execution.concurrency` and how many wait for a slot.

## Orchestrator

Below *Needs you*, the *Orchestrator* section is the conversation with the session that oversees every ticket (see
[009 Orchestrator](009-orchestrator.md)): what you asked, what it answered, and one line for each turn it took on its
own, such as routing a request or telling a worker about an overlapping pull request. A pill says whether it is idle,
working, or whether its last turn failed. Write in the box to ask it something or to tell it to act; the answer
appears when its turn ends. When the orchestrator is off, the section says how to turn it on.

## Actions

| Action | Effect |
| --- | --- |
| Stop | Ends the Ticket and withdraws its Mandate; it receives nothing more (asks for confirmation) |
| Retry | Starts a new Assignment for an undelivered, blocked or failed Ticket |
| Pause / Resume processing | Stops or resumes Evals, launches, deliveries and outgoing messages |
| Evaluate again / as new work | Sends a Trigger to Eval, such as a follow-up a Ticket owns that should be new work |
| Send (Orchestrator) | Gives the orchestrator a message; it answers after its next turn |

Actions are queued as commands and applied by the running agent on its next tick. The CLI offers the same commands:
`superagent stop`, `retry`, `pause`, `resume`, `reevaluate` and `chat`.

## Access

The panel binds to `127.0.0.1` and refuses to bind anywhere else without `[panel] token`. With a token set, every
request must carry it (as `?token=` on the page URL, which the page then sends as a header). Actions also require a
header only the page sends, so a link on another site cannot trigger them.

## Principles

- **What matters first.** Every element answers one of the three questions, or it is removed.
- **Precise labels.** Stages are mutually exclusive and say whom a Ticket waits for.
- **Honest counts.** A number equals the length of the list behind it.
- **Quiet when healthy.** Problems appear only when they exist and are actionable.
- **Confirm destructive actions.**
