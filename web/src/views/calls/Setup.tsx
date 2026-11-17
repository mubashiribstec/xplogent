import { useEffect, useState } from "react";
import { VoipStatus, listOllamaServers, testOllama } from "../../api";
import { CardBoundary } from "../../components/CardBoundary";
import { ModelTestButton } from "../../components/ModelTest";
import { FAST_CALL_MODELS, FREE_MODELS } from "../../lib/models";
import { Callout, Card, Field, SelectField, Switch, TextField } from "../../ui";
import { AlertsCard } from "../AlertsCard";
import { CallingHoursCard } from "../CallingHoursCard";
import type { Saver, Toaster } from "./shared";

const num = (v: string, fallback: number) => (v.trim() === "" || Number.isNaN(Number(v)) ? fallback : Number(v));

export function Setup({ cfg, status, mainModel, presets, secrets, save, toast }: {
  cfg: Record<string, any>; status: VoipStatus | null; mainModel: string; presets: { label: string; model: string }[];
  secrets: Record<string, boolean>; save: Saver; toast: Toaster;
}) {
  const outbound = cfg.outbound ?? {};
  const vm = cfg.voicemail ?? {};
  const rec = cfg.recording ?? {};
  const s2s = cfg.mode === "voice_agent" || cfg.mode === "realtime";
  return (
    <div className="stack">
      <Card data-tour="setup" title="Phone agent" description="Switch the agent on when the checks on Overview are green."
        actions={<Switch checked={!!cfg.enabled} label={cfg.enabled ? "On" : "Off"} onChange={(on) => save({ enabled: on })} />}>
        <div className="form-grid">
          <TextField label="Company name" value={cfg.company ?? ""} hint="Said in the greeting as {company}."
            onCommit={(v) => save({ company: v.trim() })} />
          <TextField label="Human hand-off number" value={cfg.transfer_number ?? ""} placeholder="+441134960000" inputMode="tel"
            hint="Rings when a caller asks for a person." onCommit={(v) => save({ transfer_number: v.trim() })} />
        </div>
      </Card>

      <Card title="Conversation" description="What the agent says first, which model answers, and the limits.">
        <div className="form-grid">
          <Field label="Greeting" className="span-all" hint={<>{"{company}"} is replaced by the company name. The AI and recording notice is added automatically.</>}>
            {(id) => <textarea id={id} rows={2} defaultValue={cfg.greeting ?? ""} key={cfg.greeting}
              onBlur={(e) => { if (e.target.value !== (cfg.greeting ?? "")) save({ greeting: e.target.value }); }} />}
          </Field>
          <CallModelPicker value={cfg.model ?? ""} mainModel={mainModel} presets={presets} secrets={secrets}
            onPick={(m) => save({ model: m })} disabled={s2s} />
          <TextField label="Max call length (seconds)" type="number" min={30} value={cfg.max_call_seconds ?? 900}
            onCommit={(v) => save({ max_call_seconds: num(v, 900) || 900 })} />
        </div>
        {s2s && <Callout tone="info">Calls use the {cfg.mode === "voice_agent" ? "Deepgram Voice Agent" : "OpenAI Realtime"} model
          (Speech tab). The model above answers only if that service can't be reached.</Callout>}
      </Card>

      <Card title="Costs" description="Estimates for the dashboard and hard caps that stop spending.">
        <div className="form-grid">
          <TextField label="Est. cost per minute ($)" type="number" step="0.001" min={0} value={cfg.cost_per_minute ?? 0}
            onCommit={(v) => save({ cost_per_minute: Math.max(0, num(v, 0)) })} />
          <TextField label="Cost cap per call ($)" type="number" step="0.05" min={0} value={cfg.per_call_cost_cap ?? 0}
            hint="0 = no cap. The call ends politely at the cap." onCommit={(v) => save({ per_call_cost_cap: Math.max(0, num(v, 0)) })} />
          <TextField label="Daily cap ($)" type="number" step="1" min={0} value={cfg.daily_cost_cap ?? 0}
            hint="0 = no cap. No new outbound calls once reached." onCommit={(v) => save({ daily_cost_cap: Math.max(0, num(v, 0)) })} />
        </div>
        {status?.spend_today !== undefined && (
          <p className="fld-hint mt-3">Spent today (estimate): <b>${status.spend_today.toFixed(2)}</b>
            {status.daily_cost_cap ? <> of ${status.daily_cost_cap.toFixed(2)}{status.spend_today >= status.daily_cost_cap
              ? <span className="warn"> · cap reached: no new outbound calls until midnight</span>
              : status.spend_today >= 0.8 * status.daily_cost_cap ? <span className="warn"> · over 80 %</span> : null}</> : null}</p>
        )}
      </Card>

      <Card title="Recording">
        <div className="form-grid">
          <SelectField label="Call recording" value={rec.enabled === false ? "off" : "on"}
            options={[{ value: "on", label: "On: callers are told the call is recorded" }, { value: "off", label: "Off: transcripts only" }]}
            onChange={(v) => save({ recording: { enabled: v === "on" } })} />
          <TextField label="Keep recordings (days)" type="number" min={0} value={rec.retention_days ?? 90} hint="0 = keep forever."
            onCommit={(v) => save({ recording: { retention_days: Math.max(0, num(v, 90)) } })} />
        </div>
      </Card>

      <Card title="Outbound calls" description="How the agent opens a call it places, and what it does with answering machines.">
        <div className="form-grid">
          <SelectField label="Listen first" value={outbound.listen_first === false ? "off" : "on"} disabled={s2s}
            hint={s2s ? "Not used in the speech-to-speech modes." : undefined}
            options={[{ value: "on", label: "On: wait for \"Hello?\" (2.5 s at most)" }, { value: "off", label: "Off: speak as soon as they answer" }]}
            onChange={(v) => save({ outbound: { ...outbound, listen_first: v === "on" } })} />
          <SelectField label="Identity check" value={outbound.identity_check === false ? "off" : "on"}
            options={[{ value: "on", label: "On: \"Am I speaking with Sarah?\" first" }, { value: "off", label: "Off: the greeting says why we're calling" }]}
            onChange={(v) => save({ outbound: { ...outbound, identity_check: v === "on" } })} />
          <SelectField label="Answering machines" value={vm.detect === false ? "off" : "on"} disabled={s2s}
            hint={s2s ? "Not used in the speech-to-speech modes (carrier detection on Telnyx still works)." : undefined}
            options={[{ value: "on", label: "Detect (leave the message below, if set)" }, { value: "off", label: "Don't detect" }]}
            onChange={(v) => save({ voicemail: { ...vm, detect: v === "on" } })} />
        </div>
        {vm.detect !== false && !s2s && (
          <div className="form-grid mt-4">
            <Field label="Voicemail message" className="span-all" optional
              hint={<>Left after the beep; blank = hang up without one. {"{first_name}"}, {"{company}"}, {"{role}"}, {"{location}"} and
                {" "}{"{callback_number}"} are filled from the call; if one has no value, no message is left.</>}>
              {(id) => <textarea id={id} rows={2} defaultValue={vm.message ?? ""} key={vm.message}
                onBlur={(e) => save({ voicemail: { ...vm, message: e.target.value.trim() } })} />}
            </Field>
            <TextField label="Call-back number in it" value={vm.callback_number ?? ""} placeholder="0113 496 0000" inputMode="tel"
              onCommit={(v) => save({ voicemail: { ...vm, callback_number: v.trim() } })} />
          </div>
        )}
        {vm.detect !== false && !s2s && !vm.callback_number && <Callout tone="warn">No call-back number yet: answering machines get no message.</Callout>}
      </Card>

      <CardBoundary name="Calling hours"><CallingHoursCard cfg={cfg} toast={toast} /></CardBoundary>
      <CardBoundary name="Alerts"><AlertsCard toast={toast} /></CardBoundary>
      <p className="fld-hint">Add your scripts and FAQs under Knowledge and your customers under Contacts: the agent answers only from those.</p>
    </div>
  );
}

// "Model for calls": the free OpenRouter models, your presets and the models on your
// named Ollama servers (listed live), or any provider:model typed by hand.
const KEY_FOR_PROVIDER: Record<string, string> = {
  openrouter: "OPENROUTER_API_KEY", vercel: "AI_GATEWAY_API_KEY", openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY", gemini: "GEMINI_API_KEY",
};

function CallModelPicker({ value, mainModel, presets, secrets, onPick, disabled }: {
  value: string; mainModel: string; presets: { label: string; model: string }[]; secrets: Record<string, boolean>;
  onPick: (m: string) => void; disabled?: boolean;
}) {
  const [remote, setRemote] = useState<{ server: string; models: string[] }[]>([]);
  const [custom, setCustom] = useState(false);
  const [asked, setAsked] = useState(false);
  // Ask the Ollama servers for their models only when the list is opened, once per visit.
  const loadRemote = () => {
    if (asked) return;
    setAsked(true);
    listOllamaServers().then(async ({ servers }) => {
      const found = await Promise.all((servers ?? []).map(async (s) => {
        const probe = await testOllama("", "", s.name).catch(() => null);
        return { server: s.name, models: probe?.ok ? probe.models ?? [] : [] };
      }));
      setRemote(found.filter((f) => f.models.length));
    }).catch(() => undefined);
  };
  useEffect(() => { if (value.includes("@")) loadRemote(); /* eslint-disable-next-line */ }, []);
  const free = [...FREE_MODELS, ...FAST_CALL_MODELS].map((f) => f.model);
  const own = presets.filter((p) => !free.includes(p.model));
  const known = new Set(["", ...free, ...own.map((p) => p.model), ...remote.flatMap((r) => r.models.map((m) => `ollama:${m}@${r.server}`))]);
  const isFree = value.startsWith("openrouter:") && value.endsWith(":free");
  const need = KEY_FOR_PROVIDER[(value || mainModel).split(":")[0]];
  const has = need && (secrets[need] || (need === "GEMINI_API_KEY" && secrets.GOOGLE_API_KEY));
  return (
    <Field label="Model for calls" className="span-2"
      hint={isFree ? "Free models are rate-limited by OpenRouter: set a backup model (Settings → Models) so a busy minute never leaves a caller waiting." : undefined}
      error={need && !has ? `Add ${need} under Settings → API keys.` : undefined}>
      {(id) => <>
        {custom || !known.has(value) ? (
          <div className="fld-row">
            <input id={id} defaultValue={value} placeholder="provider:model" autoFocus={custom} disabled={disabled}
              onBlur={(e) => onPick(e.target.value.trim())} />
            <button type="button" className="btn sm" onClick={() => setCustom(false)}>Pick from list</button>
          </div>
        ) : (
          <select id={id} value={value} disabled={disabled} onFocus={loadRemote} onMouseDown={loadRemote}
            onChange={(e) => e.target.value === "__custom" ? setCustom(true) : onPick(e.target.value)}>
            <option value="">Main model{mainModel ? ` (${mainModel})` : ""}</option>
            <optgroup label="Fast for calls (Vercel AI Gateway)">
              {FAST_CALL_MODELS.map((f) => <option key={f.model} value={f.model}>{f.label}</option>)}</optgroup>
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
        <div><ModelTestButton spec={value || mainModel} label="Test this model" /></div>
      </>}
    </Field>
  );
}
