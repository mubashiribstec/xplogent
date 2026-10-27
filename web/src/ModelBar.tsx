import { useEffect, useState } from "react";
import { Cpu } from "lucide-react";
import { ModelPreset, getModels } from "./api";

export interface GenChoice {
  model: string;
  effort: string;
  thinking: boolean;
}

const PROVIDER_LABEL: Record<string, string> = {
  ollama: "Ollama", openai: "OpenAI", anthropic: "Anthropic", gemini: "Gemini",
  openrouter: "OpenRouter",
  vercel: "Vercel AI Gateway",
};

const RECENT_KEY = "xplogent_recent_models";
const loadRecent = (): string[] => {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch { return []; }
};

export function ModelBar({ value, onChange }:
  { value: GenChoice; onChange: (c: GenChoice) => void }) {
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [active, setActive] = useState("");
  const [recent, setRecent] = useState<string[]>(loadRecent);

  // Pull the latest presets + active model. Called on mount, on window focus,
  // and whenever the dropdown is opened, so models added in Settings show up
  // without a page refresh.
  const load = () => getModels().then((r) => {
    setModels(r.models || []);
    setActive(r.active || "");
    if (!value.model && r.active) onChange({ ...value, model: r.active });
  }).catch(() => {});

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);
  useEffect(() => {
    const h = () => load();
    window.addEventListener("focus", h);
    return () => window.removeEventListener("focus", h);
    // eslint-disable-next-line
  }, []);

  const remember = (m: string) => {
    if (!m) return;
    const next = [m, ...recent.filter((x) => x !== m)].slice(0, 15);
    setRecent(next);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(next)); } catch { /* ignore */ }
  };

  const select = (m: string) => {
    const p = models.find((x) => x.model === m);
    remember(m);
    onChange({ model: m, effort: p?.effort ?? value.effort, thinking: p?.thinking ?? value.thinking });
  };

  const provider = (value.model || "").split(":")[0];
  const modelName = (value.model || "").split(":").slice(1).join(":") || value.model;

  // Option set = presets + any other known model (active, current, recents) that
  // isn't already a preset — so a selected model never disappears from the list.
  const presetSet = new Set(models.map((m) => m.model));
  const others = [...new Set([active, value.model, ...recent])]
    .filter((m) => m && !presetSet.has(m));
  const labelFor = (m: string) =>
    models.find((x) => x.model === m)?.label || m.split(":").slice(1).join(":") || m;

  return (
    <div className="modelbar">
      <span className="badge" title="active provider">
        <Cpu size={13} /> {PROVIDER_LABEL[provider] ?? provider ?? "model"}
      </span>
      <select value={value.model} onMouseDown={load} onChange={(e) => select(e.target.value)}>
        {models.map((m) => <option key={m.model} value={m.model}>{m.label}</option>)}
        {others.length > 0 && (
          <optgroup label="other models">
            {others.map((m) => <option key={m} value={m}>{labelFor(m)}</option>)}
          </optgroup>
        )}
      </select>
      <span className="dim mono" style={{ fontSize: 12 }}>{modelName}</span>

      <label className="eff">effort
        <select value={value.effort} onChange={(e) => onChange({ ...value, effort: e.target.value })}>
          {["off", "low", "medium", "high"].map((e) => <option key={e} value={e}>{e}</option>)}
        </select>
      </label>
      <label className="think">
        <input type="checkbox" checked={value.thinking}
               onChange={(e) => onChange({ ...value, thinking: e.target.checked })} />
        thinking
      </label>
    </div>
  );
}
