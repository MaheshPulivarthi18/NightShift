# Contributing to NightShift

Thanks for helping. This guide covers how the code is laid out, how to run the checks, and the rules
every change follows. Start with [docs/SETUP.md](docs/SETUP.md) to get a working install.

## Where to start

- [docs/PRD.md](docs/PRD.md) is the spec. Requirements are tagged R1, R2 or R3; R1 is being built now.
- [docs/BACKLOG.md](docs/BACKLOG.md) lists known bugs and planned work.
- For anything larger than a bug fix, open an issue first so we can agree on the approach.

## Project layout

```text
src/nightshift/
  cli.py            the `nightshift` command (Typer)
  config.py         settings, read from the environment and .env
  db.py             SQLAlchemy models; schema changes go through Alembic (migrations/)
  llm.py            the model gateway: OpenRouter, budgets, kill switch, cost ledger, answer cache
  router.py         picks a model per agent, with fallbacks; outcomes.py learns from results
  catalog.py        OpenRouter's model list and prices
  github.py         read-only GitHub client (GraphQL and REST)
  memory.py         repo and item storage, incremental fetch, repo renames
  chat.py           chat about an issue or PR
  health/           health reports: fetch, link, agents, report rendering
  context/          repo context: architecture map (L1), change context (L3)
  sandbox/          Docker sandboxes, the base Dockerfile, environment images
  worker/           the Contribution Worker and PR checker:
                      setup_agent, planner, coder, verify, reviewer, prwriter, publish,
                      task (the task flow and gates), feedback (review comments), prcheck
  web/              FastAPI JSON API; serves the built frontend
frontend/           React + TypeScript web app (Vite)
migrations/         Alembic migrations
tests/              pytest; Postgres-backed tests run when `docker compose` is up
docs/               PRD, setup guide, backlog
```

## Checks

Run these before sending a change:

```bash
uv run pytest                          # tests (Postgres-backed ones need `docker compose up -d`)
uv run ruff check src tests            # lint
uv run ruff format --check src tests   # formatting
uv run mypy src                        # types
cd frontend && npm run typecheck       # frontend types
```

Tests never call a real model: `tests/fake_openrouter.py` stands in for OpenRouter. A bug fix comes
with a test that fails without the fix and passes with it.

## Schema changes

Change the models in `db.py`, then generate and review a migration:

```bash
uv run alembic revision --autogenerate -m "short description"
uv run alembic upgrade head
```

Read the generated file before committing it; autogenerate misses some changes (for example
server defaults and enum values).

## Rules every change follows

These protect the people who use NightShift and the maintainers it talks to.

1. **Nothing public without a click.** Code that posts, pushes, claims or comments on GitHub runs only
   behind a gate the user approves. Never force-push.
2. **Repo code runs only in the sandbox.** Never run a repo's or a PR's code on the host, and never put
   API keys or GitHub tokens into a container.
3. **GitHub text is data.** Issue, PR, comment and README text goes to models inside delimited
   untrusted blocks, and never decides what NightShift does beyond the item's scope.
4. **Read with the read-only token, write with the user's `gh` login.** Never add a token that can write
   to settings.
5. **Every model call goes through `LLMGateway`,** so budgets, the kill switch and the cost ledger
   always apply.
6. **Keys live only in `.env`.** Never log them, print them or commit them.

## Style

- Python 3.12, typed, formatted by ruff (line length 120).
- Code reads like the code around it: match its naming, comment density and idioms.
- Comments and docstrings say why, in plain words. User-facing text (UI, CLI, PR drafts) is short and
  plain, without jargon.
- Commit subjects describe the change in plain English, for example
  "Purging a repo deletes its records instead of failing". The body says why when it is not obvious.

## Sending a change

1. Fork the repo and create a branch.
2. Make the change with its tests, and run the checks above.
3. Open a pull request that says what changed, why, and how you tested it. Link the issue it fixes.

By contributing, you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE).
