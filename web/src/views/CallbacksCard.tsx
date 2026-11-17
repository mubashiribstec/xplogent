import { useEffect, useState } from "react";
import { CallbackRow, cancelCallback, getCallbacks } from "../api";
import { Badge, Button, Card } from "../ui";
import { useConfirm } from "../ui/confirm";
import { visibleInterval } from "./calls/shared";

type Toaster = (m: string, kind?: any) => void;
const STATUS: Record<string, string> = {
  pending: "booked", dialled: "called back", failed: "call failed", missed: "missed", cancelled: "cancelled",
  replaced: "replaced by a newer time",
};

/** Callbacks the agent agreed with callers: the AI places them at the agreed time. */
export function CallbacksCard({ toast }: { toast: Toaster }) {
  const [rows, setRows] = useState<CallbackRow[] | null>(null);
  const confirm = useConfirm();
  const load = () => getCallbacks().then((r) => setRows(r.callbacks)).catch(() => {});
  useEffect(() => { load(); return visibleInterval(load, 30000); }, []);
  if (!rows || rows.length === 0) return null;
  const pending = rows.filter((r) => r.status === "pending").length;
  return (
    <Card title={<>Callbacks {pending > 0 && <Badge tone="info">{pending} booked</Badge>}</>}
      description="Times the agent agreed with callers. It calls back itself at that time (inside calling hours); callbacks a recruiter must make go to the CRM instead.">
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>When</th><th>Number</th><th>Agreed as</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{rows.slice(0, 15).map((r) => (
          <tr key={r.id}>
            <td data-label="When" style={{ whiteSpace: "nowrap" }}>{new Date(r.at * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
            <td data-label="Number" className="mono">{r.phone}</td><td data-label="Agreed as">{r.spoken}</td>
            <td data-label="Status"><Badge tone={r.status === "pending" ? "info" : r.status === "dialled" ? "ok" : "warn"}>{STATUS[r.status] ?? r.status}</Badge>
              {r.detail && <span className="dim xsmall"> {r.detail}</span>}</td>
            <td>{r.status === "pending" && <Button size="sm" variant="ghost" onClick={async () => {
              if (!(await confirm({ title: "Cancel this callback?", body: `The agent won't call ${r.phone} back.`, confirmLabel: "Cancel callback", cancelLabel: "Keep it", danger: true }))) return;
              await cancelCallback(r.id); toast("callback cancelled", "success"); load();
            }}>Cancel</Button>}</td>
          </tr>))}</tbody>
      </table></div>
    </Card>
  );
}
