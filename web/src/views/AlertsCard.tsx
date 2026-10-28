import { useEffect, useState } from "react";
import { AlertRow, getAlerts, putSecrets, testAlert } from "../api";

type Toaster = (m: string, kind?: any) => void;

const KINDS: Record<string, string> = {
  call_failures: "Calls failing", crm_unreachable: "CRM down", calls_interrupted: "Calls cut off",
};

/** Where the agent shouts for help: a Slack/Teams (or any) webhook, and the recent alerts. */
export function AlertsCard({ toast }: { toast: Toaster }) {
  const [a, setA] = useState<{ alerts: AlertRow[]; webhook_set: boolean; min_gap_minutes: number } | null>(null);
  const load = () => getAlerts().then(setA).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!a) return null;
  return (
    <div className="card">
      <h3>Alerts</h3>
      <p className="explain">You're told when 5 calls in a row fail, the CRM stops answering for 5 minutes, or calls
        were cut off by a restart. Paste a Slack or Microsoft Teams incoming-webhook address (any URL that accepts
        JSON works). At most one alert of each kind every {a.min_gap_minutes} minutes.</p>
      <div className="row wrap" style={{ gap: 8, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Webhook address {a.webhook_set ? <span className="ok">✓ saved</span> : <span className="dim">not set</span>}</span>
          <input type="password" placeholder={a.webhook_set ? "•••••• (paste to replace)" : "https://hooks.slack.com/services/…"}
            style={{ width: 340 }} autoComplete="off"
            onBlur={async (e) => {
              const v = e.target.value.trim();
              if (!v) return;
              if (!/^https?:\/\//.test(v)) { toast("the address must start with https://", "error"); return; }
              await putSecrets({ XPLOGENT_ALERT_WEBHOOK_URL: v }); e.target.value = ""; toast("alert webhook saved", "success"); load();
            }} /></label>
        <button className="btn sm" disabled={!a.webhook_set} title={a.webhook_set ? "" : "save a webhook address first"}
          onClick={async () => {
            const r = await testAlert();
            toast(r.ok ? "test alert sent: check the channel" : (r.error ?? "could not send"), r.ok ? "success" : "error");
          }}>Send test alert</button>
      </div>
      {a.alerts.length === 0 ? <p className="dim" style={{ fontSize: 12 }}>No alerts yet.</p> : (
        <div className="table-scroll"><table className="data-table" style={{ marginTop: 8 }}>
          <thead><tr><th>When</th><th>What</th><th>Details</th></tr></thead>
          <tbody>{a.alerts.slice(0, 10).map((r) => (
            <tr key={`${r.at}-${r.kind}`}>
              <td style={{ whiteSpace: "nowrap" }}>{new Date(r.at * 1000).toLocaleString()}</td>
              <td>{KINDS[r.kind] ?? r.kind}</td><td>{r.text}</td>
            </tr>))}</tbody>
        </table></div>)}
    </div>
  );
}
