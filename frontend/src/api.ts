// Typed client for /api. Every write sends JSON, which the server requires (it refuses cross-site calls).

export interface Overview {
  spend: { today: number; last_7_days: number; this_month: number };
  killed: boolean;
}

export interface RepoRow {
  full_name: string;
  role: "contributor" | "maintainer";
  auto_check: boolean; // others' open PRs checked on every background sync
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
  coverage: string; // how much of the suite the test commands run, and what they leave out
}

// The report's listed issues, asked about on GitHub again: the ones someone has taken since.
export interface LiveCheck {
  checked: number;
  taken: { number: number; why: string }[];
  at: string;
}

// What happened to the pull requests made with NightShift
export interface RecordPR {
  repo: string;
  number: number;
  url: string;
  task_id: number;
  title: string;
  outcome: "merged" | "approved" | "changes requested" | "waiting" | "closed" | "unknown";
  opened: string | null;
  days_to_merge: number | null;
  days_open: number | null;
  cost_usd: number;
  imported: boolean; // added by hand: no cost recorded
  attempts: number;
  understood: boolean | null; // passed the understanding check; null = not taken
  ci: "passing" | "failing" | "running" | "none"; // the PR's CI on its last commit
}
export interface TrackRecord {
  summary: {
    prs: number; merged: number; approved: number; changes_requested: number; waiting: number; closed: number;
    unknown: number; accepted: number; merge_rate: number | null; median_days_to_merge: number | null;
    median_cost_usd: number | null; total_cost_usd: number; cost_per_merged_usd: number | null; with_cost: number;
    ci_failing: number;
  };
  prs: RecordPR[];
}

// One watched repo on the Compare page, from its latest contributor report (no pulse: no report yet).
export interface CompareRow {
  repo: string;
  refresh: "queued" | "running" | "done" | "failed" | null;
  report_at: string | null;
  run_id: string | null;
  pulse: {
    verdict: string;
    verdict_reason: string;
    merge_rate: number | null;
    outside_prs_merged: number;
    outside_prs_closed_unmerged: number;
    outside_prs_waiting: number;
    median_pr_response_days: number | null;
    median_issue_response_days: number | null;
    last_merge: string | null;
  } | null;
  starters: { number: number; title: string; size: "small" | "medium" | "large"; kind: string; summary: string; first_step: string }[];
  ask_first: number;
  ai_rules: string[];
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

export type ChatAction = "plan" | "approve_and_run" | "continue" | "start_over" | "plan_again" | "review_and_publish" | "archive";
export interface ChatCard { action: ChatAction; note: string; why: string }

export type ChatTurn =
  | { role: "user"; text: string }
  | { role: "assistant"; html: string }
  | { role: "tools"; tools: string[] }
  | ({ role: "action" } & ChatCard);

export interface ChatView {
  repo: string;
  item: { number: number; kind: string; title: string; state: string; url: string; author: string | null; labels: string[] };
  body_html: string;
  history: ChatTurn[];
  cost_usd: number;
  // The task the chat's cards act on, and what its state allows now
  task: { id: number; status: string; stopped_at: string | null; error: string | null; pr_url: string | null } | null;
  allowed: ChatAction[];
}

export type Stage = "plan" | "gate1" | "work" | "gate2" | "done" | "stopped" | "closed";

export interface TaskSummary {
  id: number;
  repo: string;
  issue: number;
  title: string;
  status: string;
  stage: Stage;
  cost_usd: number;
  error: string | null;
  busy: boolean;
  archived: boolean;
  updated_at: string | null;
  feedback: number; // unanswered review feedback on its open PR, or replies to its question, from the sync
  ci_failing: string[]; // CI jobs failing on its open PR, from the sync: a red PR waits on its author
  fixed_open: boolean; // its PR merged and the issue is still open: a "fixed by" comment is offered
  asking: boolean; // a plan that asks the maintainers before any code
  asked: boolean; // and the question is posted on the issue
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
  suites_run?: boolean; // false until the Reviewer approves; older tasks ran the suites on every submit
  test_files: string[];
  source_files: string[];
  fails_without_fix: CommandResult | null;
  passes_with_fix: CommandResult | null;
  suite: CommandResult[];
  regressions: string[];
  already_failing: string[];
  problems: string[];
}

// One check a task's change passed or failed, in the order they ran (cheapest first; round 0 is the plan).
export interface CheckStep {
  check: "plan" | "scope" | "test" | "review" | "suites";
  ok: boolean;
  round: number;
  seconds: number;
  detail: string;
}

// Can the contributor explain the change? Questions before the first answers; points and grades after.
export interface Understanding {
  questions: { question: string; about: string; expects?: string[] }[];
  last: {
    answers: string[];
    grades: { verdict: "understood" | "partly" | "missed"; feedback: string }[];
    passed: boolean;
    at: string;
  } | null;
  attempts: number;
  passed: boolean;
  cost_usd: number;
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
  fixed_comment: string; // the drafted "fixed by" comment, while fixed_open
  baseline: CommandResult[];
  check: FixCheck | null;
  understanding: Understanding | null;
  ai_rules: string[]; // the repo's PR rules about AI use
  checks: CheckStep[];
  permission: { files: string[]; reason: string } | null; // the Coder asks to keep regenerated files
  attempts: { n: number; status: string; stopped_at: string | null; error: string | null; cost_usd: number; lines: number; at: string }[];
  resume: { stopped_at: string; todo: string } | null; // where the queued run picks up
  can_continue: boolean; // the last attempt left a change to continue from
  // Did the planning context hold the files the fix changed? Null for tasks planned before it was kept.
  context_check: { changed: string[]; relevant: string[]; planned: string[]; missed: string[]; new: string[] } | null;
  reviews: { round: number; verdict: string; html: string }[];
  diff: string;
  pr: { title: string; html: string; commit: string } | null;
  pr_url: string | null;
  coder_steps: number | null;
  models: ModelUse[];
  chosen_models: Record<string, string>;
  // At Gate 1 only: per agent whose model can be chosen, the router's first pick and the options.
  model_choices: Record<string, { default: string; why: string; options: ModelView[] }> | null;
  replies: { author: string; text: string; url: string }[]; // to the question on the issue, since it was asked
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
  review_url: string | null; // the review posted from this check
  auto: boolean; // started by the background sync, for a repo that opted in
  dismissed: boolean; // not to be posted
}

export interface PRCheckView extends PRCheckRow {
  html: string;
}

export type ReviewEvent = "COMMENT" | "REQUEST_CHANGES" | "APPROVE";

// A check as a review on its PR: the draft to edit before posting, or what was posted.
export interface ReviewDraft {
  body: string;
  event: ReviewEvent;
  events: ReviewEvent[];
  attribution_line: string;
  warnings: string[];
  review_url: string | null;
  posted?: boolean;
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
  mode?: "follow_up" | "replies_only"; // a revised published task: one more commit on its PR, or replies only
  replies?: (ReviewComment & { text: string })[]; // follow-up: a drafted reply per addressed comment
  blockers: string[];
  warnings: string[];
  published: boolean;
  url?: string;
  reply_urls?: string[];
}

// Review feedback on a published PR (written on GitHub: shown as text, never as HTML).
export interface ReviewComment {
  id: string;
  kind: "thread" | "review" | "comment" | "issue";
  author: string;
  body: string;
  path: string | null;
  line: number | null;
  url: string;
  outdated: boolean;
  bot: boolean;
  state: string | null;
  superseded: boolean;
  issue: number | null; // for kind "issue": the issue the PR closes
  suggested?: boolean;
}

// What Gate 2 may change before publishing; omitted fields keep the draft.
export interface PublishEdits {
  replies?: Record<string, string>;
  post_replies?: boolean;
  title?: string;
  body?: string;
  commit_message?: string;
  attribution?: string;
  branch?: string;
  remember?: boolean;
}

// ---------- repo workspace ----------

// One issue or PR in a repo: a task (an issue NightShift worked on, or a PR of yours) or a PR check.
export interface RepoItem {
  kind: "task" | "check";
  id: number;
  number: number;
  title: string;
  role: "yours" | "incoming" | "issue";
  status: string;
  stage?: Stage;
  verdict?: string;
  must?: number;
  should?: number;
  pr_url: string | null;
  pr_number?: number | null; // a task's PR, once published; `number` stays the issue it fixes
  pr_state: "OPEN" | "MERGED" | "CLOSED" | null;
  open: boolean;
  hidden: boolean;
  updated_at: string | null;
  feedback?: number;
  ci_failing?: string[];
  fixed_open?: boolean;
  asking?: boolean;
  asked?: boolean;
}

export interface SyncStatus {
  at: string | null;
  running: boolean;
  checked: number;
  with_feedback: number;
  error: string | null;
  minutes: number;
  github_down_since?: string | null; // GitHub cannot be connected to; requests wait for it
}

export interface MapComponent {
  id: string;
  name: string;
  path: string | null;
  group: string;
  responsibility: string;
}

export interface RepoMap {
  commit: string;
  built_at: string;
  summary: string;
  components: MapComponent[];
  relationships: { source: string; target: string; verb: string; evidence_path: string | null }[];
}

export interface RepoKnowledge {
  environment: RepoEnvironment | null;
  recipe: { setup: string[]; tests: string[]; passing: string[] } | null;
  map: { commit: string; built_at: string; components: number; relationships: number } | null;
  brief: string | null;
  // The latest run that set the repo up: environment, then map, then brief.
  setup: { run_id: string; status: string; step: string | null; error: string | null; at: string } | null;
}

export interface RepoStats {
  open_issues: number | null;
  open_prs: number | null;
  merged_30d: number | null;
  stars: number | null;
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
  version: () => call<{ running: string | null; latest: string | null; old: boolean }>("/version"),
  setKill: (killed: boolean) => call<Overview>("/control/kill", { killed }),
  repos: () => call<RepoRow[]>("/repos"),
  repoItems: (repo: string) => call<RepoItem[]>(`/repos/${repo}/items`),
  repoMap: (repo: string) => call<RepoMap>(`/repos/${repo}/map`),
  repoKnowledge: (repo: string) => call<RepoKnowledge>(`/repos/${repo}/knowledge`),
  repoStats: (repo: string) => call<RepoStats>(`/repos/${repo}/stats`),
  syncStatus: () => call<SyncStatus>("/sync"),
  replan: (id: number, answer: string) => call<{ status: string; repo: string }>(`/tasks/${id}/replan`, { answer }),
  onboardRepo: (repo: string) => call<{ run_id: string }>(`/repos/${repo}/onboard`, {}),
  syncNow: () => call<SyncStatus>("/sync", {}),
  addRepo: (full_name: string, role: string) => call<RepoRow[]>("/repos", { full_name, role }),
  setRole: (repo: string, role: string) => call<RepoRow[]>(`/repos/${repo}/role`, { role }),
  runReport: (repo: string) => call<{ run_id: string }>(`/repos/${repo}/run`, {}),
  removeRepo: (repo: string) => call<RepoRow[]>(`/repos/${repo}/remove`, {}),
  setupRepo: (repo: string) => call<{ run_id: string }>(`/repos/${repo}/setup`, {}),
  understandingQuestions: (id: number, fresh = false) => call<Understanding>(`/tasks/${id}/understanding`, { fresh }),
  understandingAnswers: (id: number, answers: string[]) =>
    call<Understanding>(`/tasks/${id}/understanding/answers`, { answers }),
  trackRecord: () => call<TrackRecord>("/track-record"),
  addToRecord: (url: string) => call<TrackRecord>("/track-record", { url }),
  compare: () => call<CompareRow[]>("/compare"),
  refreshCompare: (staleDays = 7) => call<{ queued: string[]; already: boolean }>("/compare/refresh", { stale_days: staleDays }),
  reportLive: (repo: string, runId: string) => call<LiveCheck>(`/repos/${repo}/report/live?run_id=${runId}`),
  report: (repo: string, runId?: string) => call<ReportView>(`/repos/${repo}/report${runId ? `?run_id=${runId}` : ""}`),
  jobs: () => call<JobRow[]>("/jobs"),
  job: (id: string) => call<JobDetail>(`/jobs/${id}`),
  stopJob: (id: string) => call<JobDetail>(`/jobs/${id}/stop`, {}),
  resumeJob: (id: string) => call<JobDetail>(`/jobs/${id}/resume`, {}),
  chat: (repo: string, n: number) => call<ChatView>(`/chat/${repo}/${n}`),
  tasks: (archived = false) => call<TaskSummary[]>(`/tasks${archived ? "?archived=true" : ""}`),
  feedback: (id: number) => call<ReviewComment[]>(`/tasks/${id}/feedback`),
  address: (id: number, comment_ids: string[], note: string) =>
    call<TaskDetail>(`/tasks/${id}/address`, { comment_ids, note }),
  draftReplies: (id: number, comment_ids: string[]) =>
    call<(ReviewComment & { text: string })[]>(`/tasks/${id}/replies/draft`, { comment_ids }),
  postReplies: (id: number, replies: Record<string, string>) =>
    call<{ posted: string[] }>(`/tasks/${id}/replies/post`, { replies }),
  fromPr: (url: string) => call<TaskDetail>("/tasks/from-pr", { url }),
  attach: (id: number, url: string) => call<TaskDetail>(`/tasks/${id}/attach`, { url }),
  archive: (id: number, archived: boolean) => call<TaskDetail>(`/tasks/${id}/archive`, { archived }),
  task: (id: number) => call<TaskDetail>(`/tasks/${id}`),
  create: (repo: string, issue: number, base: string, note = "") =>
    call<{ status: string; repo: string; note: string }>("/tasks", { repo, issue, base: base || null, note }),
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
  incomingChecks: () => call<PRCheckRow[]>("/incoming-checks"),
  dismissCheck: (id: number) => call<{ dismissed: boolean }>(`/pr-checks/${id}/dismiss`, {}),
  setAutoCheck: (repo: string, on: boolean) => call<RepoRow[]>(`/repos/${repo}/auto-check`, { on }),
  prCheck: (id: number) => call<PRCheckView>(`/pr-checks/${id}`),
  checkPr: (url: string) => call<{ run_id: string }>("/pr-checks", { url }),
  reviewPr: (id: number, confirm: boolean, edits: { body?: string; event?: ReviewEvent; attribution?: boolean } = {}) =>
    call<ReviewDraft>(`/pr-checks/${id}/review`, { confirm, ...edits }),
  run: (id: number) => call<{ status: string }>(`/tasks/${id}/run`, {}),
  permission: (id: number, allow: boolean) => call<TaskDetail>(`/tasks/${id}/permission`, { allow }),
  revise: (id: number, note: string, latest: boolean, mode: "continue" | "restart" = "continue") =>
    call<TaskDetail>(`/tasks/${id}/revise`, { note, latest, mode }),
  claim: (id: number, confirm: boolean) => call<{ result: string }>(`/tasks/${id}/claim`, { confirm }),
  fixedComment: (id: number, body: { confirm?: boolean; text?: string; dismiss?: boolean }) =>
    call<{ result: string }>(`/tasks/${id}/fixed-comment`, body),
  publish: (id: number, confirm: boolean, edits: PublishEdits = {}) =>
    call<GateInfo>(`/tasks/${id}/publish`, { confirm, ...edits }),
};
