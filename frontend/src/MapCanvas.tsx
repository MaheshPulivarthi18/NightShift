import { useEffect, useMemo, useRef, useState } from "react";
import { MapComponent, RepoMap } from "./api";
import { ago } from "./ui";

// The architecture map on a canvas: groups of components laid out by code, links between them, drag to
// move, scroll or − / + to zoom. Clicking a component fades everything not linked to it and lists what it
// uses and what uses it, each with the file that proves the link.

const NW = 184; // node width
const NH = 46; // node height
const GAP_X = 20;
const GAP_Y = 16;
const ROWS = 6; // nodes per column inside a group before it wraps into another column
const PAD = 16;
const HEAD = 28; // room for the group's name
const ROW_WIDTH = 1500; // groups wrap onto a new row past this width

interface Box { x: number; y: number }
interface Layout { pos: Record<string, Box>; groups: { name: string; x: number; y: number; w: number; h: number }[]; w: number; h: number }

const HUES = ["var(--lamp)", "var(--violet)", "var(--ok)", "var(--warn)", "var(--bad)", "var(--info)", "var(--ink-3)"];

function layout(components: MapComponent[]): Layout {
  const byGroup = new Map<string, MapComponent[]>();
  for (const c of components) byGroup.set(c.group, [...(byGroup.get(c.group) ?? []), c]);
  // Smaller groups first reads left to right from entry points to the many helpers, in most maps.
  const names = [...byGroup.keys()].sort((a, b) => byGroup.get(a)!.length - byGroup.get(b)!.length || a.localeCompare(b));
  const pos: Record<string, Box> = {};
  const groups: Layout["groups"] = [];
  let x = 0, y = 0, rowH = 0, maxW = 0;
  for (const name of names) {
    const members = byGroup.get(name)!;
    const cols = Math.ceil(members.length / ROWS);
    const rows = Math.min(ROWS, members.length);
    const w = PAD * 2 + cols * NW + (cols - 1) * GAP_X;
    const h = HEAD + PAD + rows * NH + (rows - 1) * GAP_Y;
    if (x > 0 && x + w > ROW_WIDTH) { x = 0; y += rowH + 48; rowH = 0; }
    members.forEach((c, i) => {
      pos[c.id] = { x: x + PAD + Math.floor(i / ROWS) * (NW + GAP_X), y: y + HEAD + (i % ROWS) * (NH + GAP_Y) };
    });
    groups.push({ name, x, y, w, h });
    x += w + 64;
    rowH = Math.max(rowH, h);
    maxW = Math.max(maxW, x);
  }
  return { pos, groups, w: maxW, h: y + rowH };
}

function edgePath(a: Box, b: Box): { d: string; arrow: string } {
  const ax = a.x + NW / 2, ay = a.y + NH / 2, bx = b.x + NW / 2, by = b.y + NH / 2;
  const dx = bx - ax, dy = by - ay;
  let d: string, tip: [number, number], dir: "r" | "l" | "u" | "d";
  if (Math.abs(dx) < 10) {
    const o = 30 + Math.abs(dy) * 0.12; // same column: curve out to the left so it does not cross the nodes between
    d = `M${a.x} ${ay} C${a.x - o} ${ay} ${b.x - o} ${by} ${b.x} ${by}`; tip = [b.x, by]; dir = "r";
  } else if (Math.abs(dx) >= Math.abs(dy)) {
    const sx = dx > 0 ? a.x + NW : a.x, tx = dx > 0 ? b.x : b.x + NW, c = (tx - sx) / 2;
    d = `M${sx} ${ay} C${sx + c} ${ay} ${tx - c} ${by} ${tx} ${by}`; tip = [tx, by]; dir = dx > 0 ? "r" : "l";
  } else {
    const sy = dy > 0 ? a.y + NH : a.y, ty = dy > 0 ? b.y : b.y + NH, c = (ty - sy) / 2;
    d = `M${ax} ${sy} C${ax} ${sy + c} ${bx} ${ty - c} ${bx} ${ty}`; tip = [bx, ty]; dir = dy > 0 ? "d" : "u";
  }
  const s = 8, w = 4.5;
  const tri = { r: [[-s, -w], [-s, w]], l: [[s, -w], [s, w]], d: [[-w, -s], [w, -s]], u: [[-w, s], [w, s]] }[dir];
  const arrow = `M${tip[0]} ${tip[1]} L${tip[0] + tri[0][0]} ${tip[1] + tri[0][1]} L${tip[0] + tri[1][0]} ${tip[1] + tri[1][1]} Z`;
  return { d, arrow };
}

export function MapCanvas({ map, repo }: { map: RepoMap; repo: string }) {
  const L = useMemo(() => layout(map.components), [map]);
  const byId = useMemo(() => Object.fromEntries(map.components.map((c) => [c.id, c])), [map]);
  const hue = useMemo(() => Object.fromEntries(L.groups.map((g, i) => [g.name, HUES[i % HUES.length]])), [L]);
  const [view, setView] = useState({ z: 0.8, x: 24, y: 24 });
  const [sel, setSel] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const port = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
  const moved = useRef(false);

  const fit = () => {
    const el = port.current;
    if (!el) return;
    const z = Math.min(1.2, Math.max(0.25, Math.min((el.clientWidth - 48) / L.w, (el.clientHeight - 48) / L.h)));
    setView({ z, x: 24, y: 24 });
  };
  useEffect(fit, [L]); // eslint-disable-line react-hooks/exhaustive-deps
  const zoomAt = (f: number, mx: number, my: number) =>
    setView((v) => {
      const z = Math.max(0.25, Math.min(2.5, v.z * f)), k = z / v.z;
      return { z, x: mx - (mx - v.x) * k, y: my - (my - v.y) * k };
    });
  useEffect(() => {
    const el = port.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault(); // zoom the canvas, not the page
      const b = el.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.1 : 1 / 1.1, e.clientX - b.left, e.clientY - b.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const near = new Set<string>();
  if (sel) {
    near.add(sel);
    for (const r of map.relationships) {
      if (r.source === sel) near.add(r.target);
      if (r.target === sel) near.add(r.source);
    }
  }
  const query = q.trim().toLowerCase();
  const hits = new Set(query ? map.components.filter((c) => `${c.name} ${c.path ?? ""}`.toLowerCase().includes(query)).map((c) => c.id) : []);
  const edges = map.relationships.filter((r) => L.pos[r.source] && L.pos[r.target]).map((r) => ({ r, ...edgePath(L.pos[r.source], L.pos[r.target]) }));
  const on = (r: { source: string; target: string }) => !!sel && (r.source === sel || r.target === sel);
  const selected = sel ? byId[sel] : null;
  const rel = (dirOut: boolean) =>
    map.relationships.filter((r) => (dirOut ? r.source : r.target) === sel).map((r) => ({ r, other: byId[dirOut ? r.target : r.source] })).filter((x) => x.other);

  return (
    <div className="map">
      <div className="map-tool">
        <span className="dim">Built at <span className="mono">{map.commit.slice(0, 7)}</span> · {ago(map.built_at)} · {map.components.length} components, {map.relationships.length} links</span>
        <span className="grow" />
        <input className="map-search" placeholder="Find a component or file…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find a component" />
        <div className="zoom" role="group" aria-label="Zoom">
          <button onClick={() => zoomAt(1 / 1.2, 400, 300)} aria-label="Zoom out">−</button>
          <button className="pct" onClick={fit} title="Fit to the window">{Math.round(view.z * 100)}%</button>
          <button onClick={() => zoomAt(1.2, 400, 300)} aria-label="Zoom in">+</button>
        </div>
      </div>
      <div className="map-body">
        <div
          className="viewport"
          ref={port}
          onMouseDown={(e) => (drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false })}
          onMouseMove={(e) => {
            const d = drag.current;
            if (!d) return;
            const dx = e.clientX - d.x, dy = e.clientY - d.y;
            if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
            if (d.moved) setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
          }}
          onMouseUp={() => { moved.current = !!drag.current?.moved; drag.current = null; }}
          onMouseLeave={() => (drag.current = null)}
        >
          <div className="world" style={{ width: L.w, height: L.h, transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }}>
            <svg className="edges" width={L.w} height={L.h} aria-hidden>
              {edges.map(({ r, d, arrow }, i) => (
                <g key={i} className={on(r) ? "on" : ""}>
                  <path className="e" d={d} />
                  <path className="ah" d={arrow} />
                </g>
              ))}
            </svg>
            {L.groups.map((g) => (
              <div key={g.name} className="grp" style={{ left: g.x, top: g.y, width: g.w, height: g.h }}><span>{g.name}</span></div>
            ))}
            {map.components.map((c) => {
              const p = L.pos[c.id];
              const dim = (sel && !near.has(c.id)) || (!sel && hits.size > 0 && !hits.has(c.id));
              const cls = ["node", c.path ? "" : "ext", sel === c.id ? "sel" : "", hits.has(c.id) ? "hit" : "", dim ? "dim" : ""].join(" ");
              return (
                <button
                  key={c.id}
                  className={cls}
                  style={{ left: p.x, top: p.y, borderLeftColor: hue[c.group] }}
                  title={c.path ?? "outside the repo"}
                  onClick={() => { if (moved.current) { moved.current = false; return; } setSel(sel === c.id ? null : c.id); }}
                >
                  <span className="nn">{c.name}</span>
                  <span className="np mono">{(c.path ?? "outside the repo").split("/").slice(-2).join("/")}</span>
                </button>
              );
            })}
          </div>
        </div>
        <aside className="map-side">
          {selected ? (
            <>
              <div className="row-flex"><span className="pill">{selected.group}</span><button className="ghost small push" onClick={() => setSel(null)}>Close</button></div>
              <h2>{selected.name}</h2>
              {selected.path ? (
                <a className="mono dim" href={`https://github.com/${repo}/blob/${map.commit}/${selected.path}`} target="_blank" rel="noreferrer">{selected.path} ↗</a>
              ) : <span className="dim">Outside the repo</span>}
              <p>{selected.responsibility}</p>
              {(["Uses", "Used by"] as const).map((label) => {
                const list = rel(label === "Uses");
                return (
                  <div key={label}>
                    <h3>{label}</h3>
                    {list.length === 0 && <p className="dim">Nothing</p>}
                    {list.map(({ r, other }, i) => (
                      <button key={i} className="rel" onClick={() => setSel(other.id)}>
                        <span className="dim">{r.verb}</span>
                        <span className="rn">{other.name}</span>
                        <span className="mono dim">{r.evidence_path ?? "inferred: no file cited"}</span>
                      </button>
                    ))}
                  </div>
                );
              })}
            </>
          ) : (
            <>
              <h2>{repo}</h2>
              <p className="dim">{map.summary}</p>
              <div className="legend">
                {L.groups.map((g) => <span key={g.name}><i style={{ background: hue[g.name] }} />{g.name}</span>)}
              </div>
              <p className="dim small-print">Drag to move, scroll or − / + to zoom. Click a component to see its files and links; everything else fades.</p>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
