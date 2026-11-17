// The dashboard's shared components. Styles live in styles.css (one design system).
import {
  ButtonHTMLAttributes, KeyboardEvent, ReactNode, useEffect, useId, useRef, useState,
} from "react";
import { AlertTriangle, CheckCircle2, Info, type LucideIcon, X, XCircle } from "lucide-react";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

// ── Button ─────────────────────────────────────────────────────────────────
type Variant = "primary" | "secondary" | "ghost" | "danger";
export function Button({ variant = "secondary", size, loading, icon: Icon, block, className, children, disabled, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: Variant; size?: "sm"; loading?: boolean; icon?: LucideIcon; block?: boolean;
  }) {
  return (
    <button type="button" {...rest} disabled={disabled || loading} aria-busy={loading || undefined}
      className={cx("btn", variant !== "secondary" && variant, size, block && "block", className)}>
      {loading ? <span className="spin" aria-hidden /> : Icon ? <Icon size={size === "sm" ? 14 : 16} aria-hidden /> : null}
      {children}
    </button>
  );
}

export function IconButton({ label, icon: Icon, size = 18, className, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { label: string; icon: LucideIcon; size?: number }) {
  return (
    <button type="button" {...rest} className={cx("icon-btn", className)} aria-label={label} title={rest.title ?? label}>
      <Icon size={size} aria-hidden />
    </button>
  );
}

// ── Layout ─────────────────────────────────────────────────────────────────
export function PageHeader({ icon: Icon, title, description, actions }: {
  icon?: LucideIcon; title: string; description?: ReactNode; actions?: ReactNode;
}) {
  return (
    <>
      <div className="page-head">
        <h1>{Icon && <Icon size={22} aria-hidden />}{title}</h1>
        {actions && <div className="row wrap" style={{ gap: 8 }}>{actions}</div>}
      </div>
      {description && <p className="page-desc">{description}</p>}
    </>
  );
}

export function Card({ title, description, actions, footer, children, className, ...rest }: {
  title?: ReactNode; description?: ReactNode; actions?: ReactNode; footer?: ReactNode; children?: ReactNode;
  className?: string; id?: string; "data-tour"?: string;
}) {
  return (
    <section className={cx("card", className)} {...rest}>
      {(title || actions) && (
        <div className="card-head">
          <div className="grow">
            {title && <h3 className="card-title">{title}</h3>}
            {description && <p className="card-desc">{description}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </div>
      )}
      {children}
      {footer && <div className="card-foot">{footer}</div>}
    </section>
  );
}

// ── Forms ──────────────────────────────────────────────────────────────────
export function Field({ label, hint, error, optional, children, className, id }: {
  label: ReactNode; hint?: ReactNode; error?: ReactNode; optional?: boolean; children: (id: string) => ReactNode;
  className?: string; id?: string;
}) {
  const auto = useId();
  const fid = id ?? auto;
  return (
    <div className={cx("fld", className)}>
      <label className="fld-label" htmlFor={fid}>{label}{optional && <span className="opt">optional</span>}</label>
      {children(fid)}
      {hint && <p className="fld-hint">{hint}</p>}
      {error && <p className="fld-error" role="alert">{error}</p>}
    </div>
  );
}

/** A text input that saves when it loses focus (or on Enter), only if the value changed. */
export function TextField({ label, hint, value, onCommit, placeholder, type = "text", optional, className, inputMode, step, min }: {
  label: ReactNode; hint?: ReactNode; value: string | number | undefined; onCommit: (v: string) => void;
  placeholder?: string; type?: string; optional?: boolean; className?: string;
  inputMode?: "text" | "numeric" | "decimal" | "tel" | "url" | "email"; step?: string; min?: number;
}) {
  const initial = value === undefined || value === null ? "" : String(value);
  return (
    <Field label={label} hint={hint} optional={optional} className={className}>
      {(id) => (
        <input id={id} key={initial} type={type} defaultValue={initial} placeholder={placeholder} inputMode={inputMode}
          step={step} min={min}
          onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
          onBlur={(e) => { if (e.target.value !== initial) onCommit(e.target.value); }} />
      )}
    </Field>
  );
}

export function SelectField({ label, hint, value, onChange, options, className, disabled }: {
  label: ReactNode; hint?: ReactNode; value: string; onChange: (v: string) => void; className?: string; disabled?: boolean;
  options: { value: string; label: string; disabled?: boolean; group?: string }[];
}) {
  const groups = Array.from(new Set(options.map((o) => o.group ?? "")));
  const opt = (o: { value: string; label: string; disabled?: boolean }) =>
    <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>;
  return (
    <Field label={label} hint={hint} className={className}>
      {(id) => (
        <select id={id} value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
          {!options.some((o) => o.value === value) && <option value={value}>{value || "choose…"}</option>}
          {groups.length > 1
            ? groups.map((g) => <optgroup key={g} label={g || "Other"}>{options.filter((o) => (o.group ?? "") === g).map(opt)}</optgroup>)
            : options.map(opt)}
        </select>
      )}
    </Field>
  );
}

/** An API key field: never shows the stored value, says whether one is set, saves on blur / Enter. */
export function SecretField({ label, isSet, onSave, hint, placeholder }: {
  label: ReactNode; isSet: boolean; onSave: (v: string) => Promise<void> | void; hint?: ReactNode; placeholder?: string;
}) {
  const [busy, setBusy] = useState(false);
  const commit = async (el: HTMLInputElement) => {
    const v = el.value.trim();
    if (!v || busy) return;
    setBusy(true);
    try { await onSave(v); el.value = ""; } finally { setBusy(false); }
  };
  return (
    <Field label={<>{label} <span className={cx("secret-state", isSet ? "set" : "unset")}>{isSet ? "saved" : "not set"}</span></>} hint={hint}>
      {(id) => (
        <input id={id} type="password" autoComplete="off" spellCheck={false} disabled={busy}
          placeholder={placeholder ?? (isSet ? "•••••••• (type to replace)" : "paste the key")}
          onKeyDown={(e) => { if (e.key === "Enter") commit(e.target as HTMLInputElement); }}
          onBlur={(e) => commit(e.target)} />
      )}
    </Field>
  );
}

export function Switch({ checked, onChange, label, disabled }: {
  checked: boolean; onChange: (v: boolean) => void; label: ReactNode; disabled?: boolean;
}) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="track" aria-hidden />
      <span>{label}</span>
    </label>
  );
}

export function Segmented<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; options: { value: T; label: ReactNode; icon?: LucideIcon }[]; label: string;
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(({ value: v, label: l, icon: Icon }) => (
        <button key={v} type="button" aria-pressed={v === value} onClick={() => onChange(v)}>
          {Icon && <Icon size={14} aria-hidden />}{l}
        </button>
      ))}
    </div>
  );
}

// ── Choice cards (a radio group shown as cards) ────────────────────────────
export function ChoiceCards<T extends string>({ value, onChange, options, label }: {
  value: T; onChange: (v: T) => void; label: string;
  options: { value: T; title: ReactNode; description?: ReactNode; icon?: LucideIcon; badge?: ReactNode; disabled?: boolean }[];
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const move = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (i + d + options.length) % options.length;
    refs.current[n]?.focus();
    onChange(options[n].value);
  };
  return (
    <div className="choice-grid" role="radiogroup" aria-label={label}>
      {options.map((o, i) => {
        const on = o.value === value;
        const Icon = o.icon;
        return (
          <button key={o.value} ref={(el) => { refs.current[i] = el; }} type="button" role="radio" aria-checked={on}
            tabIndex={on || (!options.some((x) => x.value === value) && i === 0) ? 0 : -1} disabled={o.disabled}
            className="choice" onClick={() => onChange(o.value)} onKeyDown={(e) => move(e, i)}>
            {Icon && <span className="choice-icon"><Icon size={18} aria-hidden /></span>}
            <span className="grow">
              <span className="choice-title">{o.title}{o.badge}</span>
              {o.description && <span className="choice-desc" style={{ display: "block" }}>{o.description}</span>}
            </span>
            {on && <CheckCircle2 className="choice-tick" size={16} aria-hidden />}
          </button>
        );
      })}
    </div>
  );
}

// ── Status / feedback ──────────────────────────────────────────────────────
export function Badge({ tone, children, title }: { tone?: "ok" | "bad" | "warn" | "info" | "demo"; children: ReactNode; title?: string }) {
  return <span className={cx("badge", tone)} title={title}>{children}</span>;
}

const CALLOUT_ICON = { info: Info, ok: CheckCircle2, warn: AlertTriangle, error: XCircle };
export function Callout({ tone = "info", children, title }: { tone?: "info" | "ok" | "warn" | "error"; children: ReactNode; title?: ReactNode }) {
  const Icon = CALLOUT_ICON[tone];
  return (
    <div className={cx("callout", tone)} role={tone === "error" ? "alert" : undefined}>
      <Icon size={16} aria-hidden />
      <div className="grow">{title && <p><b>{title}</b></p>}{typeof children === "string" ? <p>{children}</p> : children}</div>
    </div>
  );
}

/** One line of test output: a tick or a cross, then the text. */
export function ResultLine({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <div className={cx("result-line", ok ? "ok" : "bad")} role="status">
      {ok ? <CheckCircle2 size={16} aria-hidden /> : <XCircle size={16} aria-hidden />}
      <div className="grow">{children}</div>
    </div>
  );
}

export function EmptyState({ icon: Icon, title, children, actions }: {
  icon?: LucideIcon; title: ReactNode; children?: ReactNode; actions?: ReactNode;
}) {
  return (
    <div className="empty-state">
      {Icon && <Icon size={28} aria-hidden />}
      <b>{title}</b>
      {children && <p>{children}</p>}
      {actions && <div className="row wrap">{actions}</div>}
    </div>
  );
}

// ── Tabs ───────────────────────────────────────────────────────────────────
export function Tabs<T extends string>({ value, onChange, tabs, label }: {
  value: T; onChange: (v: T) => void; label: string;
  tabs: { id: T; label: string; icon?: LucideIcon; badge?: ReactNode }[];
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const key = (e: KeyboardEvent, i: number) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const n = (i + d + tabs.length) % tabs.length;
    refs.current[n]?.focus();
    onChange(tabs[n].id);
  };
  useEffect(() => {
    const i = tabs.findIndex((t) => t.id === value);
    refs.current[i]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [value, tabs]);
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((t, i) => {
        const Icon = t.icon;
        return (
          <button key={t.id} ref={(el) => { refs.current[i] = el; }} role="tab" type="button" className="tab"
            aria-selected={t.id === value} tabIndex={t.id === value ? 0 : -1}
            onClick={() => onChange(t.id)} onKeyDown={(e) => key(e, i)}>
            {Icon && <Icon size={15} aria-hidden />}{t.label}{t.badge}
          </button>
        );
      })}
    </div>
  );
}

// ── Dialog ─────────────────────────────────────────────────────────────────
const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Modal dialog: role="dialog", focus kept inside, Esc / backdrop close, focus returned afterwards. */
export function Dialog({ open, onClose, title, children, actions, className, labelledBy }: {
  open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; actions?: ReactNode; className?: string;
  labelledBy?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const titleId = useId();
  useEffect(() => {
    if (!open) return;
    const before = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const first = el?.querySelector<HTMLElement>("[autofocus], input, select, textarea") ?? el?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); closeRef.current(); return; }
      if (e.key !== "Tab" || !el) return;
      const items = Array.from(el.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (!items.length) return;
      const [a, z] = [items[0], items[items.length - 1]];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); before?.focus?.(); };
  }, [open]);
  if (!open) return null;
  return (
    <div className="overlay center" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} className={cx("modal", className)} role="dialog" aria-modal="true"
        aria-labelledby={labelledBy ?? (title ? titleId : undefined)}>
        <div className="modal-body">
          {title && <div className="row" style={{ alignItems: "flex-start", marginBottom: 8 }}>
            <h2 id={titleId} className="grow" style={{ margin: 0 }}>{title}</h2>
            <IconButton icon={X} label="Close" onClick={onClose} size={16} />
          </div>}
          {children}
        </div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

export { cx };
