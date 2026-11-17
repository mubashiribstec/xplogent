import { useEffect, useState } from "react";
import { BrainCircuit, Download, History, Package, Sparkles, Trash2, Wand2 } from "lucide-react";
import {
  SkillEvent, SkillPack, compactMemory, deleteSkill, getMemoryMd, getSkillHistory,
  getSkillLibrary, getSkills, getSoul, installSkill, putMemoryMd, putSoul,
} from "../api";
import { skillProgress } from "../lib/skills";
import { useConfirm } from "../ui/confirm";
import { useToast } from "../components/Toast";

const fmtTime = (t: number) => new Date(t * 1000).toLocaleString();

export function Persona() {
  const toast = useToast();
  const confirm = useConfirm();
  const [soul, setSoul] = useState("");
  const [memory, setMemory] = useState("");
  const [library, setLibrary] = useState<SkillPack[]>([]);
  const [skills, setSkills] = useState<any[]>([]);
  const [history, setHistory] = useState<SkillEvent[]>([]);
  const [skillMd, setSkillMd] = useState("");
  const [src, setSrc] = useState("");
  const [busy, setBusy] = useState(false);

  const reloadSkills = () => {
    getSkills().then((s) => setSkills(s.skills)).catch(() => {});
    getSkillHistory().then((r) => setHistory(r.events)).catch(() => {});
  };
  useEffect(() => {
    getSoul().then((r) => setSoul(r.content)).catch(() => {});
    getMemoryMd().then((r) => setMemory(r.content)).catch(() => {});
    getSkillLibrary().then((r) => setLibrary(r.packs)).catch(() => {});
    reloadSkills();
  }, []);

  const installed = new Set(skills.map((s) => s.name));

  const install = async (body: { src?: string; skill_md?: string }) => {
    const res = await installSkill(body);
    if (res.ok) { toast(`installed ${res.installed?.join(", ")}`, "success"); reloadSkills(); }
    else toast(res.error || "install failed", "error");
  };

  const compact = async () => {
    setBusy(true);
    const res = await compactMemory();
    setBusy(false);
    if (res.ok && res.content) { setMemory(res.content); toast("MEMORY.md compacted", "success"); }
    else toast("compaction failed", "error");
  };

  return (
    <div className="pane">
      <div className="pane-wide">
        <div className="page-head"><h1><Sparkles size={22} /> Persona &amp; Skills</h1></div>

        <div className="card">
          <h3><BrainCircuit size={16} /> SOUL.md — who your agent is</h3>
          <p className="dim">Persona, instructions, boundaries, and values. Injected at the top of every session.</p>
          <textarea className="md-edit" value={soul} onChange={(e) => setSoul(e.target.value)} rows={14} />
          <div className="row mt-2">
            <button className="btn primary" onClick={() => putSoul(soul).then(() => toast("SOUL.md saved", "success"))}>Save SOUL.md</button>
          </div>
        </div>

        <div className="card">
          <h3>MEMORY.md — curated long-term memory</h3>
          <p className="dim">The agent distills durable facts &amp; preferences here. Editable; injected each session.</p>
          <textarea className="md-edit" value={memory} onChange={(e) => setMemory(e.target.value)} rows={10} />
          <div className="row" style={{ marginTop: 8, gap: 8 }}>
            <button className="btn primary" onClick={() => putMemoryMd(memory).then(() => toast("MEMORY.md saved", "success"))}>Save MEMORY.md</button>
            <button className="btn" onClick={compact} disabled={busy}><Wand2 size={15} /> {busy ? "Compacting…" : "Compact now"}</button>
          </div>
        </div>

        <div className="card">
          <h3><Package size={16} /> Skills hub</h3>
          <p className="dim">Install ready-made skill packs, or paste a SKILL.md. Skills are markdown workflows that tell the agent how &amp; when to use tools.</p>
          <div className="provider-grid" style={{ marginBottom: 12 }}>
            {library.map((p) => (
              <div className="opt" key={p.name}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <b>{p.name}</b>
                  {installed.has(p.name)
                    ? <span className="badge ok">installed</span>
                    : <button className="btn sm" onClick={() => install({ src: p.name })}>install</button>}
                </div>
                <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>{p.description}</div>
                {p.tools?.length ? <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>tools: {p.tools.join(", ")}</div> : null}
              </div>
            ))}
            {library.length === 0 && <p className="dim">no bundled packs found</p>}
          </div>
          <div className="row wrap" style={{ gap: 8 }}>
            <input placeholder="install from path or http(s) URL…" value={src} onChange={(e) => setSrc(e.target.value)} />
            <button className="btn" onClick={() => { if (src.trim()) install({ src: src.trim() }); setSrc(""); }}><Download size={15} /> Install</button>
          </div>
          <textarea className="md-edit mt-2" rows={6} placeholder="…or paste a SKILL.md here"
            value={skillMd} onChange={(e) => setSkillMd(e.target.value)} />
          <button className="btn" style={{ marginTop: 6 }} onClick={() => { if (skillMd.trim()) install({ skill_md: skillMd }); setSkillMd(""); }}>Install pasted SKILL.md</button>
        </div>

        <div className="card">
          <h3>Your skills ({skills.length})</h3>
          <ul className="list">
            {skills.map((s) => {
              const prog = skillProgress(s);
              return (
                <li key={s.name}>
                  <span className="grow">
                    <b>{s.name}</b> <span className="stars" style={{ color: "var(--amber)" }}>{"★".repeat(s.stars ?? 1)}{"☆".repeat(3 - (s.stars ?? 1))}</span>{" "}
                    <span className="badge">{s.level || "novice"}</span> <span className="badge">{s.source || "learned"}</span><br />
                    <span className="dim small">{s.trigger || s.description}</span>
                    <div className="skill-bar" title={`${s.uses ?? 0} uses · ${prog.pct}% to ${prog.next}`}>
                      <span className="skill-bar-fill" style={{ width: `${prog.pct}%` }} />
                    </div>
                    <span className="faint xsmall">
                      {s.uses ?? 0} uses · {s.successes ?? 0}✓/{s.failures ?? 0}✗ · {prog.next === "max" ? "maxed" : `${prog.pct}% → ${prog.next}`}</span>
                  </span>
                  <button className="x" aria-label={`Delete the skill ${s.name}`} title="Delete" onClick={async () => {
                    if (await confirm({ title: `Delete the skill "${s.name}"?`, body: "Its progress is lost; the agent can learn it again.", confirmLabel: "Delete", danger: true }))
                      deleteSkill(s.name).then(reloadSkills);
                  }}><Trash2 size={14} /></button>
                </li>
              );
            })}
            {skills.length === 0 && <p className="dim">none yet — install a pack above or let the agent learn.</p>}
          </ul>
        </div>

        <div className="card">
          <h3><History size={16} /> Skill activity</h3>
          <p className="dim">When the agent learns, improves, or you install a skill.</p>
          <ul className="list">
            {history.map((e, i) => (
              <li key={i}>
                <span className="grow">
                  <span className={`badge ${e.action === "learned" ? "ok" : e.action === "updated" ? "warn" : ""}`}>{e.action}</span>{" "}
                  <b>{e.name}</b> <span className="stars" style={{ color: "var(--amber)" }}>{"★".repeat(e.stars || 1)}</span><br />
                  <span className="dim small">{e.detail}</span>
                </span>
                <span className="faint" style={{ fontSize: 11, whiteSpace: "nowrap" }}>{fmtTime(e.created_at)}</span>
              </li>
            ))}
            {history.length === 0 && <p className="dim">no skill activity yet</p>}
          </ul>
        </div>
      </div>
    </div>
  );
}
