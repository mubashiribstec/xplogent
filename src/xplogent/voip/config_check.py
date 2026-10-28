"""Validate the ``voip:`` settings: unknown keys (typos) and wrong types.

The packaged ``config/default.yaml`` is the schema: every key it defines is known, with
the type of its default. Problems are logged at startup and listed in the readiness
checklist, so a mistyped key never silently does nothing again.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml

# Optional sections that default.yaml leaves out on purpose (off unless you add them).
_OPTIONAL: dict[str, Any] = {
    "stt_fallback": {"provider": "", "model": "", "base_url": "", "api_key_env": "", "language": "",
                     "device": ""},
    "tts_fallback": {"provider": "", "base_url": "", "model": "", "voice": "", "sample_rate": 0,
                     "api_key_env": "", "language": ""},
    "training": {"whisper_model": "", "language": ""},
}
_FREE_FORM = {"dtmf.keys"}          # user-defined keys inside these dicts
_RETIRED = {"voip.provider"}        # old keys that may linger in existing configs: ignored
_NUMBER = (int, float)


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    from xplogent.core.config import _DEFAULT_CONFIG

    voip = dict((yaml.safe_load(_DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}).get("voip") or {})
    for key, value in _OPTIONAL.items():
        voip.setdefault(key, value)
    return voip


def _type_ok(default: Any, value: Any) -> bool:
    if value is None or default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, _NUMBER):
        return isinstance(value, _NUMBER) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, (str, int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, (list, str))      # comma-separated strings are accepted
    if isinstance(default, dict):
        return isinstance(value, dict)
    return True


def _kind(v: Any) -> str:
    return {bool: "true/false", int: "a number", float: "a number", str: "text", list: "a list",
            dict: "a section"}.get(type(v), type(v).__name__)


def check_voip(v: dict[str, Any] | None, schema: dict[str, Any] | None = None, path: str = "voip") -> list[str]:
    """Human-readable problems in a ``voip`` config dict ([] = fine)."""
    schema = _schema() if schema is None else schema
    problems: list[str] = []
    for key, value in (v or {}).items():
        name = f"{path}.{key}"
        if name in _RETIRED:
            continue
        if key not in schema:
            close = [k for k in schema if k.replace("_", "") == str(key).replace("_", "").lower()]
            hint = f" (did you mean {close[0]}?)" if close else ""
            problems.append(f"{name} is not a setting{hint}")
            continue
        default = schema[key]
        if not _type_ok(default, value):
            problems.append(f"{name} should be {_kind(default)}, not {_kind(value)}")
        elif isinstance(default, dict) and isinstance(value, dict) and name.removeprefix("voip.") not in _FREE_FORM:
            problems += check_voip(value, default, name) if default else []
    if path == "voip" and isinstance((v or {}).get("calling_hours"), dict):
        from xplogent.voip.hours import profile_problems

        problems += profile_problems(v["calling_hours"])
    return problems
