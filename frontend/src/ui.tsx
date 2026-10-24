// Small shared pieces: status badge, money, and a unified-diff viewer.

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
};

export function StatusBadge({ status, busy }: { status: string; busy?: boolean }) {
  return (
    <span className={`badge badge-${status}`}>
      {(busy || status === "working" || status === "planning") && <span className="pulse" aria-hidden />}
      {LABELS[status] ?? status}
    </span>
  );
}

export const money = (usd: number) => `$${usd.toFixed(2)}`;

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

// Server-sanitized HTML (nh3) only: plan, review and PR markdown is rendered and cleaned by the API.
export function Html({ html }: { html: string }) {
  return <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />;
}
