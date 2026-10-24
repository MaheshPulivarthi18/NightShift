import { useEffect, useState } from "react";
import { Board } from "./Board";
import { TaskPage } from "./TaskPage";

// Hash routes (#/ and #/tasks/12) so the server only serves one page.
function useRoute(): string {
  const [hash, setHash] = useState(window.location.hash || "#/");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export function App() {
  const route = useRoute();
  const match = route.match(/^#\/tasks\/(\d+)/);
  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="lamp" aria-hidden />
          NightShift
        </a>
        <nav>
          <a href="#/" className={match ? "" : "active"}>Tasks</a>
          <a href="/repos">Repos</a>
          <a href="/jobs">Jobs</a>
        </nav>
        <span className="hint">
          <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>enter</kbd> open · <kbd>esc</kbd> back
        </span>
      </header>
      <main>{match ? <TaskPage id={Number(match[1])} /> : <Board />}</main>
    </div>
  );
}
