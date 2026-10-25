# Getting started

Xplogent is a personal, self-improving AI agent you run yourself. It works with
any LLM provider (including local **Ollama**, fully offline), keeps its own
persistent memory, learns reusable skills from its work, and can answer and
place **phone calls** (Telnyx or your own FreePBX/Asterisk) — all behind a
configurable safety/approval layer.

## Install

### On a Linux server (one command, Docker)

```bash
curl -fsSL https://raw.githubusercontent.com/mubashiribstec/xplogent/main/get.sh | sudo bash
```

It installs Docker if needed, asks for your domain (automatic HTTPS) or uses no-domain
mode, optionally runs local models with Ollama, and prints the address and your access
token. Manage it afterwards with `xplogent-docker` (`status`, `update`, `backup`, `token`,
`help`). Full details: `docs/deploy-linux.md` in the repository.

### On your own computer

```bash
git clone https://github.com/mubashiribstec/xplogent.git && cd xplogent
# Linux / macOS
bash install.sh
# Windows (PowerShell)
./install.ps1
```

The installer creates a virtual environment, installs Xplogent, and builds the
dashboard (if Node.js is present).

## Configure

Run the wizard once:

```bash
xplogent setup
```

It asks for your provider (Ollama / OpenAI / Anthropic / OpenRouter), the model,
and an API key if you chose a hosted provider. For Ollama it prints the exact
`ollama pull` commands for the models you picked. Everything is saved under
`~/.xplogent/`. If anything looks off afterwards, run `xplogent doctor` — it
checks your config, keys, model and ports and tells you how to fix each problem.

## Launch

```bash
xplogent up        # foreground: opens the dashboard in your browser
```

### Keep it running in the background

```bash
xplogent start     # detached — keeps running after you close the terminal
xplogent status    # is it up?
xplogent stop      # stop it
xplogent service install   # optional: auto-start at login/boot
```

Open the dashboard at `http://127.0.0.1:8765`. From there you can chat (with a
**model switcher** and effort/thinking controls), run the phone agent from **Calls**,
change every setting in **Settings**, and read this guide.

Setup is **one time** — your choices are saved under `~/.xplogent`, so `start`/`up`
just work afterward.

Prefer the terminal? `xplogent chat` for a single agent.

## Your first tour of the dashboard

- **Take the tour.** It starts once after first-run setup and walks through the health card,
  the call list, a test call, Chat, Call training, the CRM card and Setup. Restart it any time
  from **Guide → Take the tour** or the command palette (Ctrl/Cmd-K → "tour").
- **Go live checklist** (top of **Calls**): connect a phone line → add the FreePBX extension
  (optional) → make a test call → connect your CRM → go live. Each step is checked against
  the real setup and has an **Open** button that jumps to the right card. Under it, one
  health row shows every line, the voices, the CRM and line capacity in green, amber or red.
- **Demo data** (the switch on the Recent calls card): adds four sample calls with
  transcripts, outcomes and timings, badged *Demo data*. They are never counted, never sent
  to your CRM and never touch contacts or training; switching it off removes only them.
- **Branding** (Calls → Settings → Branding): your company name, logo (PNG, JPEG or WebP, up
  to 512 KB) and accent colour, shown in the menu, on the login page and in the tour. The
  colour is adjusted automatically so text stays readable in both themes.
- **On a phone** the menu is a drawer (☰) and tables become cards.
