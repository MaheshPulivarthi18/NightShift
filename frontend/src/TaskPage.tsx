import { useCallback, useEffect, useRef, useState } from "react";
import { api, CommandResult, GateInfo, ModelUse, ModelView, TaskDetail } from "./api";
import { Diff, Html, Log, StatusBadge, Title, money } from "./ui";

const TABS = ["Plan", "Log", "Verification", "Review", "Diff", "PR"] as const;
type Tab = (typeof TABS)[number];

export function TaskPage({ id }: { id: number }) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Plan");

  const load = useCallback(() => {
    api.task(id).then(setTask, (e: Error) => setError(e.message));
  }, [id]);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  // Open on the tab that matters for the task's state, once.
  const opened = useRef(false);
  useEffect(() => {
    if (!task || opened.current) return;
    opened.current = true;
    if (task.status === "working" || task.status === "planning") setTab("Log");
    else if (task.status === "ready" || task.status === "published") setTab("PR");
    else if (task.status === "needs_operator") setTab(task.check ? "Verification" : "Log");
  }, [task]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "Escape") window.location.hash = "#/";
      const n = Number(e.key);
      if (n >= 1 && n <= TABS.length) setTab(TABS[n - 1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (error) return <p className="error">{error}</p>;
  if (!task) return <p className="muted pad">Loading task {id}…</p>;

  return (
    <div className="task">
      <a className="back" href="#/">← Tasks</a>
      <header className="task-head">
        <div className="mono dim">
          {task.repo}#{task.issue} · task {task.id} · base {task.base_commit.slice(0, 7) || "—"}
          {task.rehearsal && <span className="tag">rehearsal</span>}
        </div>
        <h1><Title text={task.title} /></h1>
        <div className="facts">
          <StatusBadge status={task.status} busy={task.busy} />
          <span className="mono">{money(task.cost_usd)}</span>
          {task.coder_steps != null && <span className="mono dim">{task.coder_steps} coder steps</span>}
          <ModelsUsed uses={task.models} />
          {task.issue_url && (
            <a href={task.issue_url} target="_blank" rel="noreferrer">
              issue ↗
            </a>
          )}
          {task.pr_url && (
            <a href={task.pr_url} target="_blank" rel="noreferrer">
              pull request ↗
            </a>
          )}
        </div>
        {task.error && <p className="stop-reason">Stopped: {task.error}</p>}
      </header>

      <ActionPanel task={task} onChange={load} />

      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>
      <section className="tab-body">
        {tab === "Plan" && <Html html={task.plan_html} />}
        {tab === "Log" && <Log runId={task.run_id} onStatus={load} />}
        {tab === "Verification" && <Verification task={task} />}
        {tab === "Review" &&
          (task.reviews.length ? task.reviews.map((r) => <Html key={r.round} html={r.html} />) : <p className="muted">No review yet.</p>)}
        {tab === "Diff" && <Diff text={task.diff} />}
        {tab === "PR" &&
          (task.pr ? (
            <article className="pr">
              <h2><Title text={task.pr.title} /></h2>
              <Html html={task.pr.html} />
              <h3>Commit message</h3>
              <pre className="block">{task.pr.commit}</pre>
            </article>
          ) : (
            <p className="muted">The PR is drafted once the fix is verified and reviewed.</p>
          ))}
      </section>
    </div>
  );
}

// "coder sonnet-5.5 → opus-5.5": the router's choices, so a run's models are never a guess.
function ModelsUsed({ uses }: { uses: ModelUse[] }) {
  if (!uses.length) return null;
  const short = (m: string) => m.split("/").pop();
  return (
    <span className="models dim">
      {uses.map((u) => (
        <span key={u.agent} title={`${u.agent}: ${u.models.join(" → ")}${u.failed.length ? ` (failed: ${u.failed.join(", ")})` : ""} · ${money(u.cost_usd)}`}>
          {u.agent} <span className="mono">{u.models.map(short).join(" → ")}</span>
        </span>
      ))}
    </span>
  );
}

function ActionPanel({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  if (task.status === "planned") return <Gate1 task={task} onChange={onChange} />;
  if (task.status === "ready") return <Gate2 task={task} onChange={onChange} />;
  if (task.status === "approved") return <RunPanel task={task} onChange={onChange} />;
  return null;
}

const lines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

function Gate1({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  if (!task.actionable) return <AskFirst task={task} onChange={onChange} />;
  return <ApproveForm task={task} onChange={onChange} />;
}

// The Planner found nothing to fix yet: the way forward is the question on the issue, not a run.
function AskFirst({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [armed, setArmed] = useState(false);
  const [msg, setMsg] = useState<string | null>(task.claim_url);
  return (
    <section className="gate">
      <header>
        <span className="gate-mark">Gate 1</span>
        <h2>Not actionable yet</h2>
        <p className="muted">The Planner needs answers before there is anything to fix, so this plan cannot run. Ask on the issue, then plan it again once it is answered.</p>
      </header>
      <p className="quote">{task.claim_comment}</p>
      <div className="actions">
        {!msg && !armed && <button className="primary" onClick={() => setArmed(true)}>Ask on the issue…</button>}
        {!msg && armed && (
          <>
            <span>Post this publicly on {task.repo}#{task.issue}?</span>
            <button className="primary" onClick={() => api.claim(task.id, true).then((r) => setMsg(r.result), (e: Error) => setMsg(e.message))}>
              Yes, post it
            </button>
            <button className="ghost" onClick={() => setArmed(false)}>Cancel</button>
          </>
        )}
        {msg && <span className="mono dim">{msg}</span>}
        <button className="ghost" onClick={() => api.decide(task.id, false, "", [], []).then(onChange, (e: Error) => setMsg(e.message))}>
          Reject
        </button>
      </div>
    </section>
  );
}

function ApproveForm({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [setup, setSetup] = useState(task.setup_commands.join("\n"));
  const [tests, setTests] = useState(task.test_commands.join("\n"));
  const [note, setNote] = useState("");
  const [models, setModels] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);

  const decide = (approve: boolean) =>
    api.decide(task.id, approve, note, lines(setup), lines(tests), models).then(onChange, (e: Error) => setMsg(e.message));

  return (
    <section className="gate">
      <header>
        <span className="gate-mark">Gate 1</span>
        <h2>Approve the plan</h2>
        <p className="muted">Nothing runs or costs more until you approve. Commands run only inside the sandbox.</p>
      </header>
      <div className="gate-grid">
        <label>
          <span>Setup commands</span>
          <textarea className="mono" rows={4} value={setup} onChange={(e) => setSetup(e.target.value)} />
        </label>
        <label>
          <span>Test commands</span>
          <textarea className="mono" rows={4} value={tests} onChange={(e) => setTests(e.target.value)} />
        </label>
      </div>
      {task.model_choices && (
        <ModelPickers choices={task.model_choices} value={models} onChange={setModels} />
      )}
      <label className="full">
        <span>Note (optional)</span>
        <input value={note} onChange={(e) => setNote(e.target.value)} />
      </label>
      <div className="actions">
        <button className="primary" disabled={!lines(tests).length} onClick={() => decide(true)}>Approve plan</button>
        {!lines(tests).length && <span className="muted">Add a test command: without one, a fix cannot be verified.</span>}
        <button className="ghost" onClick={() => decide(false)}>Reject</button>
        {msg && <span className="error">{msg}</span>}
      </div>
    </section>
  );
}

const AGENT_LABEL: Record<string, string> = { coder: "Coder", reviewer: "Reviewer", pr_writer: "PR writer" };

export const modelLabel = (m: ModelView) =>
  `${m.id} · $${m.input_per_m}/$${m.output_per_m} per M${m.intelligence ? ` · score ${m.intelligence}` : ""}`;

// The router picks each agent's model; here the operator may pick another one for this task instead.
function ModelPickers({
  choices,
  value,
  onChange,
}: {
  choices: NonNullable<TaskDetail["model_choices"]>;
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
}) {
  return (
    <div className="gate-grid models-pick">
      {Object.entries(choices).map(([agent, c]) => (
        <label key={agent}>
          <span>{AGENT_LABEL[agent] ?? agent} model</span>
          <select value={value[agent] ?? ""} onChange={(e) => onChange({ ...value, [agent]: e.target.value })}>
            <option value="">
              Router decides: {c.default} ({c.why})
            </option>
            {c.options.map((m) => (
              <option key={m.id} value={m.id}>
                {modelLabel(m)}
              </option>
            ))}
          </select>
        </label>
      ))}
    </div>
  );
}

function RunPanel({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <section className="gate calm">
      <header>
        <h2>Plan approved</h2>
        <p className="muted">Runs setup, a baseline, the Coder, verification and the Reviewer. Limits: $5, 30 minutes.</p>
        {Object.keys(task.chosen_models).length > 0 && (
          <p className="mono dim">
            Chosen models:{" "}
            {Object.entries(task.chosen_models)
              .map(([agent, model]) => `${AGENT_LABEL[agent] ?? agent} ${model}`)
              .join(" · ")}
          </p>
        )}
      </header>
      <div className="actions">
        <button className="primary" onClick={() => api.run(task.id).then(onChange, (e: Error) => setMsg(e.message))}>
          Run task
        </button>
        {msg && <span className="error">{msg}</span>}
      </div>
    </section>
  );
}

function Gate2({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [info, setInfo] = useState<GateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [typed, setTyped] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [claimMsg, setClaimMsg] = useState<string | null>(task.claim_url);
  const [armed, setArmed] = useState(false);

  const dryRun = () => {
    setBusy(true);
    setMsg(null);
    api.publish(task.id, false).then(setInfo, (e: Error) => setMsg(e.message)).finally(() => setBusy(false));
  };
  const publish = () => {
    setBusy(true);
    api.publish(task.id, true).then(
      (r) => {
        setInfo(r);
        onChange();
      },
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  const blocked = info ? info.blockers.length > 0 || info.drift.length > 0 : true;

  return (
    <section className="gate">
      <header>
        <span className="gate-mark">Gate 2</span>
        <h2>Publish the pull request</h2>
        <p className="muted">
          Checks the issue is still free and upstream has not changed these files, then pushes to your fork and opens the PR.
        </p>
      </header>
      <div className="actions">
        <button className="ghost" onClick={dryRun} disabled={busy}>
          {busy && !info ? "Checking…" : "Check freshness (dry run)"}
        </button>
        {msg && <span className="error">{msg}</span>}
      </div>
      {info && (
        <div className="gate-result">
          <p className="mono">
            {info.branch} → {info.base} @ {info.head.slice(0, 7)} · {info.files.length} files
          </p>
          {info.blockers.map((b) => (
            <p key={b} className="blocked">Blocked: {b}</p>
          ))}
          {info.drift.length > 0 && (
            <div className="blocked">
              <p>Upstream changed {info.drift.join(", ")} since the fix was verified, so its tests no longer cover that code.</p>
              <button
                className="primary small"
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  api
                    .revise(task.id, `Upstream changed ${info.drift.join(", ")} after the fix was verified. Redo the fix on the latest commit.`, true)
                    .then(onChange, (e: Error) => setMsg(e.message))
                    .finally(() => setBusy(false));
                }}
              >
                Redo on latest commit
              </button>
            </div>
          )}
          {info.published && info.url && (
            <p className="ok">
              Opened <a href={info.url} target="_blank" rel="noreferrer">{info.url}</a>
            </p>
          )}
          {!blocked && !info.published && (
            <div className="confirm">
              <label>
                <span>
                  Type <b className="mono">{task.repo}</b> to publish under your name
                </span>
                <input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} />
              </label>
              <button className="danger" disabled={typed !== task.repo || busy} onClick={publish}>
                Push and open PR
              </button>
            </div>
          )}
        </div>
      )}
      <details className="claim">
        <summary>Claim comment {claimMsg ? "· posted" : "· not posted"}</summary>
        <p className="quote">{task.claim_comment}</p>
        {!claimMsg && !armed && (
          <button className="ghost" onClick={() => setArmed(true)}>
            Post claim comment…
          </button>
        )}
        {!claimMsg && armed && (
          <div className="actions">
            <span>Post this publicly on {task.repo}#{task.issue}?</span>
            <button className="primary" onClick={() => api.claim(task.id, true).then((r) => setClaimMsg(r.result), (e: Error) => setClaimMsg(e.message))}>
              Yes, post it
            </button>
            <button className="ghost" onClick={() => setArmed(false)}>Cancel</button>
          </div>
        )}
        {claimMsg && <p className="mono dim">{claimMsg}</p>}
      </details>
    </section>
  );
}

function Run({ r, label }: { r: CommandResult; label?: string }) {
  return (
    <details className={`run ${r.ok ? "pass" : "fail"}`}>
      <summary>
        <span className="dot" aria-hidden />
        {label && <b>{label}</b>}
        <span className="mono">{r.command}</span>
        <span className="mono dim">{r.timed_out ? "timed out" : r.ok ? "passed" : `exit ${r.exit_code}`} · {r.seconds.toFixed(0)}s</span>
      </summary>
      <pre className="block">{r.tail}</pre>
    </details>
  );
}

function Verification({ task }: { task: TaskDetail }) {
  const c = task.check;
  return (
    <div className="verify">
      {c ? (
        <>
          <p className={c.passed ? "ok" : "blocked"}>
            {c.passed ? "Verified by code: the test fails without the fix, passes with it, and no suite regressed." : "Verification did not pass."}
          </p>
          {c.problems.map((p) => (
            <p key={p} className="blocked">{p}</p>
          ))}
          <p className="mono dim">
            tests: {c.test_files.join(", ") || "none"} · source: {c.source_files.join(", ") || "none"}
          </p>
          {c.fails_without_fix && <Run r={c.fails_without_fix} label="Without the fix" />}
          {c.passes_with_fix && <Run r={c.passes_with_fix} label="With the fix" />}
          <h3>Suites after the change</h3>
          {c.suite.map((r) => <Run key={r.command} r={r} />)}
        </>
      ) : (
        <p className="muted">No verification has run yet.</p>
      )}
      {task.baseline.length > 0 && (
        <>
          <h3>Baseline (before any change)</h3>
          {task.baseline.map((r) => <Run key={r.command} r={r} />)}
        </>
      )}
    </div>
  );
}
