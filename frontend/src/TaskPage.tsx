import { useCallback, useEffect, useRef, useState } from "react";
import { CommandResult, GateInfo, ModelUse, ModelView, PublishEdits, ReviewComment, TaskDetail, api } from "./api";
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
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const send = () => {
    setBusy(true);
    api
      .revise(task.id, note.trim(), latest)
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
  const [edits, setEdits] = useState<PublishEdits | null>(null); // what will go out, as last checked or edited
  const [remember, setRemember] = useState(false);
  const followUp = Boolean(task.pr_url);
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
        {followUp ? (
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
          <p className="mono">
            {followUp
              ? `${info.branch} (the PR's branch) · ${info.files.length} files: ${info.files.join(", ")}`
              : `${info.branch} → ${info.base} @ ${info.head.slice(0, 7)} · ${info.files.length} files`}
          </p>
          {info.blockers.map((b) => (
            <p key={b} className="blocked">Blocked: {b}</p>
          ))}
          {info.warnings.map((w) => (
            <p key={w} className="warn">{w}</p>
          ))}
          {edits && !info.published && (
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
              {info.mode === "replies_only" && (
                <p className="warn">The run changed no code: there is nothing to push. Post the replies below, or revise.</p>
              )}
              {followUp && info.replies && info.replies.length > 0 && (
                <div className="replies">
                  <h3>Replies to the reviewers</h3>
                  {info.replies.map((r) => (
                    <label key={r.id} className="full">
                      <span>
                        To @{r.author}{r.path ? ` on ${r.path}${r.line ? `:${r.line}` : ""}` : ""}: {r.body.split("\n")[0].slice(0, 120)}
                      </span>
                      <textarea rows={2} value={replies[r.id] ?? r.text}
                        onChange={(e) => setReplies((cur) => ({ ...cur, [r.id]: e.target.value }))} />
                    </label>
                  ))}
                  <label className="check">
                    <input type="checkbox" checked={postReplies} onChange={(e) => setPostReplies(e.target.checked)} />
                    <span>Post these replies on the PR after pushing (in each thread; others as one comment)</span>
                  </label>
                </div>
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
              <div className="actions">
                <label className="check">
                  <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                  <span>Use this attribution and branch prefix for {task.repo} from now on</span>
                </label>
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
