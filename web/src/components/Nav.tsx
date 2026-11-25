import type { Branding } from "../api";
import {
  BarChart3, BookOpen, BookText, ChevronsLeft, ChevronsRight, Contact, FileSpreadsheet, GraduationCap, type LucideIcon, Megaphone,
  MessageSquare, MoreHorizontal, PhoneCall, Settings as Cog, ShieldAlert, Sparkles,
} from "lucide-react";

export type Tab = "chat" | "calls" | "cdr" | "campaigns" | "contacts" | "training" | "knowledge" | "analytics" | "persona" | "audit" | "settings" | "guide";

export interface TabInfo { id: Tab; label: string; icon: LucideIcon; group: string }
export const TABS: TabInfo[] = [
  { id: "calls", label: "Calls", icon: PhoneCall, group: "Phone agent" },
  { id: "cdr", label: "CDR Report", icon: FileSpreadsheet, group: "Phone agent" },
  { id: "campaigns", label: "Campaigns", icon: Megaphone, group: "Phone agent" },
  { id: "contacts", label: "Contacts", icon: Contact, group: "Phone agent" },
  { id: "training", label: "Call training", icon: GraduationCap, group: "Phone agent" },
  { id: "chat", label: "Chat", icon: MessageSquare, group: "Assistant" },
  { id: "knowledge", label: "Knowledge", icon: BookText, group: "Assistant" },
  { id: "persona", label: "Persona & Skills", icon: Sparkles, group: "Assistant" },
  { id: "settings", label: "Settings", icon: Cog, group: "Admin" },
  { id: "analytics", label: "Usage & costs", icon: BarChart3, group: "Admin" },
  { id: "audit", label: "Audit", icon: ShieldAlert, group: "Admin" },
  { id: "guide", label: "Guide", icon: BookOpen, group: "Admin" },
];
export const GROUPS = Array.from(new Set(TABS.map((t) => t.group)));
export const tabInfo = (t: Tab) => TABS.find((x) => x.id === t)!;
export const isTab = (s: string | undefined): s is Tab => TABS.some((t) => t.id === s);

/** The customer's logo when they uploaded one, else their initial (or "X"). */
export function BrandMark({ brand }: { brand: Branding | null }) {
  const name = brand?.company || "Xplogent";
  return brand?.logo
    ? <div className="brand logo"><img src={`/branding/logo?v=${brand.logo_version}`} alt={name} /></div>
    : <div className="brand" aria-hidden>{name.slice(0, 1).toUpperCase()}</div>;
}

/** The labelled, grouped side navigation (desktop / laptop; icons only when collapsed or on tablets). */
export function SideNav({ tab, onSelect, brand, collapsed, onToggleCollapse, inDrawer, liveCalls }: {
  tab: Tab; onSelect: (t: Tab) => void; brand: Branding | null; collapsed?: boolean;
  onToggleCollapse?: () => void; inDrawer?: boolean; liveCalls?: number;
}) {
  return (
    <nav className={`sidenav ${collapsed ? "collapsed" : ""} ${inDrawer ? "in-drawer" : ""}`} aria-label="Main">
      <div className="sidenav-head">
        <BrandMark brand={brand} />
        {!collapsed && <span className="brand-name">{brand?.company || "Xplogent"}</span>}
      </div>
      <div className="nav-scroll">
        {GROUPS.map((g) => (
          <div className="nav-group" key={g} role="group" aria-label={g}>
            <div className="nav-group-label" aria-hidden>{g}</div>
            {TABS.filter((t) => t.group === g).map(({ id, label, icon: Icon }) => (
              <button key={id} type="button" className="nav-item" aria-current={tab === id ? "page" : undefined}
                title={collapsed ? label : undefined} aria-label={collapsed ? label : undefined} onClick={() => onSelect(id)}>
                <Icon size={18} aria-hidden />
                <span className="nav-label">{label}</span>
                {id === "calls" && !!liveCalls && <span className="nav-count" aria-label={`${liveCalls} live`}>{liveCalls}</span>}
              </button>
            ))}
          </div>
        ))}
      </div>
      {onToggleCollapse && (
        <div className="sidenav-foot">
          <button type="button" className="nav-item" onClick={onToggleCollapse}
            aria-label={collapsed ? "Expand the menu" : "Collapse the menu"} title={collapsed ? "Expand the menu" : undefined}>
            {collapsed ? <ChevronsRight size={18} aria-hidden /> : <ChevronsLeft size={18} aria-hidden />}
            <span className="nav-label">Collapse</span>
          </button>
        </div>
      )}
    </nav>
  );
}

const BOTTOM: Tab[] = ["calls", "campaigns", "contacts", "chat"];

/** Phones: the four main pages, plus "More" (the full menu). */
export function BottomNav({ tab, onSelect, onMore }: { tab: Tab; onSelect: (t: Tab) => void; onMore: () => void }) {
  return (
    <nav className="bottomnav" aria-label="Main pages">
      {BOTTOM.map((id) => {
        const { label, icon: Icon } = tabInfo(id);
        return (
          <button key={id} type="button" aria-current={tab === id ? "page" : undefined} onClick={() => onSelect(id)}>
            <Icon size={20} aria-hidden /><span>{label}</span>
          </button>
        );
      })}
      <button type="button" onClick={onMore} aria-current={BOTTOM.includes(tab) ? undefined : "page"}>
        <MoreHorizontal size={20} aria-hidden /><span>More</span>
      </button>
    </nav>
  );
}
