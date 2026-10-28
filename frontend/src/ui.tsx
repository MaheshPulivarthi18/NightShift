// Shared pieces used by every page: status badge, money and time, the live log, the diff viewer, sanitized HTML.

import { useEffect, useState } from "react";

const LABELS: Record<string, string> = {
  planning: "planning",
  planned: "gate 1",
  approved: "approved",
  working: "working",
  ready: "gate 2",
  published: "published",
  rejected: "rejected",
  needs_operator: "needs you",
  failed: "failed",
  running: "running",
  done: "done",
  stopped: "stopped",
};
const LIVE = new Set(["working", "planning", "running"]);

export function StatusBadge({ status, busy }: { status: string; busy?: boolean }) {
  return (
    <span className={`badge badge-${status}`}>
      {(busy || LIVE.has(status)) && <span className="pulse" aria-hidden />}
      {LABELS[status] ?? status}
    </span>
  );
}

export const money = (usd: number) => `$${usd.toFixed(2)}`;

export function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function duration(start: string, end: string | null): string {
  const s = Math.round(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Re-run `load` every `ms` while the page is open. */
export function usePoll<T>(load: () => Promise<T>, ms: number, deps: unknown[]): [T | null, string | null, () => void] {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const run = () =>
      load().then(
        (d) => alive && (setData(d), setError(null)),
        (e: Error) => alive && setError(e.message),
      );
    run();
    const timer = window.setInterval(run, ms);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return [data, error, () => setTick((t) => t + 1)];
}

export function Diff({ text }: { text: string }) {
  if (!text.trim()) return <p className="muted">No changes.</p>;
  const files = text.split(/^(?=diff --git )/m).filter((f) => f.trim());
  return (
    <div className="diff">
      {files.map((file, i) => {
        const name = file.match(/^diff --git a\/(\S+)/)?.[1] ?? `file ${i + 1}`;
        const lines = file.split("\n");
        const added = lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length;
        const removed = lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
        return (
          <details key={name + i} open={files.length <= 8}>
            <summary>
              <span className="mono">{name}</span>
              <span className="diffstat">
                <span className="add">+{added}</span> <span className="del">−{removed}</span>
              </span>
            </summary>
            <pre>
              {lines
                .filter((l) => !/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l))
                .map((l, n) => (
                  <span key={n} className={l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : ""}>
                    {l || " "}
                    {"\n"}
                  </span>
                ))}
            </pre>
          </details>
        );
      })}
    </div>
  );
}

// Server-sanitized HTML (nh3) only: every Markdown body is rendered and cleaned by the API.
/** A title written on GitHub: `backticked` parts render as inline code, the rest as plain text. */
export function Title({ text }: { text: string }) {
  const parts = text.split(/(`[^`]+`)/);
  return (
    <>
      {parts.map((p, i) =>
        p.length > 2 && p.startsWith("`") && p.endsWith("`") ? <code key={i}>{p.slice(1, -1)}</code> : p,
      )}
    </>
  );
}

export function Html({ html }: { html: string }) {
  return <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />;
}

interface Step {
  id: number;
  kind: string;
  node: string | null;
  message: string;
  at: string;
  data: { cost_usd?: number; cached?: boolean };
}

/** Live events of a run (report, map or task), streamed from the server. */
// Kinds that need a word, not just a colour: caught and handled reads differently from broken.
const LOG_TAGS: Record<string, string> = { caught: "caught", fixed: "fixed", warning: "warning", error: "error" };

export function Log({ runId, onStatus }: { runId: string; onStatus?: () => void }) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [live, setLive] = useState(true);
  useEffect(() => {
    setSteps([]);
    setLive(true);
    const source = new EventSource(`/api/jobs/${runId}/events`);
    source.addEventListener("step", (e) => setSteps((s) => [...s, JSON.parse((e as MessageEvent).data) as Step]));
    source.addEventListener("status", () => onStatus?.());
    source.addEventListener("end", () => {
      setLive(false);
      source.close();
    });
    source.onerror = () => {
      setLive(false);
      source.close();
    };
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);
  if (!steps.length) return <p className="muted">{live ? "Waiting for the first step…" : "No steps recorded."}</p>;
  // A model call's start line is only useful while it runs; its end line carries the cost.
  const shown = steps.filter((s, i) => s.kind !== "llm_start" || (live && i === steps.length - 1));
  return (
    <ol className="log">
      {shown.map((s) => (
        <li key={s.id} className={`step step-${s.kind}`}>
          <span className="mono dim">{s.at}</span>
          <span>
            {LOG_TAGS[s.kind] && <span className="step-tag">{LOG_TAGS[s.kind]}</span>}
            {s.message}
          </span>
          <span className="mono dim">
            {s.data?.cached ? "cached" : s.data?.cost_usd != null ? money(s.data.cost_usd) : ""}
          </span>
        </li>
      ))}
      {live && <li className="step step-live"><span /><span className="muted">live</span><span /></li>}
    </ol>
  );
}
