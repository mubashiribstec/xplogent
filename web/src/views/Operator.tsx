import { useEffect, useRef, useState } from "react";
import { Check, Monitor, Play, Square, X } from "lucide-react";
import { OperatorSocket, wsTokenParam, XplogentEvent } from "../api";

interface Approval { id: string; tool: string; risk: string; reason: string; arguments: Record<string, unknown>; }
interface Line { kind: string; text: string; }

export function Operator() {
  const [goal, setGoal] = useState("");
  const [running, setRunning] = useState(false);
  const [lines, setLines] = useState<Line[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [shot, setShot] = useState<string>("");
  const sock = useRef<OperatorSocket | null>(null);
  const feed = useRef<HTMLDivElement>(null);

  const push = (kind: string, text: string) => setLines((l) => [...l, { kind, text }]);
  useEffect(() => { feed.current?.scrollTo(0, feed.current.scrollHeight); }, [lines]);

  // Refresh the live screen preview while the operator runs.
  useEffect(() => {
    if (!running) return;
    const tick = () => setShot(`/operator/screen?t=${Date.now()}${wsTokenParam()}`);
    tick();
    const id = setInterval(tick, 1500);
    return () => clearInterval(id);
  }, [running]);

  const onEvent = (ev: XplogentEvent) => {
    switch (ev.type) {
      case "tool_call": push("act", `→ ${ev.tool} ${JSON.stringify(ev.arguments ?? {}).slice(0, 120)}`); break;
      case "tool_result": push(ev.ok === false ? "err" : "obs", `${ev.tool}: ${String(ev.output ?? "").slice(0, 200)}`); break;
      case "message": if (ev.content) push("say", String(ev.content)); break;
      case "agent_status": push("status", `status: ${ev.status}`); break;
      case "error": push("err", String(ev.message ?? "error")); break;
      case "approval_required":
        setApprovals((a) => [...a, ev as unknown as Approval]); break;
      case "done": setRunning(false); push("status", "finished"); break;
    }
  };

  // Close the socket when leaving the view (the server then stops the run).
  useEffect(() => () => sock.current?.close(), []);

  const start = () => {
    if (!goal.trim() || running) return;
    setLines([]); setApprovals([]); setRunning(true);
    sock.current?.close();
    const s = new OperatorSocket(onEvent, () => { setRunning(false); setApprovals([]); });
    sock.current = s;
    s.start(goal.trim());  // queued until the socket opens
  };
  const stop = () => { sock.current?.cancel(); setRunning(false); };
  const resolve = (id: string, ok: boolean) => {
    sock.current?.resolveApproval(id, ok);
    setApprovals((a) => a.filter((x) => x.id !== id));
  };

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head"><h1><Monitor size={22} /> Operator</h1>
          <span className="dim">computer-use · screenshot → analyze → act</span></div>
        <p className="dim">Give a goal and the agent will drive your screen, one approval-gated action at a time.
          Needs the <code>control</code> extra and a desktop session.</p>

        <div className="card">
          <div className="row" style={{ gap: 8 }}>
            <input placeholder="e.g. open the calculator and compute 12 × 9" value={goal}
              disabled={running} onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && !running && start()} style={{ flex: 1 }} />
            {running
              ? <button className="btn danger" onClick={stop}><Square size={14} /> Stop</button>
              : <button className="btn primary" onClick={start}><Play size={14} /> Start</button>}
          </div>
        </div>

        {approvals.map((a) => (
          <div key={a.id} className="card approval">
            <b>Approve {a.tool}?</b> <span className={`badge ${a.risk === "high" || a.risk === "critical" ? "bad" : "warn"}`}>{a.risk}</span>
            <p className="dim" style={{ margin: "6px 0" }}>{a.reason} · <code>{JSON.stringify(a.arguments)}</code></p>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn primary" onClick={() => resolve(a.id, true)}><Check size={14} /> Allow</button>
              <button className="btn" onClick={() => resolve(a.id, false)}><X size={14} /> Deny</button>
            </div>
          </div>
        ))}

        <div className="op-grid">
          <div className="card">
            <h3>Activity</h3>
            <div className="op-feed" ref={feed}>
              {lines.length === 0 && <p className="dim">no activity yet</p>}
              {lines.map((l, i) => <div key={i} className={`op-line ${l.kind}`}>{l.text}</div>)}
            </div>
          </div>
          <div className="card">
            <h3>Live screen</h3>
            {running && shot
              ? <img className="op-screen" src={shot} alt="live screen"
                  onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
              : <p className="dim">the screen preview appears here while the operator runs</p>}
          </div>
        </div>
      </div>
    </div>
  );
}
