import { useState } from "react";
import { Check, Cpu, KeyRound, Sparkles } from "lucide-react";
import { ollamaPull, patchConfig, putSecrets, saveOllama, testOllama } from "../api";
import { useToast } from "./Toast";

const PROVIDERS = [
  { id: "ollama", label: "Ollama", note: "Your own Ollama server (or this machine)", model: "ollama:llama3.1", keyEnv: "" },
  { id: "openai", label: "OpenAI", note: "GPT-4o and o-series", model: "openai:gpt-4o", keyEnv: "OPENAI_API_KEY" },
  { id: "anthropic", label: "Anthropic", note: "Claude via API key", model: "anthropic:claude-sonnet-4-6", keyEnv: "ANTHROPIC_API_KEY" },
  { id: "gemini", label: "Google Gemini", note: "Gemini 1.5/2.x", model: "gemini:gemini-1.5-pro", keyEnv: "GEMINI_API_KEY" },
  { id: "openrouter", label: "OpenRouter", note: "200+ models, one key", model: "openrouter:meta-llama/llama-3.1-70b-instruct", keyEnv: "OPENROUTER_API_KEY" },
];

export function Onboarding({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [step, setStep] = useState(0);
  const [provider, setProvider] = useState(PROVIDERS[0]);
  const [model, setModel] = useState(PROVIDERS[0].model);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [ollamaUrl, setOllamaUrl] = useState("");
  const [ollamaToken, setOllamaToken] = useState("");

  const pick = (p: typeof PROVIDERS[number]) => { setProvider(p); setModel(p.model); };

  const finish = async () => {
    setBusy(true);
    try {
      if (provider.keyEnv && key.trim()) await putSecrets({ [provider.keyEnv]: key.trim() });
      if (provider.id === "ollama") {
        if (ollamaUrl.trim()) {
          const saved = await saveOllama(ollamaUrl.trim(), ollamaToken.trim());
          if (!saved.ok) { toast(saved.error || "Could not save the Ollama server", "error"); return; }
        }
        const probe = await testOllama(ollamaUrl.trim(), ollamaToken.trim());
        if (!probe.ok) { toast(`Ollama: ${probe.error}`, "error"); return; }
        const name = model.split(":")[1];
        if (!probe.models.some((m) => m === name || m === `${name}:latest`)) {
          toast(`Downloading ${name} on the Ollama server in the background…`, "info");
          ollamaPull(name).catch(() => {});
        }
      }
      await patchConfig({ model });
      localStorage.setItem("xplogent_onboarded", "1");
      toast("You're all set!", "success");
      onDone();
    } catch {
      toast("Could not save settings", "error");
    } finally { setBusy(false); }
  };

  return (
    <div className="overlay center">
      <div className="modal onboard">
        <div className="modal-body">
          <div className="steps">
            {[0, 1].map((i) => <div key={i} className={`step-dot ${i <= step ? "on" : ""}`} />)}
          </div>
          {step === 0 ? (
            <>
              <h3><Sparkles size={18} /> Welcome to Xplogent</h3>
              <p className="dim">Pick how you want to power the agent. You can change this any time in Settings.</p>
              <div className="provider-grid" style={{ marginTop: 16 }}>
                {PROVIDERS.map((p) => (
                  <div key={p.id} className={`opt ${provider.id === p.id ? "sel" : ""}`} onClick={() => pick(p)}>
                    <b>{p.label}</b>
                    <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>{p.note}</div>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              <h3><Cpu size={18} /> {provider.label}</h3>
              <label className="field-l">Model
                <input value={model} onChange={(e) => setModel(e.target.value)} />
              </label>
              {provider.keyEnv ? (
                <label className="field-l"><span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <KeyRound size={14} /> {provider.keyEnv}</span>
                  <input type="password" placeholder="paste your API key" value={key}
                    onChange={(e) => setKey(e.target.value)} />
                </label>
              ) : (
                <>
                  <label className="field-l">Ollama server URL <span className="dim">(blank = this machine)</span>
                    <input value={ollamaUrl} placeholder="https://ai.example.com" onChange={(e) => setOllamaUrl(e.target.value)} />
                  </label>
                  <label className="field-l">Bearer token <span className="dim">(only if your server needs one)</span>
                    <input type="password" value={ollamaToken} onChange={(e) => setOllamaToken(e.target.value)} />
                  </label>
                  <p className="dim">We'll check the connection and download <code>{model.split(":")[1]}</code> there if it's missing.</p>
                </>
              )}
            </>
          )}
        </div>
        <div className="modal-actions">
          <button className="btn ghost" onClick={() => { localStorage.setItem("xplogent_onboarded", "1"); onDone(); }}>Skip</button>
          {step === 0
            ? <button className="btn primary" onClick={() => setStep(1)}>Continue</button>
            : <button className="btn primary" onClick={finish} disabled={busy}>
                {busy ? "Saving…" : <><Check size={16} /> Finish</>}</button>}
        </div>
      </div>
    </div>
  );
}
