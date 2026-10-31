# Backlog

Collected 2026-10-24 after the first live PR ([microsoft/multilspy#169](https://github.com/microsoft/multilspy/pull/169))
and the move of every page into the React app; extended 2026-10-31 with what the repo workspace, the laya #50
task, the multilspy setup and the 20-repo comparison showed. Tick items off as they land; add the UI bugs you
found under "Found by you".

**Next up:** the plan below.

## Plan, 2026-10-31: trustworthy for maintainers, productive for contributors

From the evidence in "NightShift: is it useful?": contributors' pull requests are accepted (6 of 9 merged or
approved, none rejected, median $0.75); the PR checker is right when it blocks but blocked only 3 of 10 PRs
maintainers rejected, partly because the sandbox runs 3 of pydantic's 257 test files; a model call once hung
15 minutes; one task cost $4.55. In order:

**Maintainers**
- [x] M1. A deadline on every model call (`NIGHTSHIFT_MODEL_CALL_SECONDS`, 300 s), then the next model.
- [x] M2. Ship the checker calibration with the benchmark, and say "not fully checked" when the plan or the
      review could not run (the budget cap once made such checks read "ready"). Done in #17.
- [x] M3. The PR's own CI as evidence: a job failing on the PR and not on its base is a must-fix, named; failing
      on both is advice. Done in #17. (opentelemetry-python #5237's failing job also failed on its base, so CI was
      not why it was rejected, as first assumed.)
- [x] M4. Run the tests related to the change (beside or importing the changed modules), not only the
      environment's narrow set. Done: up to 15 test files (name match, then the same folder, then tests importing
      the module) or the changed Go packages, with the environment's own runner, on the base and with the PR.
      Live on pydantic #14153: 3 related files, 280 tests, beside the suite.
- [x] M5. Incoming PRs for repos you maintain: checked automatically (opt-in per repo), listed in "Needs you",
      the review posted only on your click. Done: "check new PRs" on the Repos page (maintainer view); each sync
      checks up to `NIGHTSHIFT_AUTO_CHECK_PER_SYNC` new PRs or new pushes per repo (no drafts, bots or your own);
      Needs you lists each finished check until its review is posted or it is dismissed.
- [ ] M6. A third benchmark (a Go repo) after M3 and M4. Target: 8 of 10 merged PRs ready, 5 of 10 rejected blocked.
- [ ] M7. The GitHub App, once M5 shows maintainers use the checks.

**Contributors**
- [x] C1. Each repo's AI policy at Gate 2 (done in #18): the `Assisted-by:` trailer where the rules ask for it (OpenTelemetry);
      "defend it yourself" repos (sarama) named beside the understanding check.
- [x] C2. A cost guard per task: review rounds after the first on a cheaper model; expected against actual cost at
      Gate 2 (tortoise-orm: $2.65 of $4.55 was 11 review calls on Sol). Done: rounds from the second use
      `NIGHTSHIFT_REVIEWER_FOLLOWUP` (Terra); Gate 2 warns past 1.5x the expected cost, naming the agent that spent most.
- [x] C3. CI status of published PRs: on the Track record, with a warning when an open one is red (#17), and
      in "Needs you" and on the board: the sync reads the CI on each open PR's latest commit (benchmark and
      coverage jobs are advice, not a failure). (tortoise-orm #2390 shows no CI: a first-time contributor's workflows wait for a
      maintainer's approval.)
- [ ] C4. Fit with you (D5); C5. a drafted "fixed by #N" comment (D7); C6. lock-file churn as a package summary
      (P3) and setup time apart from the Coder's (P4).

**Reliability:** R1 a separate test database, R2 a backup before a purge, R3 "GitHub is unreachable" detected
once, L4 sidebar search, L5 a banner when the server runs old code.

## Found by you

- [ ] _(add the UI bugs you noticed here)_

## UI bugs

- [x] **Repos, "Last run" counts task and map runs.** microsoft/multilspy shows "done 1h ago" next to
      "no report yet". Show only health report runs there. Fixed: the column shows health report runs only.
- [x] **Moved repos keep their old name in the watchlist.** MadsLorentzen/ai-job-search is now
      ai-job-search-dev/ai-job-search; Repos, the report and its links still use the old name. Rename
      the row when GitHub reports a new name. Fixed: planning and reports follow GitHub's rename; a row already made under the new name is merged into the watched one (done for ai-job-search).
- [x] **"plan" link on issues that are already taken.** The report shows it under "Already being worked
      on" too. Hide it there, or mark it as taken. Fixed: issues under "Already being worked on" get a chat link, no plan link.
- [x] **Backticks in titles show raw** (multilspy #101: `` `start_server` context manager ``). Render
      inline code in task titles and cards. Fixed: task cards, task, PR and chat titles render `inline code`.
- [x] **No way to archive a task.** The laya rehearsal (task 1) sits in "Waiting on you" forever. Add
      archive, and remove repos from the watchlist in the UI. Fixed: Archive / Unarchive on the task page (`nightshift task archive`), "Show archived" on the board; Repos has Remove, which keeps history (adding the repo again restores it; `watch remove --purge` deletes).
- [x] **PR Writer skips required template sections** (headroom #4456: no Real Behavior Proof, Runtime Rollout
      Safety or Review Readiness; its own Testing section replaced the template's checkboxes). Fill every
      template section, tick only what ran, and have code refuse Gate 2 when a template heading is missing.
      Repos that require real-run proof need it gathered before Gate 2, not after. Fixed: the PR Writer keeps every template heading and fills it (ticks only what ran; says what is missing instead of inventing it), test runs go into the template's testing section, and Gate 2 blocks a body that lost a section. Still open: gathering real-run proof before Gate 2 (see "Proof environments").
- [x] **Follow-ups on a published PR end at "ready"** (headroom #4456 after review fixes), and there is no way to
      push a follow-up commit or attach an existing PR (laya #60) from NightShift. Add "push follow-up to the
      PR" at Gate 2 for published tasks, and "attach PR" for work published by hand; set "published" from them. Fixed: a revised published task's Gate 2 pushes one follow-up commit (the difference between the published fix and the revised one) to its PR's branch on your fork, never forced, optionally replacing the description; "Attach it" links a PR opened by hand (`nightshift task attach`), keeping its diff for later follow-ups.
- [ ] **Proof environments must match the repo's pinned models** (headroom pins Hugging Face revisions; caching
      the latest revision made a working fix look broken), and proof inputs must be ones the code acts on
      (use the issue's own reproduction payload).
- [x] **Gate 2 does not show what will be published:** the exact commit message, branch name and
      attribution line (ai-job-search #768 went out with "Generated by NightShift" and a `nightshift/` branch
      on defaults). Show them, editable, and warn when the Testing section contains failures. Fixed: Gate 2 shows the title, body, commit message, attribution line and branch, all editable (edits are kept), and warns about failing suites and the attribution line.
- [x] **No general Revise button.** Gate 2 now has "Redo on latest commit" for upstream drift (and
      `POST /api/tasks/{id}/revise` exists); ready and stopped tasks still need a revise box with a note. Fixed: "Revise and run again" on ready, stopped and published tasks: a note, and optionally the latest commit.
- [x] **After "#N is not free", no way to see who took it.** Link the PRs named in the refusal. Fixed: every #N in the refusal links to GitHub.
- [x] **Report page repeats itself:** the header shows repo, date and cost that the report body shows again. Fixed: the header keeps the repo name and the link hint only.
- [x] **Jobs "Step" column is empty** for finished runs. Show the step only while a run is live. Fixed: the step shows next to the status while a run is live; the column is gone.
- [x] **`nightshift serve` keeps old code** until restarted. Add `--reload` for development. Fixed: `nightshift serve --reload`.

## Worker bugs and gaps

- [x] **Planner proposed setup commands that cannot work** (laya: `pip install -e` on an engine with no
      pyproject). Fixed: planning runs the setup in the sandbox before Gate 1; a failure goes back to the
      Planner once, and the plan says "setup commands ran successfully" or why they failed.
- [x] **Planner proposed a test that cannot fail** (multilspy: a jedi test for a bug jedi does not have).
      Reproduce the issue in the sandbox before planning the test. Fixed: for a bug the Planner writes a reproduction script (the issue's own case); it runs in the sandbox before Gate 1, which says whether it failed as the issue says or passed (a misread bug). Live: ai-job-search #766's reproduction failed with the expected AssertionError.
- [x] **Repo virtualenv and node tools are not on PATH** in the sandbox (`jedi-language-server` not found,
      so a suite showed red for no reason). Put `.venv/bin` and `node_modules/.bin` on PATH. Fixed: every sandbox command has the repo's `.venv/bin` and `node_modules/.bin` first on PATH.
- [x] **Testing section is noisy:** it quotes log lines. Quote the test runner's summary and the failing
      assertion only. Fixed: it quotes the test runner's result line and the failing assertion (the last lines only when neither is recognised).
- [x] **Reviewer missed the event-loop bug** in the first multilspy fix (the issue's own `SyncLanguageServer`
      case). When feasible, run the issue's reproduction as part of review. Fixed: verification re-runs the issue's reproduction with the fix and refuses a fix that passes its own test but not the issue's case; the Reviewer sees both runs, and the PR says the issue's case now passes.
- [ ] **Decide the Reviewer policy on error and fallback paths** that skip a new field (laya `ws_router.py`).
- [x] **Setups that pull GPU builds time out** (headroom: `uv sync --extra dev` downloads CUDA PyTorch, over
      2.5 GB). The Planner should default to CPU-only wheels (`uv pip install --torch-backend cpu`) and call
      `.venv/bin/...` instead of `uv run`, which re-syncs. Task 3 was revised this way by hand. Fixed: the Setup agent picks CPU-only builds and calls `.venv/bin/...`.
- [x] **A run that dies leaves its task "working" forever** (process killed, session closed), and
      `task revise` refuses "working" tasks; its sandbox container can also be left running. Add a heartbeat
      so stale runs become "needs you", remove orphaned `ns-task-*` containers at startup, and allow revise. Fixed: planning and runs write a heartbeat every 30 s; one silent for 3 minutes becomes "needs you" (or "failed" while planning) and can be revised; the server removes task sandboxes older than a run's time limit at startup.
- [x] **Health reports go stale:** #705 was "free" in a 4-day-old report. Re-check listed issues when the
      report is opened, or re-run reports older than a day. Fixed: opening a report re-checks its top 15 listed issues on GitHub (cached 10 min): taken ones are named in a banner and their plan link becomes "taken". First catch: multilspy #147 (open PR #148).
- [x] **Report claim detection misses timeline cross-references** with the read-only token (planning now
      uses three sources; reports still rely on closing references). Fixed: the live re-check uses the same three sources as planning.
- [x] **Per-repo publish settings:** attribution line and branch prefix are environment variables today;
      store them per repo (multilspy: no attribution). Fixed: Gate 2's "use this attribution and branch prefix from now on" stores them per repo; multilspy and headroom are set to no attribution.

- [x] **Sandbox lacked common toolchains**, so setup failed after approval: no C/C++ compiler (headroom:
      `hnswlib`) and no Bun (ai-job-search). Fixed: image v3 has build-essential and Bun 1.4.4, and the base
      image is part of the repo-image key. Rust and Go are still missing (see "Toolchains on demand").

## Enhancements, in priority order

1. [ ] **Screenshot evidence** (the PRD's "web evidence first"): Playwright in the sandbox, before and
       after images in the PR.
2. [x] **PR watcher:** follow published PRs (#169) for reviews, CI and merges; draft replies for approval. Done:
       background sync (at startup and every `NIGHTSHIFT_SYNC_MINUTES`, plus "Sync now") brings review feedback and
       replies to asked questions into "Needs you"; replies are drafted and posted only on confirm. CI status on the
       PR is still not shown.
3. [ ] **Free issues across the watchlist:** one live-checked list of issues nobody has taken. Part of D4.
4. [x] **Toolchains on demand in the sandbox** (Bun for ai-job-search #765, Rust, Go). Done: the Setup agent's environment images (system packages; pnpm, yarn, Go, Rust).
5. [ ] **Repo recipes in the UI:** see and edit each repo's saved setup and test commands.
6. [x] **Architecture map view** (the L1 data exists; show it per repo). Done: the repo's Map tab (canvas).
7. [x] **Track record:** PRs opened, merged, time to merge; estimated vs actual cost per task. Done: the Track
       record page (`#/record`): outcomes from GitHub, median time to merge, cost per PR and per merged PR,
       attempts and the understanding check; earlier PRs can be added by link.
8. [ ] **Frontend tests** (vitest); there are none yet.

## Found 2026-10-28 to 2026-10-31

### Discovery and reports

- [x] **D1. "Is it free" misses a fix PR that never names the issue.** redis-py #4454 was listed as a free starter
      issue, but the reporter's PR #4455 (merged Oct 22) fixed it without mentioning it; only a maintainer's comment
      on a duplicate PR said "This fixes the same problem as #4455". Reports and the check before planning should
      also read maintainer comments on PRs that reference the issue for other PR numbers, and search the repo's
      recent PRs (open and merged since the issue was opened) for its key terms (here `TS.ALTER`, `labels`).
      Fixed: two more sources. PRs named in comments (on the issue, and on referencing PRs closed unmerged) block
      it when the comment says they do its work; PRs opened since the issue whose titles share its key terms are
      possible matches, shown as a Gate 1 warning. Live: #4454 now reads "merged PR #4455 … does its work, says a
      comment on #4462".
- [x] **D2. A null byte in GitHub text fails the whole report.** jaegertracing/jaeger: a PR body with `\u0000`
      cannot be stored in Postgres (`UntranslatableCharacter`). Strip null bytes from everything fetched from GitHub.
      Fixed: the GitHub client drops null bytes from everything it returns.
- [x] **D3. GitHub timeouts on large repos fail the whole report.** fastapi, starlette, django and instructor timed
      out (`GitHubTimeout`/`ReadTimeout`, after up to 31 minutes), kafka-go lost the connection. Retry a timed-out
      page with a smaller page size and backoff, and finish with what was fetched, saying what is missing.
      Fixed: a slow or dropped answer shrinks the page like a 504; a list that still fails at the smallest page
      is left incomplete after two tries, the report says what is missing, and the next run asks for it again.
      A fetch that got nothing still fails.
- [x] **D4. Compare the watchlist: "where should I contribute first?"** Today that took 20 CLI reports and a
      script. One page ranking watched repos on outside-PR merge rate and response time (last 90 days), PRs left
      unanswered, free starter issues by size, and the repo's AI policy; with "report all" running a few at a time
      and showing progress. Includes enhancement 3 (free issues across the watchlist).
      Done: the "Where to contribute" page (`#/compare`): every watched repo ranked (best first, merge rate,
      response time, starter issues), its AI rules, and its starter issues with a live "still free?" check;
      "Refresh stale reports" reruns missing or week-old reports two at a time.
- [x] **D8. Reports list taken issues as free.** redis-py's report listed 10 starter issues; the live check found
      9 taken (open PRs that close or name them, one assigned, one fixed by a PR named only in a comment). The report
      should run the live check's sources on its starter list before rendering it, and rank the comparison by
      live-checked counts.
      Fixed: an open PR, even one that looks abandoned, never makes an issue a starter (it goes to "ask first",
      naming the PR); a PR that names `#N` counts as linked; and every starter, then every "ask first" issue (up
      to 15), is checked live on GitHub before the report is saved, taken ones moving to "Already being worked
      on" with the reason. redis-py now lists one starter (#3141), the one that is free.
- [ ] **D5. Fit with you.** Ranking by stack fit needs a profile: languages, frameworks and past PRs (from your
      GitHub), so "fit" is not typed in by hand.
- [ ] **D6. Show a repo's AI policy where it matters.** sarama: "explain and defend the changes … without relying
      on AI tools"; OpenTelemetry Python: no AI-written comments, an `Assisted-by:` trailer. Show it on the repo and
      at Gate 2, and add the trailer when the repo asks for it (product direction: follow repo AI policy).
- [ ] **D7. Offer the maintainer courtesy when an issue is already fixed** (#4454 is open after #4455 merged): a
      drafted "fixed by #N" comment, posted only on confirm.

### Repo setup

- [x] **S1. The environment image build ran 20 silent minutes** before timing out (multilspy, attempt 1 of 3; 40 of
      the setup's 43 minutes). Its output is now on the log; also lower the limit (5 to 10 minutes), and cache
      package layers across images.
      Done: the build streams to the log; its limit is a setting (`NIGHTSHIFT_IMAGE_BUILD_SECONDS`, 10 minutes);
      a build stopped at the limit tells the Setup agent where it was and to ask for smaller packages; downloads
      stay in BuildKit's cache between builds (a second image with one more package: 2 s), and toolchain layers
      come first so images needing the same toolchain share them.
- [x] **S2. Setup agent speed** (proposed, not built): attach the log files a failure names, keep the sandbox
      between attempts instead of rebuilding, and a rule table for known failures (no pip in a uv venv, missing
      `-dev` packages).
      Done: known causes (no pip in a uv venv, a missing C header and its -dev package, pg_config, a missing
      toolchain, no root, the Python version, no disk) are named in the attempt for the agent; the end of each log
      file a failed setup command names is attached; and after each slow setup step that succeeds the sandbox is
      saved, so the next attempt starting with the same steps resumes there (tested: 4.6 s instead of re-running).
- [x] **S3. A "test command" must run tests.** multilspy's environment counts `ls tests/multilspy` as a passing
      test command; require a test runner (pytest, go test, jest …) and say which parts of the suite were left out
      (multilspy ran only the Python language-server tests).
      Done: a proposal whose test commands run no test runner is refused before it runs (and a remembered
      recipe's non-test commands are dropped); once the tests pass, NightShift counts how many of the repo's test
      files they cover and which folders they leave out, says so to the Planner and the PR checker, and shows it
      on the Knowledge tab.

### Planning and the checks

- [x] **P1. The revised plan is not checked again.** The plan check sends must-fix findings back once and the new
      plan goes to Gate 1 unchecked (the log says so now). Option: re-check only the earlier must-fix findings,
      at a fraction of the cost.
      Done: after the revision, one short call (no code reading, ~$0.001) marks each must-fix finding addressed or
      not, with where; the log says so, the plan shows it, the Checks ladder's plan row passes only if all are
      addressed, and Gate 1 warns about any that are not (no second revision). Live: laya #50's revised plan,
      addressed, with the steps that fix it.
- [x] **P2. Tune the scope check from real runs.** Size limits (400 / 1,000 / 2,500 changed lines) are a first
      guess. A lock file the plan did not name sends the change back; a dependency upgrade that truly needs it
      should be able to ask you instead of re-planning.
      Done: limits are a setting (`NIGHTSHIFT_SCOPE_LIMITS`), now 250 / 600 / 2,500 from the 20 benchmark PRs
      maintainers merged (non-test lines: median 8, 90% under 190, largest 376). A lock or generated file the Coder
      truly needs can be asked for (`keep_generated` on submit): the run stops, you allow or refuse, and it continues
      from where it stopped.
- [ ] **P3. Show regenerated files as a summary,** not a diff: task 72 (laya #50) re-locked ~630 lines in each of
      two lock files; a reviewer, human or model, needs "these packages changed versions", not the lines.
- [ ] **P4. Long installs eat the task's 30 minutes** (task 72 stopped at the time limit). Count setup and the
      baseline apart from the Coder's time, or reuse the repo image so they are not repeated.
- [ ] Decide the Reviewer policy on error and fallback paths (from above, still open).

### Task lifecycle and the UI

- [x] **L1. Understanding check before publishing** (product direction): before Gate 2, you explain the change
      in your own words, or answer two or three questions about it, so you can defend it as repos like sarama
      require. It warns and never blocks (agreed 2026-10-25; PRD FR-16). Done: "Can you explain this change?"
      above Gate 2: three questions about the diff (the cause, why the fix works, what the test proves), your
      answers graded against the code with feedback, the repo's AI rules shown, every attempt kept; Gate 2 warns
      until it is passed. About $0.002 a check (Luna).
- [x] **L2. A chat thread through the whole life of an issue** (proposed: explain, diagnose a failure, gather
      evidence, revise, raise and post the PR from one thread with action cards). Done: the chat knows the task's
      state (stage, why it stopped, the review's comments) and proposes its next step as a card (plan, approve and
      run, continue from where it stopped, start over, plan again, Gate 2, archive); a card runs only on a click,
      through the task page's own endpoints, and only if the task's state still allows it.
- [x] **L3. Revising keeps no history.** Revising task 72 cleared its diff and results from the database (the last
      attempt survives only in `reports/…/diff.patch`). Keep each attempt: its diff, checks, review and cost.
      Fixed with resume: revising keeps every attempt (summary on the task, diff in its folder) and, by default,
      continues from where it stopped: the change applied, the baseline reused on the same commit, and the
      Coder given only what failed (the review's comments, the regressions, the check's problems). "Start over"
      begins from the plan.
- [ ] **L4. A long sidebar.** With 24 repos the sidebar needs search, pinning, or grouping by "has work".
- [ ] **L5. Say when the server runs old code.** Changes need a restart of `nightshift serve`, and nothing on the
      page shows it; show the running version next to the latest.
- [ ] **L6. CI status of published PRs** on the task and in "Needs you" (the rest of enhancement 2).
- [ ] **L7. Check the live log on screen:** the running timer, auto-scrolling output and open-while-running
      states of the new sandbox log were tested through the event stream only.

### Reliability and data

- [ ] **R1. Tests share the development database.** They no longer leave rows or containers behind, but a separate
      test database (or schema) would make that impossible rather than cleaned up.
- [ ] **R2. A backup before destructive actions.** `watch remove --purge` and data wipes have no undo; take a
      `pg_dump` first (the last one is from 2026-10-27).
- [ ] **R3. Network trouble is reported as each command's own failure.** One flaky connection failed five reports
      and a GitHub check in a row; detect it once ("GitHub is not reachable"), pause and retry, instead of failing
      every job.

### Done in this stretch

- [x] Runs write a heartbeat, so a long quiet step (a 30-minute install) is never marked interrupted.
- [x] Tests no longer start a real repo setup, leak sandbox containers, or leave run events behind.
- [x] Checks run cheapest first (plan check, scope, test, review, then the full suites once), shown live on the
      task's Checks tab.
- [x] The log tells "caught" and "fixed" apart from warnings and errors, and shows times in your time zone.
- [x] The log shows what happened in the sandbox: each command with its output while it runs, file edits, and
      folded reads; with an All / Steps / Sandbox filter. Image builds stream there too.
- [x] The watchlist limit is a setting (`NIGHTSHIFT_WATCHLIST_MAX`, 30) enforced by the CLI and the web app.
- [x] `watch remove --purge` deletes a repo's run logs too.
