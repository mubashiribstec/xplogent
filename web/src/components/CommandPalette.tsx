import { useEffect, useMemo, useState } from "react";
import { CornerDownLeft } from "lucide-react";
import { TABS, Tab } from "./NavRail";

interface Cmd { id: string; label: string; run: () => void; }

export function CommandPalette({ open, onClose, onTab, onNewChat, onTour }: {
  open: boolean;
  onClose: () => void;
  onTab: (t: Tab) => void;
  onNewChat: () => void;
  onTour?: () => void;
}) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);

  const cmds: Cmd[] = useMemo(() => [
    { id: "new", label: "New chat", run: onNewChat },
    ...(onTour ? [{ id: "tour", label: "Start the product tour", run: onTour }] : []),
    ...TABS.map((t) => ({ id: t.id, label: `Go to ${t.label}`, run: () => onTab(t.id) })),
  ], [onTab, onNewChat, onTour]);

  const filtered = cmds.filter((c) => c.label.toLowerCase().includes(q.toLowerCase()));

  useEffect(() => { if (open) { setQ(""); setActive(0); } }, [open]);

  if (!open) return null;
  const choose = (c: Cmd) => { c.run(); onClose(); };

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal palette" onClick={(e) => e.stopPropagation()}>
        <input autoFocus value={q} placeholder="Type a command…"
          onChange={(e) => { setQ(e.target.value); setActive(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, filtered.length - 1));
            else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
            else if (e.key === "Enter" && filtered[active]) choose(filtered[active]);
            else if (e.key === "Escape") onClose();
          }} />
        <ul>
          {filtered.map((c, i) => (
            <li key={c.id} className={i === active ? "active" : ""}
                onMouseEnter={() => setActive(i)} onClick={() => choose(c)}>
              <span style={{ flex: 1 }}>{c.label}</span>
              {i === active && <CornerDownLeft size={14} className="dim" />}
            </li>
          ))}
          {filtered.length === 0 && <li className="dim">no matches</li>}
        </ul>
      </div>
    </div>
  );
}
