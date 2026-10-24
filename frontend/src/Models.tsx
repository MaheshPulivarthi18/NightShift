import { AgentModels, api, ModelsPage } from "./api";
import { money, usePoll } from "./ui";

// Which model each agent uses, in the order the router tries them now, and how each has done.
export function Models() {
  const [data, error] = usePoll<ModelsPage>(api.models, 30000, []);
  return (
    <div className="page">
      <section className="page-head">
        <div>
          <h1>Models</h1>
          <p className="muted">
            Every call goes through OpenRouter. For each agent the router tries these models top to bottom, moving on when one
            fails. {data?.learning === false
              ? "Learning is off: the order is the configured one."
              : "It reorders them from results: models whose calls keep failing go last, a model that clearly succeeds more goes first."}{" "}
            Change an agent's models with <span className="mono">NIGHTSHIFT_CODER__MODEL</span> and{" "}
            <span className="mono">NIGHTSHIFT_CODER__LADDER</span> in <span className="mono">.env</span>, or pick one per task at Gate 1.
          </p>
        </div>
      </section>
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="muted">Loading models…</p>}
      {data?.agents.map((a) => <AgentCard key={a.agent} a={a} />)}
    </div>
  );
}

const LABEL: Record<string, string> = {
  relevance_analyst: "Relevance analyst",
  pr_assessor: "PR assessor",
  verifier: "Verifier",
  opportunity_scout: "Opportunity scout",
  repo_briefer: "Repo briefer",
  chat: "Chat",
  cartographer: "Cartographer",
  setup: "Setup agent",
  planner: "Planner",
  coder: "Coder",
  reviewer: "Reviewer",
  pr_writer: "PR writer",
};

function AgentCard({ a }: { a: AgentModels }) {
  const reordered = a.routes.map((r) => r.id).join() !== a.configured.filter((m) => a.routes.some((r) => r.id === m)).join();
  return (
    <section className="agent-models">
      <header>
        <h2>{LABEL[a.agent] ?? a.agent}</h2>
        <span className="tag">{a.tools ? "tool calls" : "structured answers"}</span>
        <span className="dim">effort {a.effort}</span>
        {reordered && <span className="tag learned">reordered by results</span>}
      </header>
      {a.error && <p className="error">{a.error}</p>}
      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th>#</th>
              <th>Model</th>
              <th>Why here</th>
              <th className="right">$ in / out per M</th>
              <th className="right">Score</th>
              <th className="right">Calls (failed)</th>
              <th className="right">Tasks ok</th>
              <th className="right">Spent</th>
              <th className="right">Avg time</th>
            </tr>
          </thead>
          <tbody>
            {a.routes.map((r, i) => (
              <tr key={r.id}>
                <td className="dim">{i + 1}</td>
                <td className="mono">{r.id}</td>
                <td className={/^(promoted|demoted)/.test(r.why) ? "learned-why" : "dim"}>{r.why}</td>
                <td className="right mono">
                  {r.input_per_m} / {r.output_per_m}
                </td>
                <td className="right mono" title={`coding ${r.coding ?? "–"} · agentic ${r.agentic ?? "–"}`}>
                  {r.intelligence ?? "–"}
                </td>
                <td className="right mono">
                  {r.stats.calls}
                  {r.stats.failed_calls > 0 && <span className="bad"> ({r.stats.failed_calls})</span>}
                </td>
                <td className="right mono">{r.stats.tasks ? `${r.stats.tasks_ok} of ${r.stats.tasks}` : "–"}</td>
                <td className="right mono">{money(r.stats.cost_usd)}</td>
                <td className="right mono">{r.stats.latency_ms ? `${(r.stats.latency_ms / 1000).toFixed(1)}s` : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
