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


def test_default_roles_only_name_existing_tools():
    """Every tool named in the shipped role profiles must be a real built-in
    tool (the slim-down removed web/team tools, leaving roles silently empty)."""
    from xplogent.core.config import _DEFAULT_CONFIG, _load_yaml
    from xplogent.tools.registry import ToolRegistry

    known = {t.name for t in ToolRegistry.from_config().all()}
    roles = _load_yaml(_DEFAULT_CONFIG).get("roles") or {}
    assert roles, "default.yaml must ship role profiles"
    for role, spec in roles.items():
        allowed = (spec or {}).get("allowed_tools", "*")
        if allowed == "*":
            continue
        missing = [name for name in allowed if name not in known]
        assert not missing, f"role '{role}' names tools that do not exist: {missing}"


def test_load_yaml_malformed_file_names_the_file(tmp_path):
    import pytest

    from xplogent.core.config import _load_yaml

    bad = tmp_path / "config.yaml"
    bad.write_text("model: [unclosed\n  bad indent: :\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML in config file"):
        _load_yaml(bad)


def test_load_yaml_non_mapping_is_a_clear_error(tmp_path):
    import pytest

    from xplogent.core.config import _load_yaml

    bad = tmp_path / "config.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must hold key: value pairs"):
        _load_yaml(bad)


def test_load_yaml_empty_and_missing_files_are_empty(tmp_path):
    from xplogent.core.config import _load_yaml

    assert _load_yaml(tmp_path / "missing.yaml") == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert _load_yaml(empty) == {}
