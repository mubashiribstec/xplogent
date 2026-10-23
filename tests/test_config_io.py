"""Writable config + dotenv helpers."""

from __future__ import annotations

from xplogent.core import config as cfgmod


def test_save_and_load_user_config(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    monkeypatch.delenv("XPLOGENT_MODEL", raising=False)
    cfgmod.save_user_config({"model": "openai:gpt-4o", "memory": {"enabled": False}})
    cfg = cfgmod.load_config()
    assert cfg.model == "openai:gpt-4o"
    assert cfg.memory["enabled"] is False


def test_save_user_config_deep_merges(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    cfgmod.save_user_config({"safety": {"policy": {"low": "auto"}}})
    cfgmod.save_user_config({"safety": {"policy": {"high": "deny"}}})
    cfg = cfgmod.load_config()
    assert cfg.safety["policy"]["low"] == "auto"      # preserved
    assert cfg.safety["policy"]["high"] == "deny"     # added


def test_dotenv_loads_without_override(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    cfgmod.save_env({"OPENAI_API_KEY": "from-dotenv", "ANTHROPIC_API_KEY": "from-dotenv"})
    # simulate a fresh process: OPENAI unset, ANTHROPIC provided by the real env
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-env-key")
    cfgmod.load_dotenv()
    assert os.environ["OPENAI_API_KEY"] == "from-dotenv"     # filled from file
    assert os.environ["ANTHROPIC_API_KEY"] == "real-env-key" # real env wins


def test_secret_status(tmp_path, monkeypatch):
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfgmod.save_env({"OPENAI_API_KEY": "x"})
    status = cfgmod.secret_status()
    assert status["OPENAI_API_KEY"] is True
    assert status["OPENROUTER_API_KEY"] is False
