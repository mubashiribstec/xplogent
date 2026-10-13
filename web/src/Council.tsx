import { useEffect, useRef, useState } from "react";
import { Sparkles, Users } from "lucide-react";
import { ModelPreset, XplogentEvent, XplogentSocket, getModels } from "./api";
import { Markdown } from "./Markdown";

export function Council() {
  const [models, setModels] = useState<ModelPreset[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [synth, setSynth] = useState("");
  const [task, setTask] = useState("");
  const [running, setRunning] = useState(false);
  const [cols, setCols] = useState<Record<string, string>>({});
  const [doneCh, setDoneCh] = useState<Set<string>>(new Set());
  const sock = useRef<XplogentSocket | null>(null);

  useEffect(() => {
    getModels().then((r) => {
      setModels(r.models);
      const first = r.models.slice(0, 2).map((m) => m.model);
      setSelected(first);
      setSynth(r.active || first[0] || "");
    }).catch(() => {});
    return () => sock.current?.close();
  }, []);

  const toggle = (model: string) =>
    setSelected((s) => (s.includes(model) ? s.filter((m) => m !== model) : [...s, model]));

  const handle = (ev: XplogentEvent) => {
    if (ev.type === "council_token") {
      const ch = String(ev.channel);
      setCols((c) => ({ ...c, [ch]: (c[ch] ?? "") + String(ev.text ?? "") }));
    } else if (ev.type === "council_done") {
      setDoneCh((d) => new Set(d).add(String(ev.channel)));
    } else if (ev.type === "done") {
      setRunning(false);
    }
  };

  const run = () => {
    if (!task.trim() || selected.length < 2 || running) return;
    setCols({}); setDoneCh(new Set()); setRunning(true);
    sock.current?.close();
    sock.current = new XplogentSocket(handle, null);
    sock.current.sendCouncil(task, selected, synth || selected[0]);  // queued until open
  };

  const channels = [...selected, "synthesis"];

  return (
    <div className="council">
      <div className="page-head" style={{ padding: "16px 24px 0" }}>
        <h1 style={{ fontSize: 20 }}><Users size={20} /> Council</h1>
        <span className="dim" style={{ fontSize: 13 }}>ask several models at once, get a synthesized answer</span>
      </div>
      <div className="council-bar">
        <input value={task} placeholder="Ask every selected model the same question…"
               onChange={(e) => setTask(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && run()} />
        <select value={synth} onChange={(e) => setSynth(e.target.value)} title="synthesis model">
          {models.map((m) => <option key={m.model} value={m.model}>synth: {m.label}</option>)}
        </select>
        <button className="btn primary" onClick={run} disabled={running || selected.length < 2}>
          <Sparkles size={16} />{running ? "asking…" : "Ask the council"}
        </button>
      </div>
      <div className="council-models">
        {models.map((m) => (
          <label key={m.model} className="check">
            <input type="checkbox" checked={selected.includes(m.model)}
                   onChange={() => toggle(m.model)} />
            {m.label}
          </label>
        ))}
      </div>
      <div className="council-grid">
        {channels.map((ch) => (
          <section key={ch} className={ch === "synthesis" ? "council-col synthesis" : "council-col"}>
            <h3>{ch === "synthesis" ? "✦ Synthesis" : ch}
              {doneCh.has(ch) ? <span className="dim"> ✓</span> : running ? <span className="dim"> …</span> : null}
            </h3>
            <Markdown text={cols[ch] ?? ""} />
          </section>
        ))}
      </div>
    </div>
  );
}
