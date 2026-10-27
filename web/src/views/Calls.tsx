import { Fragment, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Copy, PhoneCall, PhoneIncoming, PhoneOutgoing, RefreshCw } from "lucide-react";
import {
  CallRow, CallTurn, ReadinessItem, VoipStatus, getCall, getCalls, getContacts, getFullConfig, getVoipReadiness,
  getVoipStatus, listOllamaServers, patchConfig, testOllama, putSecrets, saveSip, clearSipBusy, getVoices, resendCrm, setDemo, testSpeech, testTelnyx, deleteRecording, getRecordingUrl, trainOnRecording,
} from "../api";
import { useToast } from "../components/Toast";
import { CallNow, LiveCalls, TestCall } from "./CallControls";
import { SipCard } from "./SipCard";
import { SipExtensionCard } from "./SipExtensionCard";
import { CallAgentCard } from "./CallAgentCard";
import { CallDebug } from "./CallDebug";
import { GoLive } from "./GoLive";
import { BrandingCard } from "./BrandingCard";
import { FREE_MODELS } from "../lib/models";
import { CardBoundary } from "../components/CardBoundary";
import { ModelTestButton } from "../components/ModelTest";

const CRM_BADGE: Record<string, string> = { sent: "ok", failed: "bad", skipped: "bad", retrying: "warn", sending: "warn" };
const fmtTime = (t?: number | null) => (t ? new Date(t * 1000).toLocaleString() : "—");
const fmtDur = (c: CallRow) => {
  const end = c.ended_at ?? Date.now() / 1000;
  const s = Math.max(0, Math.round(end - c.started_at));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const randomSecret = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(18))).map((b) => b.toString(16).padStart(2, "0")).join("");

export function Calls() {
  const toast = useToast();
  const [status, setStatus] = useState<VoipStatus | null>(null);
  const [calls, setCalls] = useState<CallRow[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [turns, setTurns] = useState<CallTurn[]>([]);
  const [debugOpen, setDebugOpen] = useState<number | null>(null);
  const [cfg, setCfg] = useState<Record<string, any>>({});
  const [loaded, setLoaded] = useState(false);
  const [telnyxKeySet, setTelnyxKeySet] = useState(false);
  const [ready, setReady] = useState<{ ready: boolean; items: ReadinessItem[] } | null>(null);
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});
  const [presets, setPresets] = useState<{ label: string; model: string }[]>([]);
  const [mainModel, setMainModel] = useState("");
  const loadCfg = () => getFullConfig().then((c) => {
    setCfg(c.voip ?? {}); setPresets(c.models ?? []); setMainModel(c.model ?? ""); setTelnyxKeySet(!!c.secrets?.TELNYX_API_KEY); setSecrets(c.secrets ?? {}); setLoaded(true);
  }).catch(() => setLoaded(true));
  const loadReady = () => getVoipReadiness().then(setReady).catch(() => {});
  const [contacts, setContacts] = useState<{ id: number; name: string }[]>([]);
  useEffect(() => { getContacts("").then((r) => setContacts(r.contacts.slice(0, 200))).catch(() => {}); }, []);

  const reload = () => {
    getVoipStatus().then(setStatus).catch(() => {});
    getCalls().then((r) => setCalls(r.calls)).catch(() => {});
    if (open !== null) getCall(open).then((r) => setTurns(r.call?.turns ?? [])).catch(() => {});
  };
  useEffect(() => { loadCfg(); loadReady(); }, []);
  useEffect(() => {
    reload();
    const id = setInterval(reload, 3000);   // live transcript while a call is active
    return () => clearInterval(id);
    // eslint-disable-next-line
  }, [open]);

  const save = async (updates: Record<string, any>) => {
    setCfg((c) => ({ ...c, ...updates }));
    await patchConfig({ voip: updates });
    toast("saved", "success");
    reload(); loadReady();
  };

  const webhook = cfg.public_url && cfg.webhook_secret
    ? `${String(cfg.public_url).replace(/\/$/, "")}/telephony/webhook/${cfg.webhook_secret}` : "";

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head">
          <h1><PhoneCall size={22} /> Calls</h1>
          <div className="row" style={{ gap: 8 }}>
            <span className={`badge ${status?.enabled ? "ok" : "bad"}`}>{status?.enabled ? "phone agent on" : "phone agent off"}</span>
            {(status?.active.length ?? 0) > 0 && <span className="badge warn">{status!.active.length} live</span>}
            <button className="icon-btn" onClick={reload} aria-label="refresh"><RefreshCw size={16} /></button>
          </div>
        </div>
        <p className="dim">Answers inbound calls using your scripts and knowledge base. Calls are transcribed live (and recorded, unless you switch recording off);
          the agent only answers from your knowledge and hands off to a person when it can't.</p>

        {ready && loaded && <CardBoundary name="Go live"><GoLive ready={ready.ready} items={ready.items} calls={calls} cfg={cfg} /></CardBoundary>}
        {ready && <details className="card" open={ready.items.some((i) => !i.ok && i.required)}>
          <summary><b>All checks</b> <span className="dim">({ready.items.filter((i) => i.ok).length} of {ready.items.length} fine)</span></summary>
          <Readiness ready={ready.ready} items={ready.items} onClearBusy={async (line) => {
            await clearSipBusy(line); loadReady();
          }} />
        </details>}

        <CardBoundary name="Live calls"><LiveCalls active={status?.active ?? []} toast={toast} onChange={reload} /></CardBoundary>

        {loaded && <CardBoundary name="Call now"><CallNow toast={toast} onCalled={reload}
          via={[...(cfg.telephony === "sip" ? ["sip"] : cfg.telephony === "both" ? ["telnyx", "sip"] : ["telnyx"]),
                ...(cfg.sip_extension?.enabled && cfg.sip_extension?.server ? ["extension"] : [])]} /></CardBoundary>}
        <div data-tour="test-call"><CardBoundary name="Test call in the browser"><TestCall contacts={contacts} /></CardBoundary></div>

        <div className="card" data-tour="calls-list">
          <h3 style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
            <span>Recent calls ({calls.filter((c) => !c.demo).length}{calls.some((c) => c.demo) ? ` + ${calls.filter((c) => c.demo).length} demo` : ""})</span>
            <label className="row check" style={{ gap: 6, fontSize: 13, fontWeight: 400, margin: 0 }}
              title="Sample calls for showing the product: badged, never counted, never sent to your CRM">
              <input type="checkbox" checked={calls.some((c) => c.demo)} onChange={async (e) => {
                await setDemo(e.target.checked); toast(e.target.checked ? "demo data added" : "demo data cleared", "success"); reload();
              }} /> Demo data</label>
          </h3>
          {calls.length === 0 ? (
            <div className="empty-state">
              <p>No calls yet. Try your agent first: it takes a minute.</p>
              <button className="btn primary" onClick={() => document.querySelector('[data-tour="test-call"]')?.scrollIntoView({ behavior: "smooth", block: "center" })}>
                Make a test call</button>{" "}
              <button className="btn" onClick={async () => { await setDemo(true); reload(); }}>Show demo data</button>
            </div>
          ) : (
          <table className="data-table">
            <thead><tr><th></th><th>When</th><th>Caller</th><th>Duration</th><th>Outcome</th><th>Est. cost</th></tr></thead>
            <tbody>
              {calls.map((c) => (
                <Fragment key={c.id}>
                  <tr style={{ cursor: "pointer" }}
                    onClick={() => { const next = open === c.id ? null : c.id; setOpen(next); setTurns([]); }}>
                    <td>{open === c.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      {c.direction === "inbound" ? <PhoneIncoming size={14} /> : <PhoneOutgoing size={14} />}</td>
                    <td className="dim" data-label="When">{fmtTime(c.started_at)}</td>
                    <td data-label="Caller">{c.contact_name || (c.direction === "outbound" ? c.to_number : c.from_number) || "unknown"}
                      {!!c.demo && <span className="badge demo" style={{ marginLeft: 6 }}>Demo data</span>}
                      {!!c.test && <span className="badge" style={{ marginLeft: 6 }}>test</span>}
                      {!!c.via_queue && <span className="badge warn" style={{ marginLeft: 6 }}
                        title={`came through the office queue${c.queue_wait_s ? ` after ${c.queue_wait_s}s` : ""}${c.original_called_number ? `; dialled ${c.original_called_number}` : ""}`}>via queue</span>}</td>
                    <td data-label="Duration">{fmtDur(c)}{c.status === "active" && <span className="badge warn" style={{ marginLeft: 6 }}>live</span>}</td>
                    <td data-label="Outcome">{c.outcome ? <span className="badge">{c.outcome}</span> : <span className="dim">—</span>}
                      {c.crm_status && <span className={`badge ${CRM_BADGE[c.crm_status] ?? ""}`} style={{ marginLeft: 6 }}
                        title={c.crm_detail || ""}>CRM {c.crm_status}</span>}</td>
                    <td className="dim" data-label="Est. cost">${(c.cost ?? 0).toFixed(3)}</td>
                  </tr>
                  {open === c.id && (
                    <tr><td colSpan={6}>
                      {c.summary && <p className="dim" style={{ marginBottom: 6 }}><b>Summary:</b> {c.summary}</p>}
                      {c.recording && <Recording callId={c.id} toast={toast} onDeleted={reload} />}
                      {(c.crm_status === "failed" || c.crm_status === "skipped") && (
                        <p className="warn" style={{ margin: "4px 0" }}>CRM: {c.crm_status}{c.crm_detail ? ` (${c.crm_detail})` : ""}{" "}
                          <button className="btn" onClick={async () => {
                            const r = await resendCrm(c.id);
                            toast(r.ok ? "sent to the CRM" : r.error ?? r.detail ?? "still failing", r.ok ? "success" : "error");
                            reload();
                          }}>Resend</button></p>)}
                      <button className="linkx" onClick={() => setDebugOpen(debugOpen === c.id ? null : c.id)}>
                        {debugOpen === c.id ? "Hide debug" : "Debug this call"}</button>
                      {debugOpen === c.id && <CallDebug callId={c.id} toast={toast} />}
                      {turns.length === 0 && <p className="dim">no transcript yet</p>}
                      {turns.map((t, i) => (
                        <div key={i} style={{ margin: "3px 0" }}>
                          <b style={{ color: t.speaker === "agent" ? "var(--accent)" : "var(--amber)" }}>
                            {t.speaker === "agent" ? "Agent" : "Caller"}:</b> {t.text}
                        </div>
                      ))}
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>)}
        </div>

        <h2 style={{ margin: "28px 0 10px", fontSize: 18 }}>Settings</h2>
        {loaded && (<>
        <div className="card" data-tour="setup">
          <h3>Setup</h3>
          <div className="row wrap" style={{ gap: 10 }}>
            <label className="bud"><span className="lbl-sm">Phone agent</span>
              <select value={cfg.enabled ? "on" : "off"} onChange={(e) => save({ enabled: e.target.value === "on" })}>
                <option value="off">off</option><option value="on">on</option></select></label>
            <label className="bud"><span className="lbl-sm">Phone line</span>
              <select value={cfg.telephony ?? "telnyx"} onChange={async (e) => {
                const telephony = e.target.value;
                setCfg((c) => ({ ...c, telephony }));
                const r = await saveSip({ telephony });
                toast(r.ok ? "phone line saved" : r.error ?? "could not save", r.ok ? "success" : "error");
                loadReady();
              }}>
                <option value="telnyx">Telnyx Voice API</option>
                <option value="sip">SIP trunk (FreePBX, Telnyx SIP…)</option>
                <option value="both">Both</option>
              </select></label>
            <label className="bud"><span className="lbl-sm">Company name</span>
              <input defaultValue={cfg.company ?? ""} onBlur={(e) => save({ company: e.target.value })} /></label>
            {cfg.telephony !== "sip" && (
            <label className="bud"><span className="lbl-sm">Public HTTPS address of this server</span>
              <input defaultValue={cfg.public_url ?? ""} placeholder="https://agent.example.com"
                onBlur={(e) => save({ public_url: e.target.value.trim().replace(/\/$/, "") })} style={{ width: 240 }} /></label>)}
            <label className="bud"><span className="lbl-sm">Human hand-off number</span>
              <input defaultValue={cfg.transfer_number ?? ""} placeholder="+15551234567"
                onBlur={(e) => save({ transfer_number: e.target.value.trim() })} /></label>
          </div>
          {cfg.telephony !== "sip" && <div className="row wrap" style={{ gap: 8, marginTop: 10, alignItems: "center" }}>
            <button className="btn" onClick={() => save({ webhook_secret: randomSecret() })}>
              {cfg.webhook_secret ? "Regenerate webhook secret" : "Generate webhook secret"}</button>
            {webhook && (
              <span className="mono dim" style={{ fontSize: 12, wordBreak: "break-all" }}>
                {webhook}
                <button className="linkx" title="copy" onClick={() => { navigator.clipboard?.writeText(webhook); toast("webhook URL copied", "success"); }}>
                  <Copy size={12} /></button>
              </span>
            )}
          </div>}
        </div>

        {cfg.telephony !== "sip" && <CardBoundary name="Telnyx"><TelnyxCard cfg={cfg} keySet={telnyxKeySet} save={save} toast={toast}
          onKeySaved={() => { setTelnyxKeySet(true); loadReady(); }} /></CardBoundary>}
        {(cfg.telephony === "sip" || cfg.telephony === "both") && <CardBoundary name="SIP trunk"><SipCard cfg={cfg} toast={toast} onChange={() => { loadCfg(); loadReady(); }} /></CardBoundary>}
        <div data-tour="pbx-extension"><CardBoundary name="PBX extension"><SipExtensionCard cfg={cfg} toast={toast} onChange={() => { loadCfg(); loadReady(); }} /></CardBoundary></div>
        <CardBoundary name="Speech"><SpeechCard cfg={cfg} secrets={secrets} save={save} toast={toast} onKey={loadCfg} /></CardBoundary>

        <CardBoundary name="Conversation"><div className="card">
          <h3>Conversation</h3>
          <label>Greeting <span className="dim">({"{company}"} is replaced by the company name)</span>
            <textarea defaultValue={cfg.greeting ?? ""} rows={2} style={{ width: "100%" }}
              onBlur={(e) => save({ greeting: e.target.value })} /></label>
          <div className="row wrap" style={{ gap: 10 }}>
            {loaded && <CallModelPicker value={cfg.model ?? ""} mainModel={mainModel} presets={presets}
              keySet={!!secrets.OPENROUTER_API_KEY} onPick={(m) => save({ model: m })} />}
            <label className="bud"><span className="lbl-sm">Est. cost per minute ($)</span>
              <input type="number" step="0.001" defaultValue={cfg.cost_per_minute ?? 0}
                onBlur={(e) => save({ cost_per_minute: Number(e.target.value) || 0 })} style={{ width: 90 }} /></label>
            <label className="bud"><span className="lbl-sm">Max call length (s)</span>
              <input type="number" defaultValue={cfg.max_call_seconds ?? 900}
                onBlur={(e) => save({ max_call_seconds: Number(e.target.value) || 900 })} style={{ width: 90 }} /></label>
            <label className="bud"><span className="lbl-sm">Call recording</span>
              <select value={cfg.recording?.enabled === false ? "off" : "on"}
                onChange={(e) => save({ recording: { enabled: e.target.value === "on" } })}>
                <option value="on">on: callers are told the call is recorded</option>
                <option value="off">off: transcripts only</option></select></label>
            <label className="bud"><span className="lbl-sm">Keep recordings (days, 0 = forever)</span>
              <input type="number" min={0} defaultValue={cfg.recording?.retention_days ?? 90}
                onBlur={(e) => save({ recording: { retention_days: Math.max(0, Number(e.target.value) || 0) } })} style={{ width: 90 }} /></label>
          </div>
          <p className="dim" style={{ fontSize: 12 }}>Add your scripts and FAQs under Knowledge and your customers under
            Contacts: the agent answers only from those.</p>
        </div></CardBoundary>
        <div data-tour="crm"><CardBoundary name="Call agent"><CallAgentCard toast={toast} onChange={loadReady} /></CardBoundary></div>
        <CardBoundary name="Branding"><BrandingCard toast={toast} /></CardBoundary>
        </>)}

      </div>
    </div>
  );
}


function Recording({ callId, toast, onDeleted }: { callId: number; toast: Toaster; onDeleted: () => void }) {
  const [url, setUrl] = useState("");
  useEffect(() => () => { if (url) URL.revokeObjectURL(url); }, [url]);
  const load = async () => {
    try { setUrl(await getRecordingUrl(callId)); } catch { toast("recording not found", "error"); }
  };
  return (
    <div className="row wrap" style={{ gap: 8, margin: "4px 0 8px", alignItems: "center" }}>
      {url ? <audio controls autoPlay src={url} style={{ height: 32 }} />
        : <button className="btn" onClick={load}>▶ Play recording</button>}
      {url && <a className="btn" href={url} download={`call-${callId}.wav`}>Download</a>}
      <button className="btn" title="Transcribe it and add it to Call training (caller left, agent right)"
        onClick={async () => { const r = await trainOnRecording(callId); toast(r.ok ? "added to Call training" : r.error ?? "failed", r.ok ? "success" : "error"); }}>
        Use for training</button>
      <button className="btn ghost" onClick={async () => {
        if (!confirm("Delete this recording? The transcript stays.")) return;
        await deleteRecording(callId); setUrl(""); onDeleted();
      }}>Delete</button>
      <span className="dim" style={{ fontSize: 12 }}>left: caller · right: agent</span>
    </div>
  );
}

function Readiness({ ready, items, onClearBusy }: {
  ready: boolean; items: ReadinessItem[]; onClearBusy: (line: string) => void;
}) {
  return (
    <div className="card">
      <h3>{ready ? "✓ Ready for calls" : "Before calls can work"}</h3>
      <ul className="list">
        {items.map((i) => (
          <li key={i.id}>
            <span style={{ color: i.ok ? "var(--green, #2e7d32)" : i.required ? "var(--red, #c62828)" : "var(--amber, #b26a00)" }}>
              {i.ok ? "✔" : i.required ? "✗" : "○"}</span>{" "}
            <b>{i.label}</b>{i.detail && <span className="dim"> · {i.detail}</span>}
            {i.id.endsWith("_capacity") && i.detail.includes("got busy") && (
              <button className="linkx" style={{ marginLeft: 6 }}
                onClick={() => onClearBusy(i.id === "sip_capacity" ? "trunk" : "extension")}>clear</button>)}
          </li>
        ))}
      </ul>
    </div>
  );
}

type Saver = (u: Record<string, any>) => Promise<void>;
type Toaster = (m: string, kind?: any) => void;

function TelnyxCard({ cfg, keySet, save, toast, onKeySaved }: {
  cfg: Record<string, any>; keySet: boolean; save: Saver; toast: Toaster; onKeySaved: () => void;
}) {
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);
  const test = async () => {
    setBusy(true);
    try { setResult(await testTelnyx()); } catch (e) { setResult({ ok: false, error: String(e) }); }
    finally { setBusy(false); }
  };
  return (
    <div className="card">
      <h3>Telnyx (Voice API)</h3>
      <div className="row wrap" style={{ gap: 10 }}>
        <label className="bud"><span className="lbl-sm">API key {keySet ? "✓ set" : "✗ not set"}</span>
          <input type="password" placeholder={keySet ? "•••••• (type to replace)" : "KEY0123…"}
            onBlur={async (e) => {
              const v = e.target.value.trim();
              if (!v) return;
              await putSecrets({ TELNYX_API_KEY: v }); e.target.value = ""; onKeySaved(); toast("Telnyx key saved", "success");
            }} /></label>
        <label className="bud"><span className="lbl-sm">Voice API application ID</span>
          <input defaultValue={cfg.connection_id ?? ""} placeholder="1234567890123456789"
            onBlur={(e) => save({ connection_id: e.target.value.trim() })} /></label>
        <label className="bud"><span className="lbl-sm">Your Telnyx number</span>
          <input defaultValue={cfg.from_number ?? ""} placeholder="+15551234567"
            onBlur={(e) => save({ from_number: e.target.value.trim() })} /></label>
        <button className="btn" onClick={test} disabled={busy || !keySet}>{busy ? "Testing…" : "Test Telnyx"}</button>
      </div>
      <label className="bud" style={{ marginTop: 8 }}><span className="lbl-sm">Webhook public key (Telnyx portal → Keys &amp; Credentials → Public Key).
        With it, forged webhooks are rejected even if the webhook URL leaks.</span>
        <input defaultValue={cfg.telnyx_public_key ?? ""} placeholder="base64 public key" style={{ width: 420 }}
          onBlur={(e) => save({ telnyx_public_key: e.target.value.trim() })} /></label>
      {result && (result.ok
        ? <p className="ok">✓ Key valid{result.application ? ` · application "${result.application}"` : ""}
            {result.webhook_matches ? " · webhook URL matches" : ""}</p>
        : <p className="warn">✗ {result.error}</p>)}
      {result?.warning && <p className="warn">⚠ {result.warning} (Telnyx has: <code>{result.webhook_in_telnyx || "nothing"}</code>)</p>}
      <details style={{ marginTop: 8 }}>
        <summary className="dim">How to set this up in Telnyx (and with FreePBX)</summary>
        <ol className="dim" style={{ fontSize: 13 }}>
          <li>Telnyx portal → <b>Voice → Programmable Voice → Applications</b>: create an application (Call Control / Voice API).</li>
          <li>Set its <b>webhook URL</b> to the address shown under Setup (needs a public <code>https://</code> address).</li>
          <li>Copy the application's <b>ID</b> into "Voice API application ID" above.</li>
          <li><b>Numbers</b>: assign your phone number to that application. Calls to it now reach the AI.</li>
          <li><b>Auth → API Keys</b>: create a key and paste it above, then press "Test Telnyx".</li>
          <li>FreePBX (optional): send calls to the AI with an inbound route / follow-me to the number above
            through your Telnyx SIP trunk, and put a FreePBX extension's DID in "Human hand-off number" so
            "talk to a person" rings your team. To connect FreePBX directly instead, choose "SIP trunk" as the phone line.</li>
        </ol>
      </details>
    </div>
  );
}

const LOCAL_SPEECH = {
  stt: { provider: "whisper-local", model: "auto", base_url: "" },
  tts: { provider: "openai", base_url: "http://kokoro:8880/v1", model: "kokoro", voice: "af_heart", sample_rate: 24000 },
};
const OPENAI_SPEECH = {
  stt: { provider: "openai-compatible", model: "whisper-1", base_url: "https://api.openai.com/v1" },
  tts: { provider: "openai", base_url: "https://api.openai.com/v1", model: "tts-1", voice: "alloy", sample_rate: 24000 },
};
const TTS_PRESETS: Record<string, Record<string, any>> = {
  local: LOCAL_SPEECH.tts, openai: OPENAI_SPEECH.tts,
  elevenlabs: { provider: "elevenlabs", model: "eleven_flash_v2_5", voice: "", sample_rate: 24000 },
  cartesia: { provider: "cartesia", model: "sonic-2", voice: "", sample_rate: 24000, language: "en" },
};
const STT_PRESETS: Record<string, Record<string, any>> = {
  local: LOCAL_SPEECH.stt, openai: OPENAI_SPEECH.stt,
  deepgram: { provider: "deepgram", model: "nova-3", streaming: true, base_url: "" },
};
const KEY_FOR: Record<string, string> = {
  elevenlabs: "ELEVENLABS_API_KEY", cartesia: "CARTESIA_API_KEY", deepgram: "DEEPGRAM_API_KEY",
};

function SpeechCard({ cfg, secrets, save, toast, onKey }: {
  cfg: Record<string, any>; secrets: Record<string, boolean>; save: Saver; toast: Toaster; onKey: () => void;
}) {
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [busy, setBusy] = useState(false);
  const [voices, setVoices] = useState<{ id: string; name: string }[] | null>(null);
  const [voiceErr, setVoiceErr] = useState("");
  const tts = cfg.tts ?? {};
  const stt = cfg.stt ?? {};
  const ttsKind = tts.provider === "elevenlabs" || tts.provider === "cartesia" ? tts.provider
    : String(tts.base_url ?? "").includes("kokoro") ? "local"
    : String(tts.base_url ?? "api.openai.com").includes("api.openai.com") ? "openai" : "custom";
  const sttKind = stt.provider === "deepgram" ? "deepgram"
    : (stt.provider ?? "whisper-local") === "whisper-local" ? "local" : "openai";
  const loadVoices = () => {
    setVoices(null); setVoiceErr("");
    getVoices().then((r) => {
      // Always plain strings: an unexpected shape from a voice server must not crash the page.
      setVoices((r.voices ?? []).map((v: any) => typeof v === "string" ? { id: v, name: v }
        : { id: String(v?.id?.id ?? v?.id ?? ""), name: String(v?.name?.name ?? v?.name ?? v?.id ?? "") }).filter((v) => v.id));
      if (!r.ok) setVoiceErr(r.error ?? "could not list voices"); })
      .catch((e) => setVoiceErr(String(e)));
  };
  useEffect(() => { loadVoices(); /* eslint-disable-next-line */ }, [tts.provider, tts.base_url]);
  const test = async () => {
    setBusy(true); setResult(null);
    try { setResult(await testSpeech(true)); } catch (e) { setResult({ ok: false, error: String(e) }); }
    finally { setBusy(false); }
  };
  const keyInput = (name: string) => (
    <label className="bud"><span className="lbl-sm">{name} {secrets[name] ? "✓ set" : "✗ not set"}</span>
      <input type="password" placeholder={secrets[name] ? "•••••• (type to replace)" : "paste the API key"}
        onBlur={async (e) => {
          const v = e.target.value.trim();
          if (!v) return;
          await putSecrets({ [name]: v }); e.target.value = ""; onKey(); toast("key saved", "success"); loadVoices();
        }} /></label>
  );
  return (
    <div className="card">
      <h3>Speech</h3>
      <p className="dim" style={{ fontSize: 12 }}>Small server without a GPU? Local voice and built-in Whisper
        (auto) run fine on a few CPU cores; put the <b>model for calls</b> on a hosted model (e.g. a free OpenRouter
        one) or on your stronger Ollama server, and keep a backup model.</p>
      <div className="row wrap" style={{ gap: 10, alignItems: "end" }}>
        <label className="bud"><span className="lbl-sm">Voice (text-to-speech)</span>
          <select value={ttsKind} onChange={async (e) => {
            await save({ tts: { ...TTS_PRESETS[e.target.value] } }); toast("voice provider saved", "success");
          }}>
            <option value="local">Local on this server (free)</option>
            <option value="openai">OpenAI (needs an OpenAI key)</option>
            <option value="elevenlabs">ElevenLabs</option>
            <option value="cartesia">Cartesia</option>
            <option value="custom" disabled>Custom (edit the URL below)</option>
          </select></label>
        {KEY_FOR[ttsKind] && keyInput(KEY_FOR[ttsKind])}
        <label className="bud"><span className="lbl-sm">Voice {voiceErr && <span className="warn">({voiceErr.slice(0, 60)})</span>}</span>
          {voices && voices.length > 0
            ? <select value={tts.voice ?? ""} onChange={(e) => save({ tts: { ...tts, voice: e.target.value } })} style={{ maxWidth: 220 }}>
                <option value="" disabled>choose a voice…</option>
                {!voices.some((v) => v.id === tts.voice) && tts.voice && <option value={tts.voice}>{tts.voice}</option>}
                {voices.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
              </select>
            : <input defaultValue={tts.voice ?? ""} key={tts.voice} style={{ width: 140 }} placeholder="voice id"
                onBlur={(e) => save({ tts: { ...tts, voice: e.target.value.trim() } })} />}</label>
        {(ttsKind === "local" || ttsKind === "openai" || ttsKind === "custom") && (
          <label className="bud"><span className="lbl-sm">Text-to-speech URL</span>
            <input defaultValue={tts.base_url ?? ""} key={tts.base_url} style={{ width: 230 }}
              onBlur={(e) => save({ tts: { ...tts, base_url: e.target.value.trim() } })} /></label>)}
      </div>
      <div className="row wrap" style={{ gap: 10, alignItems: "end", marginTop: 10 }}>
        <label className="bud"><span className="lbl-sm">Conversation mode</span>
          <select value={cfg.mode === "realtime" ? "realtime" : "cascaded"}
            onChange={async (e) => { await save({ mode: e.target.value }); toast("conversation mode saved", "success"); }}>
            <option value="cascaded">Standard: speech-to-text, model, voice</option>
            <option value="realtime">Realtime speech-to-speech (OpenAI: lowest delay, higher cost)</option>
          </select></label>
        {cfg.mode === "realtime" && (<>
          {keyInput("OPENAI_API_KEY")}
          <label className="bud"><span className="lbl-sm">Realtime voice</span>
            <input defaultValue={cfg.realtime?.voice ?? "marin"} style={{ width: 110 }}
              onBlur={(e) => save({ realtime: { ...(cfg.realtime ?? {}), voice: e.target.value.trim() || "marin" } })} /></label>
        </>)}
      </div>
      {cfg.mode === "realtime" && <p className="dim" style={{ fontSize: 12 }}>Realtime: the OpenAI model hears the
        caller and answers with its own voice. The greeting (with the AI and recording notice) is still spoken by
        the voice above, required fields and "Pause AI" still apply, and if OpenAI can't be reached the call runs
        in standard mode. OpenAI receives the call audio.</p>}
      <div className="row wrap" style={{ gap: 10, alignItems: "end", marginTop: 10 }}>
        <label className="bud"><span className="lbl-sm">Listening (speech-to-text)</span>
          <select value={sttKind} onChange={async (e) => {
            await save({ stt: { ...STT_PRESETS[e.target.value] } }); toast("speech-to-text saved", "success");
          }}>
            <option value="local">Built-in Whisper (free; large-v3-turbo on a GPU, small.en on CPU)</option>
            <option value="openai">OpenAI Whisper</option>
            <option value="deepgram">Deepgram, streaming (fastest)</option>
          </select></label>
        {sttKind === "deepgram" && keyInput("DEEPGRAM_API_KEY")}
        <button className="btn" onClick={test} disabled={busy}>{busy ? "Testing… (first time downloads the speech model)" : "Test voice"}</button>
      </div>
      {result && (result.ok
        ? <p className="ok">✓ Spoke {result.audio_seconds}s of audio in {result.tts_ms} ms
            {result.transcript !== undefined ? ` · heard back: "${result.transcript}" (${result.stt_ms} ms)` : ""}</p>
        : <p className="warn">✗ {result.error}</p>)}
      {ttsKind === "local" && (
        <p className="dim" style={{ fontSize: 12 }}>Local voices run in a small extra container. On the server run
          {" "}<code>xplogent-docker speech on</code> once (add <code>--gpu</code> on an NVIDIA server). Listening
          (speech-to-text) runs inside Xplogent and downloads its model on first use (about 1.6 GB for
          large-v3-turbo on a GPU).</p>
      )}
      <p className="dim" style={{ fontSize: 12 }}>Cloud choices send call audio (OpenAI, Deepgram) or the agent's
        words (OpenAI, ElevenLabs, Cartesia) to that company; the local choices keep everything on this server.
        If the main voice fails, the call keeps going on the fallback (<code>voip.tts_fallback</code>).</p>
    </div>
  );
}

// "Model for calls": the free OpenRouter models, your presets and the models on your
// named Ollama servers (listed live), or any provider:model typed by hand.
function CallModelPicker({ value, mainModel, presets, keySet, onPick }: {
  value: string; mainModel: string; presets: { label: string; model: string }[]; keySet: boolean;
  onPick: (m: string) => void;
}) {
  const [remote, setRemote] = useState<{ server: string; models: string[] }[]>([]);
  const [custom, setCustom] = useState(false);
  useEffect(() => {
    listOllamaServers().then(async ({ servers }) => {
      const found = await Promise.all((servers ?? []).map(async (s) => {
        const probe = await testOllama("", "", s.name).catch(() => null);
        return { server: s.name, models: probe?.ok ? probe.models ?? [] : [] };
      }));
      setRemote(found.filter((f) => f.models.length));
    }).catch(() => undefined);
  }, []);
  const free = FREE_MODELS.map((f) => f.model);
  const own = presets.filter((p) => !free.includes(p.model));
  const known = new Set(["", ...free, ...own.map((p) => p.model),
    ...remote.flatMap((r) => r.models.map((m) => `ollama:${m}@${r.server}`))]);
  const isFree = value.startsWith("openrouter:") && value.endsWith(":free");
  return (
    <div className="bud" style={{ minWidth: 260 }}>
      <span className="lbl-sm">Model for calls</span>
      {custom || !known.has(value) ? (
        <span className="row" style={{ gap: 6 }}>
          <input defaultValue={value} placeholder="provider:model" autoFocus={custom}
            onBlur={(e) => onPick(e.target.value.trim())} style={{ width: 240 }} />
          <button className="linkx" onClick={() => setCustom(false)} title="back to the list">list</button>
        </span>
      ) : (
        <select value={value} onChange={(e) => e.target.value === "__custom" ? setCustom(true) : onPick(e.target.value)}>
          <option value="">Main model{mainModel ? ` (${mainModel})` : ""}</option>
          <optgroup label="OpenRouter, free">
            {FREE_MODELS.map((f) => <option key={f.model} value={f.model}>{f.label}</option>)}</optgroup>
          {remote.map((r) => (
            <optgroup key={r.server} label={`Ollama server "${r.server}"`}>
              {r.models.map((m) => <option key={m} value={`ollama:${m}@${r.server}`}>{m}</option>)}</optgroup>))}
          {own.length > 0 && <optgroup label="Your presets">
            {own.map((p) => <option key={p.model} value={p.model}>{p.label}</option>)}</optgroup>}
          <option value="__custom">Other (type provider:model)…</option>
        </select>
      )}
      <ModelTestButton spec={value || mainModel} label="Test this model" />
      {value.startsWith("openrouter:") && !keySet &&
        <span className="err" style={{ fontSize: 12 }}>Add your OpenRouter key under Settings → API keys.</span>}
      {isFree && <span className="dim" style={{ fontSize: 12 }}>Free models are rate-limited by OpenRouter;
        set a backup model (Settings → Backup model) so a busy minute never leaves a caller waiting.</span>}
    </div>
  );
}
