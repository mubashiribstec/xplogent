# Troubleshooting

## The dashboard doesn't open / shows a blank page
`xplogent up` serves the dashboard from `web/dist`. If you don't have it built:

```bash
cd web && npm install && npm run build
```

Then run `xplogent up` again. Without Node.js, run the API (`xplogent serve`) and
the dev server (`cd web && npm run dev`) separately.

## "confirmation required but no approver" / actions blocked
Over the API there's no human to approve `confirm`-tier actions, so they are
blocked by default. Either lower the risk policy for that tier in **Settings
→ Safety**, or use a role whose policy is `auto` for it.

## Ollama errors / no response
Xplogent doesn't run Ollama; it talks to your Ollama server. Make sure it is running and
reachable from Xplogent (Settings → Ollama servers → Test), and that the model is pulled on
that machine:

```bash
ollama pull llama3.1
```

Or switch to a hosted model in Settings → Models.

## Something's off but you're not sure what
Run `xplogent doctor`. It checks your config files, saved API keys, whether
your model answers, the database, the dashboard build and the server port,
and prints a plain-English fix for anything it finds.

## Updating
See **Updating** (guide 06) — Docker installs update with `xplogent-docker
update`; git installs with `git pull` + reinstall.
