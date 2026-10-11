"""CSV contact import for the calling agent.

Headers are matched loosely (``Phone``, ``mobile``, ``Phone Number`` ...). Anything we
don't recognise is kept as a custom field and shown to the agent during the call, so
columns like ``plan``, ``balance`` or ``last_order`` can be used in conversation.
Numbers already in the database are skipped; a ``dnc`` / ``do_not_call`` column set to
yes/true/1 puts the number on the do-not-call list.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from xplogent.memory.store import Store

_ALIASES = {
    "name": {"name", "full name", "fullname", "customer", "customer name", "contact", "client"},
    "first": {"first name", "firstname", "first"},
    "last": {"last name", "lastname", "last", "surname"},
    "phone": {"phone", "phone number", "mobile", "cell", "telephone", "tel", "number", "contact number"},
    "email": {"email", "e-mail", "email address"},
    "timezone": {"timezone", "time zone", "tz"},
    "notes": {"notes", "note", "comments", "comment"},
    "dnc": {"dnc", "do not call", "do_not_call", "donotcall"},
}


def _map_header(header: str) -> str:
    h = header.strip().lower().replace("_", " ")
    for key, names in _ALIASES.items():
        if h in names:
            return key
    return ""


def import_csv(store: Store, text: str, source: str = "csv") -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        return {"ok": False, "error": "the CSV has no header row", "imported": 0, "skipped": 0}
    mapping = {col: _map_header(col) for col in reader.fieldnames}
    if "phone" not in mapping.values():
        return {"ok": False, "error": "no phone column found (expected e.g. 'phone' or 'mobile')",
                "imported": 0, "skipped": 0}

    imported = skipped = dnc = 0
    errors: list[str] = []
    for line, row in enumerate(reader, start=2):
        values: dict[str, str] = {}
        custom: dict[str, str] = {}
        for col, val in row.items():
            if col is None or val is None or not str(val).strip():
                continue
            key = mapping.get(col, "")
            if key:
                values[key] = str(val).strip()
            else:
                custom[col.strip()] = str(val).strip()
        phone = Store.normalize_phone(values.get("phone", ""))
        if len(phone.lstrip("+")) < 7:
            skipped += 1
            errors.append(f"line {line}: missing or invalid phone")
            continue
        if store.find_contact_by_phone(phone):
            skipped += 1
            continue
        name = values.get("name") or " ".join(
            p for p in (values.get("first", ""), values.get("last", "")) if p)
        store.add_contact(name, phone, values.get("email", ""), values.get("timezone", ""),
                          values.get("notes", ""), custom, source)
        imported += 1
        if values.get("dnc", "").lower() in ("1", "yes", "y", "true", "x"):
            store.add_dnc(phone, "imported flag")
            dnc += 1
    return {"ok": True, "imported": imported, "skipped": skipped, "dnc": dnc, "errors": errors[:20]}
