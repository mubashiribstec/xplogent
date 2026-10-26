"""Encrypted secrets at rest.

Provider API keys are stored **Fernet-encrypted** in ``data_dir()/secrets.enc``
instead of as plaintext in ``.env``. The symmetric key lives in the OS keyring
when ``keyring`` is installed, otherwise in a ``0600`` key file beside the store.
A one-time migration lifts any existing plaintext ``.env`` secrets into the
encrypted store.

Everything degrades gracefully: if ``cryptography`` is unavailable, the helpers
become no-ops and the caller falls back to the plaintext ``.env`` path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xplogent.core.config import data_dir

_KEYRING_SERVICE = "xplogent"
_KEYRING_USER = "fernet-key"


def available() -> bool:
    """True when Fernet encryption is usable.

    Catches ``BaseException`` because a broken native build of ``cryptography``
    (e.g. missing ``_cffi_backend``) raises a pyo3 ``PanicException`` — a
    ``BaseException`` — on import. Such a machine must degrade to the plaintext
    ``.env`` path rather than crash.
    """
    try:
        import cryptography.fernet  # noqa: F401
        return True
    except BaseException:  # noqa: B036 - degrade gracefully on any import failure
        return False


def _store_path() -> Path:
    return data_dir() / "secrets.enc"


def _key_file() -> Path:
    return data_dir() / "secret.key"


def _keyring_key() -> bytes | None:
    """The key stored in the OS keyring, or None (no backend / locked / not set)."""
    try:
        import keyring

        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
        return existing.encode("ascii") if existing else None
    except Exception:  # noqa: BLE001 - no usable keyring backend
        return None


def _candidate_keys() -> list[bytes]:
    """Every key that might have encrypted the store, without creating any.

    Order matters only for speed: whichever key actually decrypts the store wins, so a
    keyring that appears (or gets locked) after the key file was used can never make us
    "lose" the secrets by silently switching keys.
    """
    keys: list[bytes] = []
    path = _key_file()
    if path.exists():
        keys.append(path.read_bytes().strip())
    kr = _keyring_key()
    if kr and kr not in keys:
        keys.append(kr)
    return keys


def _load_or_create_key() -> bytes:
    """Return the Fernet key to use, creating one only when none exists anywhere.

    An existing key file always wins (stable across keyring changes); otherwise an
    existing keyring key; otherwise a new key, stored in the keyring if possible, else in
    a 0600 key file in the data dir.
    """
    from cryptography.fernet import Fernet

    existing = _candidate_keys()
    if existing:
        return existing[0]
    key = Fernet.generate_key()
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USER, key.decode("ascii"))
        if keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER) == key.decode("ascii"):
            return key
    except Exception:  # noqa: BLE001 - no keyring backend; fall through to a key file
        pass
    path = _key_file()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _decrypt_store() -> tuple[dict[str, str], bytes | None, bool]:
    """Return ``(secrets, key_used, ok)``. ``ok`` is False when a store exists but no
    known key can decrypt it — callers must then refuse to overwrite it."""
    from cryptography.fernet import Fernet

    path = _store_path()
    if not path.exists():
        return {}, None, True
    blob = path.read_bytes()
    for key in _candidate_keys():
        try:
            raw = Fernet(key).decrypt(blob)
            data = json.loads(raw.decode("utf-8"))
            return {str(k): str(v) for k, v in data.items()}, key, True
        except BaseException:  # noqa: B036,S112 - wrong key; try the next one
            continue
    return {}, None, False


def read_secrets() -> dict[str, str]:
    """Decrypt and return the stored secrets (``{}`` if none / unavailable)."""
    if not available():
        return {}
    try:
        data, _key, _ok = _decrypt_store()
        return data
    except BaseException:  # noqa: B036 - broken crypto → behave as empty
        return {}


def write_secrets(updates: dict[str, str], remove: tuple[str, ...] | list[str] = ()) -> bool:
    """Merge ``updates`` into the encrypted store (and drop the ``remove`` names). Returns
    False if unavailable, or if an existing store can't be decrypted (it is never
    overwritten in that case)."""
    if not available():
        return False
    try:
        from cryptography.fernet import Fernet

        current, key, ok = _decrypt_store()
        if not ok:
            return False  # e.g. keyring locked: keep the old store intact
        key = key or _load_or_create_key()
        for k, v in updates.items():
            if v:
                current[k] = v
        for k in remove:
            current.pop(k, None)
        blob = Fernet(key).encrypt(json.dumps(current).encode("utf-8"))
        path = _store_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)  # atomic: a crash can't leave a half-written store
    except BaseException:  # noqa: B036 - never let a crypto failure lose the caller's keys
        return False
    # Reflect immediately in-process so the running server sees new keys.
    for k, v in updates.items():
        if v:
            os.environ[k] = v
    return True


def load_into_env(keys: list[str]) -> None:
    """Load stored secrets into ``os.environ`` (without clobbering real env vars)."""
    if not available():
        return
    stored = read_secrets()
    for k in keys:
        if k in stored and not os.environ.get(k):
            os.environ[k] = stored[k]
