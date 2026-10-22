"""Skills de Claude Code: las del pack activo (packs/<nombre>/skills) y cómo están instaladas en ~/.claude/skills.

Sigue las reglas de install.sh: se instala con un symlink al pack, y solo se crea, repara o quita un symlink que
apunta al pack. Una copia, un link a otro lado o cualquier otra skill del usuario se muestra y no se toca. (En la
API y el visor se siguen llamando «repo»: es de donde salen.)
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config


def pack_skills_dir() -> Path:
    """Las skills del pack activo; sin pack, una carpeta que no existe (no hay skills propias)."""
    pack = config.active_pack()
    return pack.parent / "skills" if pack else config.PACKS_DIR / ".sin-pack" / "skills"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_PREVIEW_BYTES = 200_000


def user_skills_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "skills"


class SkillError(ValueError):
    pass


# Estados de una skill del repo en ~/.claude/skills.
LINKED = "linked"  # symlink al repo: siempre al día
MISSING = "missing"  # no está instalada
BROKEN = "broken"  # symlink al repo que ya no lleva a ningún lado (se movió el repo)
FOREIGN_LINK = "foreign_link"  # symlink a otra carpeta
COPY = "copy"  # carpeta real (copia): puede quedar desactualizada


@dataclass
class Paths:
    repo: Path
    user: Path


def _paths(repo: Path | None, user: Path | None) -> Paths:
    return Paths(repo or pack_skills_dir(), user or user_skills_dir())


def _frontmatter(text: str) -> dict[str, str]:
    """name y description del bloque --- inicial (YAML simple de una línea por clave)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("name", "description"):
            out[key.strip()] = value.strip().strip("'\"")
    return out


def _files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(folder).parts))


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in _files(folder):
        h.update(str(p.relative_to(folder)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _info(folder: Path) -> dict[str, Any]:
    skill_md = folder / "SKILL.md"
    text = skill_md.read_text(errors="replace") if skill_md.is_file() else ""
    meta = _frontmatter(text)
    files = _files(folder)
    mtime = max((p.stat().st_mtime for p in files), default=None)
    return {
        "description": meta.get("description", ""),
        "declared_name": meta.get("name"),
        "files": [str(p.relative_to(folder)) for p in files],
        "bytes": sum(p.stat().st_size for p in files),
        "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "tokens": round(len(text) / 4),  # aproximado
        "modified": datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds") if mtime else None,
    }


def _points_into(link: Path, root: Path) -> bool:
    """Como points_here de install.sh: el symlink resuelve (aunque esté roto) a algo dentro de `root`."""
    return link.is_symlink() and Path(os.path.realpath(link)).is_relative_to(root.resolve())


def _state(name: str, p: Paths) -> dict[str, Any]:
    src = p.repo / name
    dst = p.user / name
    link_target = os.readlink(dst) if dst.is_symlink() else None
    if dst.is_symlink():
        if not dst.exists():
            status = BROKEN if _points_into(dst, p.repo.parent) else FOREIGN_LINK
        elif dst.resolve() == src.resolve():
            status = LINKED
        else:
            status = FOREIGN_LINK
    elif dst.is_dir():
        status = COPY
    elif dst.exists():
        status = FOREIGN_LINK  # un archivo con ese nombre: no es nuestro
    else:
        status = MISSING
    out = {"status": status, "installed_path": str(dst), "link_target": link_target}
    if status == COPY:
        out["same_as_repo"] = _digest(dst) == _digest(src)
    # Se puede instalar o reparar si no hay nada o si lo que hay es un link a este repo.
    out["can_install"] = status in (MISSING, BROKEN) or (status == FOREIGN_LINK and _points_into(dst, p.repo.parent))
    out["can_uninstall"] = status in (LINKED, BROKEN) or (status == FOREIGN_LINK and _points_into(dst, p.repo.parent))
    return out


def repo_names(repo: Path | None = None) -> list[str]:
    root = repo or pack_skills_dir()
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file() and _NAME.match(d.name))


def listing(repo: Path | None = None, user: Path | None = None) -> dict[str, Any]:
    p = _paths(repo, user)
    ours = repo_names(p.repo)
    repo_skills = [{"name": n, "source": "repo", "path": str(p.repo / n), **_info(p.repo / n), **_state(n, p)} for n in ours]
    others = []
    if p.user.is_dir():
        for entry in sorted(p.user.iterdir(), key=lambda e: e.name.lower()):
            if entry.name in ours or entry.name.startswith(".") or not (entry / "SKILL.md").is_file():
                continue
            others.append({
                "name": entry.name,
                "source": "user",
                "path": str(entry),
                "link_target": os.readlink(entry) if entry.is_symlink() else None,
                **_info(entry),
            })
    return {"repo_dir": str(p.repo), "user_dir": str(p.user), "repo": repo_skills, "others": others}


def _find(name: str, p: Paths) -> tuple[str, Path]:
    if not _NAME.match(name):
        raise SkillError("nombre de skill inválido")
    if name in repo_names(p.repo):
        return "repo", p.repo / name
    entry = p.user / name
    if (entry / "SKILL.md").is_file():
        return "user", entry
    raise SkillError(f"no hay una skill llamada {name}")


def detail(name: str, repo: Path | None = None, user: Path | None = None) -> dict[str, Any]:
    """SKILL.md para la vista previa y, si la instalada es una copia distinta, el diff contra el repo."""
    p = _paths(repo, user)
    source, folder = _find(name, p)
    data = (folder / "SKILL.md").read_bytes()
    out: dict[str, Any] = {
        "name": name,
        "source": source,
        "skill_md": data[:MAX_PREVIEW_BYTES].decode(errors="replace"),
        "truncated": len(data) > MAX_PREVIEW_BYTES,
        "diff": None,
    }
    if source == "repo":
        state = _state(name, p)
        if state["status"] == COPY and not state["same_as_repo"]:
            out["diff"] = _diff(p.user / name, folder)
    return out


def _diff(installed: Path, repo: Path) -> str:
    chunks = []
    names = sorted({str(f.relative_to(installed)) for f in _files(installed)} | {str(f.relative_to(repo)) for f in _files(repo)})
    for rel in names:
        a, b = installed / rel, repo / rel
        left = a.read_text(errors="replace").splitlines(keepends=True) if a.is_file() else []
        right = b.read_text(errors="replace").splitlines(keepends=True) if b.is_file() else []
        chunks.extend(difflib.unified_diff(left, right, f"instalada/{rel}", f"repo/{rel}"))
    return "".join(chunks)[:MAX_PREVIEW_BYTES]


def install(name: str, repo: Path | None = None, user: Path | None = None) -> dict[str, Any]:
    p = _paths(repo, user)
    if name not in repo_names(p.repo):
        raise SkillError("solo se instalan desde aquí las skills de este repo")
    state = _state(name, p)
    if state["status"] == LINKED:
        return state
    if not state["can_install"]:
        raise SkillError(f"{state['installed_path']} ya existe y no es de este repo; no lo toco. Quítalo a mano y vuelve a intentar.")
    dst = p.user / name
    p.user.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink():
        dst.unlink()
    dst.symlink_to(p.repo / name)
    return _state(name, p)


def uninstall(name: str, repo: Path | None = None, user: Path | None = None) -> dict[str, Any]:
    p = _paths(repo, user)
    if name not in repo_names(p.repo):
        raise SkillError("solo se desinstalan desde aquí las skills de este repo")
    state = _state(name, p)
    if state["status"] == MISSING:
        return state
    if not state["can_uninstall"]:
        raise SkillError(f"{state['installed_path']} no es un link a este repo; no lo toco.")
    (p.user / name).unlink()
    return _state(name, p)
