# Updating

When the repository changes, you can update Xplogent in one click.

## From the dashboard

Open **Settings → Update**:
1. Click **Check for updates** — Xplogent fetches the remote and shows how many
   commits you're behind, with a changelog.
2. Click **Update & restart** — it pulls, reinstalls, and restarts the backend.
   The page reconnects automatically when it's back.

## From the terminal

```bash
xplogent update      # git pull + reinstall (then restart xplogent up yourself)
```

## Notes

- One-click update works for **git-clone / editable installs**. If you installed
  from a package, the dashboard tells you and you update with your package
  manager instead.
- Updates use a **fast-forward pull**; if you have local commits or conflicts,
  resolve them in git first.

## Docker installs (one-command server install)

The dashboard can't replace its own container, so Settings → Update tells you to
update on the server instead:

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
