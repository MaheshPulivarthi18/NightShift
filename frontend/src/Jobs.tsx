import { useState } from "react";
import { api, JobDetail } from "./api";
import { Log, StatusBadge, ago, duration, money, usePoll } from "./ui";

const KIND: Record<string, string> = { report: "health report", task: "task", map: "architecture map" };

export function Jobs() {
  const [jobs, error] = usePoll(api.jobs, 4000, []);
  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>Jobs</h1>
          <p className="muted">Every run, from the app or the CLI: health reports, tasks and architecture maps.</p>
        </div>
      </section>
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>Repo</th>
              <th>Kind</th>
              <th>Status</th>
              <th>Step</th>
              <th className="right">Cost</th>
              <th>Started</th>
              <th className="right">Took</th>
            </tr>
          </thead>
          <tbody>
            {jobs?.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">No runs yet.</td>
              </tr>
            )}
            {jobs?.map((j) => (
              <tr key={j.run_id} className="clickable" onClick={() => (window.location.hash = jobHref(j.run_id))}>
                <td>
                  <a className="mono" href={jobHref(j.run_id)} onClick={(e) => e.stopPropagation()}>
                    {j.repo}
                  </a>
                </td>
                <td className="dim">{KIND[j.kind]}</td>
                <td><StatusBadge status={j.status} /></td>
                <td className="mono dim">{j.node ?? ""}</td>
                <td className="mono right">{money(j.cost_usd)}</td>
                <td className="dim">{ago(j.started_at)}</td>
                <td className="mono dim right">{duration(j.started_at, j.finished_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Task runs open their task page; everything else its job page. */
const jobHref = (runId: string) => (runId.startsWith("task-") ? `#/tasks/${runId.slice(5)}` : `#/jobs/${runId}`);

export function Job({ id }: { id: string }) {
  const [job, error, reload] = usePoll<JobDetail>(() => api.job(id), 5000, [id]);
  const [msg, setMsg] = useState<string | null>(null);
  if (error) return <div className="page"><a className="back" href="#/jobs">← Jobs</a><p className="error">{error}</p></div>;
  if (!job) return <p className="muted page">Loading run…</p>;
  const act = (p: Promise<unknown>) => p.then(reload, (e: Error) => setMsg(e.message));
  return (
    <div className="page narrow">
      <a className="back" href="#/jobs">← Jobs</a>
      <header className="page-head">
        <div>
          <h1 className="mono-title">{job.repo}</h1>
          <div className="facts">
            <StatusBadge status={job.status} />
            {job.node && <span className="mono dim">{job.node}</span>}
            <span className="mono">{money(job.cost_usd)}</span>
            <span className="dim">started {ago(job.started_at)} · {duration(job.started_at, job.finished_at)}</span>
            {job.has_report && <a href={`#/repos/${job.repo}/report`}>open report →</a>}
          </div>
        </div>
        <div className="actions">
          {job.status === "running" && job.stoppable && (
            <button className="danger small" onClick={() => act(api.stopJob(id))}>Stop</button>
          )}
          {(job.status === "stopped" || job.status === "failed") && (
            <button className="ghost small" onClick={() => act(api.resumeJob(id))}>Resume</button>
          )}
        </div>
      </header>
      {(job.error || msg) && <p className="stop-reason">{msg || job.error}</p>}
      <Log runId={id} onStatus={reload} />
    </div>
  );
}
