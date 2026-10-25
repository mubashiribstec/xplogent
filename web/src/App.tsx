import { useEffect, useState } from "react";
import { Menu } from "lucide-react";
import { BrandMark, NavRail, TABS, Tab } from "./components/NavRail";
import { CommandPalette } from "./components/CommandPalette";
import { Onboarding } from "./components/Onboarding";
import { Login } from "./components/Login";
import { TOUR_EVENT, Tour } from "./components/Tour";
import { BRANDING_EVENT, applyAccent } from "./lib/brand";
import { useIsMobile } from "./hooks/useMediaQuery";
import { UNAUTHORIZED_EVENT, checkAuth, getBranding, getFullConfig, getSessions, getStatus, setToken, setTourDone,
  type Branding, type StatusInfo } from "./api";
import { Chat } from "./views/Chat";
import { Persona } from "./views/Persona";
import { Knowledge } from "./views/Knowledge";
import { Analytics } from "./views/Analytics";
import { Audit } from "./views/Audit";
import { Calls } from "./views/Calls";
import { Contacts } from "./views/Contacts";
import { Training } from "./views/Training";
import { Settings } from "./Settings";
import { Guide } from "./Guide";

const initialTheme = (): "dark" | "light" => {
  const t = localStorage.getItem("xplogent_theme");
  if (t === "light" || t === "dark") return t;
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
};


export function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [theme, setTheme] = useState<"dark" | "light">(initialTheme);
  const [online, setOnline] = useState(true);
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [bannerOff, setBannerOff] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [onboard, setOnboard] = useState(false);
  // null = still checking: render nothing that calls the API until we know (calls made
  // without the token got 401s and crashed the first-run check).
  const [authed, setAuthed] = useState<boolean | null>(null);
  const isMobile = useIsMobile();
  const [brand, setBrand] = useState<Branding | null>(null);
  const [tourOpen, setTourOpen] = useState(false);
  const [tourDone, setTourDoneState] = useState<boolean | null>(null);

  // Branding (public: the login page shows it too); the accent follows the theme.
  useEffect(() => {
    const load = () => getBranding().then(setBrand).catch(() => {});
    load();
    window.addEventListener(BRANDING_EVENT, load);
    return () => window.removeEventListener(BRANDING_EVENT, load);
  }, []);
  useEffect(() => { applyAccent(brand?.accent ?? "", theme); }, [brand?.accent, theme]);

  // The product tour: once, after first-run onboarding; restartable from Guide / the palette.
  useEffect(() => {
    if (!authed) return;
    getFullConfig().then((c) => setTourDoneState(!!c.ui?.tour_done)).catch(() => setTourDoneState(true));
    const start = () => setTourOpen(true);
    window.addEventListener(TOUR_EVENT, start);
    return () => window.removeEventListener(TOUR_EVENT, start);
  }, [authed]);
  useEffect(() => {
    if (tourDone === false && !onboard) setTourOpen(true);
  }, [tourDone, onboard]);
  const closeTour = () => {
    setTourOpen(false); setTourDoneState(true);
    setTourDone(true).catch(() => {});
  };

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
      .then((st) => { setOnline(true); setStatus(st); })
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
        const usingLocal = st?.model?.startsWith("ollama");
        const noSessions = (ss.sessions?.length ?? 0) === 0;
        // Also onboard when the (default) local model can't answer: the wizard is
        // the flow that tests the connection and pulls the model — without this a
        // fresh install just lands in a chat whose every message fails.
        const localDown = usingLocal && st?.ollama != null && !st.ollama.reachable;
        if (noSessions && ((!anyKey && !usingLocal) || localDown)) setOnboard(true);
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

  if (authed === null) return <div className="overlay center"><p className="dim">Loading…</p></div>;
  if (!authed) return <Login brand={brand} onAuthed={() => { setAuthed(true); location.reload(); }} />;

  return (
    <div className="shell">
      <NavRail tab={tab} onSelect={go} online={online} theme={theme} onTheme={toggleTheme}
               className={navOpen ? "open" : ""} brand={brand} />
      {isMobile && navOpen && <div className="scrim" onClick={() => setNavOpen(false)} />}

      <div className="main">
        {isMobile && (
          <div className="topbar">
            <button className="icon-btn" aria-label="menu" onClick={() => setNavOpen(true)}><Menu size={20} /></button>
            <BrandMark brand={brand} />
            <b>{TABS.find((t) => t.id === tab)?.label}</b>
          </div>
        )}
        {tab === "chat" && !bannerOff && status?.model?.startsWith("ollama")
          && status.ollama && !status.ollama.reachable && (
          <div className="warn-banner" style={{ margin: "12px 16px 0" }}>
            ⚠ Your local model isn't answering ({status.ollama.host}). Chat will fail until
            Ollama is running and the model is pulled — start Ollama, or{" "}
            <button className="linklike" onClick={() => go("settings")}>switch provider in Settings</button>.
            Running <code>xplogent doctor</code> in a terminal pinpoints it.{" "}
            <button className="linklike" onClick={() => setBannerOff(true)}>Dismiss</button>
          </div>
        )}
        {tab === "chat" && <Chat />}
        {tab === "calls" && <Calls />}
        {tab === "contacts" && <Contacts />}
        {tab === "training" && <Training />}
        {tab === "knowledge" && <Knowledge />}
        {tab === "analytics" && <Analytics />}
        {tab === "persona" && <Persona />}
        {tab === "audit" && <Audit />}
        {tab === "settings" && <Settings />}
        {tab === "guide" && <Guide />}
      </div>

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)}
        onTab={go} onNewChat={() => { localStorage.removeItem("xplogent_session"); go("chat"); }}
        onTour={() => setTourOpen(true)} />
      {onboard && <Onboarding onDone={() => setOnboard(false)} />}
      <Tour open={tourOpen && !onboard} onTab={go} onClose={closeTour} />
    </div>
  );
}
