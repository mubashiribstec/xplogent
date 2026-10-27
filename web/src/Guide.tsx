import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { getGuidePage, getGuidePages } from "./api";
import { startTour } from "./components/Tour";

export function Guide() {
  const [pages, setPages] = useState<{ slug: string; title: string }[]>([]);
  const [active, setActive] = useState<string>("");
  const [content, setContent] = useState<string>("");

  useEffect(() => {
    getGuidePages().then((r) => {
      setPages(r.pages);
      if (r.pages[0]) setActive(r.pages[0].slug);
    });
  }, []);

  useEffect(() => {
    if (active) getGuidePage(active).then((r) => setContent(r.content));
  }, [active]);

  return (
    <div className="guide">
      <aside className="guide-nav">
        <button className="btn primary sm" style={{ marginBottom: 8 }} onClick={startTour}>▶ Take the tour</button>
        {pages.map((p) => (
          <button key={p.slug} className={p.slug === active ? "active" : ""}
                  onClick={() => setActive(p.slug)}>
            {p.title}
          </button>
        ))}
        {pages.length === 0 && <p className="dim">The guide pages are missing from this install. Update the
          server (<span className="mono">sudo xplogent-docker update</span>); the tour above works meanwhile.</p>}
      </aside>
      <article className="guide-body markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
      </article>
    </div>
  );
}
