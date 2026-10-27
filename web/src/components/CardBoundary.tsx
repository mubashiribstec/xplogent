import React from "react";

// A render error in one card used to blank the whole page ("Something went wrong").
// Now only that card shows the error, with the running build, and the rest keeps working.
export class CardBoundary extends React.Component<{ name: string; children: React.ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error) { console.error(`card "${this.props.name}" failed:`, error); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="card">
        <h3>{this.props.name}</h3>
        <p className="err" style={{ fontSize: 13 }}>This card hit an error: {String(this.state.error.message || this.state.error).slice(0, 300)}</p>
        <p className="dim" style={{ fontSize: 12 }}>The rest of the page still works. If it keeps happening, update the server
          (<span className="mono">sudo xplogent-docker update</span>) and reload the page.</p>
        <button className="btn" onClick={() => this.setState({ error: null })}>Retry</button>
      </div>
    );
  }
}
