# 003 Execution

This document follows an accepted Ticket through execution, delivery and recovery. It builds on
[001 Domain model](001-domain.md) and [002 Authorization](002-authorization.md).

## Ticket stages

Every open Ticket has exactly one stage, worked out from stored facts. The first matching row wins.

| Stage | Condition | Panel label |
| --- | --- | --- |
| person / delivery | Handing an Input to the worker failed | Input undelivered |
| person / answer | The Ticket has an open question | Awaiting answer |
| person / review | The latest Report is `ready` (with a pull request still open) | Awaiting review |
| person / blocked, failed | The latest Report is `blocked` or `failed` and no person wrote since | Blocked · Failed |
| working / queued | No Assignment yet, or a pending one | Queued |
| idle | The latest Report is `waiting`, or the Assignment ended | Waiting for a trigger |
| working | Everything else | Working |

`person` stages are the ones the panel lists under *Needs you*.

## The worker

Each Ticket gets a directory under the work root (`<state_dir>/work/<ticket-name>` by default) and a session file
that stores its whole model conversation. A turn is a loop: the model reads the conversation and calls tools, the
host runs them and appends the results, until the model ends the turn with a Report.

| Tool | What it does |
| --- | --- |
| `bash` | Runs a shell command in the ticket directory with a timeout; output is cut in the middle when long |
| `read_file`, `write_file`, `edit_file` | Read and change text files inside the ticket directory |
| `reply` | Sends a message of at most 150 words to a conversation, as an acknowledgement, an answer or a result |
| `report` | Records the Ticket's state; every status except `progress` ends the turn |
| `hand_back` | Returns a person's message the Ticket should not handle, so Eval can judge it |
| `follow_thread` | Subscribes the Ticket to another Issue or pull request for context only |

The model call uses adaptive thinking, a configurable effort level, prompt caching and, by default, the server-side
refusal fallback. Passing API failures (rate limits, overload, network errors) are retried after 5, 20 and 60 seconds;
after that, or on any other error, the turn reports `failed`. A turn that runs out of steps (`max_steps`) reports
`blocked` so a person can look.

New Inputs that arrive while a turn runs are added to the conversation before the next model call, so a person can
redirect a worker without waiting for it to finish.

Worker commands run without approval as the operator's user. Blocked commands are refused when invoked by name and
file tools stay inside the ticket directory, but these are defaults that prevent mistakes, not a security boundary:
run superagent under a user and on a machine whose access you are comfortable giving the agent.

## Many repositories at once

Tickets are independent of each other; Tickets of different repositories run side by side. Each worker receives the
repository of its own Ticket in `SUPERAGENT_GITHUB_REPOSITORY`, and the prepare command uses it to clone or check out
the right code. The concurrency limit applies across all repositories.

## Preparing a ticket directory

Before the first Assignment, if the directory does not exist yet, the `prepare` command runs once with `sh` in the
work root, with the execution variables in its environment. A typical command clones the Ticket's repository into
the directory and creates the branch:

```sh
git clone --quiet "https://github.com/$SUPERAGENT_GITHUB_REPOSITORY.git" "$SUPERAGENT_NAME" \
  && git -C "$SUPERAGENT_NAME" switch -c "$SUPERAGENT_NAME"
```

A failing prepare command fails the Ticket with its output; a retry runs it again.

## Execution variables

| Variable | Value |
| --- | --- |
| `SUPERAGENT_DIR`, `SUPERAGENT_NAME`, `SUPERAGENT_TICKET` | The ticket directory, short name and id |
| `SUPERAGENT_BLOCKED_COMMANDS` | Commands the worker may not run |
| `SUPERAGENT_GITHUB_REPOSITORY` | The repository of the Ticket (`owner/name`), for Tickets that came from GitHub |
| `SUPERAGENT_GITHUB_NUMBER` | The Issue or pull request the Ticket started from |
| `SUPERAGENT_GITHUB_REPOSITORIES` | Every repository the GitHub Source watches |
| `SUPERAGENT_GITHUB_ACCOUNT`, `SUPERAGENT_GITHUB_COMMIT_EMAIL` | The agent account and its no-reply commit email |
| `SUPERAGENT_INBOX_THREAD` | The inbox conversation, for Tickets from the local inbox |

The worker reads the same values in its prompt. None of them contains a credential; workers reach GitHub through the
`gh` login of the operator's user.

## Concurrency

A Ticket holds a worker slot while it prepares its directory and while a turn runs. `concurrency` limits these slots,
with one extra slot that only a person's message can use, so new work never keeps a person from continuing an existing
Ticket. Launches and CI results wait for a normal slot.

## How a Ticket ends

- **Ready without a pull request:** a `ready` Report ends the Ticket as completed.
- **Ready with a pull request:** the Ticket waits in *Awaiting review* and follows the pull request. When every pull
  request it opened is merged or closed, it ends as `merged` or `closed`. A reopened pull request is followed again.
- **Nothing to do:** a `dropped` Report ends the Ticket quietly.
- **Stopped:** a person's stop ends it for good.

When a Ticket ends, the people's messages it owned but never read are released to Eval, so nothing a person asked is
silently lost.

## Blocked and failed Tickets

After a `blocked` or `failed` Report the Ticket takes no automatic Input: bot and CI messages are recorded but not
delivered. A person's message, or **Retry** in the panel, releases it. A retry ends the current Assignment and starts
a new one in the same directory and session.

## Restart

Work is stored before it is done: decisions, pending launches, undelivered Inputs and queued messages all live in the
database, so a crash loses nothing. On start:

- Evals that were running are queued again.
- Assignments that were busy end with "host restarted"; new ones start with a recovery note that tells the worker to
  check the directory, git history and conversation before repeating any outside action.
- Inputs that were handed to a worker but not read are delivered again.
- Waiting, ready, blocked and failed Tickets stay as they are.

## Outgoing messages

Replies, acknowledgements and questions are stored in an outbox and sent afterwards, retried with a backoff from 5
seconds doubling up to 5 minutes. A Ticket's messages to one conversation keep their order: a later message waits
until an earlier one is sent, or has failed three times and shows as a problem. A message is dropped when its
conversation can never receive it, such as a locked Issue.

## Pause

**Pause processing** stops Evals, launches, deliveries and outgoing messages. Sources keep recording Triggers and a
running turn is not interrupted. Everything resumes where it stopped.
