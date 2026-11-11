# 006 Deployment

How to install, configure, run, upgrade and troubleshoot superagent. It builds on the documents before it.

## Requirements

- Python 3.11 or later.
- Access to Claude through the Anthropic API: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or `ant auth login`.
- For GitHub: the [GitHub CLI](https://cli.github.com) logged in (`gh auth login`) as the account the agent acts as.
  A dedicated machine user with write access to the watched repositories is recommended, so the team can tell the
  agent's work apart and you can revoke its access in one place.
- `git` on the machine, for the prepare command and the workers.

## Install

```sh
git clone https://github.com/onurkagan/superagent.git
cd superagent
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Configure

```sh
superagent init        # writes superagent.toml
$EDITOR superagent.toml
superagent doctor      # checks the configuration, credentials, gh login and access to every repository
```

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `state_dir` | `.superagent` | Database, sessions, work directories and prompt overrides |
| `[model]` | see [004 Prompts](004-prompts.md) | Models, effort levels, fallbacks, endpoint |
| `execution.concurrency` | `2` | Worker slots across all repositories (plus one for people's messages) |
| `execution.prepare` | empty | Shell command run once per Ticket in the work root |
| `execution.root` | `<state_dir>/work` | Where ticket directories are created |
| `execution.blocked_commands` | `sudo shutdown reboot mkfs dd` | Programs workers may not run by name |
| `execution.max_steps` | `200` | Model steps per turn before a Ticket reports `blocked` |
| `policy.mode` | `observe` | `off`, `observe` or `auto` for proactive candidates |
| `policy.eval_timeout_seconds` | `600` | How long an assessment may take |
| `[[policy.rules]]` | none | Proactive rules: `id`, `name`, `instruction`, `enabled` |
| `[panel]` | `127.0.0.1:8765` | Panel address and optional `token` |

### Sources

Each `[sources.<name>]` table is one Source instance. Its kind defaults to the name for `github` and `inbox`; other
names set `kind` explicitly.

| Setting | Kinds | Meaning |
| --- | --- | --- |
| `kind` | all | `github` or `inbox` |
| `enabled` | all | Turn the instance off without deleting it |
| `allowed` | all | People who may ask for work by addressing the agent |
| `bots` | all | Bots whose messages may become proactive candidates |
| `admission` | all | The admission table, see [002 Authorization](002-authorization.md) |
| `poll_seconds` | all | Seconds between polls (GitHub: 60, inbox: 2) |
| `repositories` | github | Every repository this instance watches, as `owner/name` |
| `account` | github | The agent's login; empty means the account `gh` is logged in as |
| `disclosure` | github | The line that begins every message the agent posts |
| `receive_all.issues`, `.pull_requests`, `.comments` | github | Also receive messages that do not address the agent (needed for proactive rules) |
| `path`, `replies` | inbox | Inbox and replies files |

Watching many repositories:

```toml
[sources.github]
repositories = ["acme/api", "acme/web", "acme/docs"]
allowed = ["alice", "bob"]
bots = ["dependabot[bot]"]
```

Each repository is polled with three API calls per poll interval (Issues, comments, review comments). With the
default 60 seconds that is 180 calls per hour per repository, well inside GitHub's 5,000 per hour for a user; raise
`poll_seconds` if you watch dozens of repositories.

## Run

```sh
superagent run              # the agent and its panel; Ctrl-C stops
superagent status           # what needs you, problems and the last 24 hours
superagent tickets --all
superagent show <ticket-id>
```

Run it as a service under a dedicated user, for example with systemd:

```ini
[Service]
WorkingDirectory=/srv/superagent
ExecStart=/srv/superagent/.venv/bin/superagent run
Environment=ANTHROPIC_API_KEY=...
Restart=on-failure
User=superagent
```

A restart is safe at any time; see *Restart* in [003 Execution](003-execution.md).

## Try it without GitHub

```sh
python examples/offline_demo.py --serve
```

runs the whole flow with a scripted model, no API key and no GitHub, and opens the panel. To talk to a real model
locally, enable `[sources.inbox]` and use `superagent say "Please add a CONTRIBUTING.md" --as me`, then
`superagent replies`.

## Upgrade

Pull the new version and reinstall. Prompt overrides are kept; parts you did not override follow the new defaults.
Compare an override with the new default with `superagent prompts show <part>` and the file in
`superagent/prompts/`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Nothing happens after a mention | `superagent status`: is processing paused? Threads tab: was the message *filtered*, and why? Is the person on `allowed` of the right Source instance? |
| A Source shows as failing | `gh auth status`, and `gh api repos/<owner>/<name>` for the repository named in the problem |
| Assessments are `unavailable` | Model credentials and the `model.*` settings; the problem row shows the error |
| A Ticket failed at once | Its first Report: usually the prepare command; run it by hand in the work root |
| Replies never arrive | *System problems* lists failing messages with the error from GitHub |
