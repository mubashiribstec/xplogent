import { createContext, ReactNode, useCallback, useContext, useState } from "react";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";

type Kind = "info" | "success" | "error";
interface Toast { id: number; kind: Kind; msg: string; }

const ToastCtx = createContext<(msg: string, kind?: Kind) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

const ICON = { info: Info, success: CheckCircle2, error: XCircle };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => setItems((x) => x.filter((t) => t.id !== id)), []);
  const push = useCallback((msg: string, kind: Kind = "info") => {
    const id = Date.now() + Math.random();
    // The same message twice in a row (e.g. two quick saves) shows once.
    setItems((x) => [...x.filter((t) => t.msg !== msg), { id, kind, msg }].slice(-4));
    setTimeout(() => dismiss(id), kind === "error" ? 8000 : 4200);
  }, [dismiss]);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map((t) => {
          const I = ICON[t.kind];
          return (
            <div key={t.id} className={`toast-item ${t.kind}`} role={t.kind === "error" ? "alert" : undefined}>
              <I size={17} aria-hidden /> <span className="toast-msg">{t.msg}</span>
              <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => dismiss(t.id)}><X size={14} /></button>
            </div>
          );
        })}
      </div>
    </ToastCtx.Provider>
  );
}
