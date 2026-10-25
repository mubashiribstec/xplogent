# Updating

## Git installs (desktop / from source)

```bash
cd xplogent && git pull && pip install -e ".[all]"
```

Then restart (`xplogent stop` / `xplogent start`, or re-run `xplogent up`).
Updates use a **fast-forward pull**; if you have local commits or conflicts,
resolve them in git first.

## Docker installs (one-command server install)

The dashboard can't replace its own container, so you update on the server:

```bash
xplogent-docker update     # backup → new deploy files → pull the new image → restart
```

If the new image can't be downloaded, nothing changes and the current version keeps
running. To stay on a specific version, set
`XPLOGENT_IMAGE=ghcr.io/mubashiribstec/xplogent:1.2.3` with `xplogent-docker config`;
use `:latest` to follow every release again.

Backups before risky changes: `xplogent-docker backup` (restore with
`xplogent-docker restore <file>`).

## systemd installs

```bash
cd xplogent && git pull && sudo ./deploy/install-server.sh
```
