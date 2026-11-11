"""Conocimiento compartido por el equipo en una rama git del repo del pack ([pack.knowledge] en pack.toml).

La rama solo trae archivos .json exportados (ver KnowledgeStore.export): son datos, nunca se ejecuta nada de ella.
`pull` clona la rama y la importa sin duplicar; `push` une lo local con knowledge.json de la rama y lo sube.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config
from .knowledge import FORMAT, SIGNED, KnowledgeError, KnowledgeStore, parse_export

DEFAULT_BRANCH = "knowledge"
FILE = "knowledge.json"  # el archivo que escribe `push`; `pull` lee todos los .json de la rama
MAX_FILE_BYTES = 20 * 1024 * 1024
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


@dataclass
class Remote:
    repo: str
    branch: str

    def __str__(self) -> str:
        return f"{self.repo} (rama {self.branch})"


def remote(cfg: config.Config | None = None) -> Remote | None:
    """De dónde se comparte el conocimiento según el pack activo, o None si el pack no lo declara."""
    from . import packs

    pack = (cfg or config.load()).pack
    if not pack or pack.knowledge_branch is None:
        return None
    repo = pack.knowledge_repo or pack.repo
    if not repo:
        origin = packs.origin(pack.path.parent.name)
        repo = origin if origin and packs._is_git(origin) else None
    if not repo:
        raise KnowledgeError("el pack declara [pack.knowledge] pero no dice el repo: agrega repo en [pack]")
    return check(Remote(repo.removeprefix("git+"), pack.knowledge_branch))


def check(target: Remote) -> Remote:
    from . import packs

    if not _BRANCH.match(target.branch) or ".." in target.branch:
        raise KnowledgeError(f"nombre de rama inválido: {target.branch!r}")
    if not packs._is_git(target.repo) and not (not target.repo.startswith("-") and Path(target.repo).is_dir()):
        raise KnowledgeError(f"repo inválido: {target.repo!r} (tiene que ser una URL git: git@…, ssh://… o https://….git)")
    return target


def _git(*args: str, cwd: Path | None = None, interactive: bool = False) -> subprocess.CompletedProcess:
    # LC_ALL=C: los mensajes de git se leen para distinguir «la rama no existe» y «push rechazado».
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "1" if interactive else "0"}
    if not interactive:
        # Sin terminal (visor, pack update --yes) un ssh que pide passphrase se quedaría colgado.
        env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    try:
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=180,
                              stdin=None if interactive else subprocess.DEVNULL)
    except FileNotFoundError:
        raise KnowledgeError("no encontré git en el PATH") from None
    except subprocess.TimeoutExpired:
        raise KnowledgeError("git tardó demasiado: revisa la red, la VPN o el acceso al repo") from None


def _clone(target: Remote, folder: Path, interactive: bool) -> bool:
    """Clona solo la rama. False si el repo responde pero la rama todavía no existe."""
    got = _git("clone", "--depth", "1", "--single-branch", "--branch", target.branch, "--quiet", "--",
               target.repo, str(folder), interactive=interactive)
    if got.returncode == 0:
        return True
    detail = got.stderr.strip() or got.stdout.strip()
    if "not found in upstream" in detail or "Could not find remote branch" in detail:
        return False
    raise KnowledgeError(f"no pude leer {target}: {detail.splitlines()[-1] if detail else got.returncode}")


def _load(path: Path) -> list:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise KnowledgeError(f"{path.name} supera {MAX_FILE_BYTES // 1024 // 1024} MB")
    try:
        return parse_export(json.loads(path.read_text()))[0]
    except ValueError as exc:
        raise KnowledgeError(f"{path.name}: {exc}") from None


def pull(db: KnowledgeStore, target: Remote, interactive: bool = False, dry_run: bool = False) -> dict:
    """Trae la rama e importa lo que falta (con dry_run, solo cuenta). Devuelve el resumen de import_entries más
    `files`."""
    total = {"new": 0, "existing": 0, "updated": 0, "kept": 0, "invalid": 0, "files": 0, "items": []}
    with tempfile.TemporaryDirectory(prefix="casebook-knowledge-") as tmp:
        if not _clone(target, Path(tmp), interactive):
            raise KnowledgeError(f"la rama {target.branch} todavía no existe en {target.repo}: "
                                 "créala con `casebook knowledge push`")
        for path in sorted(Path(tmp).glob("*.json")):
            if path.is_symlink():
                continue
            result = db.import_entries(_load(path), origin=f"{target.branch} · {path.name}", dry_run=dry_run)
            total["files"] += 1
            for key, value in result.items():
                total[key] += value
    total["items"] = total["items"][:200]
    return total


def _key(entry: dict) -> tuple:
    return tuple(str(entry.get(k)) for k in SIGNED[:4])


def merge(remote_entries: list, local_entries: list) -> tuple[list, dict]:
    """Lo de la rama más lo local. En la misma entidad con distinto contenido gana la más reciente."""
    merged = {_key(e): e for e in remote_entries if isinstance(e, dict)}
    stats = {"new": 0, "updated": 0}
    for entry in local_entries:
        old = merged.get(_key(entry))
        if old is None:
            stats["new"] += 1
        elif old.get("signature") == entry["signature"] or str(old.get("updated_at", "")) >= entry["updated_at"]:
            continue
        else:
            stats["updated"] += 1
        merged[_key(entry)] = entry
    return [merged[k] for k in sorted(merged)], stats


def push(db: KnowledgeStore, target: Remote, confirm: Callable[[str], bool] | None = None,
         interactive: bool = True) -> dict:
    """Une el conocimiento local con knowledge.json de la rama y lo sube. Si alguien subió antes, vuelve a unir."""
    local = db.export()["entries"]
    for attempt in range(3):
        with tempfile.TemporaryDirectory(prefix="casebook-knowledge-") as tmp:
            folder = Path(tmp)
            exists = _clone(target, folder, interactive)
            if not exists:
                for args in (("init", "--quiet"), ("checkout", "--quiet", "--orphan", target.branch),
                             ("remote", "add", "origin", target.repo)):
                    _run(folder, *args)
            file = folder / FILE
            entries, stats = merge(_load(file) if file.is_file() else [], local)
            if not stats["new"] and not stats["updated"]:
                return {**stats, "pushed": False, "total": len(entries)}
            summary = (f"Subir a {target}: {stats['new']} nuevas, {stats['updated']} actualizadas "
                       f"({len(entries)} en total). Lo verá todo el que tenga acceso al repo.")
            if attempt == 0 and confirm is not None and not confirm(summary):
                raise KnowledgeError("cancelado: no se subió nada")
            file.write_text(json.dumps({FORMAT: 1, "entries": entries}, ensure_ascii=False, indent=1) + "\n")
            _run(folder, "add", "--", FILE)
            _run(folder, "commit", "--quiet", "-m",
                 f"knowledge: {stats['new']} nuevas, {stats['updated']} actualizadas")
            got = _git("push", "--quiet", "origin", f"HEAD:refs/heads/{target.branch}", cwd=folder,
                       interactive=interactive)
            if got.returncode == 0:
                return {**stats, "pushed": True, "total": len(entries)}
            detail = got.stderr.strip() or got.stdout.strip()
            if not any(word in detail for word in ("rejected", "fetch first", "non-fast-forward")):
                raise KnowledgeError(f"no pude subir a {target}: {detail.splitlines()[-1] if detail else got.returncode}")
    raise KnowledgeError("otra persona está subiendo conocimiento al mismo tiempo: vuelve a intentarlo")


def _run(folder: Path, *args: str) -> None:
    got = _git(*args, cwd=folder)
    if got.returncode != 0:
        detail = got.stderr.strip() or got.stdout.strip()
        raise KnowledgeError(f"git {args[0]} falló: {detail.splitlines()[-1] if detail else got.returncode}")
