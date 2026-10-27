"""Health checks for ``xplogent doctor``.

A fresh install fails in a handful of predictable ways: a typo'd config file,
no API key for the chosen provider, Ollama not running (or the model not
pulled), the dashboard never built, the port already taken. Each check here
returns a plain-English verdict plus the fix, so a new user can self-serve
instead of reading a traceback. Checks never mutate anything.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

import httpx

from xplogent.core.config import Config, load_config, secret_status

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str
    fix: str = ""


# Provider → the env/secret key that must be set for it (Ollama needs none).
_PROVIDER_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "vercel": ("AI_GATEWAY_API_KEY",),
}


def _ollama_tags(host: str) -> list[str]:
    """Model names the Ollama server reports (raises on any failure)."""
    resp = httpx.get(f"{host}/api/tags", timeout=3.0)
    resp.raise_for_status()
    return [str(m.get("name", "")) for m in resp.json().get("models", [])]


def _model_present(tags: list[str], model: str) -> bool:
    return any(t == model or t.startswith(model + ":") for t in tags)


def _check_config(cfg: Config) -> Check:
    return Check("Config", OK,
                 f"loaded; model is {cfg.model} (data in {cfg.db_path.parent})")


def _check_database(cfg: Config) -> Check:
    try:
        from xplogent.memory.store import Store

        with Store(cfg.db_path) as store:
            facts = len(store.all_facts())
            skills = len(store.all_skills())
        return Check("Memory database", OK,
                     f"{cfg.db_path.name} opens fine ({facts} facts, {skills} skills stored)")
    except Exception as exc:  # noqa: BLE001 - the fix text is the point
        return Check("Memory database", FAIL, f"cannot open {cfg.db_path}: {exc}",
                     "Check the folder is writable, or delete a corrupted xplogent.db "
                     "(you'll lose memory) and restart.")


def _check_api_extra() -> Check:
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

        return Check("Dashboard/API package", OK, "FastAPI + Uvicorn installed")
    except ImportError:
        return Check("Dashboard/API package", FAIL, "FastAPI/Uvicorn not installed",
                     "Run: pip install 'xplogent[api]' (the CLI still works without it).")


def _check_dashboard_build() -> Check:
    from xplogent.interfaces.api.server import _dashboard_dir

    found = _dashboard_dir()
    if found is not None:
        return Check("Dashboard build", OK, f"serving from {found}")
    return Check("Dashboard build", WARN, "no built dashboard found (API-only mode)",
                 "Build it once: cd web && npm install && npm run build "
                 "(needs Node.js), or use the Docker install which includes it.")


def _check_model(cfg: Config) -> list[Check]:
    provider, model = cfg.provider_name, cfg.model_name
    if provider == "ollama":
        from xplogent.providers.ollama import ollama_settings

        host, _token = ollama_settings()
        try:
            tags = _ollama_tags(host)
        except Exception:  # noqa: BLE001 - unreachable is the finding
            return [Check("Model (Ollama)", FAIL, f"no answer from Ollama at {host}",
                          "Start Ollama (run `ollama serve`, or launch the app), or set "
                          "OLLAMA_HOST if it runs elsewhere. No Ollama? Pick a hosted "
                          "model in Settings instead.")]
        checks = []
        if _model_present(tags, model):
            checks.append(Check("Model (Ollama)", OK, f"{model} is pulled and Ollama answers"))
        else:
            checks.append(Check("Model (Ollama)", FAIL,
                                f"Ollama is up but model '{model}' is not pulled",
                                f"Run: ollama pull {model}"))
        embed = cfg.embedding_model
        if embed.startswith("ollama:"):
            name = embed.split(":", 1)[1]
            if not _model_present(tags, name):
                checks.append(Check("Embedding model", WARN,
                                    f"'{name}' not pulled (memory search will be limited)",
                                    f"Run: ollama pull {name}"))
        return checks
    keys = _PROVIDER_KEYS.get(provider)
    if keys is None:
        return [Check("Model", WARN, f"provider '{provider}' — no built-in check",
                      "If chats fail, verify the provider name and its API key.")]
    status = secret_status()
    if any(status.get(k) for k in keys):
        return [Check(f"Model ({provider})", OK, f"{model}; API key is set")]
    return [Check(f"Model ({provider})", FAIL,
                  f"no API key set for {provider} ({' or '.join(keys)})",
                  "Run `xplogent setup` or add the key in Settings → Providers.")]


def _check_port(port: int) -> Check:
    try:
        from xplogent.core import service

        if service.status().get("healthy"):
            return Check("Server port", OK, "your background server is already running")
    except Exception:  # noqa: BLE001 - status probing must never break doctor
        pass
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return Check("Server port", OK, f"port {port} is free")
    except OSError:
        return Check("Server port", WARN, f"port {port} is already in use",
                     "Stop the other program, or start on another port: xplogent up --port 8766")
    finally:
        sock.close()


def _check_voip(cfg: Config) -> Check | None:
    if not (cfg.voip or {}).get("enabled"):
        return None
    from xplogent.voip.config_check import check_voip

    problems = check_voip(cfg.voip or {})
    if problems:
        return Check("Phone agent", FAIL, "config problems: " + "; ".join(problems),
                     "Fix the voip: section of your config (see docs/voip.md).")
    return Check("Phone agent", OK, "enabled and the config checks pass "
                                    "(live carrier/PBX registration isn't tested here)")


def run_checks(cfg: Config | None = None, *, port: int = 8765) -> list[Check]:
    """Run every check. A config that won't even load short-circuits the rest."""
    if cfg is None:
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return [Check("Config", FAIL, str(exc),
                          "Fix or delete the file named above, then run doctor again.")]
    checks = [_check_config(cfg), _check_database(cfg), _check_api_extra(),
              _check_dashboard_build(), *_check_model(cfg), _check_port(port)]
    voip = _check_voip(cfg)
    if voip is not None:
        checks.append(voip)
    return checks
