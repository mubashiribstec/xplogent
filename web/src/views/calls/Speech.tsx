// Calls → Speech: how the agent hears and speaks. Three conversation modes:
//   Standard      — listening (speech-to-text) and voice (text-to-speech) chosen separately
//   Voice Agent   — one Deepgram connection: Deepgram listens, a hosted model answers, a voice speaks
//   Realtime      — OpenAI speech-to-speech
// Every choice has a Test button; keys are saved as secrets and never shown.
import { useEffect, useMemo, useState } from "react";
import { AudioLines, AudioWaveform, Ear, Layers, Link2, Mic, Play, Server, Sparkles, Volume2, Waves, Zap } from "lucide-react";
import {
  getVoiceAgentModels, getVoiceAgentVoices, getVoices, playWav, putSecrets, testSpeech, testVoiceAgent,
  type SpeechTest, type ThinkModel, type VoiceAgentTest,
} from "../../api";
import {
  Badge, Button, Callout, Card, ChoiceCards, Field, ResultLine, SecretField, Segmented, SelectField, TextField,
} from "../../ui";
import type { Saver, Toaster } from "./shared";

const GEMMA = "62ae83ad-4f6a-430b-af41-a9bede9286ca";      // Cartesia "Gemma": UK English, female

type SttKind = "local" | "deepgram" | "cartesia" | "openai";
type TtsKind = "local" | "openai" | "elevenlabs" | "cartesia" | "deepgram" | "custom";
type Mode = "cascaded" | "voice_agent" | "realtime";

const STT_PRESETS: Record<Exclude<SttKind, never>, Record<string, any>> = {
  local: { provider: "whisper-local", model: "auto", base_url: "" },
  deepgram: { provider: "deepgram", model: "nova-3", streaming: true, base_url: "" },
  cartesia: { provider: "cartesia", model: "ink-2", streaming: true, base_url: "" },
  openai: { provider: "openai-compatible", model: "whisper-1", base_url: "https://api.openai.com/v1" },
};
const STT_MODELS: Record<SttKind, { value: string; label: string }[]> = {
  local: [{ value: "auto", label: "Automatic (large-v3-turbo on a GPU, small.en on CPU)" },
    { value: "small.en", label: "small.en (fast on CPU)" }, { value: "large-v3-turbo", label: "large-v3-turbo (GPU)" }],
  deepgram: [{ value: "nova-3", label: "Nova-3 (best)" }, { value: "nova-2", label: "Nova-2" }],
  cartesia: [{ value: "ink-2", label: "Ink 2 (live, fastest)" }, { value: "ink-whisper", label: "Ink Whisper" }],
  openai: [{ value: "whisper-1", label: "Whisper" }, { value: "gpt-4o-mini-transcribe", label: "GPT-4o mini transcribe" },
    { value: "gpt-4o-transcribe", label: "GPT-4o transcribe" }],
};
const TTS_PRESETS: Record<Exclude<TtsKind, "custom">, Record<string, any>> = {
  local: { provider: "openai", base_url: "http://kokoro:8880/v1", model: "kokoro", voice: "af_heart", sample_rate: 24000 },
  openai: { provider: "openai", base_url: "https://api.openai.com/v1", model: "tts-1", voice: "alloy", sample_rate: 24000 },
  elevenlabs: { provider: "elevenlabs", model: "eleven_flash_v2_5", voice: "", sample_rate: 24000 },
  cartesia: { provider: "cartesia", model: "sonic-3.6", voice: GEMMA, sample_rate: 24000, language: "en" },
  deepgram: { provider: "deepgram", model: "aura-2", voice: "aura-2-pandora-en", sample_rate: 24000 },
};
const KEY: Partial<Record<SttKind | TtsKind, string>> = {
  deepgram: "DEEPGRAM_API_KEY", cartesia: "CARTESIA_API_KEY", elevenlabs: "ELEVENLABS_API_KEY", openai: "OPENAI_API_KEY",
};
const REALTIME_VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"];
const THINK_GROUP: Record<string, string> = { open_ai: "OpenAI", anthropic: "Anthropic", google: "Google" };

export const sttKindOf = (stt: Record<string, any>): SttKind =>
  stt.provider === "deepgram" ? "deepgram" : stt.provider === "cartesia" ? "cartesia"
    : (stt.provider ?? "whisper-local") === "whisper-local" ? "local" : "openai";
export const ttsKindOf = (tts: Record<string, any>): TtsKind =>
  ["elevenlabs", "cartesia", "deepgram"].includes(tts.provider) ? tts.provider
    : String(tts.base_url ?? "").includes("kokoro") ? "local"
    : String(tts.base_url ?? "api.openai.com").includes("api.openai.com") ? "openai" : "custom";

export function SpeechSection({ cfg, secrets, save, toast, onKey }: {
  cfg: Record<string, any>; secrets: Record<string, boolean>; save: Saver; toast: Toaster; onKey: () => void;
}) {
  const mode: Mode = cfg.mode === "realtime" ? "realtime" : cfg.mode === "voice_agent" ? "voice_agent" : "cascaded";
  const saveKey = async (name: string, value: string) => {
    await putSecrets({ [name]: value });
    toast(`${name} saved`, "success");
    onKey();
  };
  return (
    <div className="stack">
      <Card title="Conversation mode" description="How the agent hears the caller and speaks on a call. You can switch at any time.">
        <ChoiceCards<Mode> label="Conversation mode" value={mode}
          onChange={async (m) => { if (m !== mode) await save({ mode: m }); }}
          options={[
            { value: "cascaded", icon: Layers, title: "Standard",
              description: "Choose the listening and the voice separately; the model for calls (Setup) answers." },
            { value: "voice_agent", icon: AudioLines, title: "Deepgram Voice Agent", badge: <Badge tone="info">one connection</Badge>,
              description: "Deepgram listens, a Deepgram-hosted model answers and your chosen voice speaks." },
            { value: "realtime", icon: Zap, title: "OpenAI Realtime",
              description: "OpenAI's speech-to-speech model hears the caller and answers in its own voice." },
          ]} />
      </Card>
      {mode === "cascaded" && <>
        <ListeningCard cfg={cfg} secrets={secrets} save={save} toast={toast} saveKey={saveKey} />
        <VoiceCard cfg={cfg} secrets={secrets} save={save} toast={toast} saveKey={saveKey} />
        <Callout tone="info">Cloud choices send call audio (Deepgram, Cartesia, OpenAI) or the agent's words (OpenAI,
          ElevenLabs, Cartesia, Deepgram) to that company; the local choices keep everything on this server. If the
          main voice fails, the call carries on with the fallback voice.</Callout>
      </>}
      {mode === "voice_agent" && <VoiceAgentCards cfg={cfg} secrets={secrets} save={save} toast={toast} saveKey={saveKey} />}
      {mode === "realtime" && <RealtimeCard cfg={cfg} secrets={secrets} save={save} saveKey={saveKey} />}
    </div>
  );
}

type CardProps = { cfg: Record<string, any>; secrets: Record<string, boolean>; save: Saver; toast: Toaster;
  saveKey: (name: string, value: string) => Promise<void> };

// ── Standard: listening ────────────────────────────────────────────────────
function ListeningCard({ cfg, secrets, save, saveKey }: CardProps) {
  const stt = cfg.stt ?? {};
  const kind = sttKindOf(stt);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SpeechTest | null>(null);
  const keyName = KEY[kind];
  const run = async () => {
    setBusy(true); setResult(null);
    try { setResult(await testSpeech(true)); } catch (e) { setResult({ ok: false, error: String(e) }); }
    finally { setBusy(false); }
  };
  return (
    <Card title={<><Ear size={17} aria-hidden /> Listening (speech-to-text)</>}
      description="Turns what the caller says into text for the model."
      actions={<Button icon={Mic} loading={busy} onClick={run} disabled={!!keyName && !secrets[keyName]}>Test listening</Button>}>
      <ChoiceCards<SttKind> label="Listening provider" value={kind}
        onChange={async (k) => { if (k !== kind) { await save({ stt: { ...STT_PRESETS[k] } }); setResult(null); } }}
        options={[
          { value: "local", icon: Server, title: "Built-in Whisper", badge: <Badge tone="ok">free</Badge>,
            description: "Runs on this server; downloads its model on first use." },
          { value: "deepgram", icon: AudioWaveform, title: "Deepgram Nova-3", badge: <Badge>live</Badge>,
            description: "Streams while the caller speaks: the fastest replies." },
          { value: "cartesia", icon: Waves, title: "Cartesia Ink", badge: <Badge>live</Badge>,
            description: "Cartesia's streaming speech-to-text, built for voice agents." },
          { value: "openai", icon: Sparkles, title: "OpenAI", description: "Whisper or GPT-4o transcribe, per utterance." },
        ]} />
      <div className="form-grid mt-4">
        {keyName && <SecretField label={keyName} isSet={!!secrets[keyName]} onSave={(v) => saveKey(keyName, v)} />}
        <SelectField label="Model" value={String(stt.model ?? STT_PRESETS[kind].model)}
          options={STT_MODELS[kind]} onChange={(m) => save({ stt: { ...stt, model: m } })} />
        {(kind === "deepgram" || kind === "cartesia") && (
          <SelectField label="Streaming" value={stt.streaming === false ? "off" : "on"}
            hint="Live transcription while the caller speaks; off = one request per sentence."
            options={[{ value: "on", label: "On (recommended)" }, { value: "off", label: "Off" }]}
            onChange={(v) => save({ stt: { ...stt, streaming: v === "on" } })} />
        )}
      </div>
      {result && <SpeechResult r={result} />}
      {!result && <p className="fld-hint mt-3">The test speaks a sentence with your voice below, then this listening
        writes it back, so both directions are checked.</p>}
    </Card>
  );
}

// ── Standard: voice ────────────────────────────────────────────────────────
function VoiceCard({ cfg, secrets, save, toast, saveKey }: CardProps) {
  const tts = cfg.tts ?? {};
  const kind = ttsKindOf(tts);
  const keyName = KEY[kind];
  const missingKey = !!keyName && !secrets[keyName];
  const voices = useVoices(kind === "custom" || missingKey ? "" : tts.provider ?? "openai", `${kind}|${tts.base_url ?? ""}|${missingKey}`);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SpeechTest | null>(null);
  const play = async () => {
    setBusy(true); setResult(null);
    try {
      const r = await testSpeech(false, { play: true });
      setResult(r);
      if (r.ok && r.wav) await playWav(r.wav).catch((e) => toast(String(e), "error"));
    } catch (e) { setResult({ ok: false, error: String(e) }); } finally { setBusy(false); }
  };
  return (
    <Card title={<><Volume2 size={17} aria-hidden /> Voice (text-to-speech)</>} description="The voice your callers hear."
      actions={<Button icon={Play} loading={busy} onClick={play} disabled={missingKey}>Play a sample</Button>}>
      <ChoiceCards<TtsKind> label="Voice provider" value={kind}
        onChange={async (k) => { if (k !== kind && k !== "custom") { await save({ tts: { ...TTS_PRESETS[k] } }); setResult(null); } }}
        options={[
          { value: "local", icon: Server, title: "Local voice", badge: <Badge tone="ok">free</Badge>,
            description: "Kokoro on this server (needs the speech container)." },
          { value: "cartesia", icon: Waves, title: "Cartesia Sonic", badge: <Badge tone="info">Gemma UK</Badge>,
            description: "Natural, very fast; Gemma is a UK English voice." },
          { value: "deepgram", icon: AudioWaveform, title: "Deepgram Aura-2", badge: <Badge>UK voices</Badge>,
            description: "Pandora (UK female), Draco (UK male) and 40 more." },
          { value: "elevenlabs", icon: Volume2, title: "ElevenLabs", description: "Flash v2.5, your ElevenLabs voices." },
          { value: "openai", icon: Sparkles, title: "OpenAI", description: "tts-1 voices such as Alloy." },
          ...(kind === "custom" ? [{ value: "custom" as TtsKind, icon: Link2, title: "Custom server", description: "An OpenAI-compatible URL." }] : []),
        ]} />
      <div className="form-grid mt-4">
        {keyName && <SecretField label={keyName} isSet={!!secrets[keyName]} onSave={(v) => saveKey(keyName, v)} />}
        <VoicePicker value={String(tts.voice ?? "")} voices={voices} onChange={(v) => save({ tts: { ...tts, voice: v } })}
          extra={kind === "cartesia" ? [{ id: GEMMA, name: "Gemma · UK female (recommended for UK calls)" }] : []} />
        {(kind === "local" || kind === "openai" || kind === "custom") && (
          <TextField label="Server URL" value={tts.base_url ?? ""} onCommit={(v) => save({ tts: { ...tts, base_url: v.trim() } })} />
        )}
        {kind === "deepgram" && (
          <SelectField label="Audio quality" value={String(tts.sample_rate ?? 24000)}
            hint="Phone lines carry 8 kHz; 24 kHz gives the best quality for browser test calls."
            options={[{ value: "24000", label: "24 kHz (recommended)" }, { value: "16000", label: "16 kHz" }, { value: "8000", label: "8 kHz" }]}
            onChange={(v) => save({ tts: { ...tts, sample_rate: Number(v) } })} />
        )}
      </div>
      {voices.error && <p className="fld-hint warn mt-2">Couldn't list the voices: {voices.error.slice(0, 140)}</p>}
      {kind === "local" && <Callout tone="info"><p>Local voices run in a small extra container: on the server run{" "}
        <code>xplogent-docker speech on</code> once (add <code>--gpu</code> on an NVIDIA server).</p></Callout>}
      {result && <SpeechResult r={result} />}
    </Card>
  );
}

function useVoices(provider: string, dep: string) {
  const [state, setState] = useState<{ list: { id: string; name: string }[] | null; error: string }>({ list: null, error: "" });
  useEffect(() => {
    if (!provider) { setState({ list: [], error: "" }); return; }
    let live = true;
    setState({ list: null, error: "" });
    getVoices(provider).then((r) => {
      if (!live) return;
      // Plain strings only: an unexpected shape from a voice server must not break the page.
      const list = (r.voices ?? []).map((v: any) => typeof v === "string" ? { id: v, name: v }
        : { id: String(v?.id?.id ?? v?.id ?? ""), name: String(v?.name?.name ?? v?.name ?? v?.id ?? "") }).filter((v) => v.id);
      setState({ list, error: r.ok ? "" : r.error ?? "could not list voices" });
    }).catch((e) => live && setState({ list: [], error: String(e) }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dep]);
  return { list: state.list, error: state.error };
}

function VoicePicker({ value, voices, onChange, extra = [], label = "Voice" }: {
  value: string; voices: { list: { id: string; name: string }[] | null }; onChange: (v: string) => void;
  extra?: { id: string; name: string }[]; label?: string;
}) {
  const list = useMemo(() => {
    const all = [...extra, ...(voices.list ?? [])];
    return all.filter((v, i) => all.findIndex((x) => x.id === v.id) === i);
  }, [voices.list, extra]);
  if (!list.length) {
    return <TextField label={label} value={value} placeholder={voices.list === null ? "loading voices…" : "voice id"}
      hint="Add the key to choose from a list, or paste a voice id." onCommit={(v) => onChange(v.trim())} />;
  }
  return (
    <SelectField label={label} value={value} onChange={onChange}
      options={list.map((v) => ({ value: v.id, label: v.name || v.id }))} />
  );
}

function SpeechResult({ r }: { r: SpeechTest }) {
  if (!r.ok) return <ResultLine ok={false}>{r.error || "the test failed"}</ResultLine>;
  return (
    <ResultLine ok>
      Spoke {r.audio_seconds}s of audio in <b>{r.tts_ms} ms</b>
      {r.transcript !== undefined && <> · heard back “{r.transcript}” in <b>{r.stt_ms} ms</b></>}
    </ResultLine>
  );
}

// ── Deepgram Voice Agent ───────────────────────────────────────────────────
function VoiceAgentCards({ cfg, secrets, save, toast, saveKey }: CardProps) {
  const va: Record<string, any> = cfg.voice_agent ?? {};
  const think: Record<string, any> = va.think ?? { provider: "open_ai", model: "gpt-4o-mini", temperature: 0.6 };
  const voiceProvider: "deepgram" | "cartesia" = va.voice_provider === "cartesia" ? "cartesia" : "deepgram";
  const putVa = (u: Record<string, any>) => save({ voice_agent: { ...va, ...u } });
  const [models, setModels] = useState<ThinkModel[]>([]);
  const [dgVoices, setDgVoices] = useState<{ list: { id: string; name: string }[] | null }>({ list: null });
  const cartesiaVoices = useVoices(voiceProvider === "cartesia" && secrets.CARTESIA_API_KEY ? "cartesia" : "", `${voiceProvider}|${secrets.CARTESIA_API_KEY}`);
  const [busy, setBusy] = useState<"" | "agent" | "voice">("");
  const [result, setResult] = useState<VoiceAgentTest | null>(null);

  useEffect(() => {
    getVoiceAgentModels().then((r) => setModels(r.models ?? [])).catch(() => setModels([]));
    getVoiceAgentVoices().then((r) => setDgVoices({ list: r.voices ?? [] })).catch(() => setDgVoices({ list: [] }));
  }, [secrets.DEEPGRAM_API_KEY]);

  const modelValue = `${think.provider ?? "open_ai"}|${think.model ?? "gpt-4o-mini"}`;
  const preview = async () => {
    setBusy("voice");
    try {
      const tts = voiceProvider === "cartesia"
        ? { provider: "cartesia", model: "sonic-3.6", voice: va.cartesia_voice || GEMMA, sample_rate: 24000, language: "en" }
        : { provider: "deepgram", voice: va.voice || "aura-2-pandora-en", sample_rate: 24000 };
      const r = await testSpeech(false, { play: true, tts, text: "Hello, thanks for calling. How can I help you today?" });
      if (r.ok && r.wav) await playWav(r.wav);
      else toast(r.error ?? "the voice didn't answer", "error");
    } catch (e) { toast(String(e), "error"); } finally { setBusy(""); }
  };
  const runTest = async () => {
    setBusy("agent"); setResult(null);
    try { setResult(await testVoiceAgent()); } catch (e) { setResult({ ok: false, error: String(e) }); } finally { setBusy(""); }
  };
  const needCartesia = voiceProvider === "cartesia" && !secrets.CARTESIA_API_KEY;

  return <>
    <Card title="Deepgram connection" description="One Deepgram key covers listening, the hosted model and Aura-2 voices.">
      <div className="form-grid">
        <SecretField label="DEEPGRAM_API_KEY" isSet={!!secrets.DEEPGRAM_API_KEY} onSave={(v) => saveKey("DEEPGRAM_API_KEY", v)}
          hint="Deepgram console → API keys. Deepgram bills the agent per minute." />
        <SelectField label="Listening model" value={String(va.listen_model ?? "nova-3")}
          options={[{ value: "nova-3", label: "Nova-3 (recommended)" }, { value: "nova-2", label: "Nova-2" }]}
          onChange={(v) => putVa({ listen_model: v })} />
      </div>
    </Card>

    <Card title="Thinking" description="The model that writes the replies, hosted by Deepgram: no separate key needed.">
      <div className="form-grid">
        <SelectField label="Model" value={modelValue}
          options={models.map((m) => ({ value: `${m.provider}|${m.model}`, label: m.name || m.model, group: THINK_GROUP[m.provider] ?? m.provider }))}
          onChange={(v) => { const [provider, model] = v.split("|"); putVa({ think: { ...think, provider, model } }); }} />
        <TextField label="Creativity (temperature)" type="number" step="0.1" min={0} value={think.temperature ?? 0.6}
          hint="Lower is steadier. 0.4–0.7 suits phone calls."
          onCommit={(v) => putVa({ think: { ...think, temperature: Math.max(0, Math.min(2, Number(v) || 0)) } })} />
      </div>
      <p className="fld-hint mt-3">The agent gets the same prompt, knowledge search and tools as in standard mode
        (callbacks, outcomes, transfer, required fields).</p>
    </Card>

    <Card title="Voice" description="Deepgram speaks with Aura-2, or with your Cartesia voice (such as Gemma, UK)."
      actions={<Button icon={Play} loading={busy === "voice"} onClick={preview}
        disabled={!secrets.DEEPGRAM_API_KEY && voiceProvider === "deepgram" || needCartesia}>Preview</Button>}>
      <Segmented<"deepgram" | "cartesia"> label="Voice provider" value={voiceProvider}
        onChange={(p) => putVa({ voice_provider: p, ...(p === "cartesia" && !va.cartesia_voice ? { cartesia_voice: GEMMA } : {}) })}
        options={[{ value: "deepgram", label: "Deepgram Aura-2" }, { value: "cartesia", label: "Cartesia" }]} />
      <div className="form-grid mt-4">
        {voiceProvider === "deepgram"
          ? <VoicePicker label="Aura-2 voice" value={String(va.voice ?? "aura-2-pandora-en")} voices={dgVoices}
              onChange={(v) => putVa({ voice: v })} />
          : <>
              <SecretField label="CARTESIA_API_KEY" isSet={!!secrets.CARTESIA_API_KEY} onSave={(v) => saveKey("CARTESIA_API_KEY", v)} />
              <VoicePicker label="Cartesia voice" value={String(va.cartesia_voice || GEMMA)} voices={cartesiaVoices}
                extra={[{ id: GEMMA, name: "Gemma · UK female (recommended for UK calls)" }]}
                onChange={(v) => putVa({ cartesia_voice: v })} />
            </>}
      </div>
      {voiceProvider === "cartesia" && <Callout tone="info">With a Cartesia voice, your Cartesia key is sent to Deepgram in
        the session settings, so Deepgram can call Cartesia for you.</Callout>}
    </Card>

    <Card title="Test the Voice Agent" description="Connects to Deepgram with these choices, sends one question and times the reply. No call is placed."
      actions={<Button variant="primary" icon={AudioLines} loading={busy === "agent"} onClick={runTest}
        disabled={!secrets.DEEPGRAM_API_KEY || needCartesia}>Test Voice Agent</Button>}>
      {!secrets.DEEPGRAM_API_KEY && <Callout tone="warn">Add your Deepgram key above first.</Callout>}
      {result && (result.ok
        ? <ResultLine ok>Connected in <b>{result.connect_ms} ms</b>, first audio after <b>{result.first_audio_ms} ms</b>
            {result.audio_seconds ? <> ({result.audio_seconds}s of speech)</> : null}
            {result.reply && <div className="dim">“{result.reply}”</div>}</ResultLine>
        : <ResultLine ok={false}>{result.error || "the test failed"}</ResultLine>)}
      <Callout tone="warn" title="Different in this mode">
        <p>The greeting (with the AI and recording notice) is spoken by the agent's voice; Pause AI, required fields
          and the call limits still apply. Not available here: the price guard on the model's sentences, local
          answering-machine detection, the voicemail message and "listen first". If Deepgram can't be reached, the call
          runs in standard mode.</p>
      </Callout>
    </Card>
  </>;
}

// ── OpenAI Realtime ────────────────────────────────────────────────────────
function RealtimeCard({ cfg, secrets, save, saveKey }: Omit<CardProps, "toast">) {
  const rt: Record<string, any> = cfg.realtime ?? {};
  return (
    <Card title="OpenAI Realtime" description="The OpenAI model hears the caller and answers with its own voice: the lowest delay, at a higher cost.">
      <div className="form-grid">
        <SecretField label="OPENAI_API_KEY" isSet={!!secrets.OPENAI_API_KEY} onSave={(v) => saveKey("OPENAI_API_KEY", v)} />
        <Field label="Voice">
          {(id) => (
            <select id={id} value={rt.voice ?? "marin"} onChange={(e) => save({ realtime: { ...rt, voice: e.target.value } })}>
              {REALTIME_VOICES.map((v) => <option key={v} value={v}>{v[0].toUpperCase() + v.slice(1)}</option>)}
            </select>
          )}
        </Field>
      </div>
      <Callout tone="info"><p>The greeting is still spoken by your standard voice (Standard mode's voice), required
        fields and Pause AI still apply, and if OpenAI can't be reached the call runs in standard mode. OpenAI receives
        the call audio.</p></Callout>
    </Card>
  );
}
