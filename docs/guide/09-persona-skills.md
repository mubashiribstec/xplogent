# Persona & skills hub

Ways to shape who your agent is and teach it reusable ways of working.

## SOUL.md — who your agent is

Your agent's identity lives in a **SOUL.md** file (persona, instructions, boundaries,
values). It's injected at the top of every session, so editing it changes how the
agent thinks and talks. Edit it in **Persona & Skills → SOUL.md**, or directly at
`<install>/data/SOUL.md`. Keep it focused (a few hundred words).

## MEMORY.md — curated long-term memory

**MEMORY.md** is a compact, human-readable note of durable facts, preferences, and
standing decisions. It's injected each session alongside semantic recall. The agent
distills it from recent history when you click **Compact now** (or automatically every
N tasks if you set `memory.auto_compact_every`). Edit it directly any time.

## Skills hub

A **skill** is a markdown workflow (`SKILL.md`) that tells the agent *how and when* to
use tools — distinct from a tool (a raw capability). Install ready-made packs from the
bundled library, a path, or an http(s) URL, or paste a `SKILL.md`:

```bash
xplogent skills packs              # list the bundled starter packs
xplogent skills install code_review
xplogent skills install ./my-skill/SKILL.md
xplogent skills new my_skill       # scaffold a SKILL.md to edit
```

A `SKILL.md` has YAML frontmatter (`name`, `description`, `trigger`, `tools`) and a
markdown body. When a skill is relevant, the agent sees its trigger and the tools it
expects. Skills also gain a ★ proficiency level as they're used successfully.
