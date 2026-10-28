"""Mandatory-field enforcement — the anti-Alex guarantee (vs-Retell brief Part D).

A Retell agent closed a callback call without ever capturing the phone number:
the prompt *asked* for discipline, nothing *enforced* it. Here the discipline is
in the engine, not the prompt:

- The operator declares fields in config (``voip.fields``): name, validator,
  whether the call may close successfully without them, and whether the value
  must be **read back and confirmed** by the caller (phone numbers!).
- The brain gets a per-turn checklist of what is still missing, and a
  ``record_field`` tool to store validated values.
- ``end_call`` is **refused** while required fields are open. The only ways out
  without them are explicit exit paths — the caller refused, hung up, was the
  wrong party, or is not interested / do-not-call — and the exit used is kept on
  the call state, so a "successful" close with missing data is impossible to
  confuse with a legitimate one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

EXIT_PATHS = {"refused", "hung_up", "wrong_party", "not_interested", "do_not_call", "off_topic"}


def _validate_phone(raw: str, default_country: str = "") -> str | None:
    """Normalise a phone number to E.164-ish form, or return None if implausible.

    Accepts +, 00-prefix, spaces/dashes/brackets. A national-format number
    (leading 0) only normalises when a default country code is configured.
    """
    digits = re.sub(r"[^\d+]", "", str(raw or ""))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("+"):
        num = digits[1:]
        return "+" + num if num.isdigit() and 7 <= len(num) <= 15 else None
    if not digits.isdigit():
        return None
    if digits.startswith("0") and default_country:
        num = default_country.lstrip("+") + digits[1:]
        return "+" + num if 7 <= len(num) <= 15 else None
    if 7 <= len(digits) <= 15 and not digits.startswith("0"):
        return "+" + digits
    return None


def _validate_email(raw: str) -> str | None:
    value = str(raw or "").strip()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else None


def _validate_nonempty(raw: str) -> str | None:
    value = str(raw or "").strip()
    return value or None


VALIDATORS = {"phone": _validate_phone, "email": _validate_email, "nonempty": _validate_nonempty}


@dataclass
class FieldSpec:
    name: str
    label: str = ""
    validator: str = "nonempty"
    required: bool = True            # the call may not close successfully without it
    require_confirmed: bool = False  # value must be read back + confirmed by the caller
    default_country: str = ""        # for phone: national numbers get this country code

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> FieldSpec:
        return cls(
            name=str(data.get("name", "")).strip(),
            label=str(data.get("label", "")).strip(),
            validator=str(data.get("validator", "nonempty")).strip() or "nonempty",
            required=bool(data.get("required", True)),
            require_confirmed=bool(data.get("require_confirmed", False)),
            default_country=str(data.get("default_country", "")).strip(),
        )


@dataclass
class FieldCollector:
    """Values collected on one call, checked against the declared schema."""

    specs: list[FieldSpec] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)
    confirmed: dict[str, bool] = field(default_factory=dict)

    def spec(self, name: str) -> FieldSpec | None:
        return next((s for s in self.specs if s.name == name), None)

    def set(self, name: str, raw: Any, *, confirmed: bool = False) -> tuple[bool, str]:
        """Validate + store a value. Returns (ok, message for the model)."""
        spec = self.spec(name)
        if spec is None:
            return False, f"unknown field {name!r} (not in the field schema)"
        validator = VALIDATORS.get(spec.validator, _validate_nonempty)
        if spec.validator == "phone":
            value = validator(str(raw), spec.default_country)
        else:
            value = validator(str(raw))
        if value is None:
            return False, f"{raw!r} is not a valid {spec.validator}; ask the caller to repeat it"
        self.values[name] = value
        if confirmed:
            self.confirmed[name] = True
        if spec.require_confirmed and not self.confirmed.get(name):
            return True, (f"stored {name} = {value}. Now READ IT BACK to the caller and, "
                          f"when they confirm it is right, call record_field again with "
                          f"confirmed=true. The call cannot close successfully before that.")
        return True, f"stored {name} = {value}"

    def missing(self) -> list[FieldSpec]:
        """Required fields still open: no valid value, or unconfirmed read-back."""
        out = []
        for spec in self.specs:
            if not spec.required:
                continue
            if spec.name not in self.values:
                out.append(spec)
            elif spec.require_confirmed and not self.confirmed.get(spec.name):
                out.append(spec)
        return out

    def checklist(self) -> str:
        """The per-turn prompt block: what to collect, what is still open."""
        if not self.specs:
            return ""
        lines = ["FIELDS TO COLLECT on this call:"]
        for spec in self.specs:
            label = spec.label or spec.name
            if spec.name in self.values:
                state = "confirmed" if (not spec.require_confirmed
                                        or self.confirmed.get(spec.name)) else \
                        "collected — READ BACK and confirm"
                lines.append(f"- {spec.name} ({label}): {self.values[spec.name]} [{state}]")
            else:
                req = "REQUIRED" if spec.required else "optional"
                lines.append(f"- {spec.name} ({label}): not collected yet [{req}]")
        open_names = [s.name for s in self.missing()]
        if open_names:
            lines.append("You CANNOT end this call successfully until these are collected"
                         + (" and confirmed" if any(s.require_confirmed for s in self.missing()) else "")
                         + f": {', '.join(open_names)}.")
        return "\n".join(lines)

    def report(self) -> dict[str, Any]:
        """Field-completeness for the call record / CRM payload."""
        return {
            "values": dict(self.values),
            "confirmed": {k: bool(v) for k, v in self.confirmed.items()},
            "missing_required": [s.name for s in self.missing()],
            "complete": not self.missing(),
        }


def build_collector(field_configs: list[dict[str, Any]] | None) -> FieldCollector:
    specs = [FieldSpec.from_config(d) for d in (field_configs or [])]
    return FieldCollector(specs=[s for s in specs if s.name])
