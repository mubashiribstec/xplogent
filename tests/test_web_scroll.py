"""The dashboard must not pull the page back to the top while someone is reading it.

Pages rebuild their section tabs on every render (Calls re-renders with each live-status
refresh), and the Tabs component used to call scrollIntoView() in an effect keyed on that
array, so every refresh scrolled the page back up to the tabs. The browser check that found it:
scroll a page down, wait through a few refreshes, and the position must not change.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "web" / "src"


def _tabs_component() -> str:
    ui = (SRC / "ui" / "index.tsx").read_text(encoding="utf-8")
    start = ui.index("export function Tabs")
    end = ui.index("\nexport function", start + 1)
    return ui[start:end]


def test_tabs_never_scroll_the_page():
    tabs = _tabs_component()
    assert "scrollIntoView" not in tabs  # scrolls every scrollable ancestor, the page included
    # Arrow-key focus must not scroll the page either.
    assert re.search(r"\.focus\(\s*\)", tabs) is None


def test_tabs_scroll_effect_runs_only_when_the_selection_changes():
    tabs = _tabs_component()
    deps = re.findall(r"\}, \[([^\]]*)\]\);", tabs)
    assert deps, "the Tabs component should keep its strip-scrolling effect"
    for d in deps:
        assert "tabs" not in d.split(", "), "an effect keyed on the tabs array re-runs on every render"
