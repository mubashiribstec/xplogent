"""TPS / CTPS screening before automatic calls (UK Telephone Preference Service).

A provider adapter checks numbers; results are cached for ``voip.tps.cache_days``
(28 by default: the TPS register is refreshed daily, and screening every 28 days is the
usual standard). Campaign calls **fail closed**: if the check can't be made, the number is
not dialled. Manual and test calls only warn.

The adapter speaks one small JSON shape so any screening service can be wired in:
``POST {voip.tps.url}`` with ``{"numbers": ["+447700900123", …]}`` and a bearer key
(``XPLOGENT_TPS_API_KEY``), answering ``{"results": {"+447700900123": {"tps": false,
"ctps": false}}}`` (or a list of ``{"number", "tps", "ctps"}``). A vendor with another
format needs a 20-line proxy, or a new adapter here.
"""

from __future__ import annotations

import os
import time
from typing import Any

from xplogent.core.config import Config
from xplogent.core.logging import get_logger

_log = get_logger("voip.tps")
KEY_ENV = "XPLOGENT_TPS_API_KEY"


def tps_config(cfg: Config) -> dict[str, Any]:
    return dict((cfg.voip or {}).get("tps") or {})


def configured(cfg: Config) -> bool:
    t = tps_config(cfg)
    return bool(str(t.get("url") or "").startswith(("https://", "http://")) and os.environ.get(KEY_ENV))


def required(cfg: Config, consent_basis: str) -> bool:
    """Cold campaigns are always screened; others when screening is switched on."""
    return consent_basis == "cold" or bool(tps_config(cfg).get("enabled"))


def _parse(data: Any) -> dict[str, dict[str, bool]]:
    rows = data.get("results") if isinstance(data, dict) else data
    out: dict[str, dict[str, bool]] = {}
    if isinstance(rows, dict):
        rows = [{"number": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
    for r in rows or []:
        if isinstance(r, dict) and r.get("number"):
            out[str(r["number"])] = {"tps": r.get("tps") is True, "ctps": r.get("ctps") is True}
    return out


async def screen(cfg: Config, store, numbers: list[str], *, transport=None) -> dict[str, Any]:
    """``{ok, results: {number: {tps, ctps, checked_at, cached}}, error}``. Never raises."""
    import httpx

    days = float(tps_config(cfg).get("cache_days", 28))
    results: dict[str, Any] = {}
    todo = []
    for n in numbers:
        hit = store.tps_cached(n, time.time() - days * 86400)
        if hit:
            results[n] = {**hit, "cached": True}
        else:
            todo.append(n)
    if not todo:
        return {"ok": True, "results": results, "error": ""}
    if not configured(cfg):
        return {"ok": False, "results": results, "error": "TPS screening is not set up (Campaigns → TPS screening)"}
    try:
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            resp = await client.post(str(tps_config(cfg)["url"]), json={"numbers": todo},
                                     headers={"Authorization": f"Bearer {os.environ.get(KEY_ENV, '')}"})
        resp.raise_for_status()
        found = _parse(resp.json())
    except Exception as exc:  # noqa: BLE001 - the caller decides: campaigns fail closed
        _log.warning("TPS screening failed: %s", exc)
        return {"ok": False, "results": results, "error": f"TPS check failed: {type(exc).__name__}: {exc}"[:200]}
    missing = [n for n in todo if n not in found]
    for n, r in found.items():
        store.tps_store(n, r["tps"], r["ctps"])
        results[n] = {**r, "checked_at": time.time(), "cached": False}
    if missing:
        return {"ok": False, "results": results, "error": f"the TPS service gave no answer for {len(missing)} number(s)"}
    return {"ok": True, "results": results, "error": ""}


def registered(result: dict[str, Any] | None) -> str:
    """"tps" / "ctps" when the number must not get an unsolicited call, else ""."""
    if not result:
        return ""
    return "ctps" if result.get("ctps") else "tps" if result.get("tps") else ""
