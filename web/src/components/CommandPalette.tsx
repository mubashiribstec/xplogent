import { useEffect, useMemo, useRef, useState } from "react";
import { CornerDownLeft, Search } from "lucide-react";
import { TABS } from "./Nav";

interface Cmd { id: string; label: string; hint?: string; run: () => void; }

// Sub-sections worth jumping to directly.
const SECTIONS: { path: string; label: string; hint: string }[] = [
  { path: "calls/overview", label: "Calls: overview", hint: "Go live, live calls, recent calls" },
  { path: "calls/test", label: "Calls: test call", hint: "Talk to the agent in the browser" },
  { path: "calls/setup", label: "Calls: setup", hint: "Greeting, model, costs, calling hours" },
  { path: "calls/speech", label: "Calls: speech", hint: "Listening, voice, Deepgram Voice Agent" },
  { path: "calls/lines", label: "Calls: phone lines", hint: "Telnyx, SIP trunk, PBX extension" },
  { path: "calls/crm", label: "Calls: CRM", hint: "Call agent connection" },
  { path: "settings/models", label: "Settings: models", hint: "Model, Ollama servers, backup model" },
  { path: "settings/keys", label: "Settings: API keys", hint: "Provider keys" },
  { path: "settings/appearance", label: "Settings: appearance", hint: "Theme, branding, backup" },
];

export function CommandPalette({ open, onClose, onGo, onNewChat, onTour }: {
  open: boolean; onClose: () => void; onGo: (path: string) => void; onNewChat: () => void; onTour?: () => void;
}) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const list = useRef<HTMLUListElement>(null);

  const cmds: Cmd[] = useMemo(() => [
    ...TABS.map((t) => ({ id: t.id, label: t.label, hint: t.group, run: () => onGo(t.id) })),
    ...SECTIONS.map((s) => ({ id: s.path, label: s.label, hint: s.hint, run: () => onGo(s.path) })),
    { id: "new", label: "New chat", hint: "Assistant", run: onNewChat },
    ...(onTour ? [{ id: "tour", label: "Start the product tour", hint: "Help", run: onTour }] : []),
  ], [onGo, onNewChat, onTour]);

  const needle = q.trim().toLowerCase();
  const filtered = cmds.filter((c) => !needle || `${c.label} ${c.hint ?? ""}`.toLowerCase().includes(needle));

  useEffect(() => { if (open) { setQ(""); setActive(0); } }, [open]);
  useEffect(() => { list.current?.children[active]?.scrollIntoView({ block: "nearest" }); }, [active]);

  if (!open) return null;
  const choose = (c: Cmd) => { c.run(); onClose(); };

  return (
    <div className="overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal palette" role="dialog" aria-modal="true" aria-label="Go to">
        <div className="row" style={{ paddingLeft: 16, gap: 0 }}>
          <Search size={18} className="faint" aria-hidden />
          <input autoFocus value={q} placeholder="Go to a page or section…" aria-label="Search pages"
            role="combobox" aria-expanded aria-controls="palette-list"
            aria-activedescendant={filtered[active] ? `cmd-${filtered[active].id}` : undefined}
            onChange={(e) => { setQ(e.target.value); setActive(0); }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, filtered.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
              else if (e.key === "Enter" && filtered[active]) choose(filtered[active]);
              else if (e.key === "Escape") onClose();
            }} />
        </div>
        <ul id="palette-list" role="listbox" ref={list}>
          {filtered.map((c, i) => (
            <li key={c.id} id={`cmd-${c.id}`} role="option" aria-selected={i === active} className={i === active ? "active" : ""}
              onMouseEnter={() => setActive(i)} onClick={() => choose(c)}>
              <span className="grow">{c.label}{c.hint && <span className="faint small"> · {c.hint}</span>}</span>
              {i === active && <CornerDownLeft size={14} className="dim" aria-hidden />}
            </li>
          ))}
          {filtered.length === 0 && <li className="dim">No matches</li>}
        </ul>
      </div>
    </div>
  );
}
