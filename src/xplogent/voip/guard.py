"""Role facts guard: the agent may only state a figure it was given.

Before a sentence is spoken, its *claims* (money, working hours, dates) are compared
with what the agent actually knows on this call: the vacancy's ROLE FACTS, the knowledge
retrieved so far, the caller's own words, tool results (callback slots) and today's
date. A sentence with a figure from nowhere ("£15 an hour" when the brief says £12.60)
is replaced by a safe line, and the original is kept in the call's debug view.

Sources are read leniently (any number in them counts); speech is read strictly (only
figures in a money / hours / date context are claims), so the guard blocks inventions
without blocking ordinary talk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SAFE_LINE = ("I don't have that exact figure in front of me, but the recruiter will confirm it "
             "when they call you.")
KINDS = ("money", "hours", "dates")

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_WORD = "|".join(sorted([*_UNITS, *_TENS, "hundred", "thousand"], key=len, reverse=True))
_WORDS = rf"(?:\b(?:{_WORD})\b(?:[\s-]+(?:and[\s-]+)?\b(?:{_WORD})\b)*)"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"), 1)}
_MONTH = "|".join([*_MONTHS, "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
                   "nov", "dec"])
_PERIOD = r"(?:an|a|per|each|every)\s+(?:hour|year|annum|week|month|day|shift|night)"

_PAY_WORDS = r"(?:pay|pays|paid|rate|salary|earn|earns|earning|wage|wages)"
_MONEY = [
    re.compile(rf"£\s?(?P<n>{_NUM})\s?(?P<k>k\b|thousand)?(?:\s?(?:and\s+)?(?P<p>\d{{1,2}})\s?p\b)?", re.I),
    re.compile(rf"(?P<n>{_NUM}|{_WORDS})\s+(?:pounds?|quid)(?:\s+(?:and\s+)?(?P<p>{_NUM}|{_WORDS})(?:\s+(?:pence|p)\b)?)?", re.I),
    re.compile(rf"(?P<n>{_NUM})\s?(?P<k>k\b)\s+(?:a|per)\s+(?:year|annum)", re.I),
    re.compile(rf"(?P<n>\d+\.\d{{2}})\s+(?:{_PERIOD})", re.I),
    re.compile(rf"\b{_PAY_WORDS}\b\D{{0,25}}?(?P<n>{_NUM})\s*(?:{_PERIOD})?", re.I),
]
# Working hours only (not "I'll call back in two hours").
_HOURS = [
    re.compile(rf"(?P<n>{_NUM}|{_WORDS})[\s-]+hours?\s+(?:a|per|each)\s+(?:week|day|night|shift|month)", re.I),
    re.compile(rf"(?P<n>{_NUM}|{_WORDS})[\s-]+hours?\s+(?:shifts?|days?|nights?|weeks?|contract|rota)\b", re.I),
    re.compile(rf"(?P<n>{_NUM})h\s+shifts?\b", re.I),
]
_DATES = [
    re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m>{_MONTH})\b", re.I),
    re.compile(rf"\b(?P<m>{_MONTH})\s+(?:the\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b", re.I),
]
_ISO_DATE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})")
_ANY_NUM = re.compile(_NUM)


def words_to_number(text: str) -> float | None:
    total = current = 0
    seen = False
    for word in re.split(r"[\s-]+", text.lower().strip()):
        if word in ("and", ""):
            continue
        if word == "a":
            current = current or 1
        elif word in _UNITS:
            current += _UNITS[word]
        elif word in _TENS:
            current += _TENS[word]
        elif word == "hundred":
            current = (current or 1) * 100
        elif word == "thousand":
            total += (current or 1) * 1000
            current = 0
        else:
            return None
        seen = True
    return float(total + current) if seen else None


def _number(text: str | None) -> float | None:
    if not text:
        return None
    if re.fullmatch(_NUM, text.strip()):
        return float(text.replace(",", ""))
    return words_to_number(text)


@dataclass(frozen=True)
class Claim:
    kind: str
    value: float | tuple[int, int]
    text: str


def claims(text: str, kinds: tuple[str, ...] = KINDS) -> list[Claim]:
    """The figures a sentence states, normalised (£12.60 → 12.6, £25k → 25000)."""
    out: list[Claim] = []
    taken: list[tuple[int, int]] = []

    def free(m: re.Match) -> bool:
        return not any(a < m.end() and m.start() < b for a, b in taken)

    if "money" in kinds:
        for rx in _MONEY:
            for m in rx.finditer(text):
                n = _number(m.group("n"))
                if n is None or not free(m):
                    continue
                if (m.groupdict().get("k") or "").strip():
                    n *= 1000
                pence = _number(m.groupdict().get("p"))
                if pence is not None and pence < 100:
                    n += pence / 100
                taken.append(m.span())
                out.append(Claim("money", round(n, 2), m.group(0)))
    if "hours" in kinds:
        for rx in _HOURS:
            for m in rx.finditer(text):
                n = _number(m.group("n"))
                if n is not None and free(m):
                    taken.append(m.span())
                    out.append(Claim("hours", n, m.group(0)))
    if "dates" in kinds:
        for rx in _DATES:
            for m in rx.finditer(text):
                if not free(m):
                    continue
                month = _MONTHS.get(m.group("m").lower()) or next(
                    (i for name, i in _MONTHS.items() if name.startswith(m.group("m").lower()[:3])), 0)
                day = int(m.group("d"))
                if month and 1 <= day <= 31:
                    taken.append(m.span())
                    out.append(Claim("dates", (month, day), m.group(0)))
    return out


class Sources:
    """Everything the agent may quote figures from on this call (grows turn by turn)."""

    def __init__(self) -> None:
        self.numbers: set[float] = set()
        self.dates: set[tuple[int, int]] = set()

    def add(self, text: str) -> None:
        if not text:
            return
        for m in _ANY_NUM.finditer(text):
            with_k = text[m.end():m.end() + 2].lower().strip().startswith("k")
            try:
                n = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            self.numbers.add(round(n, 2))
            if with_k:
                self.numbers.add(round(n * 1000, 2))
        for c in claims(text):
            if c.kind == "dates":
                self.dates.add(c.value)            # type: ignore[arg-type]
            else:
                self.numbers.add(c.value)          # type: ignore[arg-type]
        for m in _ISO_DATE.finditer(text):
            self.dates.add((int(m.group(1)), int(m.group(2))))

    def supports(self, claim: Claim) -> bool:
        if claim.kind == "dates":
            return claim.value in self.dates
        value = float(claim.value)                 # type: ignore[arg-type]
        return round(value, 2) in self.numbers


def unsupported(sentence: str, sources: Sources, kinds: tuple[str, ...] = KINDS) -> list[Claim]:
    return [c for c in claims(sentence, kinds) if not sources.supports(c)]
