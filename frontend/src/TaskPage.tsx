import { useCallback, useEffect, useRef, useState } from "react";
import { CheckStep, CommandResult, GateInfo, ModelUse, ModelView, PublishEdits, ReviewComment, TaskDetail, api } from "./api";
import { Diff, Html, Log, StatusBadge, Title, money } from "./ui";

const TABS = ["Plan", "Log", "Checks", "Review", "Diff", "PR"] as const;
type Tab = (typeof TABS)[number];

export function TaskPage({ id, embedded = false }: { id: number; embedded?: boolean }) {
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
    if (task.status === "planning") setTab("Log");
    else if (task.status === "working") setTab("Checks");
    else if (task.status === "ready" || task.status === "published") setTab("PR");
    else if (task.status === "needs_operator") setTab(task.check || task.checks.length ? "Checks" : "Log");
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
      {!embedded && <a className="back" href={`#/r/${task.repo}/items`}>← {task.repo}</a>}
      <header className="task-head">
        <div className="mono dim">
          {task.repo}#{task.issue} · task {task.id} · base {task.base_commit.slice(0, 7) || "—"}
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
          <a href={`#/chat/${task.repo}/${task.issue}`}>Chat about it</a>
          <ArchiveButton task={task} onChange={load} />
        </div>
        {task.error && <p className="stop-reason">Stopped: {task.error}</p>}
      </header>

      <ActionPanel task={task} onChange={load} />
      {task.pr_url && FEEDBACK.has(task.status) && <FeedbackPanel task={task} onChange={load} />}
      {REVISABLE.has(task.status) && <RevisePanel task={task} onChange={load} />}
      {!task.pr_url && ATTACHABLE.has(task.status) && <AttachPr task={task} onChange={load} />}

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
        {tab === "Checks" && <Checks task={task} />}
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

// Off the board, kept with its history. Not while it plans or runs.
function ArchiveButton({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  if (task.status === "planning" || task.status === "working") return null;
  return (
    <button className="ghost small" onClick={() => api.archive(task.id, !task.archived).then(onChange)}>
      {task.archived ? "Unarchive" : "Archive"}
    </button>
  );
}

const REVISABLE = new Set(["ready", "needs_operator", "failed", "published"]);
const FEEDBACK = new Set(["published", "needs_operator", "failed"]);

// What reviewers asked on the PR: read from GitHub on request; tick what to address, then one run makes
// the changes on top of the PR, and Gate 2 pushes a follow-up commit with a drafted reply to each.
function FeedbackPanel({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [comments, setComments] = useState<ReviewComment[] | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const load = () => {
    setBusy(true);
    setMsg(null);
    api.feedback(task.id).then(
      (cs) => (setComments(cs), setChosen(new Set(cs.filter((c) => c.suggested).map((c) => c.id)))),
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  const toggle = (id: string) => setChosen((cur) => {
    const next = new Set(cur);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const address = () => {
    setBusy(true);
    api.address(task.id, [...chosen], note.trim()).then(onChange, (e: Error) => setMsg(e.message)).finally(() => setBusy(false));
  };
  const [drafts, setDrafts] = useState<(ReviewComment & { text: string })[] | null>(null);
  const [armed, setArmed] = useState(false);
  const replyOnly = () => {
    setBusy(true);
    setMsg(null);
    api.draftReplies(task.id, [...chosen]).then(setDrafts, (e: Error) => setMsg(e.message)).finally(() => setBusy(false));
  };
  const post = () => {
    if (!drafts) return;
    setBusy(true);
    api.postReplies(task.id, Object.fromEntries(drafts.map((d) => [d.id, d.text]))).then(
      (r) => (setMsg(`Posted ${r.posted.length}: ${r.posted.join(" ")}`), setDrafts(null), setArmed(false), setComments(null)),
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  return (
    <section className="gate calm feedback">
      <header>
        <h2>Review feedback</h2>
        <p className="muted">What reviewers asked on the pull request. Ticked: people's current requests; bots, approvals and outdated threads are left unticked.</p>
      </header>
      {!comments && (
        <button className="ghost" disabled={busy} onClick={load}>{busy ? "Reading the PR…" : "Read the PR's feedback"}</button>
      )}
      {comments && comments.length === 0 && <p className="muted">No open feedback on the PR.</p>}
      {comments && comments.length > 0 && (
        <ul className="comments">
          {comments.map((c) => (
            <li key={c.id}>
              <label className="check">
                <input type="checkbox" checked={chosen.has(c.id)} onChange={() => toggle(c.id)} />
                <span>
                  <b>@{c.author}</b> {c.path && <span className="mono dim">{c.path}{c.line ? `:${c.line}` : ""}</span>}{" "}
                  {c.bot && <span className="tag">bot</span>}
                  {c.outdated && <span className="tag">outdated</span>}
                  {c.superseded && <span className="tag">superseded</span>}
                  {c.state && <span className="tag">{c.state.toLowerCase().replace("_", " ")}</span>}
                  {c.issue && <span className="tag">on issue #{c.issue}</span>}{" "}
                  <a href={c.url} target="_blank" rel="noreferrer">↗</a>
                </span>
              </label>
              <pre className="comment-body">{c.body.length > 900 ? `${c.body.slice(0, 900)}…` : c.body}</pre>
            </li>
          ))}
        </ul>
      )}
      {comments && (
        <>
          <label className="full">
            <span>Your note (optional)</span>
            <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="e.g. Keep the public function name; only rename the helper." />
          </label>
          <div className="actions">
            <button className="primary" disabled={busy || (!chosen.size && !note.trim())} onClick={address}>
              Address {chosen.size} selected
            </button>
            <button className="ghost" disabled={busy || !chosen.size} onClick={replyOnly}
              title="For questions and confirmations: draft replies, change no code">
              Reply only
            </button>
            <button className="ghost small" disabled={busy} onClick={load}>Read again</button>
          </div>
        </>
      )}
      {drafts && (
        <div className="replies">
          <h3>Replies (nothing is posted until you confirm)</h3>
          {drafts.map((d, i) => (
            <label key={d.id} className="full">
              <span>To @{d.author}{d.issue ? ` on issue #${d.issue}` : d.path ? ` on ${d.path}` : ""}: {d.body.split("\n")[0].slice(0, 120)}</span>
              <textarea rows={3} value={d.text}
                onChange={(e) => setDrafts((cur) => cur && cur.map((x, j) => (j === i ? { ...x, text: e.target.value } : x)))} />
            </label>
          ))}
          <div className="actions">
            {!armed ? (
              <button className="primary" disabled={busy} onClick={() => setArmed(true)}>Post {drafts.filter((d) => d.text.trim()).length} replies…</button>
            ) : (
              <>
                <span>Post these publicly as you on {task.repo}?</span>
                <button className="danger" disabled={busy} onClick={post}>Yes, post them</button>
                <button className="ghost" onClick={() => setArmed(false)}>Cancel</button>
              </>
            )}
          </div>
        </div>
      )}
      {msg && <p className={msg.startsWith("Posted") ? "ok" : "error"}>{msg}</p>}
    </section>
  );
}
const ATTACHABLE = new Set(["ready", "needs_operator", "failed"]);

// Opened the PR yourself: link it, and the task counts as published; revisions follow up on that PR.
function AttachPr({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [url, setUrl] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <details className="gate calm revise">
      <summary>Opened the pull request yourself? Attach it</summary>
      <div className="actions">
        <input className="mono" placeholder={`https://github.com/${task.repo}/pull/123`} value={url}
          onChange={(e) => setUrl(e.target.value)} />
        <button className="primary" disabled={!url.trim()}
          onClick={() => api.attach(task.id, url.trim()).then(onChange, (e: Error) => setMsg(e.message))}>
          Attach PR
        </button>
        {msg && <span className="error">{msg}</span>}
      </div>
    </details>
  );
}

// Send a finished or stopped task back with a correction: the note joins the approved plan, and the run
// starts again (from the same commit, or the latest one). A published task's PR and claim are kept.
function RevisePanel({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const [note, setNote] = useState("");
  const [latest, setLatest] = useState(false);
  const [mode, setMode] = useState<"continue" | "restart">(task.can_continue ? "continue" : "restart");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const stopped = task.checks.filter((c) => !c.ok).at(-1)?.check;
  const send = () => {
    setBusy(true);
    api
      .revise(task.id, note.trim(), latest, mode)
      .then(onChange, (e: Error) => setMsg(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <details className="gate calm revise" open={task.status !== "ready" && task.status !== "published"}>
      <summary>Revise and run again</summary>
      <p className="muted">
        Your note is added to the approved plan for the Coder and the Reviewer.
        {task.status === "published" && " The pull request stays; the new work becomes a follow-up commit."}
      </p>
      <label className="full">
        <span>What should change</span>
        <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="e.g. Split the README paragraph into a short list; keep the example." />
      </label>
      {task.can_continue && (
        <div className="revise-mode" role="radiogroup" aria-label="Where the run starts">
          <label className="check">
            <input type="radio" checked={mode === "continue"} onChange={() => setMode("continue")} />
            <span>
              <b>Continue from where it stopped</b>
              {stopped ? ` (at ${stopped})` : ""}: keeps the change and the baseline; the Coder fixes only what failed
              and what your note asks. Fewer tokens, minutes faster.
            </span>
          </label>
          <label className="check">
            <input type="radio" checked={mode === "restart"} onChange={() => setMode("restart")} />
            <span><b>Start over</b> from the plan: for when the approach itself was wrong.</span>
          </label>
        </div>
      )}
      <div className="actions">
        <label className="check">
          <input type="checkbox" checked={latest} onChange={(e) => setLatest(e.target.checked)} />
          <span>Start from the latest commit on the default branch</span>
        </label>
        <button className="primary" disabled={busy || !note.trim()} onClick={send}>
          {busy ? "Starting…" : "Revise and run"}
        </button>
        {msg && <span className="error">{msg}</span>}
      </div>
    </details>
  );
}

function ActionPanel({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  if (task.status === "planned") return <Gate1 task={task} onChange={onChange} />;
  if (task.status === "ready")
    return (
      <>
        {!task.pr_url && <UnderstandingCheck task={task} onChange={onChange} />}
        <Gate2 task={task} onChange={onChange} />
      </>
    );
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
      {task.replies.length > 0 && (
        <div className="replies">
          <h3>Replies on the issue since the question</h3>
          {task.replies.map((r, i) => (
            <p key={i}><b>@{r.author}</b> {r.text} <a href={r.url} target="_blank" rel="noreferrer">↗</a></p>
          ))}
        </div>
      )}
      <PlanAgain task={task} />
    </section>
  );
}

// Once the questions are answered (on GitHub, or by what you found out yourself), plan the issue again.
function PlanAgain({ task }: { task: TaskDetail }) {
  const [answer, setAnswer] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = () => {
    setBusy(true);
    api.replan(task.id, answer.trim()).then(
      () => {
        setMsg("Planning again with your answer; the new plan shows in the list in a minute. This one is hidden.");
        window.setTimeout(() => (window.location.hash = `#/r/${task.repo}/items`), 2500);
      },
      (e: Error) => (setMsg(e.message), setBusy(false)),
    );
  };
  return (
    <div className="plan-again">
      <h3>Answered? Plan it again</h3>
      <textarea
        rows={3}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        placeholder="Your answer, e.g. litellm#38663 is merged and released in 1.104.2: upgrade now and pin 1.104.2"
      />
      <div className="actions">
        <button className="primary" disabled={busy || !answer.trim()} onClick={submit}>Plan again</button>
        <span className="dim">The Planner also reads the issue's newest comments.</span>
      </div>
      {msg && <p className="dim">{msg}</p>}
    </div>
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

// "[text](https://…)" reads as "text" in a one-line preview of a comment.
const plain = (markdown: string) => markdown.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[`*_]/g, "");

function RepliesEditor({
  items,
  replies,
  setReplies,
}: {
  items: (ReviewComment & { text: string })[];
  replies: Record<string, string>;
  setReplies: (f: (cur: Record<string, string>) => Record<string, string>) => void;
}) {
  return (
    <div className="replies">
      <h3>Replies</h3>
      {items.map((r) => (
        <label key={r.id} className="full">
          <span>
            To @{r.author}
            {r.issue ? ` on issue #${r.issue}` : r.path ? ` on ${r.path}${r.line ? `:${r.line}` : ""}` : ""}:{" "}
            {plain(r.body.split("\n")[0]).slice(0, 140)}
          </span>
          <textarea rows={3} value={replies[r.id] ?? r.text} onChange={(e) => setReplies((cur) => ({ ...cur, [r.id]: e.target.value }))} />
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
        <p className="muted">
          Runs setup and a baseline, then the Coder. Its change passes the checks cheapest first: scope, the test, the
          Reviewer, then the full suites once. Limits: $5, 30 minutes.
        </p>
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

// Before the first publish: can you explain this change to its reviewers? It warns, never blocks.
function UnderstandingCheck({ task, onChange }: { task: TaskDetail; onChange: () => void }) {
  const u = task.understanding;
  const [answers, setAnswers] = useState<string[]>(() => u?.last?.answers ?? []);
  const [busy, setBusy] = useState<"ask" | "grade" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(!u?.passed);
  const run = (what: "ask" | "grade", call: () => Promise<unknown>) => {
    setBusy(what);
    setError(null);
    call().then(
      () => (setBusy(null), onChange()),
      (e: Error) => (setError(e.message), setBusy(null)),
    );
  };
  const ask = (fresh: boolean) =>
    run("ask", () => api.understandingQuestions(task.id, fresh).then(() => setAnswers([])));
  const grade = () => run("grade", () => api.understandingAnswers(task.id, u!.questions.map((_, i) => answers[i] ?? "")));
  const status = !u ? "not taken" : u.passed ? "passed" : u.attempts ? "not passed yet" : "questions ready";
  return (
    <section className={`gate calm understand${u?.passed ? " passed" : ""}`}>
      <header onClick={() => setOpen(!open)} className="understand-h">
        <span className="gate-mark">Before you publish</span>
        <h2>Can you explain this change?</h2>
        <span className={`understand-status ${u?.passed ? "ok-text" : "dim"}`}>{status}</span>
      </header>
      {open && (
        <>
          <p className="muted">
            Reviewers will ask why the bug happened, why the fix works and what the test proves. Answer three questions in
            your own words; NightShift checks the answers against the code. It does not stop you publishing.
          </p>
          {task.ai_rules.length > 0 && (
            <div className="ai-rules">
              <b>This repo's rules on AI</b>
              <ul>{task.ai_rules.map((r) => <li key={r}>{r}</li>)}</ul>
            </div>
          )}
          {!u ? (
            <button className="primary" onClick={() => ask(false)} disabled={busy !== null}>
              {busy === "ask" ? "Writing questions…" : "Start the check"}
            </button>
          ) : (
            <ol className="understand-qs">
              {u.questions.map((q, i) => {
                const g = u.last?.grades[i];
                return (
                  <li key={i}>
                    <p className="q">{q.question}</p>
                    <p className="mono dim small-print">{q.about}</p>
                    <textarea
                      rows={3}
                      value={answers[i] ?? ""}
                      onChange={(e) => setAnswers((a) => Object.assign([...a], { [i]: e.target.value }))}
                      placeholder="In your own words"
                      aria-label={`Answer ${i + 1}`}
                    />
                    {g && (
                      <div className={`grade grade-${g.verdict}`}>
                        <b>{g.verdict}</b> {g.feedback}
                        {q.expects && (
                          <details>
                            <summary>What a good answer covers</summary>
                            <ul>{q.expects.map((x) => <li key={x}>{x}</li>)}</ul>
                          </details>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
          {u && (
            <div className="understand-actions">
              <button className="primary" onClick={grade} disabled={busy !== null || u.questions.some((_, i) => !(answers[i] ?? "").trim())}>
                {busy === "grade" ? "Checking…" : u.attempts ? "Check again" : "Check my answers"}
              </button>
              <button onClick={() => ask(true)} disabled={busy !== null}>{busy === "ask" ? "Writing…" : "New questions"}</button>
              {u.attempts > 0 && <span className="dim">{u.attempts} attempt{u.attempts > 1 ? "s" : ""}</span>}
            </div>
          )}
          {error && <p className="error">{error}</p>}
        </>
      )}
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
  const [edits, setEdits] = useState<PublishEdits | null>(null); // what will go out, as last checked or edited
  const [remember, setRemember] = useState(false);
  const followUp = Boolean(task.pr_url);
  const repliesOnly = info?.mode === "replies_only";
  const [replaceBody, setReplaceBody] = useState(false); // follow-up: also replace the PR's description
  const [replies, setReplies] = useState<Record<string, string>>({});
  const [postReplies, setPostReplies] = useState(false);
  const payload = (): PublishEdits => {
    // With nothing to push, Gate 2 exists to post the replies: confirming means posting them.
    const e: PublishEdits = { ...(edits ?? {}), remember, replies, post_replies: postReplies || info?.mode === "replies_only" };
    if (followUp && !replaceBody) delete e.body;
    if (followUp) delete e.title;
    return e;
  };

  const show = (r: GateInfo) => {
    setInfo(r);
    setEdits({ title: r.title, body: r.body, commit_message: r.commit_message, attribution: r.attribution, branch: r.branch });
  };
  const dryRun = () => {
    setBusy(true);
    setMsg(null);
    api.publish(task.id, false, payload()).then(show, (e: Error) => setMsg(e.message)).finally(() => setBusy(false));
  };
  const publish = () => {
    setBusy(true);
    api.publish(task.id, true, payload()).then(
      (r) => {
        show(r);
        onChange();
      },
      (e: Error) => setMsg(e.message),
    ).finally(() => setBusy(false));
  };
  const edit = (field: keyof PublishEdits) => (e: { target: { value: string } }) =>
    setEdits((cur) => ({ ...(cur ?? {}), [field]: e.target.value }));
  const blocked = info ? info.blockers.length > 0 || info.drift.length > 0 : true;

  return (
    <section className="gate">
      <header>
        <span className="gate-mark">Gate 2</span>
        {repliesOnly ? (
          <>
            <h2>Reply on the pull request</h2>
            <p className="muted">
              The comments asked no code change, so nothing is pushed: only the replies below are posted, under your name.
            </p>
          </>
        ) : followUp ? (
          <>
            <h2>Push a follow-up commit to the pull request</h2>
            <p className="muted">
              One more commit on <a href={task.pr_url ?? ""} target="_blank" rel="noreferrer">the PR</a>: the difference between
              the fix you published and this revised one. Pushed to your fork's branch, never forced.
            </p>
          </>
        ) : (
          <>
            <h2>Publish the pull request</h2>
            <p className="muted">
              Checks the issue is still free and upstream has not changed these files, then pushes to your fork and opens the PR.
            </p>
          </>
        )}
      </header>
      <div className="actions">
        <button className="ghost" onClick={dryRun} disabled={busy}>
          {busy && !info ? "Checking…" : "Check freshness (dry run)"}
        </button>
        {msg && <span className="error">{msg}</span>}
      </div>
      {info && (
        <div className="gate-result">
          {!repliesOnly && <p className="mono">
            {followUp
              ? `${info.branch} (the PR's branch) · ${info.files.length} files: ${info.files.join(", ")}`
              : `${info.branch} → ${info.base} @ ${info.head.slice(0, 7)} · ${info.files.length} files`}
          </p>}
          {info.blockers.map((b) => (
            <p key={b} className="blocked">Blocked: {b}</p>
          ))}
          {info.warnings.map((w) => (
            <p key={w} className="warn">{w}</p>
          ))}
          {edits && !info.published && repliesOnly && (
            <div className="publish-form">
              <RepliesEditor items={info.replies ?? []} replies={replies} setReplies={setReplies} />
            </div>
          )}
          {edits && !info.published && !repliesOnly && (
            <div className="publish-form">
              <h3>{followUp ? "What will be pushed" : "What will be published"}</h3>
              {!followUp && (
                <label className="full">
                  <span>Title</span>
                  <input value={edits.title ?? ""} onChange={edit("title")} />
                </label>
              )}
              <div className="gate-grid">
                {!followUp && (
                  <label>
                    <span>Branch on your fork</span>
                    <input className="mono" value={edits.branch ?? ""} onChange={edit("branch")} />
                  </label>
                )}
                <label>
                  <span>Attribution line (last line of the commit; empty leaves it out)</span>
                  <input className="mono" value={edits.attribution ?? ""} onChange={edit("attribution")} />
                </label>
              </div>
              <label className="full">
                <span>Commit message</span>
                <textarea className="mono" rows={4} value={edits.commit_message ?? ""} onChange={edit("commit_message")} />
              </label>
              {followUp && info.replies && info.replies.length > 0 && (
                <>
                  <RepliesEditor items={info.replies} replies={replies} setReplies={setReplies} />
                  <label className="check">
                    <input type="checkbox" checked={postReplies} onChange={(e) => setPostReplies(e.target.checked)} />
                    <span>Post these replies after pushing (in each review thread; on the issue; the rest as one PR comment)</span>
                  </label>
                </>
              )}
              {followUp && (
                <label className="check">
                  <input type="checkbox" checked={replaceBody} onChange={(e) => setReplaceBody(e.target.checked)} />
                  <span>Also replace the PR's description</span>
                </label>
              )}
              {(!followUp || replaceBody) && (
                <label className="full">
                  <span>
                    {followUp ? "New description" : "Body"}
                    {info.template_headings.length > 0 && ` (must keep the template's sections: ${info.template_headings.join(", ")})`}
                  </span>
                  <textarea className="mono" rows={14} value={edits.body ?? ""} onChange={edit("body")} />
                </label>
              )}
              <label className="check">
                <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                <span>Use this attribution{followUp ? "" : " and branch prefix"} for {task.repo} from now on</span>
              </label>
              <div className="actions">
                <button className="ghost small" disabled={busy} onClick={dryRun}>
                  {busy ? "Checking…" : "Save edits and check again"}
                </button>
              </div>
            </div>
          )}
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
              {repliesOnly ? "Replied on" : followUp ? "Pushed the follow-up commit to" : "Opened"}{" "}
              <a href={info.url} target="_blank" rel="noreferrer">{info.url}</a>
              {info.reply_urls && info.reply_urls.length > 0 && ` · ${info.reply_urls.length} replies posted`}
            </p>
          )}
          {!blocked && !info.published && (
            <div className="confirm">
              <label>
                <span>
                  Type <b className="mono">{task.repo}</b> to {repliesOnly ? "post the replies" : followUp ? "push" : "publish"} under your name
                </span>
                <input className="mono" value={typed} onChange={(e) => setTyped(e.target.value)} />
              </label>
              <button className="danger" disabled={typed !== task.repo || busy} onClick={publish}>
                {info.mode === "replies_only" ? "Post replies" : followUp ? "Push follow-up commit" : "Push and open PR"}
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

// Whether the Planner's context held the code the fix needed. A miss points at how context is built.
function ContextCheck({ cc }: { cc: NonNullable<TaskDetail["context_check"]> }) {
  const held = cc.changed.length - cc.missed.length;
  return (
    <>
      <h3>Context</h3>
      <p className={cc.missed.length ? "blocked" : "ok"}>
        The planning context held {held} of {cc.changed.length} files the fix changed
        {cc.missed.length ? "." : ": nothing was missing."}
      </p>
      {cc.missed.length > 0 && <p className="mono dim">missed: {cc.missed.join(", ")}</p>}
      <p className="mono dim">
        in the relevant files: {cc.relevant.join(", ") || "none"} · named in the plan: {cc.planned.join(", ") || "none"}
        {cc.new.length > 0 && <> · new files: {cc.new.join(", ")}</>}
      </p>
    </>
  );
}

// The checks a change passes, cheapest first: a wrong change is caught before the slow checks run.
const CHECKS: { key: CheckStep["check"]; name: string; proves: string }[] = [
  { key: "plan", name: "Plan check", proves: "A second model read the code and the plan before any code was written" },
  { key: "scope", name: "Scope", proves: "The change stays inside the plan: no stray lock or built files, no oversized diff" },
  { key: "test", name: "Test", proves: "The new test fails without the fix and passes with it" },
  { key: "review", name: "Review", proves: "A reviewer from another model family approves the diff" },
  { key: "suites", name: "Full suites", proves: "Everything that passed before still passes (runs once, last)" },
];

function Checks({ task }: { task: TaskDetail }) {
  const checks = task.checks;
  const last = checks[checks.length - 1];
  const rounds = Math.max(0, ...checks.map((e) => e.round));
  // While the task runs: a passed check hands over to the next one; anything else means the Coder is working.
  let next: CheckStep["check"] | undefined;
  if (task.status === "working") {
    const after = last?.ok ? CHECKS[CHECKS.findIndex((g) => g.key === last.check) + 1] : undefined;
    next = after?.key ?? "scope";
  }
  const caught = checks.filter((e) => e.check === "scope" && !e.ok).length;
  return (
    <div className="verify">
      {checks.length > 0 || next ? (
        <ol className="checks">
          {CHECKS.map((g) => {
            const tries = checks.filter((e) => e.check === g.key);
            const now = tries[tries.length - 1];
            const state = g.key === next ? "run" : now ? (now.ok ? "pass" : "fail") : "todo";
            const when =
              state === "run"
                ? g.key === "scope" ? "Coder working…" : "running…"
                : now
                  ? [rounds > 1 && now.round ? `round ${now.round}` : "", now.seconds ? fmtSeconds(now.seconds) : ""].filter(Boolean).join(" · ")
                  : "not reached";
            return (
              <li key={g.key} className={`chk-row is-${state}`}>
                <span className="chk-mark" aria-hidden>{state === "pass" ? "✓" : state === "fail" ? "✕" : ""}</span>
                <div className="chk-main">
                  <div className="chk-head">
                    <b>{g.name}</b>
                    <span className="dim">
                      {when}
                      {tries.length > 1 && ` · ${tries.length} tries`}
                    </span>
                  </div>
                  <p className="chk-what">{now?.detail || g.proves}</p>
                </div>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="muted">No checks have run yet.</p>
      )}
      {caught > 0 && (
        <p className="chk-saved">
          Scope sent {caught === 1 ? "one change" : `${caught} changes`} back before any test ran.
        </p>
      )}
      {checks.length > 1 && (
        <details className="chk-log">
          <summary>Every check, in order ({checks.length})</summary>
          <ol>
            {checks.map((e, i) => (
              <li key={i} className={e.ok ? "pass" : "fail"}>
                <span className="mono dim">{e.round ? `round ${e.round}` : "plan"}</span>
                <b>{CHECKS.find((g) => g.key === e.check)?.name ?? e.check}</b>
                <span>{e.detail}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
      {task.attempts.length > 0 && (
        <details className="chk-log">
          <summary>Earlier attempts ({task.attempts.length})</summary>
          <ol>
            {task.attempts.map((a) => (
              <li key={a.n} className={a.status === "ready" || a.status === "published" ? "pass" : "fail"}>
                <span className="mono dim">#{a.n}</span>
                <b>{a.stopped_at ? `stopped at ${a.stopped_at}` : a.status}</b>
                <span>{a.lines} diff lines · {money(a.cost_usd)}{a.error ? ` · ${a.error.slice(0, 120)}` : ""}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
      {task.resume && <p className="muted">The next run continues from where the last attempt stopped ({task.resume.stopped_at}).</p>}
      <Verification task={task} />
    </div>
  );
}

function fmtSeconds(s: number): string {
  return s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

function Verification({ task }: { task: TaskDetail }) {
  const c = task.check;
  const suitesRan = c ? (c.suites_run ?? c.suite.length > 0) : false;
  return (
    <div className="verify">
      <h3>Evidence</h3>
      {c ? (
        <>
          <p className={c.passed ? "ok" : "blocked"}>
            {!c.passed
              ? "Verification did not pass."
              : suitesRan
                ? "Verified by code: the test fails without the fix, passes with it, and no suite regressed."
                : "Verified by code: the test fails without the fix and passes with it. The full suites run once the Reviewer approves."}
          </p>
          {c.problems.map((p) => (
            <p key={p} className="blocked">{p}</p>
          ))}
          <p className="mono dim">
            tests: {c.test_files.join(", ") || "none"} · source: {c.source_files.join(", ") || "none"}
          </p>
          {c.fails_without_fix && <Run r={c.fails_without_fix} label="Without the fix" />}
          {c.passes_with_fix && <Run r={c.passes_with_fix} label="With the fix" />}
          {suitesRan && (
            <>
              <h3>Suites after the change</h3>
              {c.suite.map((r) => <Run key={r.command} r={r} />)}
            </>
          )}
        </>
      ) : (
        <p className="muted">No verification has run yet.</p>
      )}
      {task.context_check && <ContextCheck cc={task.context_check} />}
      {task.baseline.length > 0 && (
        <>
          <h3>Baseline (before any change)</h3>
          {task.baseline.map((r) => <Run key={r.command} r={r} />)}
        </>
      )}
    </div>
  );
}
