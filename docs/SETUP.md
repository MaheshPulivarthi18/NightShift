# Setting up NightShift

NightShift runs on your machine: a Python server, a Postgres database in Docker, and a Docker sandbox
where repo code runs. This guide takes a fresh clone to a working web app, then lists every setting.

## 1. Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| Docker (Docker Desktop on macOS) | recent | Postgres, and the sandbox every repo runs in |
| Python | 3.12 | the server and agents |
| [uv](https://docs.astral.sh/uv/) | recent | Python dependencies and running commands |
| Node.js | 20 or later | building the web app |
| [GitHub CLI](https://cli.github.com) (`gh`) | recent | publishing PRs and replies from your own account |
| git | any | |

Give Docker at least 4 CPUs and 8 GB of memory. Sandbox images take disk space: most repos need 1 to
3 GB, large ML repos much more (one we tested needs 25 GB).

You also need:

- An **OpenRouter API key** from [openrouter.ai/keys](https://openrouter.ai/keys), with credit on it.
  Every model call goes through OpenRouter.
- A **read-only GitHub token**: a fine-grained personal access token with public repository access only
  ([github.com/settings/tokens](https://github.com/settings/tokens?type=beta)). NightShift reads with it;
  it never writes with it.
- **`gh` logged in as you** (`gh auth login`). Only this login publishes, and only after you approve.
  Publishing pushes a branch to your fork of the repo, so `gh` must be able to create forks.

## 2. Install

```bash
git clone https://github.com/MaheshPulivarthi18/NightShift.git
cd NightShift
cp .env.example .env
```

Fill in `.env`: `OPENROUTER_API_KEY`, `GITHUB_TOKEN` and `NIGHTSHIFT_GITHUB_LOGIN` (your GitHub
username). Keys live only in `.env`, which git ignores.

```bash
docker compose up -d                 # Postgres 16 with pgvector, on 127.0.0.1:5432
uv sync                              # Python dependencies, including dev tools
uv run alembic upgrade head          # app tables
uv run nightshift db-setup           # checkpoint tables for resumable runs
uv run nightshift sandbox build      # the base sandbox image (Python, Node, uv, git, language servers)
```

`sandbox build` takes several minutes the first time.

## 3. Build and start the web app

```bash
cd frontend && npm install && npm run build && cd ..
uv run nightshift serve              # http://127.0.0.1:8000
```

Rebuild the frontend after changing anything in `frontend/`. For UI work, run `npm run dev` in
`frontend/` instead: it serves the app with hot reload and proxies `/api` to a running
`nightshift serve`. `uv run nightshift serve --reload` restarts the server when Python code changes.

## 4. First run

1. **Add a repo:** on the Repos page, or `uv run nightshift watch add owner/repo`.
2. **Set it up:** `uv run nightshift sandbox setup owner/repo`. The Setup agent reads the repo's CI,
   dev container, contributing guide and toolchain files, tries install and test commands in the
   sandbox, and saves what works. Later work in that repo reuses it with no model call.
3. **Fix an issue:** from a report, choose **plan** next to an issue, or
   `uv run nightshift task plan owner/repo 123`. Approve the plan (Gate 1); NightShift writes a failing
   test and the fix, verifies it and has it reviewed; then review the diff and PR text and publish
   (Gate 2).
4. **Check a PR:** on the PR checks page, paste a pull request URL, or
   `uv run nightshift check-pr <url>`.

Check spend with `uv run nightshift spend`; stop every model call with `uv run nightshift kill`
(`uv run nightshift resume` clears it).

## 5. Settings

All settings are environment variables, read from `.env`. Nested ones use a double underscore, for
example `NIGHTSHIFT_CODER__MODEL`.

| Variable | Default | What it does |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | (required) | Key for every model call |
| `GITHUB_TOKEN` | (required) | Read-only token for GitHub data |
| `NIGHTSHIFT_GITHUB_LOGIN` | empty | Your login, so your own PRs and assignments are not treated as someone else's claim |
| `NIGHTSHIFT_DATABASE_URL` | local Postgres from `docker-compose.yml` | Database connection |
| `NIGHTSHIFT_DAILY_BUDGET_USD` | 1.25 | Daily spend cap |
| `NIGHTSHIFT_MONTHLY_BUDGET_USD` | 40 | Monthly spend cap |
| `NIGHTSHIFT_TASK_BUDGET_USD` | 5 | Cap per fix; a task that hits it stops with its work saved |
| `NIGHTSHIFT_TASK_MINUTES` | 30 | Time limit per fix |
| `NIGHTSHIFT_PR_CHECK_BUDGET_USD` | 1.5 | Cap per checked PR |
| `NIGHTSHIFT_SETUP_BUDGET_USD` | 1.0 | Cap for setting up one repo |
| `NIGHTSHIFT_<AGENT>__MODEL` | see `nightshift models` | First model an agent tries (any OpenRouter id) |
| `NIGHTSHIFT_<AGENT>__LADDER` | see `nightshift models` | Models tried next, as a JSON list |
| `NIGHTSHIFT_ROUTER_LEARNING` | true | Reorder each agent's models from past outcomes |
| `NIGHTSHIFT_SYNC_MINUTES` | 60 | How often to check your published PRs for new review feedback (also when the app starts); 0 = only with Sync now |
| `NIGHTSHIFT_COMMIT_ATTRIBUTION` | `Generated by NightShift` | Last line of published commits; empty leaves it out |
| `NIGHTSHIFT_SANDBOX_CPUS` / `_MEMORY` / `_PIDS` | 2 / 4g / 1024 | Limits for each sandbox container |
| `NIGHTSHIFT_SANDBOX_COMMAND_TIMEOUT` | 900 | Seconds per command in the sandbox |

Agents: `planner`, `coder`, `reviewer`, `setup`, `pr_writer`, `pr_checker`, `chat`, `cartographer`,
`scout`, `briefer`, `relevance`, `pr_assessor`, `verifier`.

## 6. Troubleshooting

| Problem | Fix |
| --- | --- |
| `connection refused` on port 5432 | `docker compose up -d`, then wait for the healthcheck (`docker compose ps`) |
| A model call fails with 402 | Your OpenRouter key is out of credit or over its limit; raise it at openrouter.ai |
| A model call fails with 401 or 403 | `OPENROUTER_API_KEY` is missing or wrong in `.env` |
| A sandbox fails to start because its image is missing | `uv run nightshift sandbox build` |
| A repo's setup is "blocked" | Read the reason on the Repos page; pass working commands to `nightshift sandbox try` |
| Publishing fails with a permission error | `gh auth status`: `gh` must be logged in as you, and able to fork |
| The web app shows an old UI | Rebuild it: `cd frontend && npm run build` |
| Docker runs out of disk | Remove old sandbox images: `docker image ls nightshift-repo/*`, keep the newest per repo |

To remove a repo and everything stored about it: `uv run nightshift watch remove owner/repo --purge`.
