"""Skills + memory are pinned to the install data dir (XPLOGENT_HOME still overrides)."""

from __future__ import annotations

import os
from pathlib import Path

from xplogent.core import config


def test_xplogent_home_overrides_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_migrated", True)  # skip legacy copy
    assert config.data_dir() == tmp_path
    cfg = config.load_config()
    assert cfg.db_path == tmp_path / "xplogent.db"
    assert cfg.skills_dir == tmp_path / "skills"


def test_install_root_is_the_repo():
    root = config.install_root()
    assert root is not None
    assert (root / "pyproject.toml").exists()


def test_default_data_dir_is_install_data(monkeypatch):
    monkeypatch.delenv("XPLOGENT_HOME", raising=False)
    monkeypatch.setattr(config, "_migrated", True)  # don't copy ~/.xplogent during tests
    d = config.data_dir()
    if os.name == "nt":  # per-user folder on Windows, whatever the install location
        assert d == Path.home() / ".xplogent"
        return
    assert d.parent == config.install_root()
    assert d.name == "data"
