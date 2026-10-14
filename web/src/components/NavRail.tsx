import {
  Activity, BarChart3, BookOpen, BookText, Calendar, GraduationCap, Globe, type LucideIcon,
  MessageSquare, Monitor, Moon, PhoneCall, Contact, Settings as Cog, ShieldAlert, Sparkles, Sun, Zap,
} from "lucide-react";

export type Tab = "chat" | "operator" | "browser" | "calls" | "contacts" | "training" | "runs" | "knowledge" | "analytics" | "persona" | "schedules" | "triggers" | "audit" | "settings" | "guide";

export const TABS: { id: Tab; label: string; icon: LucideIcon }[] = [
  { id: "chat", label: "Chat", icon: MessageSquare },
  { id: "operator", label: "Operator", icon: Monitor },
  { id: "browser", label: "Browser", icon: Globe },
  { id: "calls", label: "Calls", icon: PhoneCall },
  { id: "contacts", label: "Contacts", icon: Contact },
  { id: "training", label: "Call training", icon: GraduationCap },
  { id: "runs", label: "Runs", icon: Activity },
  { id: "knowledge", label: "Knowledge", icon: BookText },
  { id: "analytics", label: "Analytics", icon: BarChart3 },
  { id: "persona", label: "Persona & Skills", icon: Sparkles },
  { id: "schedules", label: "Schedules", icon: Calendar },
  { id: "triggers", label: "Triggers", icon: Zap },
  { id: "audit", label: "Audit", icon: ShieldAlert },
  { id: "settings", label: "Settings", icon: Cog },
  { id: "guide", label: "Guide", icon: BookOpen },
];

export function NavRail({ tab, onSelect, online, theme, onTheme, className, unavailable }: {
  tab: Tab;
  onSelect: (t: Tab) => void;
  online: boolean;
  theme: "dark" | "light";
  onTheme: () => void;
  className?: string;
  /** Tabs this install can't run, with the reason (shown greyed out, not removed). */
  unavailable?: Partial<Record<Tab, string>>;
}) {
  return (
    <nav className={`rail ${className ?? ""}`}>
      <div className="brand" title="Xplogent">X</div>
      {TABS.map(({ id, label, icon: Icon }) => (
        <button key={id} className={`rail-btn ${tab === id ? "active" : ""}`}
                aria-label={label} onClick={() => onSelect(id)}
                title={unavailable?.[id] ? `${label}: ${unavailable[id]}` : undefined}
                style={unavailable?.[id] ? { opacity: 0.4 } : undefined}>
          <Icon size={20} />
          <span className="lbl">{label}</span>
        </button>
      ))}
      <div className="spacer" />
      <button className="rail-btn" aria-label="Toggle theme" onClick={onTheme}>
        {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        <span className="lbl">Theme</span>
      </button>
      <button className="rail-btn" aria-label="Connection status" title={online ? "Connected" : "Offline"}>
        <span className={`health-dot ${online ? "ok" : "bad"}`} />
      </button>
    </nav>
  );
}
