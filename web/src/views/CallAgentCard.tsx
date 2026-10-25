import { useEffect, useState } from "react";
import { CallAgentInfo, getCallAgent, patchConfig, putSecrets, testCrm } from "../api";

type Toaster = (m: string, kind?: any) => void;
const fmt = (t: number) => (t ? new Date(t * 1000).toLocaleString() : "—");
const LABELS: Record<string, string> = {
  kingsbury_inbound: "Recruitment: inbound enquiries",
  kingsbury_outbound_screener: "Recruitment: outbound candidate screening",
};

/** Template pack, CRM post-call push and the knowledge-gap report. */
export function CallAgentCard({ toast, onChange }: { toast: Toaster; onChange: () => void }) {
  const [info, setInfo] = useState<CallAgentInfo | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => getCallAgent().then(setInfo).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!info) return null;

  const save = async (voip: Record<string, any>, note = "saved") => {
    await patchConfig({ voip }); toast(note, "success"); load(); onChange();
  };
  const test = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await testCrm();
      setMsg(r.ok ? { ok: true, text: `delivered (${r.detail})` } : { ok: false, text: r.error ?? r.detail ?? r.status ?? "failed" });
      load();
    } finally { setBusy(false); }
  };
  const tpl = info.templates.find((t) => t.name === info.template);
  const defaultTokenName = info.crm.token_env === "XPLOGENT_CRM_API_TOKEN";

  return (
    <div className="card">
      <h3>Call agent</h3>
      <label className="bud"><span className="lbl-sm">Template</span>
        <select value={info.template} onChange={(e) => save({ template: e.target.value }, "template saved")}>
          <option value="">none: use my greeting and playbook</option>
          {info.templates.map((t) => <option key={t.name} value={t.name}>{LABELS[t.name] ?? t.name} ({t.direction})</option>)}
        </select></label>
      {tpl && <p className="dim" style={{ fontSize: 12 }}>This template supplies the greeting, the playbook and the
        fields the call must collect ({tpl.fields.join(", ")}); the Greeting above is not used while it is selected.</p>}

      <h4 style={{ margin: "14px 0 6px" }}>CRM: send each finished call</h4>
      <div className="row wrap" style={{ gap: 10, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Send</span>
          <select value={info.crm.enabled ? "on" : "off"} onChange={(e) => save({ crm: { enabled: e.target.value === "on" } })}>
            <option value="off">off</option><option value="on">on</option></select></label>
        <label className="bud"><span className="lbl-sm">CRM address (POSTs go to …/api/xplogent/call-summary)</span>
          <input defaultValue={info.crm.base_url} placeholder="https://crm.example.com" style={{ width: 260 }}
            onBlur={(e) => {
              const v = e.target.value.trim().replace(/\/$/, "");
              if (v === info.crm.base_url) return;
              if (v && !/^https?:\/\//.test(v)) { toast("the CRM address must start with https://", "error"); return; }
              save({ crm: { base_url: v } });
            }} /></label>
        <label className="bud"><span className="lbl-sm">API token {info.crm.token_set ? "✓ set" : "✗ not set"}</span>
          {defaultTokenName
            ? <input type="password" placeholder={info.crm.token_set ? "•••••• (type to replace)" : "the CRM's token"}
                onBlur={async (e) => {
                  const v = e.target.value.trim();
                  if (!v) return;
                  await putSecrets({ XPLOGENT_CRM_API_TOKEN: v }); e.target.value = ""; toast("CRM token saved", "success"); load();
                }} />
            : <span className="dim mono" style={{ fontSize: 12 }}>from ${info.crm.token_env}</span>}</label>
        <button className="btn" onClick={test} disabled={busy || !info.crm.enabled || !info.crm.base_url || !info.crm.token_set}>
          {busy ? "Sending…" : "Send test"}</button>
      </div>
      <p className="dim" style={{ fontSize: 12 }}>Signed with the token (HMAC-SHA256); nothing is sent until it is set.
        See docs/crm-laravel.md for the receiving side.</p>
      {msg && <p className={msg.ok ? "ok" : "warn"}>{msg.ok ? "✓" : "✗"} {msg.text}</p>}
      {info.crm.deliveries.length > 0 && (
        <details><summary className="dim">Recent deliveries ({info.crm.deliveries.length})</summary>
          <table className="data-table"><thead><tr><th>When</th><th>Call</th><th>Try</th><th>Result</th></tr></thead>
            <tbody>{info.crm.deliveries.map((d, i) => (
              <tr key={i}><td className="dim">{fmt(d.ts)}</td><td>{d.call_id || "test"}</td><td>{d.attempt}</td>
                <td><span className={`badge ${d.status === "sent" ? "ok" : "bad"}`}>{d.status}</span> <span className="dim">{d.detail}</span></td></tr>
            ))}</tbody></table>
        </details>)}

      <h4 style={{ margin: "14px 0 6px" }}>Questions the knowledge base couldn't answer</h4>
      {info.knowledge_gaps.length === 0
        ? <p className="dim">none yet</p>
        : <ul className="list">{info.knowledge_gaps.map((g) => (
            <li key={g.query}><b>{g.count}×</b> {g.query} <span className="dim">· last {fmt(g.last_ts)}</span></li>))}</ul>}
      <p className="dim" style={{ fontSize: 12 }}>Add answers to these under Knowledge or Call training; they are the
        most useful things to teach the agent next.</p>
    </div>
  );
}
