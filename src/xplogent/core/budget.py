"""Cost guardrails.

Caps spend using the persisted ``usage`` table (daily) and the agent's running
``session_cost`` (per session). When a cap is exceeded the agent either warns,
downgrades to a cheaper model, or pauses the run — driven by ``budget.on_exceed``.
Pricing reuses the same per-model table as Analytics, so local models cost $0 and
never trip a cap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from xplogent.memory.store import Store


def _local_midnight() -> float:
    # Let the C library work out whether midnight itself was in DST (tm_isdst=-1);
    # reusing *now's* DST flag is an hour off on the days the clocks change.
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


def today_spend(store: Store) -> float:
    """Total estimated USD spent since local midnight."""
    return round(sum(r["cost"] or 0.0 for r in store.usage_rows(_local_midnight())), 6)


@dataclass
class BudgetVerdict:
    exceeded: bool
    scope: str = ""       # "daily" | "session"
    action: str = "warn"  # "warn" | "downgrade" | "pause"
    reason: str = ""
    spent: float = 0.0
    cap: float = 0.0


def check_budget(budget: dict, store: Store | None, session_cost: float) -> BudgetVerdict:
    """Evaluate spend against the configured caps. Daily takes precedence."""
    daily_cap = float(budget.get("daily_usd", 0) or 0)
    session_cap = float(budget.get("session_usd", 0) or 0)
    action = str(budget.get("on_exceed", "warn") or "warn")

    if daily_cap > 0 and store is not None:
        spent = today_spend(store)
        if spent >= daily_cap:
            return BudgetVerdict(True, "daily", action,
                                 f"daily spend ${spent:.4f} reached the ${daily_cap:.2f} cap",
                                 spent, daily_cap)
    if session_cap > 0 and session_cost >= session_cap:
        return BudgetVerdict(True, "session", action,
                             f"session spend ${session_cost:.4f} reached the ${session_cap:.2f} cap",
                             session_cost, session_cap)
    return BudgetVerdict(False)
