import { useEffect, useState } from "react";
import { CallAgentInfo, CrmV2Check, getCallAgent, patchConfig, putSecrets, testCrm } from "../api";

type Toaster = (m: string, kind?: any) => void;
const fmt = (t: number) => (t ? new Date(t * 1000).toLocaleString() : "—");
const LABELS: Record<string, string> = {
  kingsbury_inbound: "Recruitment: inbound enquiries",
  kingsbury_outbound_screener: "Recruitment: outbound candidate screening",
};

/** Template pack, CRM post-call push and the knowledge-gap report. */
/** Mirrors crm.is_local_network_host: plain http is fine to this machine or the local network. */
function localNetwork(url: string): boolean {
  let host = "";
  try { host = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, ""); } catch { return false; }
  if (host === "localhost" || /^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host)) return true;
  if (host === "::1" || /^f[cd][0-9a-f]{2}:/.test(host) || /^fe80:/.test(host)) return true;
  if (/^[\d.]+$/.test(host) || host.includes(":")) return false;
  return !host.includes(".") || /\.(local|lan|home|internal|intranet|localdomain|home\.arpa|corp)$/.test(host);
}

export function CallAgentCard({ toast, onChange }: { toast: Toaster; onChange: () => void }) {
  const [info, setInfo] = useState<CallAgentInfo | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [v2, setV2] = useState<CrmV2Check | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => getCallAgent().then(setInfo).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!info) return null;

  const save = async (voip: Record<string, any>, note = "saved") => {
    await patchConfig({ voip }); toast(note, "success"); load(); onChange();
  };
  const test = async () => {
    setBusy(true); setMsg(null); setV2(null);
    try {
      const r = await testCrm();
      setMsg(r.ok ? { ok: true, text: `call summaries: delivered (${r.detail})` } : { ok: false, text: "call summaries: " + (r.error ?? r.detail ?? r.status ?? "failed") });
      setV2(r.v2 ?? null);
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
          <input defaultValue={info.crm.base_url} placeholder="http://192.168.1.20 or https://crm.example.com" style={{ width: 260 }}
            onBlur={(e) => {
              const v = e.target.value.trim().replace(/\/$/, "");
              if (v === info.crm.base_url) return;
              if (v && !/^https?:\/\/[^/\s]+/.test(v)) {
                toast("the CRM address must look like http://192.168.1.20 or https://crm.example.com", "error"); return;
              }
              if (/^http:\/\//.test(v) && !localNetwork(v))
                toast("plain http:// to an internet address: summaries won't be sent until you use https:// or the CRM's "
                      + "local-network address (or set voip.crm.allow_http for an internal name)", "error");
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
          {busy ? "Testing…" : "Test connection"}</button>
      </div>
      {info.crm.address_problem && <p className="warn">✗ {info.crm.address_problem}</p>}
      <p className="dim" style={{ fontSize: 12 }}>Signed with the token (HMAC-SHA256); nothing is sent until it is set.
        Test connection checks both the call-summary push and the CRM's v2 endpoints (vacancies, candidates, dial
        locks). The CRM side is described in docs/crm-laravel.md; to try everything before it's deployed,
        run <span className="mono">xplogent voip mock-crm</span> and use http://127.0.0.1:8089 as the address.
        Plain http:// is fine when the CRM is on your local network (e.g. http://192.168.1.20:8000); across the internet use https://.</p>
      {info.crm.callback && (
        <div className={info.crm.callback.warning ? "warn" : "dim"} style={{ fontSize: 12, margin: "6px 0" }}>
          <b>The other way (the CRM's "Call with AI"):</b> in the CRM's .env set{" "}
          <span className="mono">XPLOGENT_BASE_URL={info.crm.callback.url}</span> and the same token as
          {" "}<span className="mono">XPLOGENT_API_TOKEN</span>, then check it with
          {" "}<span className="mono">php artisan xplogent:check</span>.
          {info.crm.callback.warning && <> ✗ {info.crm.callback.warning}</>}
          {info.crm.callback.note && <span className="dim"> {info.crm.callback.note}</span>}
        </div>)}
      {msg && <p className={msg.ok ? "ok" : "warn"}>{msg.ok ? "✓" : "✗"} {msg.text}</p>}
      {v2 && (v2.ok
        ? <p className="ok">✓ CRM API v2: vacancies, candidates and dial locks reachable ({Math.round(v2.ms)} ms,
            {" "}{v2.open_vacancies_seen ? "open vacancies found" : "no open vacancies right now"})</p>
        : <p className="warn">✗ CRM API v2: {v2.reason} <span className="dim">{v2.fix}</span></p>)}
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
