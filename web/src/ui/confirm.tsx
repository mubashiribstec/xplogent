// In-app confirm / prompt dialogs (instead of the browser's confirm() and prompt()).
//   const confirm = useConfirm();  if (!(await confirm({ title, body, danger: true }))) return;
//   const prompt = usePrompt();    const name = await prompt({ title, label, initial });  // null = cancelled
import { createContext, ReactNode, useCallback, useContext, useRef, useState } from "react";
import { Button, Dialog } from ".";

interface ConfirmOpts { title: string; body?: ReactNode; confirmLabel?: string; cancelLabel?: string; danger?: boolean }
interface PromptOpts { title: string; label: string; initial?: string; placeholder?: string; confirmLabel?: string; body?: ReactNode }
type Pending =
  | ({ kind: "confirm"; resolve: (v: boolean) => void } & ConfirmOpts)
  | ({ kind: "prompt"; resolve: (v: string | null) => void } & PromptOpts);

const Ctx = createContext<{ confirm: (o: ConfirmOpts) => Promise<boolean>; prompt: (o: PromptOpts) => Promise<string | null> }>({
  confirm: async () => false,
  prompt: async () => null,
});

export const useConfirm = () => useContext(Ctx).confirm;
export const usePrompt = () => useContext(Ctx).prompt;

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const confirm = useCallback((o: ConfirmOpts) =>
    new Promise<boolean>((resolve) => setPending({ kind: "confirm", resolve, ...o })), []);
  const prompt = useCallback((o: PromptOpts) =>
    new Promise<string | null>((resolve) => setPending({ kind: "prompt", resolve, ...o })), []);
  const close = useCallback((ok: boolean) => {
    setPending((p) => {
      if (p?.kind === "confirm") p.resolve(ok);
      if (p?.kind === "prompt") p.resolve(ok ? (input.current?.value ?? "") : null);
      return null;
    });
  }, []);
  const cancel = useCallback(() => close(false), [close]);
  return (
    <Ctx.Provider value={{ confirm, prompt }}>
      {children}
      <Dialog open={!!pending} onClose={cancel} title={pending?.title}
        actions={pending && <>
          <Button onClick={cancel}>{(pending.kind === "confirm" && pending.cancelLabel) || "Cancel"}</Button>
          <Button variant={pending.kind === "confirm" && pending.danger ? "danger" : "primary"}
            className={pending.kind === "confirm" && pending.danger ? "solid" : undefined}
            onClick={() => close(true)} autoFocus={pending.kind === "confirm"}>
            {pending.confirmLabel || (pending.kind === "prompt" ? "Save" : "Confirm")}</Button>
        </>}>
        {pending?.body && <div className="dim" style={{ whiteSpace: "pre-line" }}>{pending.body}</div>}
        {pending?.kind === "prompt" && (
          <label className="fld" style={{ marginTop: 12 }}>
            <span className="fld-label">{pending.label}</span>
            <input ref={input} defaultValue={pending.initial ?? ""} placeholder={pending.placeholder}
              onKeyDown={(e) => { if (e.key === "Enter") close(true); }} />
          </label>
        )}
      </Dialog>
    </Ctx.Provider>
  );
}
