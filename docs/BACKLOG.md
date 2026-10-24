# Backlog for the next session

Collected 2026-10-24 after the first live PR ([microsoft/multilspy#169](https://github.com/microsoft/multilspy/pull/169))
and the move of every page into the React app. Tick items off as they land; add the UI bugs you found
under "Found by you".

## Found by you

- [ ] _(add the UI bugs you noticed here)_

## UI bugs

- [ ] **Repos, "Last run" counts task and map runs.** microsoft/multilspy shows "done 1h ago" next to
      "no report yet". Show only health report runs there.
- [ ] **Moved repos keep their old name in the watchlist.** MadsLorentzen/ai-job-search is now
      ai-job-search-dev/ai-job-search; Repos, the report and its links still use the old name. Rename
      the row when GitHub reports a new name.
- [ ] **"plan" link on issues that are already taken.** The report shows it under "Already being worked
      on" too. Hide it there, or mark it as taken.
- [ ] **Backticks in titles show raw** (multilspy #101: `` `start_server` context manager ``). Render
      inline code in task titles and cards.
- [ ] **No way to archive a task.** The laya rehearsal (task 1) sits in "Waiting on you" forever. Add
      archive, and remove repos from the watchlist in the UI.
- [ ] **No Revise button.** `task revise` exists only on the CLI; ready and stopped tasks need it in the UI.
- [ ] **After "#N is not free", no way to see who took it.** Link the PRs named in the refusal.
- [ ] **Report page repeats itself:** the header shows repo, date and cost that the report body shows again.
- [ ] **Jobs "Step" column is empty** for finished runs. Show the step only while a run is live.
- [ ] **`nightshift serve` keeps old code** until restarted. Add `--reload` for development.

## Worker bugs and gaps

- [ ] **Planner proposed setup commands that cannot work** (laya: `pip install -e` on an engine with no
      pyproject). Check the commands against the repo's files, or run setup once, before Gate 1.
- [ ] **Planner proposed a test that cannot fail** (multilspy: a jedi test for a bug jedi does not have).
      Reproduce the issue in the sandbox before planning the test.
- [ ] **Repo virtualenv and node tools are not on PATH** in the sandbox (`jedi-language-server` not found,
      so a suite showed red for no reason). Put `.venv/bin` and `node_modules/.bin` on PATH.
- [ ] **Testing section is noisy:** it quotes log lines. Quote the test runner's summary and the failing
      assertion only.
- [ ] **Reviewer missed the event-loop bug** in the first multilspy fix (the issue's own `SyncLanguageServer`
      case). When feasible, run the issue's reproduction as part of review.
- [ ] **Decide the Reviewer policy on error and fallback paths** that skip a new field (laya `ws_router.py`).
- [ ] **Health reports go stale:** #705 was "free" in a 4-day-old report. Re-check listed issues when the
      report is opened, or re-run reports older than a day.
- [ ] **Report claim detection misses timeline cross-references** with the read-only token (planning now
      uses three sources; reports still rely on closing references).
- [ ] **Per-repo publish settings:** attribution line and branch prefix are environment variables today;
      store them per repo (multilspy: no attribution).

- [x] **Sandbox had no C/C++ compiler**, so setup failed for packages built from source (headroom:
      `hnswlib`). Fixed: image v2 has build-essential, and the base image is part of the repo-image key.

## Enhancements, in priority order

1. [ ] **Screenshot evidence** (the PRD's "web evidence first"): Playwright in the sandbox, before and
       after images in the PR.
2. [ ] **PR watcher:** follow published PRs (#169) for reviews, CI and merges; draft replies for approval.
3. [ ] **Free issues across the watchlist:** one live-checked list of issues nobody has taken.
4. [ ] **Toolchains on demand in the sandbox** (Bun for ai-job-search #765, Rust, Go).
5. [ ] **Repo recipes in the UI:** see and edit each repo's saved setup and test commands.
6. [ ] **Architecture map view** (the L1 data exists; show it per repo).
7. [ ] **Track record:** PRs opened, merged, time to merge; estimated vs actual cost per task.
8. [ ] **Frontend tests** (vitest); there are none yet.
