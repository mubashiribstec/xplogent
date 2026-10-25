import { useEffect, useLayoutEffect, useState } from "react";
import type { Tab } from "./NavRail";

export const TOUR_EVENT = "xplogent:tour";
export const startTour = () => window.dispatchEvent(new Event(TOUR_EVENT));

interface Step { tab: Tab; target: string; title: string; text: string; }
const STEPS: Step[] = [
  { tab: "calls", target: "golive", title: "Is my agent live?",
    text: "This card shows what is set up and what is left, and the health of every phone line, the voices and the CRM, in green, amber or red." },
  { tab: "calls", target: "calls-list", title: "Every call, with its transcript",
    text: "Open a call to read what was said, play the recording, see what was sent to your CRM, and debug it if something went wrong." },
  { tab: "calls", target: "test-call", title: "Try it yourself",
    text: "Talk to your agent in the browser exactly like a caller would: same greeting, same knowledge, same tools. No phone line needed." },
  { tab: "chat", target: "chat", title: "Chat with your assistant",
    text: "Ask questions, draft messages, or try out answers before your callers hear them." },
  { tab: "training", target: "training-upload", title: "Teach it from your real calls",
    text: "Upload call recordings, scripts, FAQs or a Q&A sheet. The agent answers only from what you give it." },
  { tab: "calls", target: "crm", title: "Your CRM, connected",
    text: "Every finished call is sent to your CRM, and the agent looks callers up before it answers, so it can greet them by name." },
  { tab: "calls", target: "setup", title: "Setup",
    text: "Phone lines, your company name and branding, the hand-off number and the voices all live here. You can restart this tour from the Guide." },
];

type Box = { top: number; left: number; width: number; height: number } | null;

/** A small guided tour: highlight + tooltip + Next / Back / Skip. Never blocks the app. */
export function Tour({ open, onTab, onClose }: { open: boolean; onTab: (t: Tab) => void; onClose: (completed: boolean) => void }) {
  const [i, setI] = useState(0);
  const [box, setBox] = useState<Box>(null);
  const step = STEPS[i];

  useEffect(() => { if (open) setI(0); }, [open]);
  useEffect(() => { if (open) onTab(step.tab); /* eslint-disable-next-line */ }, [open, i]);

  useLayoutEffect(() => {
    if (!open) return;
    let tries = 0;
    let raf = 0;
    const find = () => {
      const el = document.querySelector(`[data-tour="${step.target}"]`) as HTMLElement | null;
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "auto" });
        const r = el.getBoundingClientRect();
        setBox({ top: r.top - 6, left: r.left - 6, width: r.width + 12, height: Math.min(r.height + 12, window.innerHeight - 24) });
      } else if (tries++ < 40) {
        raf = window.setTimeout(find, 50);
      } else {
        setBox(null);                                   // not on this install: tooltip only
      }
    };
    find();
    const onResize = () => { tries = 0; find(); };
    window.addEventListener("resize", onResize);
    return () => { window.clearTimeout(raf); window.removeEventListener("resize", onResize); };
  }, [open, i, step.target]);

  useEffect(() => {
    if (!open) return;
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(false);
      if (e.key === "ArrowRight") setI((n) => Math.min(n + 1, STEPS.length - 1));
      if (e.key === "ArrowLeft") setI((n) => Math.max(n - 1, 0));
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [open, onClose]);

  if (!open) return null;
  // Keep the tooltip on-screen: below the target if there's room, else above, else centred.
  const W = Math.min(340, window.innerWidth - 24);
  const H = 190;
  let top = window.innerHeight / 2 - H / 2;
  let left = window.innerWidth / 2 - W / 2;
  if (box) {
    left = Math.max(12, Math.min(box.left, window.innerWidth - W - 12));
    if (box.top + box.height + 12 + H < window.innerHeight) top = box.top + box.height + 12;
    else if (box.top - H - 12 > 0) top = box.top - H - 12;
    else top = Math.max(12, window.innerHeight - H - 12);
  }
  const last = i === STEPS.length - 1;
  return (
    <>
      {box && <div className="tour-hole" style={box} />}
      <div className="tour-tip" role="dialog" aria-label={step.title} style={{ top, left, width: W }}>
        <h4>{step.title}</h4>
        <p>{step.text}</p>
        <div className="tour-actions">
          <span className="grow">{i + 1} of {STEPS.length}</span>
          <button className="btn ghost sm" onClick={() => onClose(false)}>Skip</button>
          {i > 0 && <button className="btn sm" onClick={() => setI(i - 1)}>Back</button>}
          <button className="btn primary sm" onClick={() => (last ? onClose(true) : setI(i + 1))}>{last ? "Done" : "Next"}</button>
        </div>
      </div>
    </>
  );
}
