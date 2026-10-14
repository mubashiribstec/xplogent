"""Backup / restore and knowledge export/import.

A backup is a ``.tar.gz`` of the SQLite DB (snapshotted via the consistent
``sqlite3`` backup API so it's safe while the server runs), the ``skills/``
directory, and ``config.yaml``. Secrets (``.env``) are excluded unless opted in.

Knowledge export/import moves just facts + skills (with their embeddings and the
embedding-model that produced them) as portable JSON between instances.
"""

from __future__ import annotations

import os
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from xplogent.core.config import data_dir, env_path, xplogent_home
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store

_log = get_logger("backup")


def _snapshot_db(db_path: Path, out: Path) -> None:
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(out))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


_KEEP_BACKUPS = 10
_SECRET_FILES = ("secrets.enc", "secret.key")


def _keyring_only_key(data: Path) -> bytes | None:
    """The key that decrypts ``secrets.enc`` when it is *not* in a key file (OS keyring)."""
    if (data / "secret.key").exists() or not (data / "secrets.enc").exists():
        return None
    try:
        from xplogent.core import secrets

        _values, key, ok = secrets._decrypt_store()
        return key if ok and key else None
    except Exception:  # noqa: BLE001 - backup without the key rather than no backup
        return None


def create_backup(dest: Path | str | None = None, *, include_secrets: bool = False) -> dict[str, Any]:
    """Write a .tar.gz of DB + skills + config. Returns ``{ok, path, size}``.

    With ``include_secrets`` it also carries the encrypted key store *and* its key file
    (useless apart) plus any plaintext ``.env``, so a restore really brings the API keys back.
    """
    home = xplogent_home()
    auto = dest is None
    if dest is None:
        backups = home / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        dest = backups / f"xplogent-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Memory (DB) + skills live in the install data dir; config/.env in home.
    data = data_dir()
    db = data / "xplogent.db"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "xplogent.db"
        if db.exists():
            _snapshot_db(db, tmp_db)
        with tarfile.open(dest, "w:gz") as tar:
            if tmp_db.exists():
                tar.add(tmp_db, arcname="xplogent.db")
            skills = data / "skills"
            if skills.is_dir():
                tar.add(skills, arcname="skills")
            cfg = home / "config.yaml"
            if cfg.exists():
                tar.add(cfg, arcname="config.yaml")
            if include_secrets:
                if env_path().exists():
                    tar.add(env_path(), arcname=".env")
                for name in _SECRET_FILES:
                    if (data / name).exists():
                        tar.add(data / name, arcname=name)
                key = _keyring_only_key(data)
                if key:
                    # Windows / desktop keyrings hold the key outside the data dir: without
                    # it in the archive, a restore on another machine can't read any key.
                    key_tmp = Path(tmp) / "secret.key"
                    key_tmp.write_bytes(key)
                    tar.add(key_tmp, arcname="secret.key")
    try:
        dest.chmod(0o600)  # may contain secrets / the whole memory DB
    except OSError:
        pass
    if auto:
        _prune(dest.parent)
    return {"ok": True, "path": str(dest), "size": dest.stat().st_size}


def _prune(folder: Path, keep: int = _KEEP_BACKUPS) -> None:
    olds = sorted(folder.glob("xplogent-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    for old in olds[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def _restore_target(name: str, data: Path, home: Path) -> Path | None:
    """Where an archive member may go, or None if it isn't something a backup contains."""
    if name == "xplogent.db" or name in _SECRET_FILES or name == "skills" or name.startswith("skills/"):
        return data
    if name in ("config.yaml", ".env"):
        return home
    return None


def restore_backup(path: Path | str) -> dict[str, Any]:
    """Restore a backup: DB + skills (+ secrets) → the data dir, config/.env → home.

    Only regular files and directories are accepted. Links, devices and anything whose
    path would leave its destination are skipped, so a crafted archive can't overwrite
    files elsewhere (tar-slip via ``..``, absolute paths, symlinks or hardlinks).
    """
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": f"no such file: {path}"}
    home = xplogent_home()
    data = data_dir()
    restored: list[str] = []
    skipped: list[str] = []
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            name = m.name.replace("\\", "/")
            while name.startswith("./"):
                name = name[2:]
            name = name.rstrip("/")
            base = _restore_target(name, data, home)
            if base is None or not (m.isfile() or m.isdir()):
                skipped.append(m.name)
                continue
            if _extract_safe(tar, m, name, base):
                restored.append(name)
            else:
                skipped.append(m.name)
    if skipped:
        _log.warning("restore skipped %d unexpected/unsafe member(s): %s", len(skipped), skipped[:10])
    return {"ok": True, "restored_to": str(data), "restored": len(restored), "skipped": skipped}


def _inside(base: Path, target: Path) -> bool:
    base = base.resolve()
    return target == base or base in target.parents


def _extract_safe(tar: tarfile.TarFile, member: tarfile.TarInfo, name: str, base: Path) -> bool:
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    target = base / name
    # Resolve the *parent* (which must already be safe) and never follow a link at the
    # final component: an existing symlink there is removed, not written through.
    if not _inside(base, target.parent.resolve() / target.name):
        return False
    if member.isdir():
        if target.is_symlink():
            target.unlink()
        target.mkdir(parents=True, exist_ok=True)
        return _inside(base, target.resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    if not _inside(base, target.parent.resolve()):
        return False
    src = tar.extractfile(member)
    if src is None:
        return False
    tmp = target.with_name(target.name + ".restore-tmp")
    with src, open(tmp, "wb") as out:
        while chunk := src.read(1 << 20):
            out.write(chunk)
    if target.is_symlink():
        target.unlink()
    os.replace(tmp, target)
    if name == "xplogent.db":
        # A stale write-ahead log from the old DB would be replayed onto the restored one.
        for suffix in ("-wal", "-shm"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
    if name in _SECRET_FILES or name == ".env":
        try:
            target.chmod(0o600)
        except OSError:
            pass
    return True


# ── knowledge (facts + skills) export / import ───────────────────────────────
def export_knowledge(store: Store) -> dict[str, Any]:
    return {
        "version": 1,
        "exported_at": time.time(),
        "facts": [
            {"content": f.content, "embedding": f.embedding, "source": f.source,
             "embed_model": f.embed_model}
            for f in store.all_facts()
        ],
        "skills": [
            {"name": s.name, "description": s.description, "body": s.body,
             "embedding": s.embedding, "uses": s.uses, "successes": s.successes,
             "failures": s.failures, "embed_model": s.embed_model}
            for s in store.all_skills()
        ],
    }


def import_knowledge(store: Store, data: dict[str, Any]) -> dict[str, Any]:
    """Merge exported facts + skills into the store. Returns counts + any warnings."""
    existing_facts = {f.content for f in store.all_facts()}
    existing_skills = {s.name for s in store.all_skills()}
    models = {s.embed_model for s in store.all_skills() if s.embed_model}
    models |= {f.embed_model for f in store.all_facts() if f.embed_model}

    added_f = added_s = 0
    warn: list[str] = []
    for f in data.get("facts", []):
        if f["content"] in existing_facts:
            continue
        store.add_fact(f["content"], f.get("embedding") or [], f.get("source", "import"),
                       embed_model=f.get("embed_model", ""))
        added_f += 1
    for s in data.get("skills", []):
        if s["name"] in existing_skills:
            continue
        store.upsert_skill(s["name"], s.get("description", ""), s.get("body", ""),
                           s.get("embedding") or [], embed_model=s.get("embed_model", ""))
        added_s += 1

    imported_models = {x.get("embed_model") for x in
                       data.get("facts", []) + data.get("skills", []) if x.get("embed_model")}
    if models and imported_models and not (models & imported_models):
        warn.append(f"embedding model mismatch (have {sorted(models)}, "
                    f"imported {sorted(imported_models)}); semantic recall on imported "
                    "items may be poor until re-embedded.")
    return {"ok": True, "facts_added": added_f, "skills_added": added_s, "warnings": warn}
