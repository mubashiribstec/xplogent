import { useEffect, useState } from "react";
import { Check, RefreshCw, ShieldAlert, X } from "lucide-react";
import { AuditEntry, getAudit } from "../api";

const fmt = (t: number) => new Date(t * 1000).toLocaleString();

const RISKS = ["", "low", "medium", "high", "critical"];

export function Audit() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [action, setAction] = useState("");
  const [risk, setRisk] = useState("");

  const reload = () => getAudit(action, risk).then((r) => setEntries(r.entries)).catch(() => {});
  useEffect(() => { reload(); /* eslint-disable-next-line */ }, [action, risk]);

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><ShieldAlert size={22} /> Audit log</h1>
          <button className="icon-btn" onClick={reload} aria-label="refresh"><RefreshCw size={16} /></button>
        </div>
        <p className="dim">Every tool call, approval decision, and config/secret change is recorded here.</p>

        <div className="card">
          <div className="row" style={{ gap: 8 }}>
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              <option value="">all actions</option>
              <option value="tool">tool</option>
              <option value="config_change">config change</option>
              <option value="secret_change">secret change</option>
            </select>
            <select value={risk} onChange={(e) => setRisk(e.target.value)}>
              {RISKS.map((r) => <option key={r} value={r}>{r || "all risk"}</option>)}
            </select>
          </div>

          <table className="data-table mt-3">
            <thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>Risk</th><th>Allowed</th><th>Detail</th></tr></thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={i}>
                  <td className="dim" style={{ whiteSpace: "nowrap" }}>{fmt(e.created_at)}</td>
                  <td>{e.actor}</td>
                  <td><span className="badge">{e.action}</span></td>
                  <td className="mono">{e.target}</td>
                  <td>{e.risk && <span className={`badge ${e.risk === "high" || e.risk === "critical" ? "bad" : e.risk === "medium" ? "warn" : ""}`}>{e.risk}</span>}</td>
                  <td>{e.allowed === null ? "" : e.allowed ? <Check size={15} className="ok-i" /> : <X size={15} className="bad-i" />}</td>
                  <td className="dim ellipsis" style={{ maxWidth: 240 }}>{e.detail}</td>
                </tr>
              ))}
              {entries.length === 0 && <tr><td colSpan={7} className="dim">no audit entries yet</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
