"""`xplogent doctor`: each predictable first-run failure gets a plain fix."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from xplogent.core import doctor
from xplogent.core.config import load_config


def _by_name(checks, prefix):
    return [c for c in checks if c.name.startswith(prefix)]


def test_ollama_down_is_a_failure_with_fix(monkeypatch):
    def boom(host):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor, "_ollama_tags", boom)
    checks = doctor.run_checks(load_config())
    model = _by_name(checks, "Model (Ollama)")
    assert model and model[0].status == "fail"
    assert "ollama serve" in model[0].fix


def test_ollama_up_but_model_missing(monkeypatch):
    monkeypatch.setattr(doctor, "_ollama_tags",
                        lambda host: ["some-other-model:latest"])
    checks = doctor.run_checks(load_config())
    model = _by_name(checks, "Model (Ollama)")[0]
    assert model.status == "fail"
    assert "ollama pull llama3.1" in model.fix
    embed = _by_name(checks, "Embedding model")[0]
    assert embed.status == "warn"
    assert "ollama pull nomic-embed-text" in embed.fix


def test_ollama_ready(monkeypatch):
    monkeypatch.setattr(
        doctor, "_ollama_tags",
        lambda host: ["llama3.1:latest", "nomic-embed-text:latest"])
    checks = doctor.run_checks(load_config())
    assert _by_name(checks, "Model (Ollama)")[0].status == "ok"
    assert not _by_name(checks, "Embedding model")  # present → no warning


def test_hosted_provider_needs_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = load_config({"model": "openai:gpt-4o"})
    checks = doctor.run_checks(cfg)
    model = _by_name(checks, "Model (openai)")[0]
    assert model.status == "fail"
    assert "OPENAI_API_KEY" in model.detail

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    checks = doctor.run_checks(load_config({"model": "openai:gpt-4o"}))
    assert _by_name(checks, "Model (openai)")[0].status == "ok"


def test_broken_config_short_circuits(monkeypatch, tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("model: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("XPLOGENT_HOME", str(tmp_path))
    checks = doctor.run_checks()
    assert len(checks) == 1
    assert checks[0].status == "fail"
    assert "Invalid YAML" in checks[0].detail


def test_cli_doctor_exit_codes(monkeypatch):
    from xplogent.interfaces.cli.app import app

    monkeypatch.setattr(doctor, "_ollama_tags",
                        lambda host: ["llama3.1:latest", "nomic-embed-text:latest"])
    ok = CliRunner().invoke(app, ["doctor"])
    assert ok.exit_code == 0, ok.output
    assert "All essential checks passed" in ok.output

    monkeypatch.setattr(doctor, "_ollama_tags",
                        lambda host: (_ for _ in ()).throw(OSError("down")))
    bad = CliRunner().invoke(app, ["doctor"])
    assert bad.exit_code == 1
    assert "fix:" in bad.output


@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
def test_other_hosted_providers_need_keys(monkeypatch, provider):
    for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    cfg = load_config({"model": f"{provider}:some-model"})
    checks = doctor.run_checks(cfg)
    assert _by_name(checks, f"Model ({provider})")[0].status == "fail"
