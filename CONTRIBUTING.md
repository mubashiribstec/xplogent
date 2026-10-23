# Contributing to Xplogent

Thanks for helping! Xplogent is a personal AI agent with a phone-calling
flagship: one chat agent, one phone agent, persistent memory and self-learned
skills — kept deliberately lean.

## Ground rules

- **One agent, done well.** Multi-agent orchestration, PC control and other
  removed features were cut on purpose (see CHANGELOG). Please don't
  re-propose them without a strong case in an issue first.
- **The memory system and the skills/self-improvement loop are core.** Changes
  there need tests and a clear rationale.
- **No new external services or paid APIs** in the core. Providers and
  telephony stay behind the existing interfaces.
- **Honesty in the UI and docs:** if a feature isn't shipped, don't document
  it as if it were. The system prompt and README are held to the same bar.
- Database migrations must be **additive** (existing installs keep working).

## Development setup

```bash
git clone https://github.com/mubashiribstec/xplogent.git
cd xplogent
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[all]"
cd web && npm install && cd ..
```

Run the backend and the dashboard dev server separately:

```bash
xplogent serve                       # API on :8765
cd web && npm run dev                # dashboard on :5173 (hot reload)
```

## Before you open a PR

```bash
ruff check src tests
pytest -q
cd web && npm run build
```

CI runs the same (plus a Python 3.11–3.13 matrix, Windows, the server lock
install, the Docker build and installer checks), so green locally ≈ green CI.

- Add or update tests for behaviour changes — the core (memory, skills,
  config, providers, VoIP field enforcement) is expected to stay covered.
- Keep commits small and say *why* in the message, not just *what*.
- For VoIP changes, note what you could and couldn't test live (SIP/RTP
  needs real infrastructure; unit tests plus a clear note are fine).

## Reporting bugs

Use the bug-report template. `xplogent doctor` output answers most of the
environment questions — paste it (it never prints secret values).

## Licence

Contributions are MIT-licensed, same as the project.
