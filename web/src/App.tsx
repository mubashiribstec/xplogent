import { useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { NavRail, TABS, Tab } from "./components/NavRail";
import { CommandPalette } from "./components/CommandPalette";
import { Onboarding } from "./components/Onboarding";
import { Login } from "./components/Login";
import { useIsMobile } from "./hooks/useMediaQuery";
import { UNAUTHORIZED_EVENT, checkAuth, getSessions, getStatus, setToken } from "./api";
import { Chat } from "./views/Chat";
import { Runs } from "./views/Runs";
import { Persona } from "./views/Persona";
import { Knowledge } from "./views/Knowledge";
import { Analytics } from "./views/Analytics";
import { Evals } from "./views/Evals";
import { Audit } from "./views/Audit";
import { Graph } from "./views/Graph";
import { Workflows } from "./views/Workflows";
import { Operator } from "./views/Operator";
import { Browser } from "./views/Browser";
import { Calls } from "./views/Calls";
import { Contacts } from "./views/Contacts";
import { Triggers } from "./views/Triggers";
import { Council } from "./Council";
import { MissionControl } from "./MissionControl";
import { Schedules } from "./Schedules";
import { Settings } from "./Settings";
import { Guide } from "./Guide";

const initialTheme = (): "dark" | "light" => {
  const t = localStorage.getItem("xplogent_theme");
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
};

const NO_SCREEN = "Needs a screen; not available on this server";

export function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [theme, setTheme] = useState<"dark" | "light">(initialTheme);
  const [online, setOnline] = useState(true);
  const [caps, setCaps] = useState<{ display: boolean; docker: boolean; git: boolean } | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [onboard, setOnboard] = useState(false);
  // null = still checking: render nothing that calls the API until we know (calls made
  // without the token got 401s and crashed the first-run check).
  const [authed, setAuthed] = useState<boolean | null>(null);
  const isMobile = useIsMobile();

  // Gate the app behind the access token when the backend requires one.
  useEffect(() => {
    checkAuth().then((r) => setAuthed(!r.required || r.ok)).catch(() => setAuthed(true));
    // A 401 later on (token rotated / auth switched on): back to the login screen.
    const onUnauthorized = () => { setToken(""); setAuthed(false); };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  // Health polling drives the rail status dot.
  useEffect(() => {
    if (!authed) return;
    const poll = () => getStatus()
      .then((st) => { setOnline(true); if (st?.capabilities) setCaps(st.capabilities); })
      .catch(() => setOnline(false));
    poll();
    const id = setInterval(poll, 15000);
    return () => clearInterval(id);
  }, [authed]);

  // First-run onboarding: no API key set and no sessions yet.
  useEffect(() => {
    if (!authed) return;
    try { if (localStorage.getItem("xplogent_onboarded")) return; } catch { return; }
    Promise.all([getStatus().catch(() => null), getSessions().catch(() => ({ sessions: [] }))])
      .then(([st, ss]) => {
        const anyKey = Object.values(st?.secrets ?? {}).some(Boolean);
        const usingLocal = st?.model?.startsWith("claude-cli") || st?.model?.startsWith("ollama");
        if (!anyKey && !usingLocal && (ss.sessions?.length ?? 0) === 0) setOnboard(true);
      });
  }, [authed]);

  // Cmd/Ctrl-K command palette.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen((o) => !o); }
      if (e.key === "Escape") setPaletteOpen(false);
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("xplogent_theme", next);
    document.documentElement.setAttribute("data-theme", next);
  };

  const go = (t: Tab) => { setTab(t); setNavOpen(false); };

  if (authed === null) return null;  // the boot "Loading…" is replaced by an empty frame briefly
  if (!authed) return <Login onAuthed={() => { setAuthed(true); location.reload(); }} />;

  return (
    <div className="shell">
      <NavRail tab={tab} onSelect={go} online={online} theme={theme} onTheme={toggleTheme}
               unavailable={caps && !caps.display ? { operator: NO_SCREEN } : undefined}
               className={navOpen ? "open" : ""} />
      {isMobile && navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

      <div className="main">
        {isMobile && (
          <div className="topbar">
            <button className="icon-btn" aria-label="menu" onClick={() => setNavOpen(true)}><Menu size={20} /></button>
            <div className="brand">X</div>
            <b>{TABS.find((t) => t.id === tab)?.label}</b>
          </div>
        )}
        {tab === "chat" && <Chat />}
        {tab === "council" && <Council />}
        {tab === "mission" && <MissionControl />}
        {tab === "operator" && (caps && !caps.display
          ? <div className="pane"><div className="pane-wide"><div className="card">
              <h3>Operator isn't available on this server</h3>
              <p className="dim">{NO_SCREEN}. The Operator drives a real desktop (screenshots, mouse,
                keyboard); a Docker server has no screen. Install Xplogent on a desktop computer to use it.</p>
            </div></div></div>
          : <Operator />)}
        {tab === "browser" && <Browser />}
        {tab === "calls" && <Calls />}
        {tab === "contacts" && <Contacts />}
        {tab === "runs" && <Runs />}
        {tab === "knowledge" && <Knowledge />}
        {tab === "graph" && <Graph />}
        {tab === "workflows" && <Workflows />}
        {tab === "analytics" && <Analytics />}
        {tab === "evals" && <Evals />}
        {tab === "persona" && <Persona />}
        {tab === "schedules" && <Schedules />}
        {tab === "triggers" && <Triggers />}
        {tab === "audit" && <Audit />}
        {tab === "settings" && <Settings />}
        {tab === "guide" && <Guide />}
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
        onTab={go} onNewChat={() => { localStorage.removeItem("xplogent_session"); go("chat"); }} />
      {onboard && <Onboarding onDone={() => setOnboard(false)} />}
    </div>
  );
}
