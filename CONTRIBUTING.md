# Contributing

Thank you for helping. A few conventions keep the project small and predictable.

## Workflow

1. Create a branch, make a focused change, and add or update tests next to it.
2. Run `pytest`; the whole suite must pass and must not need network access.
3. Keep commits small and their messages in the imperative mood ("Add ...", "Fix ...").
4. Open a pull request that says what changed, why, and how you verified it.

## Design rules

- Read the documents in `docs/` first; they are the specification. A change in behaviour comes with a change in the
  matching document.
- Do not add a domain concept for a new need. Make it an attribute of an existing concept, or prompt text.
- Do not add a setting when one default suits almost every team.
- Only the Engine writes domain records, and only from the runtime's main thread. Workers and assessments report
  through events.
- Anything that limits what the agent may do (the gate, Mandates, tool checks) is enforced in code, never only in a
  prompt.
- Prompt text is written in English and in plain, short sentences. Examples in prompts show format only.

## Tests

- Domain logic: test the Engine with `tests/conftest.py`'s in-memory store, fake clock and trigger builder.
- Sources: test against a fake client; never call real services from tests.
- Model behaviour: use `superagent.llm.fake.ScriptedModelClient`; never call a real model from tests.
