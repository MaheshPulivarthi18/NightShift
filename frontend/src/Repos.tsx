import { FormEvent, useState } from "react";
import { api, RepoEnvironment, RepoRow, ReportView } from "./api";
import { Html, StatusBadge, ago, money, usePoll } from "./ui";

export function Repos() {
  const [rows, error, reload] = usePoll(api.repos, 8000, []);
  const [msg, setMsg] = useState<string | null>(null);

  const runReport = (repo: string) =>
    api.runReport(repo).then((r) => (window.location.hash = `#/jobs/${r.run_id}`), (e: Error) => setMsg(e.message));
  const setRole = (repo: string, role: string) => api.setRole(repo, role).then(reload, (e: Error) => setMsg(e.message));
  const remove = (repo: string) => api.removeRepo(repo).then(reload, (e: Error) => setMsg(e.message));
  const setup = (repo: string) =>
    api.setupRepo(repo).then((r) => (window.location.hash = `#/jobs/${r.run_id}`), (e: Error) => setMsg(e.message));

  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>Repos</h1>
          <p className="muted">Health reports: whether a repo is worth contributing to, and which issues are free.</p>
        </div>
        <AddRepo onAdded={reload} />
      </section>
      {(error || msg) && <p className="error">{error || msg}</p>}
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>Repo</th>
              <th>View</th>
              <th>Latest report</th>
              <th>Last run</th>
              <th>Environment</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows?.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">No repos yet. Add one above.</td>
              </tr>
            )}
            {rows?.map((r) => <RepoLine key={r.full_name} r={r} onRun={runReport} onRole={setRole} onSetup={setup} onRemove={remove} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RepoLine({
  r,
  onRun,
  onRole,
  onSetup,
  onRemove,
}: {
  r: RepoRow;
  onRun: (repo: string) => void;
  onRole: (repo: string, role: string) => void;
  onSetup: (repo: string) => void;
  onRemove: (repo: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const running = r.run?.status === "running";
  const settingUp = r.setup_run?.status === "running";
  return (
    <tr>
      <td>
        <a className="mono" href={`https://github.com/${r.full_name}`} target="_blank" rel="noreferrer">
          {r.full_name}
        </a>
      </td>
      <td>
        <select value={r.role} onChange={(e) => onRole(r.full_name, e.target.value)} aria-label={`View for ${r.full_name}`}>
          <option value="contributor">contributor</option>
          <option value="maintainer">maintainer</option>
        </select>
      </td>
      <td>
        {r.report_at ? (
          <a className="summary-link" href={`#/repos/${r.full_name}/report`}>
            {r.verdict && <span className={`verdict verdict-${r.verdict}`}>{r.verdict.replace(/_/g, " ")}</span>}
            <span>
              {r.findings != null && `${r.findings} findings · `}
              {r.opportunities} issues to work on
            </span>
            <span className="dim">{ago(r.report_at)}</span>
          </a>
        ) : (
          <span className="muted">no report yet</span>
        )}
      </td>
      <td>
        {r.run ? (
          <a className="summary-link" href={`#/jobs/${r.run.run_id}`}>
            <StatusBadge status={r.run.status} />
            <span className="dim">{ago(r.run.started_at)}</span>
          </a>
        ) : (
          <span className="muted">never</span>
        )}
      </td>
      <td>
        {settingUp && r.setup_run ? (
          <a className="summary-link" href={`#/jobs/${r.setup_run.run_id}`}>
            <StatusBadge status="running" />
            <span className="dim">Setup agent</span>
          </a>
        ) : r.environment ? (
          <EnvironmentSummary env={r.environment} />
        ) : (
          <span className="muted">found on first plan</span>
        )}
      </td>
      <td className="right nowrap">
        <button className="ghost small" disabled={settingUp} onClick={() => onSetup(r.full_name)}>
          {r.environment ? "Redo setup" : "Set up"}
        </button>{" "}
        <button className="ghost small" disabled={running} onClick={() => onRun(r.full_name)}>
          {running ? "Running…" : "Run report"}
        </button>{" "}
        {confirming ? (
          <>
            <button className="danger small" onClick={() => onRemove(r.full_name)} title="Reports, tasks and history are kept">
              Remove from watchlist
            </button>{" "}
            <button className="ghost small" onClick={() => setConfirming(false)}>Cancel</button>
          </>
        ) : (
          <button className="ghost small" onClick={() => setConfirming(true)}>Remove</button>
        )}
      </td>
    </tr>
  );
}

function EnvironmentSummary({ env }: { env: RepoEnvironment }) {
  const passing = env.tests.filter((t) => t.ok).length;
  return (
    <details className="env">
      <summary>
        <span className={`badge env-${env.status}`}>{env.status}</span>
        <span className="dim">
          {env.status === "ready" ? `${passing}/${env.tests.length} test commands pass` : env.detail.slice(0, 60)}
        </span>
      </summary>
      <div className="env-body">
        {env.detail && <p className="muted">{env.detail}</p>}
        {(env.system_packages.length > 0 || env.tools.length > 0) && (
          <p className="mono dim">{[...env.tools, ...env.system_packages].join(" ")}</p>
        )}
        {env.setup_commands.length > 0 && <pre className="block">{env.setup_commands.join("\n")}</pre>}
        {env.tests.map((t) => (
          <p key={t.command} className="mono">
            <span className={t.ok ? "pass" : "fail"}>{t.ok ? "pass" : "fail"}</span> {t.command}
          </p>
        ))}
        <p className="dim">
          {env.attempts.length} attempts · {money(env.cost_usd)} · {env.commit.slice(0, 7)} · {ago(env.at)}
          {env.sources.length > 0 && ` · from ${env.sources.slice(0, 3).join(", ")}`}
        </p>
      </div>
    </details>
  );
}

function AddRepo({ onAdded }: { onAdded: () => void }) {
  const [name, setName] = useState("");
  const [role, setRole] = useState("contributor");
  const [msg, setMsg] = useState<string | null>(null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    api.addRepo(name, role).then(
      () => (setName(""), setMsg(null), onAdded()),
      (err: Error) => setMsg(err.message),
    );
  };
  return (
    <form className="inline-form" onSubmit={submit}>
      <input className="mono" placeholder="owner/name or GitHub URL" value={name} onChange={(e) => setName(e.target.value)} required />
      <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="View">
        <option value="contributor">contributor</option>
        <option value="maintainer">maintainer</option>
      </select>
      <button className="primary">Add repo</button>
      {msg && <span className="error">{msg}</span>}
    </form>
  );
}

export function Report({ repo }: { repo: string }) {
  const [runId, setRunId] = useState<string | undefined>(undefined);
  const [rep, error] = usePoll<ReportView>(() => api.report(repo, runId), 60000, [repo, runId]);
  if (error) return <div className="page"><a className="back" href="#/repos">← Repos</a><p className="error">{error}</p></div>;
  if (!rep) return <p className="muted page">Loading report…</p>;
  return (
    <div className="page narrow">
      <a className="back" href="#/repos">← Repos</a>
      <header className="page-head">
        <div>
          <h1 className="mono-title">{rep.repo}</h1>
          <p className="muted">
            Links marked <b>chat</b> open a conversation about that issue; <b>plan</b> starts a fix.
          </p>
        </div>
        {rep.history.length > 1 && (
          <select value={rep.run_id} onChange={(e) => setRunId(e.target.value)} aria-label="Report version">
            {rep.history.map((h) => (
              <option key={h.run_id} value={h.run_id}>
                {new Date(h.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        )}
      </header>
      <Freshness repo={rep.repo} createdAt={rep.created_at} />
      <article className="report">
        <Html html={rep.html} />
      </article>
    </div>
  );
}

/** A report is a snapshot: issues it lists as free may have been taken since. Say so once it is old. */
function Freshness({ repo, createdAt }: { repo: string; createdAt: string }) {
  const [msg, setMsg] = useState<string | null>(null);
  const hours = (Date.now() - new Date(createdAt).getTime()) / 3_600_000;
  if (hours < 24) return null;
  const age = hours < 48 ? "a day" : `${Math.floor(hours / 24)} days`;
  return (
    <div className="stale">
      <span>
        This report is {age} old. Issues it lists as free may have been taken since; <b>plan</b> always checks GitHub
        first.
      </span>
      <button
        className="ghost small"
        onClick={() => api.runReport(repo).then((r) => (window.location.hash = `#/jobs/${r.run_id}`), (e: Error) => setMsg(e.message))}
      >
        Run it again
      </button>
      {msg && <span className="error">{msg}</span>}
    </div>
  );
}
