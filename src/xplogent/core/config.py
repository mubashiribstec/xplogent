"""Configuration loading.

Precedence (highest wins):
    1. Environment variables (``XPLOGENT_*`` and provider keys)
    2. User config at ``$XPLOGENT_HOME/config.yaml`` (default ``~/.xplogent``)
    3. Packaged ``xplogent/config/default.yaml``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Defaults ship inside the package (src/xplogent/config/default.yaml) so wheels and
# normal `pip install`s work, not only editable/source checkouts.
_DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "default.yaml"


def xplogent_home() -> Path:
    """Config/secrets/logs directory (``~/.xplogent``), creating it if needed."""
    home = Path(os.environ.get("XPLOGENT_HOME", Path.home() / ".xplogent"))
    home.mkdir(parents=True, exist_ok=True)
    return home


def install_root() -> Path | None:
    """The Xplogent source checkout root (``<root>/src/xplogent``), or None.

    Only our own ``src/`` layout counts. Walking up to *any* pyproject.toml/.git
    meant a wheel installed into some project's ``.venv`` treated that unrelated
    project as home and wrote skills + memory into it.
    """
    package = Path(__file__).resolve().parents[1]  # .../xplogent
    if package.parent.name != "src":
        return None
    root = package.parent.parent
    if (root / "pyproject.toml").exists() or (root / ".git").exists():
        return root
    return None


_migrated = False


def data_dir() -> Path:
    """Fixed location for **skills + memory**.

    On Windows this is ``C:\\Users\\<current user>\\.xplogent`` — the user part is
    resolved at runtime via ``Path.home()``, so it works for any account (admin,
    mubashir, IBSTEC-DEV, …). ``XPLOGENT_HOME`` still overrides everywhere (used by
    tests/advanced setups). On non-Windows it falls back to the install folder, or
    ``~/.xplogent`` for wheel installs with no source tree.
    """
    env = os.environ.get("XPLOGENT_HOME")
    if env:
        base = Path(env)
    elif os.name == "nt":
        base = Path.home() / ".xplogent"  # C:\Users\<current user>\.xplogent
    else:
        root = install_root()
        base = (root / "data") if root else xplogent_home()
    base.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data(base)
    return base


def _migrate_legacy_data(base: Path) -> None:
    """One-time copy of an existing ~/.xplogent db + skills into the new data dir."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    legacy = Path.home() / ".xplogent"
    try:
        if base.resolve() == legacy.resolve():
            return
        import shutil

        if not (base / "xplogent.db").exists() and (legacy / "xplogent.db").exists():
            shutil.copy2(legacy / "xplogent.db", base / "xplogent.db")
        if not (base / "skills").exists() and (legacy / "skills").is_dir():
            shutil.copytree(legacy / "skills", base / "skills")
    except OSError:
        pass  # best-effort migration


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    """Resolved runtime configuration."""

    model: str = "ollama:llama3.1"
    reflection_model: str = "ollama:llama3.1"
    embedding_model: str = "ollama:nomic-embed-text"
    vision_model: str = ""  # empty → use `model`; set to a vision-capable model
    agent: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    skills: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    voip: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, Any] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    home: Path = field(default_factory=xplogent_home)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors -------------------------------------------------
    @property
    def provider_name(self) -> str:
        return self.model.split(":", 1)[0]

    @property
    def model_name(self) -> str:
        return self.model.split(":", 1)[1] if ":" in self.model else self.model

    def split_model(self, spec: str) -> tuple[str, str]:
        """Split a ``provider:model`` spec into its parts."""
        provider, _, name = spec.partition(":")
        return provider, name or provider

    @property
    def db_path(self) -> Path:
        # Memory (the SQLite DB) is pinned to the install data dir.
        return data_dir() / "xplogent.db"

    @property
    def skills_dir(self) -> Path:
        d = data_dir() / "skills"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # A hand-edited config with a typo must say *which file* is broken and how
        # to recover, not die with a raw parser traceback at startup.
        raise ValueError(
            f"Invalid YAML in config file {path}: {exc} — "
            "fix the file (or delete it to go back to defaults) and try again."
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(
            f"Config file {path} must hold key: value pairs, "
            f"not a {type(data).__name__} — fix the file (or delete it) and try again."
        )
    return data


# ─── dotenv + writable config (used by the setup wizard and the GUI) ──────────
_SECRET_KEYS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GOOGLE_API_KEY",
                "GEMINI_API_KEY", "TELNYX_API_KEY", "OLLAMA_API_KEY", "SIP_PASSWORD",
                "SIP_EXTENSION_PASSWORD", "XPLOGENT_CRM_API_TOKEN"]
# Non-secret settings the dashboard may also write to .env. Anything else is refused so
# an API call can't plant e.g. LD_PRELOAD / PYTHONPATH into the process environment.
_ENV_SETTINGS = ["XPLOGENT_MODEL", "XPLOGENT_REFLECTION_MODEL", "XPLOGENT_EMBEDDING_MODEL",
                 "OLLAMA_HOST", "XPLOGENT_LOG_LEVEL"]


def allowed_env_key(key: str) -> bool:
    return key in _SECRET_KEYS or key in _ENV_SETTINGS


def valid_env_value(value: str) -> bool:
    """Values are written one-per-line to .env: no line breaks or NULs."""
    return not any(c in str(value) for c in ("\n", "\r", "\x00"))


def env_path() -> Path:
    return xplogent_home() / ".env"


def _read_env_file() -> dict[str, str]:
    path = env_path()
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


_secrets_migrated = False


def _migrate_plaintext_secrets() -> None:
    """One-time: lift plaintext ``.env`` provider keys into the encrypted store
    and strip them from disk, so secrets aren't left in cleartext at rest."""
    global _secrets_migrated
    if _secrets_migrated:
        return
    _secrets_migrated = True
    try:
        from xplogent.core import secrets as _sec

        if not _sec.available():
            return
        env = _read_env_file()
        leaked = {k: v for k, v in env.items() if k in _SECRET_KEYS and v}
        if not leaked:
            return
        if _sec.write_secrets(leaked):
            # Rewrite .env keeping only non-secret entries.
            kept = {k: v for k, v in env.items() if k not in _SECRET_KEYS}
            path = env_path()
            if kept:
                path.write_text("\n".join(f"{k}={v}" for k, v in kept.items()) + "\n",
                                encoding="utf-8")
            elif path.exists():
                path.unlink()
    except Exception:  # noqa: BLE001 - migration must never break startup
        pass


def load_dotenv() -> None:
    """Load secrets into ``os.environ`` without overriding real env vars.

    Reads the encrypted secrets store first (the system of record), then any
    remaining plaintext ``~/.xplogent/.env`` entries for backward compatibility.
    """
    try:
        from xplogent.core import secrets as _sec

        _sec.load_into_env(_SECRET_KEYS)
    except Exception:  # noqa: BLE001
        pass
    for key, value in _read_env_file().items():
        # An empty variable (e.g. `OLLAMA_HOST=` from a Docker .env) must not hide a
        # value saved from the dashboard.
        if key and not os.environ.get(key):
            os.environ[key] = value
    _migrate_plaintext_secrets()


def save_env(updates: dict[str, str]) -> None:
    """Persist ``KEY=VALUE`` pairs. Provider keys go to the encrypted store;
    everything else is merged into ``~/.xplogent/.env``."""
    from xplogent.core import secrets as _sec

    bad = [k for k, v in updates.items() if not allowed_env_key(k) or not valid_env_value(v)]
    if bad:
        raise ValueError(f"refusing to save unknown key or invalid value: {', '.join(bad)}")
    secret_updates = {k: v for k, v in updates.items() if k in _SECRET_KEYS and v}
    plain_updates = {k: v for k, v in updates.items() if k not in _SECRET_KEYS}

    encrypted = bool(secret_updates) and _sec.write_secrets(secret_updates)
    # Anything not stored encrypted (incl. secrets when crypto is unavailable)
    # still lands in .env so the keys aren't lost.
    if not encrypted:
        plain_updates = {**plain_updates, **secret_updates}
    for k, v in updates.items():
        if v:
            os.environ[k] = v  # reflect immediately in-process

    if plain_updates:
        existing = _read_env_file()
        existing.update({k: v for k, v in plain_updates.items() if v})
        path = env_path()
        path.write_text("\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
                        encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass


def secret_status() -> dict[str, bool]:
    """Which provider keys are set (without revealing them)."""
    return {k: bool(os.environ.get(k)) for k in _SECRET_KEYS}


def save_user_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``updates`` into ``~/.xplogent/config.yaml`` and return the merged dict."""
    path = xplogent_home() / "config.yaml"
    current = _load_yaml(path)
    merged = _deep_merge(current, updates)
    path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    return merged


def load_config(overrides: dict[str, Any] | None = None) -> Config:
    """Build a :class:`Config` from defaults, user file, env, and explicit overrides."""
    load_dotenv()
    data = _load_yaml(_DEFAULT_CONFIG)

    user_cfg = xplogent_home() / "config.yaml"
    user = _load_yaml(user_cfg)
    data = _deep_merge(data, user)

    # Environment variables are defaults (e.g. set by the Docker installer); a model
    # chosen in the dashboard (saved in the user config) wins over them.
    for env, key in (("XPLOGENT_MODEL", "model"),
                     ("XPLOGENT_REFLECTION_MODEL", "reflection_model"),
                     ("XPLOGENT_EMBEDDING_MODEL", "embedding_model")):
        value = os.environ.get(env)
        if value and not user.get(key):
            data[key] = value

    if overrides:
        data = _deep_merge(data, overrides)

    return Config(
        model=data.get("model", "ollama:llama3.1"),
        reflection_model=data.get("reflection_model", data.get("model", "ollama:llama3.1")),
        embedding_model=data.get("embedding_model", "ollama:nomic-embed-text"),
        vision_model=data.get("vision_model", ""),
        agent=data.get("agent", {}),
        memory=data.get("memory", {}),
        safety=data.get("safety", {}),
        tools=data.get("tools", {}),
        execution=data.get("execution", {}),
        skills=data.get("skills", {}),
        budget=data.get("budget", {}),
        voip=data.get("voip", {}),
        roles=data.get("roles", {}),
        models=data.get("models", []),
        raw=data,
    )
