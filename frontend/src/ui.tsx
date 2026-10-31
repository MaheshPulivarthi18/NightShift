// Shared pieces used by every page: status badge, money and time, the live log, the diff viewer, sanitized HTML.

import { useEffect, useRef, useState } from "react";

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

/** A unified diff, one collapsible file each. `locks`: regenerated lock files, as the packages they change
 * (from the server); their hundreds of lines stay folded below. */
export function Diff({ text, locks = {} }: { text: string; locks?: Record<string, string[]> }) {
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
              {locks[name] && <span className="dim"> regenerated lock file · {locks[name].length} package{locks[name].length === 1 ? "" : "s"}</span>}
              <span className="diffstat">
                <span className="add">+{added}</span> <span className="del">−{removed}</span>
              </span>
            </summary>
            {locks[name] ? (
              <div className="lock-summary">
                <ul>{locks[name].map((l) => <li key={l} className="mono">{l}</li>)}</ul>
                <details><summary className="dim">Show the {added + removed} changed lines</summary><pre>{lines.join("\n")}</pre></details>
              </div>
            ) : <pre>
              {lines
                .filter((l) => !/^(diff --git|index |--- |\+\+\+ |new file mode|deleted file mode)/.test(l))
                .map((l, n) => (
                  <span key={n} className={l.startsWith("@@") ? "hunk" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : ""}>
                    {l || " "}
                    {"\n"}
                  </span>
                ))}
            </pre>}
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
  ts?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  data: Record<string, any>;
}

// Kinds that need a word, not just a colour: caught and handled reads differently from broken.
const LOG_TAGS: Record<string, string> = { caught: "caught", fixed: "fixed", warning: "warning", error: "error" };
const SANDBOX = new Set(["cmd_start", "cmd_output", "cmd_end", "action"]);
const QUIET_TOOLS = new Set(["read", "list", "search"]); // folded together: there are many, and they change nothing
const VIEWS = ["All", "Steps", "Sandbox"] as const;
type View = (typeof VIEWS)[number];

/** One line of the log: a step, a sandbox command (with its output), or a run of quiet file reads. */
type Line =
  | { type: "step"; step: Step }
  | { type: "cmd"; start: Step; output: string[]; end?: Step }
  | { type: "reads"; steps: Step[] };

function lines(steps: Step[], view: View, live: boolean): Line[] {
  const out: Line[] = [];
  const cmds = new Map<string, Extract<Line, { type: "cmd" }>>();
  steps.forEach((s, i) => {
    const sandbox = SANDBOX.has(s.kind);
    if (view === "Steps" && sandbox) return;
    if (view === "Sandbox" && !sandbox && !["node_start", "caught", "fixed", "warning", "error"].includes(s.kind)) return;
    // A model call's start line is only useful while it runs; its end line carries the cost.
    if (s.kind === "llm_start" && !(live && i === steps.length - 1)) return;
    if (s.kind === "cmd_start") {
      const line = { type: "cmd" as const, start: s, output: [] as string[] };
      cmds.set(s.data.cmd, line);
      out.push(line);
    } else if (s.kind === "cmd_output") cmds.get(s.data.cmd)?.output.push(s.message);
    else if (s.kind === "cmd_end") {
      const line = cmds.get(s.data.cmd);
      if (line) line.end = s;
    } else if (s.kind === "action" && QUIET_TOOLS.has(s.data.tool)) {
      const last = out[out.length - 1];
      if (last?.type === "reads") last.steps.push(s);
      else out.push({ type: "reads", steps: [s] });
    } else out.push({ type: "step", step: s });
  });
  return out;
}

function clock(s: Step): string {
  return s.ts ? new Date(s.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : s.at;
}

function elapsed(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/** Live events of a run (report, map or task), streamed from the server: its steps, and what ran in the sandbox. */
export function Log({ runId, onStatus }: { runId: string; onStatus?: () => void }) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [live, setLive] = useState(true);
  const [view, setView] = useState<View>(() => {
    try {
      return (localStorage.getItem("ns-log-view") as View) || "All";
    } catch {
      return "All";
    }
  });
  const [now, setNow] = useState(Date.now());
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
  const shown = lines(steps, view, live);
  const running = live && shown.some((l) => l.type === "cmd" && !l.end);
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [running]);
  const pick = (v: View) => {
    setView(v);
    try {
      localStorage.setItem("ns-log-view", v);
    } catch {
      /* the choice just is not remembered */
    }
  };
  if (!steps.length) return <p className="muted">{live ? "Waiting for the first step…" : "No steps recorded."}</p>;
  const sandboxCount = steps.filter((s) => s.kind === "cmd_start" || s.kind === "action").length;
  return (
    <>
      <div className="log-bar">
        <div className="seg" role="tablist" aria-label="What the log shows">
          {VIEWS.map((v) => (
            <button key={v} className={v === view ? "on" : ""} onClick={() => pick(v)} aria-pressed={v === view}>
              {v}
            </button>
          ))}
        </div>
        <span className="dim">
          {view === "Steps" ? "agents and checks only" : view === "Sandbox" ? "what ran in the sandbox" : "steps and sandbox"}
          {sandboxCount > 0 && ` · ${sandboxCount} sandbox actions`}
        </span>
      </div>
      <ol className="log">
        {shown.map((l) =>
          l.type === "cmd" ? (
            <CommandLine key={l.start.id} line={l} now={now} live={live} />
          ) : l.type === "reads" ? (
            <Reads key={l.steps[0].id} steps={l.steps} />
          ) : (
            <StepLine key={l.step.id} s={l.step} />
          ),
        )}
        {live && <li className="step step-live"><span /><span className="muted">live</span><span /></li>}
      </ol>
    </>
  );
}

function StepLine({ s }: { s: Step }) {
  const edit = s.kind === "action";
  return (
    <li className={`step step-${s.kind} ${edit ? "sb" : ""}`}>
      <span className="mono dim">{clock(s)}</span>
      <span>
        {LOG_TAGS[s.kind] && <span className="step-tag">{LOG_TAGS[s.kind]}</span>}
        {edit && <span className="sb-verb">{s.data.tool === "edit" ? "edited" : "wrote"}</span>}
        {edit ? <span className="mono">{s.data.path}</span> : s.message}
        {edit && (
          <span className="sb-diff mono">
            {s.data.added != null && <span className="plus"> +{s.data.added}</span>}
            {s.data.removed != null && <span className="minus"> −{s.data.removed}</span>}
          </span>
        )}
      </span>
      <span className="mono dim">{s.data?.cached ? "cached" : s.data?.cost_usd != null ? money(s.data.cost_usd) : ""}</span>
    </li>
  );
}

function Reads({ steps }: { steps: Step[] }) {
  const reads = steps.filter((s) => s.data.tool === "read").length;
  const searches = steps.filter((s) => s.data.tool === "search").length;
  const lists = steps.length - reads - searches;
  const what = [
    reads && `read ${reads} file${reads > 1 ? "s" : ""}`,
    searches && `searched ${searches} time${searches > 1 ? "s" : ""}`,
    lists && `listed ${lists} folder${lists > 1 ? "s" : ""}`,
  ].filter(Boolean).join(", ");
  return (
    <li className="step step-action sb">
      <span className="mono dim">{clock(steps[0])}</span>
      <details className="sb-reads">
        <summary>
          <span className="sb-verb">looked</span>
          {what}
        </summary>
        <ul>
          {steps.map((s) => (
            <li key={s.id} className="mono">
              {s.message}
              {s.data.lines && <span className="dim"> · lines {s.data.lines}</span>}
              {s.data.matches != null && <span className="dim"> · {s.data.matches} matches</span>}
            </li>
          ))}
        </ul>
      </details>
      <span />
    </li>
  );
}

function CommandLine({ line, now, live }: { line: Extract<Line, { type: "cmd" }>; now: number; live: boolean }) {
  const { start, end, output } = line;
  const running = !end;
  const failed = end && (end.data.exit !== 0 || end.data.timed_out);
  const startedAt = start.ts ? Date.parse(start.ts) : now;
  const status = end
    ? end.data.timed_out
      ? `timed out · ${elapsed(end.data.seconds)}`
      : `exit ${end.data.exit} · ${elapsed(end.data.seconds)}`
    : live
      ? `running ${elapsed((now - startedAt) / 1000)}…`
      : "stopped";
  // While it runs, the output so far; once it ends, the end of its output.
  const text = end ? end.data.tail || "(no output)" : output.join("").slice(-8000) || "(no output yet)";
  const pre = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (running && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text, running]);
  return (
    <li className={`step sb cmd ${running ? (live ? "is-running" : "is-stopped") : failed ? "is-failed" : "is-ok"}`}>
      <span className="mono dim">{clock(start)}</span>
      <details className="sb-cmd" open={(running && live) || !!failed}>
        <summary>
          <span className="sb-verb">ran</span>
          <span className="mono sb-command">{start.message}</span>
          {start.data.note && <span className="sb-note">{start.data.note}</span>}
        </summary>
        <pre ref={pre} className="sb-out">{text}</pre>
      </details>
      <span className="mono sb-status">{status}</span>
    </li>
  );
}
