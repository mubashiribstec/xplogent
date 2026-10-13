import { useState } from "react";

// Lightweight, dependency-free markdown rendering for chat: fenced code blocks
// with a copy button, plus basic inline formatting. Good enough for agent output
// without pulling in a heavy parser.

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  };
  return (
    <div className="codeblock">
      <div className="codebar">
        <span className="dim">{lang || "code"}</span>
        <button onClick={copy}>{copied ? "copied ✓" : "copy"}</button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

// Only real web/mail links become clickable: a model (or a document it quotes) could
// otherwise emit [click](javascript:...) and run script in the dashboard's origin.
function safeHref(url: string): string | null {
  const u = url.trim();
  return /^(https?:|mailto:)/i.test(u) ? u : null;
}

function inline(text: string): (JSX.Element | string)[] {
  // **bold**, `code`, and [label](url) — minimal but safe.
  const nodes: (JSX.Element | string)[] = [];
  const re = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[2]) nodes.push(<strong key={i++}>{m[2]}</strong>);
    else if (m[3]) nodes.push(<code key={i++}>{m[3]}</code>);
    else if (m[4]) {
      const href = safeHref(m[5]);
      nodes.push(href
        ? <a key={i++} href={href} target="_blank" rel="noopener noreferrer">{m[4]}</a>
        : <span key={i++}>{m[4]}</span>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function Markdown({ text }: { text: string }) {
  const parts = text.split(/```/);
  return (
    <div className="markdown-msg">
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const nl = part.indexOf("\n");
          const lang = nl > 0 ? part.slice(0, nl).trim() : "";
          const code = nl > 0 ? part.slice(nl + 1) : part;
          return <CodeBlock key={i} code={code.replace(/\n$/, "")} lang={lang} />;
        }
        return part.split("\n").map((line, j) => {
          const key = `${i}-${j}`;
          if (/^#{1,3}\s/.test(line)) {
            const level = line.match(/^#+/)![0].length;
            const content = line.replace(/^#+\s/, "");
            return level <= 1 ? <h3 key={key}>{inline(content)}</h3>
              : <h4 key={key}>{inline(content)}</h4>;
          }
          if (/^[-*]\s/.test(line)) return <li key={key}>{inline(line.slice(2))}</li>;
          if (!line.trim()) return <br key={key} />;
          return <p key={key}>{inline(line)}</p>;
        });
      })}
    </div>
  );
}
