import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ToastProvider } from "./components/Toast";
import "./styles.css";

// Apply the saved theme + accent before first paint. Storage can be blocked (privacy
// settings / site-data policies): never let that stop the app from starting.
try {
  const theme = localStorage.getItem("xplogent_theme") || "auto";
  if (theme !== "auto") document.documentElement.setAttribute("data-theme", theme);
  const accent = localStorage.getItem("xplogent_accent");
  if (accent) document.documentElement.style.setProperty("--accent", accent);
} catch { /* defaults */ }

// Any render error used to unmount everything and leave a blank white page.
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "15vh auto", padding: "0 16px" }}>
        <h2>Something went wrong</h2>
        <pre style={{ whiteSpace: "pre-wrap", color: "#b00020" }}>{String(this.state.error?.message || this.state.error)}</pre>
        <p>If this keeps happening: update the server (<code>sudo xplogent-docker update</code>), then reload
          with Ctrl+Shift+R (Cmd+Shift+R on a Mac) so the browser fetches the new dashboard.</p>
        <button onClick={() => location.reload()}>Reload</button>
      </div>
    );
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <ToastProvider>
        <App />
      </ToastProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
