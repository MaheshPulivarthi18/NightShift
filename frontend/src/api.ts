// Typed client for /api. Every write sends JSON, which the server requires (it refuses cross-site calls).

export type Stage = "plan" | "gate1" | "work" | "gate2" | "done" | "stopped" | "closed";

export interface TaskSummary {
  id: number;
  repo: string;
  issue: number;
  title: string;
  status: string;
  stage: Stage;
  rehearsal: boolean;
  cost_usd: number;
  error: string | null;
  busy: boolean;
  updated_at: string | null;
}

export interface CommandResult {
  command: string;
  ok: boolean;
  exit_code: number;
  timed_out: boolean;
  seconds: number;
  tail: string;
}

export interface FixCheck {
  passed: boolean;
  test_files: string[];
  source_files: string[];
  fails_without_fix: CommandResult | null;
  passes_with_fix: CommandResult | null;
  suite: CommandResult[];
  regressions: string[];
  already_failing: string[];
  problems: string[];
}

export interface TaskDetail extends TaskSummary {
  issue_url: string | null;
  run_id: string;
  base_commit: string;
  note: string | null;
  plan_html: string;
  setup_commands: string[];
  test_commands: string[];
  claim_comment: string;
  claim_url: string | null;
  baseline: CommandResult[];
  check: FixCheck | null;
  reviews: { round: number; verdict: string; html: string }[];
  diff: string;
  pr: { title: string; html: string; commit: string } | null;
  pr_url: string | null;
  coder_steps: number | null;
}

export interface GateInfo {
  branch: string;
  base: string;
  head: string;
  drift: string[];
  files: string[];
  title: string;
  blockers: string[];
  published: boolean;
  url?: string;
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  tasks: () => call<TaskSummary[]>("/tasks"),
  task: (id: number) => call<TaskDetail>(`/tasks/${id}`),
  create: (repo: string, issue: number, base: string, rehearsal: boolean) =>
    call<{ status: string }>("/tasks", { repo, issue, base: base || null, rehearsal }),
  decide: (id: number, approve: boolean, note: string, setup: string[], test: string[]) =>
    call<TaskDetail>(`/tasks/${id}/decision`, { approve, note: note || null, setup_commands: setup, test_commands: test }),
  run: (id: number) => call<{ status: string }>(`/tasks/${id}/run`, {}),
  claim: (id: number, confirm: boolean) => call<{ result: string }>(`/tasks/${id}/claim`, { confirm }),
  publish: (id: number, confirm: boolean) => call<GateInfo>(`/tasks/${id}/publish`, { confirm }),
};
