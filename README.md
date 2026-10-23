# NightShift

Autonomous repo maintainer agents. Phase 0a: **Repo Health Reports**, private Markdown reports about GitHub repos. Read-only: NightShift never writes to the repos it watches.

Each watched repo has a role that picks the report:

- **contributor** (default): for repos you don't maintain. Is it worth contributing (outside PR merge rate, time to first maintainer response)? Which open issues are free and worth taking, which need a maintainer's OK first, and which are already being worked on?
- **maintainer**: backlog triage. Related clusters, duplicates, and which PRs can be closed, merged or need a decision, each with a reason and evidence checked by a separate Verifier.

## How it works

```
maintainer:  fetch -> link -> analyse_groups -> assess_prs -> verify -> report
contributor: fetch -> link -> scout -> contributor_report
```

| Node | Kind | What it does |
| --- | --- | --- |
| fetch | code | GitHub GraphQL, incremental after the first run; stores items in Postgres and embeds them locally |
| link | code | Candidate groups from explicit links plus pgvector similarity |
| analyse_groups | agent | **Relevance Analyst** confirms clusters and duplicates |
| assess_prs | agent | **PR Assessor** classifies open PRs using signals computed by code |
| verify | agent | **Verifier** checks each claim against its evidence only; unsupported claims are dropped |
| report | code | Markdown report and a stored snapshot, compared with the previous one |
| scout | code + agent | Contributor pulse and who is on which issue (code); **Opportunity Scout** judges which free issues are actionable, their size, and whether to ask first |
| contributor_report | code | Contributor Markdown report, compared with the previous one |

- **Orchestration:** LangGraph, checkpointed in Postgres, so a stopped run resumes with `--resume <run id>`.
- **Model access:** every Claude call goes through `nightshift.llm.LLMGateway`, which enforces the kill switch and budget caps, caches verdicts by input hash, and records cost per call.
- **Memory:** items and embeddings (repo memory), cached verdicts, and operator feedback that calibrates future reports.

## Setup

```bash
docker compose up -d            # Postgres 16 + pgvector
cp .env.example .env            # add ANTHROPIC_API_KEY (or NIGHTSHIFT_PROVIDER=openrouter + OPENROUTER_API_KEY)
                                # and a read-only GITHUB_TOKEN
uv sync
uv run alembic upgrade head     # app tables
uv run nightshift db-setup      # LangGraph checkpoint tables
```

## Use

```bash
uv run nightshift watch add aayushch/laya MadsLorentzen/ai-job-search   # contributor view
uv run nightshift watch add my-org/my-repo --role maintainer
uv run nightshift watch role aayushch/laya maintainer                   # switch views
uv run nightshift report run aayushch/laya
uv run nightshift report run --all
uv run nightshift report show aayushch/laya
uv run nightshift report feedback aayushch/laya "close_candidate:#42" --wrong --note "still wanted"
uv run nightshift spend
uv run nightshift kill          # blocks every model call; `nightshift resume` clears it
```

Reports are written to `reports/<owner>__<repo>/`.

## Sandboxes and repo context (Contribution Worker, M1)

Repo code only ever runs in a Docker container: no credentials or host files inside, a non-root
user with no capabilities, CPU, memory and process caps, and a timeout on every command. After a
repo's setup succeeds the container is saved as a per-repo image, so the next task skips the install.

```bash
uv run nightshift sandbox build                      # base image: Python, Node, uv, git, language servers
uv run nightshift sandbox try aayushch/laya \
  --setup "cd engine && uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements-dev.lock" \
  --setup "cd ui && npm ci" \
  --test "cd engine && .venv/bin/pytest -q" --test "cd ui && npm test"

uv run nightshift context map aayushch/laya           # L1 architecture map: one Sonnet call per new commit
uv run nightshift context change aayushch/laya --base e5f1610 --patch fix.diff   # L3, no model calls
```

- **L1 architecture map:** code builds a source index inside the sandbox (imports for Python,
  TypeScript, Svelte and Rust; entry points; central files), then the **Cartographer** maps
  components and relationships, each citing a file. Code drops any path that does not exist.
  Stored per commit in `repo_context`.
- **L3 change context:** for a diff, the enclosing functions, their callers and called definitions
  (jedi and typescript-language-server via multilspy), plus code elsewhere that uses the same string
  keys. Code the diff did not change but probably should have is listed first.

Outputs go to `reports/<owner>__<repo>/context/`.

## Web app

```bash
uv run nightshift serve        # http://127.0.0.1:8000
```

- **Repos:** watchlist with role, verdict and last run; add repos, switch roles, start a report.
- **Jobs:** every run (from the app or the CLI) live: each step and agent call with what it is looking at, its cost and cache hits; stop and resume.
- **Report:** reports as pages, with a **chat** link next to every issue and PR.
- **Chat:** talk to Claude about one issue or PR. It has the item, related items, NightShift's analysis and the repo brief, and can list, search and read the repo's code. It drafts replies and PR text but never posts.

The app has no login, so it binds to 127.0.0.1. Put it behind authentication before exposing it.

## Development

```bash
uv run pytest        # unit tests, plus Postgres-backed tests when docker compose is up
uv run ruff check src tests
uv run mypy src
```
