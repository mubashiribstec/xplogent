import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Menu, Monitor, Moon, Search, Sun } from "lucide-react";
import { BottomNav, SideNav, TABS, Tab, isTab, tabInfo } from "./components/Nav";
import { CommandPalette } from "./components/CommandPalette";
import { Onboarding } from "./components/Onboarding";
import { Login } from "./components/Login";
import { TOUR_EVENT, Tour } from "./components/Tour";
import { BRANDING_EVENT, applyAccent } from "./lib/brand";
import { useMediaQuery } from "./hooks/useMediaQuery";
import { useRoute } from "./lib/router";
import { type ThemeMode, useTheme } from "./lib/theme";
import { UNAUTHORIZED_EVENT, checkAuth, getBranding, getFullConfig, getSessions, getStatus, getVoipStatus, setToken, setTourDone,
  type Branding, type StatusInfo } from "./api";
import { IconButton } from "./ui";

// Pages load on first visit, so the first screen downloads less.
const Chat = lazy(() => import("./views/Chat").then((m) => ({ default: m.Chat })));
const Calls = lazy(() => import("./views/Calls").then((m) => ({ default: m.Calls })));
const Campaigns = lazy(() => import("./views/Campaigns").then((m) => ({ default: m.Campaigns })));
const Contacts = lazy(() => import("./views/Contacts").then((m) => ({ default: m.Contacts })));
const Training = lazy(() => import("./views/Training").then((m) => ({ default: m.Training })));
const Knowledge = lazy(() => import("./views/Knowledge").then((m) => ({ default: m.Knowledge })));
const Analytics = lazy(() => import("./views/Analytics").then((m) => ({ default: m.Analytics })));
const Persona = lazy(() => import("./views/Persona").then((m) => ({ default: m.Persona })));
const Audit = lazy(() => import("./views/Audit").then((m) => ({ default: m.Audit })));
const Settings = lazy(() => import("./Settings").then((m) => ({ default: m.Settings })));
const Guide = lazy(() => import("./Guide").then((m) => ({ default: m.Guide })));

const PAGES: Record<Tab, React.ComponentType> = {
  chat: Chat, calls: Calls, campaigns: Campaigns, contacts: Contacts, training: Training, knowledge: Knowledge,
  analytics: Analytics, persona: Persona, audit: Audit, settings: Settings, guide: Guide,
};

const NEXT_MODE: Record<ThemeMode, ThemeMode> = { auto: "light", light: "dark", dark: "auto" };
const MODE_ICON = { auto: Monitor, light: Sun, dark: Moon };
const MODE_LABEL = { auto: "Theme: follows your device", light: "Theme: light", dark: "Theme: dark" };

const readCollapsed = () => { try { return localStorage.getItem("xplogent_nav") === "collapsed"; } catch { return false; } };

export function App() {
  const [route, go] = useRoute();
  const tab: Tab = isTab(route[0]) ? route[0] : "calls";
  const { mode, resolved, setMode } = useTheme();
  const [online, setOnline] = useState(true);
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [live, setLive] = useState(0);
  const [bannerOff, setBannerOff] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [onboard, setOnboard] = useState(false);
  // null = still checking: render nothing that calls the API until we know.
  const [authed, setAuthed] = useState<boolean | null>(null);
  const isPhone = useMediaQuery("(max-width: 639px)");
  const isTablet = useMediaQuery("(min-width: 640px) and (max-width: 1023px)");
  const [brand, setBrand] = useState<Branding | null>(null);
  const [tourOpen, setTourOpen] = useState(false);
  const [tourDone, setTourDoneState] = useState<boolean | null>(null);

  // No page in the URL yet: start on Calls (the phone agent is the product).
  useEffect(() => { if (!isTab(route[0])) go("calls", true); }, [route, go]);
  useEffect(() => { setDrawer(false); }, [route]);
  useEffect(() => { document.title = `${tabInfo(tab).label} · ${brand?.company || "Xplogent"}`; }, [tab, brand?.company]);

  // Branding (public: the login page shows it too); the accent follows the theme.
  useEffect(() => {
    const load = () => getBranding().then(setBrand).catch(() => {});
    load();
    window.addEventListener(BRANDING_EVENT, load);
    return () => window.removeEventListener(BRANDING_EVENT, load);
  }, []);
  useEffect(() => { applyAccent(brand?.accent ?? "", resolved); }, [brand?.accent, resolved]);

  // The product tour: once, after first-run onboarding; restartable from Guide / the palette.
  useEffect(() => {
    if (!authed) return;
    getFullConfig().then((c) => setTourDoneState(!!c.ui?.tour_done)).catch(() => setTourDoneState(true));
    const start = () => setTourOpen(true);
    window.addEventListener(TOUR_EVENT, start);
    return () => window.removeEventListener(TOUR_EVENT, start);
  }, [authed]);
  useEffect(() => { if (tourDone === false && !onboard) setTourOpen(true); }, [tourDone, onboard]);
  const closeTour = () => { setTourOpen(false); setTourDoneState(true); setTourDone(true).catch(() => {}); };

  // Gate the app behind the access token when the backend requires one.
  useEffect(() => {
    checkAuth().then((r) => setAuthed(!r.required || r.ok)).catch(() => setAuthed(true));
    const onUnauthorized = () => { setToken(""); setAuthed(false); };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  // Health for the status pill: every 15 s while the tab is visible.
  useEffect(() => {
    if (!authed) return;
    const poll = () => {
      if (document.hidden) return;
      getStatus().then((st) => { setOnline(true); setStatus(st); }).catch(() => setOnline(false));
      getVoipStatus().then((v) => setLive(v.active?.length ?? 0)).catch(() => {});
    };
    poll();
    const id = setInterval(poll, 15000);
    document.addEventListener("visibilitychange", poll);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", poll); };
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
        // Also when the (default) local model can't answer: the wizard tests it and pulls the model.
        const localDown = usingLocal && st?.ollama != null && !st.ollama.reachable;
        if (noSessions && ((!anyKey && !usingLocal) || localDown)) setOnboard(true);
      });
  }, [authed]);

  // Cmd/Ctrl-K: the command palette.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen((o) => !o); }
      if (e.key === "Escape") { setPaletteOpen(false); setDrawer(false); }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  const select = useCallback((t: Tab) => { go(t); setDrawer(false); }, [go]);
  const toggleCollapsed = () => setCollapsed((c) => {
    try { localStorage.setItem("xplogent_nav", c ? "open" : "collapsed"); } catch { /* ignore */ }
    return !c;
  });

  if (authed === null) return <div className="overlay center"><p className="dim">Loading…</p></div>;
  if (!authed) return <Login brand={brand} onAuthed={() => { setAuthed(true); location.reload(); }} />;

  const Page = PAGES[tab];
  const info = tabInfo(tab);
  const ModeIcon = MODE_ICON[mode];

  return (
    <div className="app">
      <a href="#main" className="sr-only" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>
        Skip to content</a>
      {!isPhone && <SideNav tab={tab} onSelect={select} brand={brand} collapsed={isTablet || collapsed}
        onToggleCollapse={isTablet ? undefined : toggleCollapsed} liveCalls={live} />}
      {drawer && <>
        <div className="scrim" onClick={() => setDrawer(false)} />
        <div className="drawer"><SideNav tab={tab} onSelect={select} brand={brand} inDrawer liveCalls={live} /></div>
      </>}

      <div className="app-main">
        <header className="topbar">
          {(isPhone || isTablet) && <IconButton icon={Menu} label="Open the menu" onClick={() => setDrawer(true)} />}
          <div className="topbar-title">
            <span className="crumb">{info.group} /</span><span className="here">{info.label}</span>
          </div>
          <div className="topbar-actions">
            <button type="button" className="search-btn" onClick={() => setPaletteOpen(true)} aria-label="Search pages (Ctrl+K)">
              <Search size={15} aria-hidden /><span className="search-label">Search…</span><kbd>Ctrl K</kbd>
            </button>
            <IconButton icon={ModeIcon} label={`${MODE_LABEL[mode]} (click to change)`} onClick={() => setMode(NEXT_MODE[mode])} />
            <span className="status-pill" role="status" title={online ? `Connected · model ${status?.model ?? ""}` : "The server is not answering"}>
              <span className={`status-dot ${online ? "ok" : "bad"}`} aria-hidden />
              <span className="status-text">{online ? (live ? `${live} live call${live > 1 ? "s" : ""}` : "Connected") : "Offline"}</span>
            </span>
          </div>
        </header>

        {tab === "chat" && !bannerOff && status?.model?.startsWith("ollama") && status.ollama && !status.ollama.reachable && (
          <div className="warn-banner" style={{ margin: "12px 16px 0" }} role="alert">
            <span>Your local model isn't answering ({status.ollama.host}). Chat will fail until Ollama is running and the
              model is pulled. Start Ollama, or{" "}
              <button className="linklike" onClick={() => go("settings/models")}>switch provider in Settings</button>.
              {" "}Running <code>xplogent doctor</code> on the server pinpoints it.</span>
            <button className="linklike" onClick={() => setBannerOff(true)}>Dismiss</button>
          </div>
        )}
        <main id="main" className="page" tabIndex={-1} style={{ outline: "none" }}>
          <Suspense fallback={<div className="page-loading"><span className="spinner" aria-label="Loading" /></div>}>
            <Page key={tab} />
          </Suspense>
        </main>
      </div>

      {isPhone && <BottomNav tab={tab} onSelect={select} onMore={() => setDrawer(true)} />}

      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} onGo={(p) => go(p)}
        onNewChat={() => { try { localStorage.removeItem("xplogent_session"); } catch { /* ignore */ } go("chat"); }}
        onTour={() => setTourOpen(true)} />
      {onboard && <Onboarding onDone={() => setOnboard(false)} />}
      <Tour open={tourOpen && !onboard} onGo={(p) => go(p)} onClose={closeTour} />
    </div>
  );
}

export { TABS };
