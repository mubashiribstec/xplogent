export type Saver = (u: Record<string, any>) => Promise<void>;
export type Toaster = (m: string, kind?: "info" | "success" | "error") => void;

export const fmtTime = (t?: number | null) => (t ? new Date(t * 1000).toLocaleString() : "—");
export const fmtDur = (c: { started_at: number; ended_at?: number | null }) => {
  const end = c.ended_at ?? Date.now() / 1000;
  const s = Math.max(0, Math.round(end - c.started_at));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
export const randomSecret = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(18))).map((b) => b.toString(16).padStart(2, "0")).join("");

/** setInterval that pauses while the browser tab is hidden (and runs once when it comes back). */
export function visibleInterval(fn: () => void, ms: number): () => void {
  const tick = () => { if (!document.hidden) fn(); };
  const id = setInterval(tick, ms);
  document.addEventListener("visibilitychange", tick);
  return () => { clearInterval(id); document.removeEventListener("visibilitychange", tick); };
}
