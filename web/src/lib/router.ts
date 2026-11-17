// Hash routes ("#/calls/speech") so reloads, links and the back button work without
// any server-side routing. useRoute() gives the path segments and a navigate function.
import { useCallback, useEffect, useState } from "react";

const parse = () => location.hash.replace(/^#\/?/, "").split("?")[0].split("/").filter(Boolean).map(decodeURIComponent);

export function navigate(path: string, replace = false) {
  const hash = "#/" + path.replace(/^[#/]+/, "");
  if (location.hash === hash) return;
  if (replace) history.replaceState(null, "", hash);
  else location.hash = hash;
  if (replace) window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function useRoute(): [string[], (path: string, replace?: boolean) => void] {
  const [parts, setParts] = useState<string[]>(parse);
  useEffect(() => {
    const on = () => setParts(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return [parts, useCallback((p: string, r?: boolean) => navigate(p, r), [])];
}

/** A page's sub-section from the URL (e.g. #/calls/speech → "speech"), with a default. */
export function useSubRoute<T extends string>(page: string, allowed: readonly T[], fallback: T): [T, (s: T) => void] {
  const [parts, go] = useRoute();
  const sub = (parts[0] === page && allowed.includes(parts[1] as T) ? parts[1] : fallback) as T;
  return [sub, useCallback((s: T) => go(`${page}/${s}`), [go, page])];
}
