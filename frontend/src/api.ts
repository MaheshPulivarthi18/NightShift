// Typed client for /api. Every write sends JSON, which the server requires (it refuses cross-site calls).

export interface Overview {
  spend: { today: number; last_7_days: number; this_month: number };
  killed: boolean;
}

export interface RepoRow {
  full_name: string;
  role: "contributor" | "maintainer";
  verdict: string | null;
  opportunities: number;
  findings: number | null;
  report_at: string | null;
  run: { run_id: string; status: string; started_at: string } | null;
  environment: RepoEnvironment | null;
  setup_run: { run_id: string; status: string } | null;
}

// How the Setup agent installs and tests a repo in the sandbox.
export interface RepoEnvironment {
  status: "ready" | "blocked" | "failed";
  commit: string;
  at: string;
  detail: string;
  setup_commands: string[];
  tests: { command: string; ok: boolean }[];
  system_packages: string[];
  tools: string[];
  sources: string[];
  attempts: string[];
  cost_usd: number;
}

// The report's listed issues, asked about on GitHub again: the ones someone has taken since.
export interface LiveCheck {
  checked: number;
  taken: { number: number; why: string }[];
  at: string;
}

export interface ReportView {
  repo: string;
  role: string;
  run_id: string;
  created_at: string;
  cost_usd: number;
  html: string;
  history: { run_id: string; created_at: string }[];
}

export interface JobRow {
  run_id: string;
  repo: string;
  kind: "report" | "task" | "map";
  status: string;
  node: string | null;
  cost_usd: number;
  started_at: string;
  finished_at: string | null;
}

export interface JobDetail {
  run_id: string;
  repo: string;
  role: string;
  status: string;
  node: string | null;
  error: string | null;
  has_report: boolean;
  cost_usd: number;
  started_at: string;
  finished_at: string | null;
  stoppable: boolean;
}

export type ChatTurn =
  | { role: "user"; text: string }
  | { role: "assistant"; html: string }
  | { role: "tools"; tools: string[] };

export interface ChatView {
  repo: string;
  item: { number: number; kind: string; title: string; state: string; url: string; author: string | null; labels: string[] };
  body_html: string;
  history: ChatTurn[];
  cost_usd: number;
}

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
  archived: boolean;
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
  actionable: boolean;
  claim_url: string | null;
  baseline: CommandResult[];
  check: FixCheck | null;
  reviews: { round: number; verdict: string; html: string }[];
  diff: string;
  pr: { title: string; html: string; commit: string } | null;
  pr_url: string | null;
  coder_steps: number | null;
  models: ModelUse[];
  chosen_models: Record<string, string>;
  // At Gate 1 only: per agent whose model can be chosen, the router's first pick and the options.
  model_choices: Record<string, { default: string; why: string; options: ModelView[] }> | null;
}

// A model from OpenRouter's catalog. Prices in USD per million tokens; scores from Artificial Analysis.
export interface ModelView {
  id: string;
  name: string;
  input_per_m: number;
  output_per_m: number;
  context: number;
  intelligence: number | null;
  coding: number | null;
  agentic: number | null;
  tools: boolean;
  structured: boolean;
}

export interface ModelStats {
  calls: number;
  failed_calls: number;
  tasks: number;
  tasks_ok: number;
  cost_usd: number;
  latency_ms: number | null;
}

export interface AgentModels {
  agent: string;
  tools: boolean;
  effort: string;
  configured: string[];
  routes: (ModelView & { why: string; stats: ModelStats })[];
  error?: string;
}

// A pull request checked by the PR checker; `html` (the report) only on a single check.
export interface PRCheckRow {
  id: number;
  repo: string;
  number: number;
  url: string;
  title: string;
  author: string;
  status: string; // running, done, failed
  verdict: string | null;
  must: number;
  should: number;
  cost_usd: number;
  run_id: string;
  created_at: string;
  error: string | null;
}

export interface PRCheckView extends PRCheckRow {
  html: string;
}

export interface ModelsPage {
  learning: boolean;
  agents: AgentModels[];
}

// Which models one agent used in a task (from the ledger): in order, the ones that failed, the cost.
export interface ModelUse {
  agent: string;
  models: string[];
  failed: string[];
  cost_usd: number;
}

export interface GateInfo {
  branch: string;
  base: string;
  head: string;
  drift: string[];
  files: string[];
  title: string;
  body: string;
  commit_message: string;
  attribution: string;
  template_headings: string[];
  mode?: "follow_up"; // a revised published task: one more commit on its PR
  blockers: string[];
  warnings: string[];
  published: boolean;
  url?: string;
}

// What Gate 2 may change before publishing; omitted fields keep the draft.
export interface PublishEdits {
  title?: string;
  body?: string;
  commit_message?: string;
  attribution?: string;
  branch?: string;
  remember?: boolean;
}

export async function post(path: string, body: unknown): Promise<Response> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail));
  }
  return res;
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  if (body !== undefined) return (await post(path, body)).json() as Promise<T>;
  const res = await fetch(`/api${path}`);
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof detail.detail === "string" ? detail.detail : JSON.stringify(detail.detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  overview: () => call<Overview>("/overview"),
  setKill: (killed: boolean) => call<Overview>("/control/kill", { killed }),
  repos: () => call<RepoRow[]>("/repos"),
  addRepo: (full_name: string, role: string) => call<RepoRow[]>("/repos", { full_name, role }),
  setRole: (repo: string, role: string) => call<RepoRow[]>(`/repos/${repo}/role`, { role }),
  runReport: (repo: string) => call<{ run_id: string }>(`/repos/${repo}/run`, {}),
  removeRepo: (repo: string) => call<RepoRow[]>(`/repos/${repo}/remove`, {}),
  setupRepo: (repo: string) => call<{ run_id: string }>(`/repos/${repo}/setup`, {}),
  reportLive: (repo: string, runId: string) => call<LiveCheck>(`/repos/${repo}/report/live?run_id=${runId}`),
  report: (repo: string, runId?: string) => call<ReportView>(`/repos/${repo}/report${runId ? `?run_id=${runId}` : ""}`),
  jobs: () => call<JobRow[]>("/jobs"),
  job: (id: string) => call<JobDetail>(`/jobs/${id}`),
  stopJob: (id: string) => call<JobDetail>(`/jobs/${id}/stop`, {}),
  resumeJob: (id: string) => call<JobDetail>(`/jobs/${id}/resume`, {}),
  chat: (repo: string, n: number) => call<ChatView>(`/chat/${repo}/${n}`),
  tasks: (archived = false) => call<TaskSummary[]>(`/tasks${archived ? "?archived=true" : ""}`),
  attach: (id: number, url: string) => call<TaskDetail>(`/tasks/${id}/attach`, { url }),
  archive: (id: number, archived: boolean) => call<TaskDetail>(`/tasks/${id}/archive`, { archived }),
  task: (id: number) => call<TaskDetail>(`/tasks/${id}`),
  create: (repo: string, issue: number, base: string, rehearsal: boolean, note = "") =>
    call<{ status: string; repo: string; note: string }>("/tasks", { repo, issue, base: base || null, rehearsal, note }),
  decide: (id: number, approve: boolean, note: string, setup: string[], test: string[], models: Record<string, string> = {}) =>
    call<TaskDetail>(`/tasks/${id}/decision`, {
      approve,
      note: note || null,
      setup_commands: setup,
      test_commands: test,
      models,
    }),
  models: () => call<ModelsPage>("/models"),
  prChecks: () => call<PRCheckRow[]>("/pr-checks"),
  prCheck: (id: number) => call<PRCheckView>(`/pr-checks/${id}`),
  checkPr: (url: string) => call<{ run_id: string }>("/pr-checks", { url }),
  run: (id: number) => call<{ status: string }>(`/tasks/${id}/run`, {}),
  revise: (id: number, note: string, latest: boolean) => call<TaskDetail>(`/tasks/${id}/revise`, { note, latest }),
  claim: (id: number, confirm: boolean) => call<{ result: string }>(`/tasks/${id}/claim`, { confirm }),
  publish: (id: number, confirm: boolean, edits: PublishEdits = {}) =>
    call<GateInfo>(`/tasks/${id}/publish`, { confirm, ...edits }),
};
