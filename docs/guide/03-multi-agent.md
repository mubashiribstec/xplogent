# Multi-agent teams

Xplogent can run several agents at the same time. You choose how many run
concurrently (`max_concurrent_agents`).

## Two ways to run a team

**Auto** — give one goal; the orchestrator decomposes it into subtasks and runs
role-scoped agents, honoring dependencies:

```bash
xplogent orchestrate "research the top 3 vector DBs and write a comparison" --max 3
```

**Manual** — define named agents yourself:

```bash
xplogent team -a "scout:researcher:find facts about X" \
              -a "writer:coder:write a summary to notes.md"
```

Teams also run unattended: choose **team** mode for a schedule or trigger.

## How agents collaborate

Agents share a message bus and can:
- **broadcast** status to the whole team,
- **send_message** directly to a named agent,
- **read_inbox** to see what others told them.

A shared **task board** tracks each subtask (pending → active → done/failed).

## Monitoring

The CLI streams each agent's progress while a team runs. Afterwards the
dashboard's **Runs** page shows every team run with each agent's steps, tools
and messages.
