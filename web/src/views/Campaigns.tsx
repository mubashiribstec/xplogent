import { useEffect, useState } from "react";
import { Megaphone } from "lucide-react";
import {
  Campaign, CampaignItem, CrmSale, coldSignoff, createCampaign, crmQueue, crmSales, enableCampaign, getCampaign,
  getCampaigns, getFullConfig, patchConfig, putSecrets, refillCampaign, stopAllCampaigns, updateCampaign,
} from "../api";
import { CardBoundary } from "../components/CardBoundary";
import { useToast } from "../components/Toast";

const STATE: Record<string, string> = {
  queued: "waiting", dialling: "ringing", in_call: "on a call", retry_wait: "try again later", done: "done",
  skipped: "skipped", failed: "failed",
};
const OUTCOME: Record<string, string> = {
  interested: "interested", callback: "callback", recruiter_callback: "recruiter callback",
  not_interested: "not interested", no_job: "not looking", do_not_call: "do not call", voicemail: "voicemail",
  no_answer: "no answer", busy: "busy", number_unobtainable: "bad number", wrong_person: "wrong person",
  abandoned: "hung up", screen_incomplete: "screen unfinished", interrupted: "cut off",
};
const COUNTERS: [string, string][] = [
  ["queued", "waiting"], ["dialling", "ringing"], ["in_call", "on a call"], ["outcome:interested", "interested"],
  ["outcome:callback", "callback"], ["outcome:not_interested", "not interested"], ["outcome:voicemail", "voicemail"],
  ["outcome:no_answer", "no answer"], ["skipped", "skipped"], ["done", "done"],
];
const when = (t: number) => (t ? new Date(t * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "");

/** Automatic calling: one campaign per vacancy, candidates from the CRM, with hard limits. */
export function Campaigns() {
  const toast = useToast();
  const [data, setData] = useState<{ campaigns: Campaign[]; stop_all: boolean; agent_on: boolean } | null>(null);
  const load = () => getCampaigns().then(setData).catch(() => {});
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);
  if (!data) return <div className="pane"><p className="dim">loading…</p></div>;
  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><Megaphone size={22} /> Campaigns</h1>
          <button className={`btn ${data.stop_all ? "" : "danger"}`} onClick={async () => {
            if (!data.stop_all && !confirm("Stop every campaign now? Calls in progress finish; nothing new is dialled.")) return;
            await stopAllCampaigns(!data.stop_all); toast(data.stop_all ? "campaigns may dial again" : "all campaigns stopped",
              data.stop_all ? "success" : "error"); load();
          }}>{data.stop_all ? "Resume campaigns" : "Stop all"}</button>
        </div>
        {data.stop_all && <div className="warn-banner">Stop all is on: no campaign dials until you resume.</div>}
        {!data.agent_on && <div className="warn-banner">The phone agent is off (Calls → Go live): campaigns don't dial.</div>}
        <p className="explain">A campaign calls one vacancy's candidates from the CRM, nearest first, inside the calling
          hours. Before each call it re-checks the CRM (do not call, not interested, no job), the do-not-call list, TPS
          when needed and the CRM's dial lock. No answer, busy and voicemail are tried again later; every result goes back
          to the CRM.</p>
        <CardBoundary name="New campaign"><NewCampaign onMade={load} toast={toast} /></CardBoundary>
        {data.campaigns.map((c) => (
          <CardBoundary key={c.id} name={c.name}><CampaignCard c={c} onChange={load} toast={toast} /></CardBoundary>))}
        <CardBoundary name="TPS screening"><TpsCard toast={toast} /></CardBoundary>
      </div>
    </div>
  );
}

function NewCampaign({ onMade, toast }: { onMade: () => void; toast: (m: string, k?: any) => void }) {
  const [search, setSearch] = useState("");
  const [sales, setSales] = useState<CrmSale[] | null>(null);
  const [problem, setProblem] = useState("");
  const [pick, setPick] = useState<CrmSale | null>(null);
  const [radius, setRadius] = useState(15);
  const [preview, setPreview] = useState<{ n: number; skipped: Record<string, number>; error?: string } | null>(null);
  const find = async () => {
    const r = await crmSales(search);
    setProblem(r.ok ? "" : `${r.reason ?? r.error} ${r.fix ?? ""}`); setSales(r.sales ?? []);
  };
  const choose = async (s: CrmSale) => {
    setPick(s); setPreview(null);
    const q = await crmQueue(s.sale_id, radius);
    setPreview({ n: q.candidates?.length ?? 0, skipped: q.skipped ?? {}, error: q.ok ? "" : (q.message || q.error) });
  };
  return (
    <div className="card">
      <h3>New campaign</h3>
      <div className="row wrap" style={{ gap: 8, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Find an open vacancy in the CRM</span>
          <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="care leeds"
            onKeyDown={(e) => e.key === "Enter" && find()} style={{ width: 240 }} /></label>
        <button className="btn sm" onClick={find}>Search</button>
        <label className="bud"><span className="lbl-sm">Radius (km)</span>
          <input type="number" min={1} max={80} value={radius} onChange={(e) => setRadius(Number(e.target.value) || 15)}
            style={{ width: 70 }} /></label>
      </div>
      {problem && <p className="warn">{problem}</p>}
      {sales && sales.length === 0 && !problem && <p className="dim">No open vacancies match.</p>}
      {sales && sales.length > 0 && (
        <div className="table-scroll"><table className="data-table" style={{ marginTop: 8 }}>
          <thead><tr><th></th><th>Vacancy</th><th>Area</th><th>Pay</th><th>CVs sent</th></tr></thead>
          <tbody>{sales.map((s) => (
            <tr key={s.sale_id} onClick={() => choose(s)} style={{ cursor: "pointer" }}>
              <td><input type="radio" readOnly checked={pick?.sale_id === s.sale_id} /></td>
              <td>{s.job_title} <span className="dim">· {s.unit_name || s.office_name}</span></td>
              <td>{s.postcode_district}</td><td>{s.salary}</td><td>{s.cvs_sent} / {s.cv_limit}</td>
            </tr>))}</tbody>
        </table></div>)}
      {pick && preview && (
        <div style={{ marginTop: 8 }}>
          {preview.error ? <p className="warn">{preview.error}</p> : <p>
            <b>{preview.n}</b> candidates to call within {radius} km
            {Object.entries(preview.skipped).filter(([, n]) => n > 0).length > 0 && <span className="dim"> · not called: {
              Object.entries(preview.skipped).filter(([, n]) => n > 0).map(([k, n]) => `${n} ${k.replace(/_/g, " ")}`).join(", ")}</span>}
          </p>}
          <button className="btn" disabled={!!preview.error} onClick={async () => {
            const r = await createCampaign({ sale_id: pick.sale_id, sale: pick, radius_km: radius,
              name: `${pick.job_title} · ${pick.postcode_district}` });
            if (!r.ok) { toast(r.error ?? "could not create", "error"); return; }
            toast("campaign created (off): check its settings, then switch it on", "success");
            setPick(null); setSales(null); onMade();
          }}>Create campaign (starts off)</button>
        </div>)}
    </div>
  );
}

function CampaignCard({ c, onChange, toast }: { c: Campaign; onChange: () => void; toast: (m: string, k?: any) => void }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<CampaignItem[] | null>(null);
  const [signer, setSigner] = useState("");
  const loadItems = () => getCampaign(c.id).then((r) => setItems(r.items)).catch(() => {});
  useEffect(() => { if (open) loadItems(); }, [open, c.counts]);
  const save = async (body: Record<string, any>) => {
    const r = await updateCampaign(c.id, body);
    if (!r.ok) toast(r.error ?? "could not save", "error"); else onChange();
  };
  const toggle = async () => {
    const r = await enableCampaign(c.id, !c.enabled);
    if (!r.ok) toast(r.error ?? "could not switch on", "error");
    else toast(c.enabled ? "campaign off" : "campaign on: it dials inside the calling hours", "success");
    onChange();
  };
  const n = c.counts ?? {};
  return (
    <div className="card">
      <div className="row wrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>{c.name}</h3>
        <button className={`btn ${c.enabled ? "" : "primary"}`} onClick={toggle}
          title={!c.enabled && c.enable_problem ? c.enable_problem : ""}>{c.enabled ? "Switch off" : "Switch on"}</button>
      </div>
      <p style={{ fontSize: 13 }}>
        {c.enabled ? <span className="ok">● On</span> : <span className="dim">● Off</span>}
        {c.paused_reason && <span className="warn"> · paused: {c.paused_reason}</span>}
        {!c.enabled && c.enable_problem && <span className="dim"> · to switch on: {c.enable_problem}</span>}
        <span className="dim"> · spent today ${c.spend_today.toFixed(2)}{c.daily_cost_cap ? ` of $${c.daily_cost_cap.toFixed(2)}` : ""}
          {c.sale?.salary ? ` · ${c.sale.salary}` : ""}</span>
      </p>
      <div className="row wrap" style={{ gap: 6 }}>
        {COUNTERS.filter(([k]) => n[k]).map(([k, label]) => <span key={k} className="badge">{n[k]} {label}</span>)}
        {Object.keys(n).length === 0 && <span className="dim">No candidates yet: switch it on, or Refill now.</span>}
      </div>
      <div className="row wrap" style={{ gap: 10, marginTop: 10 }}>
        <label className="bud"><span className="lbl-sm">Line</span>
          <select value={c.line} onChange={(e) => save({ line: e.target.value })}>
            <option value="telnyx">Telnyx</option><option value="sip">SIP trunk</option><option value="extension">PBX extension</option>
          </select></label>
        <label className="bud"><span className="lbl-sm">Calls at once</span>
          <input type="number" min={1} max={20} defaultValue={c.max_concurrent} style={{ width: 60 }}
            onBlur={(e) => Number(e.target.value) !== c.max_concurrent && save({ max_concurrent: Number(e.target.value) })} /></label>
        <label className="bud"><span className="lbl-sm">New calls / minute</span>
          <input type="number" min={1} max={60} defaultValue={c.calls_per_min} style={{ width: 60 }}
            onBlur={(e) => Number(e.target.value) !== c.calls_per_min && save({ calls_per_min: Number(e.target.value) })} /></label>
        <label className="bud"><span className="lbl-sm">Radius (km)</span>
          <input type="number" min={1} max={80} defaultValue={c.radius_km} style={{ width: 60 }}
            onBlur={(e) => Number(e.target.value) !== c.radius_km && save({ radius_km: Number(e.target.value) })} /></label>
        <label className="bud"><span className="lbl-sm">Tries per person</span>
          <input type="number" min={1} max={5} defaultValue={c.retry_policy?.max_attempts ?? 3} style={{ width: 60 }}
            onBlur={(e) => save({ retry_policy: { ...c.retry_policy, max_attempts: Number(e.target.value) } })} /></label>
        <label className="bud"><span className="lbl-sm">Daily cap ($)</span>
          <input type="number" min={0} step={1} defaultValue={c.daily_cost_cap} style={{ width: 70 }}
            onBlur={(e) => Number(e.target.value) !== c.daily_cost_cap && save({ daily_cost_cap: Number(e.target.value) })} /></label>
        <label className="bud"><span className="lbl-sm">Cap per call ($)</span>
          <input type="number" min={0} step={0.05} defaultValue={c.per_call_cost_cap} style={{ width: 70 }}
            onBlur={(e) => Number(e.target.value) !== c.per_call_cost_cap && save({ per_call_cost_cap: Number(e.target.value) })} /></label>
        <label className="bud"><span className="lbl-sm">Why we may call them</span>
          <select value={c.consent_basis} onChange={(e) => save({ consent_basis: e.target.value })} style={{ minWidth: 270 }}>
            <option value="applied">they applied for this kind of role</option>
            <option value="registered">they registered with us</option>
            <option value="cold">cold (no prior contact)</option></select></label>
      </div>
      {c.consent_basis === "cold" && !c.cold_signoff?.by && (
        <div className="warn-banner" style={{ marginTop: 8 }}>
          Cold calling needs every number screened against TPS/CTPS and a lawful basis for the call. An admin must
          confirm that before this campaign can run.
          <div className="row" style={{ gap: 6, marginTop: 6 }}>
            <input placeholder="Your name" value={signer} onChange={(e) => setSigner(e.target.value)} />
            <button className="btn sm" disabled={!signer.trim()} onClick={async () => {
              if (!confirm("You confirm cold calling for this campaign is lawful and TPS screening is in place?")) return;
              const r = await coldSignoff(c.id, signer); toast(r.ok ? "signed off (audited)" : r.error ?? "failed", r.ok ? "success" : "error");
              onChange();
            }}>I confirm</button>
          </div>
        </div>)}
      {c.consent_basis === "cold" && c.cold_signoff?.by && <p className="dim" style={{ fontSize: 12 }}>
        Cold calling signed off by {c.cold_signoff.by}{c.cold_signoff.at ? ` on ${new Date(c.cold_signoff.at * 1000).toLocaleDateString()}` : ""}.</p>}
      <div className="row" style={{ gap: 8, marginTop: 8 }}>
        <button className="btn ghost sm" onClick={() => setOpen(!open)}>{open ? "Hide candidates" : "Show candidates"}</button>
        <button className="btn ghost sm" onClick={async () => {
          const r = await refillCampaign(c.id);
          toast(r.ok ? `${r.added} new candidate(s) from the CRM` : r.error ?? "CRM not reachable", r.ok ? "success" : "error");
          onChange();
        }}>Refill now</button>
      </div>
      {open && items && (
        <div className="table-scroll"><table className="data-table" style={{ marginTop: 8 }}>
          <thead><tr><th>Candidate</th><th>Number</th><th>Miles</th><th>Status</th><th>Tries</th><th>Result</th><th>Next try</th><th>Call</th></tr></thead>
          <tbody>{items.map((it) => (
            <tr key={it.id}>
              <td>{it.name || `#${it.applicant_id}`}</td><td className="mono">{it.phone}</td>
              <td>{it.distance_miles ?? ""}</td>
              <td>{STATE[it.state] ?? it.state}{it.skip_reason && <span className="dim" style={{ fontSize: 12 }}> · {it.skip_reason}</span>}</td>
              <td>{it.attempts}</td><td>{OUTCOME[it.last_outcome] ?? it.last_outcome}</td>
              <td>{it.state === "retry_wait" ? when(it.next_at) : ""}</td>
              <td>{it.last_call_id ? <span className="mono dim">#{it.last_call_id}</span> : ""}</td>
            </tr>))}</tbody>
        </table></div>)}
    </div>
  );
}

/** Where TPS / CTPS screening is configured (needed for cold campaigns, optional otherwise). */
export function TpsCard({ toast }: { toast: (m: string, k?: any) => void }) {
  const [cfg, setCfg] = useState<{ tps: Record<string, any>; keySet: boolean } | null>(null);
  const load = () => getFullConfig().then((c) => setCfg({ tps: c.voip?.tps ?? {}, keySet: !!c.secrets?.XPLOGENT_TPS_API_KEY }))
    .catch(() => {});
  useEffect(() => { load(); }, []);
  if (!cfg) return null;
  const save = async (tps: Record<string, any>) => { await patchConfig({ voip: { tps } }); toast("saved", "success"); load(); };
  return (
    <div className="card">
      <h3>TPS screening</h3>
      <p className="explain">Numbers registered with the Telephone Preference Service (or CTPS for businesses) must not get
        unsolicited calls. Cold campaigns are always screened; switch on "screen every campaign" to screen the others too.
        Results are kept for {cfg.tps.cache_days ?? 28} days. If the service can't answer, the number is not dialled.</p>
      <div className="row wrap" style={{ gap: 10, alignItems: "flex-end" }}>
        <label className="bud"><span className="lbl-sm">Screening service address</span>
          <input defaultValue={cfg.tps.url ?? ""} placeholder="https://your-tps-service/check" style={{ width: 280 }}
            onBlur={(e) => {
              const v = e.target.value.trim();
              if (v === (cfg.tps.url ?? "")) return;
              if (v && !/^https?:\/\//.test(v)) { toast("the address must start with https://", "error"); return; }
              save({ url: v });
            }} /></label>
        <label className="bud"><span className="lbl-sm">API key {cfg.keySet ? <span className="ok">✓ saved</span> : <span className="dim">not set</span>}</span>
          <input type="password" autoComplete="off" placeholder={cfg.keySet ? "•••••• (paste to replace)" : "the service's key"}
            onBlur={async (e) => {
              const v = e.target.value.trim();
              if (!v) return;
              await putSecrets({ XPLOGENT_TPS_API_KEY: v }); e.target.value = ""; toast("TPS key saved", "success"); load();
            }} /></label>
        <label className="bud"><span className="lbl-sm">Screen every campaign</span>
          <select value={cfg.tps.enabled ? "on" : "off"} onChange={(e) => save({ enabled: e.target.value === "on" })}>
            <option value="off">cold campaigns only</option><option value="on">every campaign</option></select></label>
      </div>
      <p className="dim" style={{ fontSize: 12 }}>The service gets <span className="mono">POST {"{"}"numbers": [...]{"}"}</span> with
        the key as a bearer token and answers <span className="mono">{"{"}"results": {"{"}"+447…": {"{"}"tps": false, "ctps": false{"}"}{"}"}{"}"}</span>.
        See docs/voip.md for wiring a provider with another format.</p>
    </div>
  );
}
