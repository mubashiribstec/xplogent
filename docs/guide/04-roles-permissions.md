# Roles & permissions

Every agent runs under a **role profile** that controls exactly what it may do.
Edit roles in **Settings → Roles** or in `config.yaml` under `roles:`.

## What a profile controls

| Field | Meaning |
|---|---|
| `allowed_tools` | which tools the agent may call (`"*"` = all) |
| `policy` | per-risk-tier action: `auto` / `confirm` / `deny` |
| `allowed_paths` | filesystem roots the agent may write to (empty = unrestricted) |
| `network` | whether network-capable tools are allowed |
| `max_steps` | the agent's step budget |

## Built-in roles

- **operator** — full access, risky actions need confirmation.
- **researcher** — read-only files + memory, no shell, no writes.
- **coder** — files + shell, can be sandboxed to `allowed_paths`.
- **reviewer** — read-only.
- **restricted** — read files / list dirs only.

## Safety tiers

Each tool call is risk-classified (`low`/`medium`/`high`/`critical`). The role's
`policy` decides whether it runs automatically, asks for approval, or is blocked.
Destructive commands (e.g. `rm -rf /`, `format c:`) always match the deny-list
and are blocked.

When there's no human to approve (e.g. unattended API runs), `confirm`-tier
actions are blocked unless you explicitly enable auto-approval.
