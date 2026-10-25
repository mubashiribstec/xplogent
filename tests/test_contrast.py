"""The glass skin keeps text readable: body and dim text >= 4.5:1 (WCAG AA) on the
worst spot of every theme — glass composited over the strongest background glow."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "web" / "src" / "styles.css").read_text(encoding="utf-8")


def _blocks(selector: str) -> list[dict[str, str]]:
    out = []
    for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", CSS):
        out.append(dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(1))))
    return out


def _tokens(theme: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for block in _blocks(":root"):
        merged.update(block)
    if theme == "light":
        for block in _blocks(':root[data-theme="light"]'):
            merged.update(block)
    return merged


def _rgba(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a
    nums = [float(x) for x in re.findall(r"[\d.]+", value)]
    return nums[0], nums[1], nums[2], nums[3] if len(nums) > 3 else 1.0


def _over(top, bottom):
    a = top[3]
    return tuple(top[i] * a + bottom[i] * (1 - a) for i in range(3)) + (1.0,)


def _lum(c) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(a, b) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_text_on_glass_is_readable_everywhere(theme):
    t = _tokens(theme)
    bg = _rgba(t["--bg"])
    worst = 99.0
    for glow in ("--bg-glow-1", "--bg-glow-2", "--bg-glow-3"):
        spot = _over(_rgba(t[glow]), bg)                      # the glow at its strongest
        panel = _over(_rgba(t["--glass"]), spot)
        for fg in ("--text", "--dim"):
            worst = min(worst, contrast(_rgba(t[fg]), panel))
    assert worst >= 4.5, f"{theme}: worst text contrast on glass is {worst:.2f}:1"


def test_skin_has_fallbacks():
    assert "@supports not ((backdrop-filter: blur(1px))" in CSS
    assert "prefers-reduced-transparency: reduce" in CSS and "prefers-reduced-motion: reduce" in CSS
