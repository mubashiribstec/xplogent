// One theme setting for the whole dashboard: "auto" (follow the device), "light" or "dark".
// Every useTheme() shares it, so the top bar and Settings → Appearance always agree.
import { useEffect, useState } from "react";

export type ThemeMode = "auto" | "light" | "dark";
const KEY = "xplogent_theme";
const EVENT = "xplogent:theme";

export function readMode(): ThemeMode {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : "auto";
  } catch { return "auto"; }
}

const systemLight = () => typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: light)").matches;
export const resolve = (m: ThemeMode): "light" | "dark" => (m === "auto" ? (systemLight() ? "light" : "dark") : m);

/** Applies a mode to <html> (no attribute = follow the device). */
export function applyMode(m: ThemeMode) {
  if (m === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", m);
}

export function setMode(m: ThemeMode) {
  try { if (m === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, m); } catch { /* private mode */ }
  applyMode(m);
  window.dispatchEvent(new CustomEvent(EVENT, { detail: m }));
}

export function useTheme(): { mode: ThemeMode; resolved: "light" | "dark"; setMode: (m: ThemeMode) => void } {
  const [mode, setLocal] = useState<ThemeMode>(readMode);
  const [resolved, setResolved] = useState(() => resolve(readMode()));
  useEffect(() => {
    const onTheme = (e: Event) => { const m = (e as CustomEvent<ThemeMode>).detail; setLocal(m); setResolved(resolve(m)); };
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onSystem = () => setResolved(resolve(readMode()));
    window.addEventListener(EVENT, onTheme);
    mq.addEventListener("change", onSystem);
    return () => { window.removeEventListener(EVENT, onTheme); mq.removeEventListener("change", onSystem); };
  }, []);
  return { mode, resolved, setMode };
}
