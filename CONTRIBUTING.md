# Contributing

AI Engineering Ledger welcomes generic, provider-neutral improvements after the
repository's public license has been approved and committed.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python3 -m pytest -q
python3 tests/run_all.py
```

## Contribution rules

- Use synthetic fixtures and anonymous account aliases.
- Never commit credentials, dashboard screenshots, real provider exports,
  personal absolute paths, prompts, chats, or local ledger databases.
- Preserve exact decimal values and explicit unknown/partial states.
- Keep provider billing, runtime telemetry, and source-control facts separate.
- Add binary acceptance tests for changed behavior.
- Do not infer tokens, costs, reset timestamps, or account identity.

The private Forgejo repository is canonical. The public GitHub repository is a
read-only distribution mirror, so normal contribution intake and synchronization
policy must be documented by the maintainers before accepting public pull
requests.
