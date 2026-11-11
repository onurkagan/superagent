# 000 Philosophy

superagent is an automated member of a software team. It watches the team's GitHub repositories, takes on work
people ask it for, and delivers results where they asked. One principle decides every part of its design:

**Take as little human attention as possible, and give as much value as possible.**

An agent that can act for a whole team can also waste a whole team's attention. A comment, a notification, a
question, a setting, a row in the panel and a paragraph in these documents each cost someone's attention. Each has
to be worth more than it costs, or it goes.

## What the agent does

- **It acts only when asked, or when a written rule allows it.** A request counts when a person on the allowed list
  addresses the agent: a mention, an assignment, or a direct message. Proactive work needs a rule the team wrote,
  and new rules start in a mode that only records what the agent would have done.
- **It speaks only when a person must act or can use a result.** It confirms a request once, saying what it will
  do. It is quiet while it works. It writes again when a result is ready, when it needs a decision, or when it cannot
  finish.
- **It asks one concrete question at a time,** so the question reaches a person instead of hiding in a long report.
- **It stops by impact, not by effort.** A focused change goes all the way to a reviewable pull request. A change
  that needs a design decision stops with a draft and one clear question.
- **It reports honestly.** A draft is called a draft, a failure is a failure, and a limit is never bypassed to avoid
  asking.

## What the design does

- **Few concepts.** The domain has the concepts the work needs (Source, Trigger, Thread, Eval, Mandate, Ticket) and
  no others. A new need becomes an attribute of one of them, or text in a prompt.
- **Few settings.** When one value suits almost every team, it is a default, not a setting. Behaviour a team can
  describe in words lives in prompts.
- **Code enforces, prompts guide.** Who may ask for work, what an accept allows and which tools a worker may use are
  checked by code. Prompts shape how the model works; no message, comment or prompt override can widen a permission.
- **A panel that shows what needs a person first.** A healthy system takes no space on screen.
- **Documents read in order.** Each numbered document builds only on the ones before it.

## What it asks of the operator

The agent comments, opens pull requests and asks questions under an account your team sees every day. You are
responsible for what it says.

- **Start narrow.** Begin with a short allowed list, a few repositories and proactive rules in `observe` mode.
  Widen them once the recorded decisions look right.
- **Tune for less noise.** When the agent is noisy, make its prompts stricter or pause it. Do not add messages to
  explain the noise.
- **Do not use it for volume.** Mass pull requests and busywork cost the team more than they give.
- **Read what it posts in your name,** and correct the prompts when it is wrong.

The goal is the right help at the right time, and nothing at any other time.
