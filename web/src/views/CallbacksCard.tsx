import { useEffect, useState } from "react";
import { CallbackRow, cancelCallback, getCallbacks } from "../api";

type Toaster = (m: string, kind?: any) => void;
const STATUS: Record<string, string> = {
  pending: "booked", dialled: "called back", failed: "call failed", missed: "missed", cancelled: "cancelled",
  replaced: "replaced by a newer time",
};

/** Callbacks the agent agreed with callers: the AI places them at the agreed time. */
export function CallbacksCard({ toast }: { toast: Toaster }) {
  const [rows, setRows] = useState<CallbackRow[] | null>(null);
  const load = () => getCallbacks().then((r) => setRows(r.callbacks)).catch(() => {});
  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id); }, []);
  if (!rows || rows.length === 0) return null;
  const pending = rows.filter((r) => r.status === "pending").length;
  return (
    <div className="card">
      <h3>Callbacks {pending > 0 && <span className="badge">{pending} booked</span>}</h3>
      <p className="explain">Times the agent agreed with callers. It calls back itself at that time (inside calling hours);
        callbacks a recruiter must make go to the CRM instead.</p>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>When</th><th>Number</th><th>Agreed as</th><th>Status</th><th></th></tr></thead>
        <tbody>{rows.slice(0, 15).map((r) => (
          <tr key={r.id}>
            <td style={{ whiteSpace: "nowrap" }}>{new Date(r.at * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
            <td className="mono">{r.phone}</td><td>{r.spoken}</td>
            <td><span className={`badge ${r.status === "pending" ? "" : r.status === "dialled" ? "ok" : "warn"}`}>{STATUS[r.status] ?? r.status}</span>
              {r.detail && <span className="dim" style={{ fontSize: 12 }}> {r.detail}</span>}</td>
            <td>{r.status === "pending" && <button className="btn ghost sm" onClick={async () => {
              if (!confirm(`Cancel the callback to ${r.phone}?`)) return;
              await cancelCallback(r.id); toast("callback cancelled", "success"); load();
            }}>Cancel</button>}</td>
          </tr>))}</tbody>
      </table></div>
    </div>
  );
}
