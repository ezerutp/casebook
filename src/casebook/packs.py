"""Packs: la configuración de un equipo (fuentes, Slack, textos, skills y conocimiento semilla), aparte de la app.

Un pack es una carpeta:

    pack.toml          mismo formato que config.toml más [pack] (name, version, display_name, requires) y,
                       opcional, [[requires.plugins]] (name, provides, path o pip)
    skills/<nombre>/   skills para los agentes (SKILL.md y sus archivos)
    knowledge/*.json   conocimiento semilla: se importa como pending, sin pisar lo que ya existe

Se instala desde esa carpeta, un .zip (el que arma `pack build`: pack/ + plugins/) o una URL git. Queda copiado en
PACKS_DIR/<name>, y PACKS_DIR/active dice cuál usa config.load() como capa base.

El pack es declarativo: no se ejecuta nada de lo que trae. Un plugin (código, un paquete de Python con proveedores)
puede venir en plugins/ del zip, pero solo se instala con pip si el usuario lo confirma aparte.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__, config, providers

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ORIGIN_FILE = ".origin.json"  # de dónde se instaló el pack, para `pack update`
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
# Lo que puede traer un pack (fuera de plugins/). Los scripts de las skills se copian sin permiso de ejecución.
PACK_SUFFIXES = {".toml", ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".png", ".jpg", ".jpeg", ".gif",
                 ".svg", ".webp", ".py", ".sh", ".js", ".ts", ""}
PLUGIN_SUFFIXES = {".py", ".toml", ".md", ".txt", ".cfg", ".json", ".in", ".typed", ""}


class PackError(ValueError):
    pass


# --- leer y validar -------------------------------------------------------------------------------------------


def manifest(folder: Path) -> dict[str, Any]:
    path = folder / "pack.toml"
    if not path.is_file():
        raise PackError(f"no encontré {path}")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise PackError(f"{path} no es TOML válido: {exc}") from None
    meta = data.get("pack") or {}
    name = str(meta.get("name", ""))
    if not _NAME.match(name):
        raise PackError(f"{path}: [pack] name tiene que ser minúsculas, números, - o _ (está {name!r})")
    if not meta.get("version"):
        raise PackError(f"{path}: falta [pack] version")
    requires = str(meta.get("requires", "")).strip()
    if requires:
        if not requires.startswith(">="):
            raise PackError(f"{path}: [pack] requires solo admite «>=versión» (está {requires!r})")
        if _version(__version__) < _version(requires[2:]):
            raise PackError(f"el pack {name} pide casebook {requires} y tienes {__version__}: actualiza la app")
    return {**meta, "plugins": data.get("requires", {}).get("plugins", [])}


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-+]", str(text).strip()) if p)


def _check_tree(root: Path, suffixes: set[str], where: str, skip: tuple[str, ...] = ()) -> None:
    total = 0
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts) or "__pycache__" in rel.parts or rel.parts[0] in skip:
            continue
        if path.is_symlink():
            raise PackError(f"{where}/{rel}: un pack no puede traer symlinks")
        if path.is_dir():
            continue
        if path.suffix.lower() not in suffixes:
            raise PackError(f"{where}/{rel}: tipo de archivo no permitido en un pack")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise PackError(f"{where}/{rel}: supera {MAX_FILE_BYTES // 1024 // 1024} MB")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise PackError(f"{where}: supera {MAX_TOTAL_BYTES // 1024 // 1024} MB en total")


def _safe_extract(archive: Path, target: Path) -> None:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise PackError(f"{archive} no es un zip válido") from None
    with zf:
        for info in zf.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                raise PackError(f"{archive}: ruta no permitida dentro del zip: {name}")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise PackError(f"{archive}: un pack no puede traer symlinks ({name})")
            if info.file_size > MAX_FILE_BYTES:
                raise PackError(f"{archive}: {name} supera {MAX_FILE_BYTES // 1024 // 1024} MB")
        if sum(i.file_size for i in zf.infolist()) > MAX_TOTAL_BYTES:
            raise PackError(f"{archive}: supera {MAX_TOTAL_BYTES // 1024 // 1024} MB descomprimido")
        zf.extractall(target)


def _is_git(source: str) -> bool:
    return source.startswith(("git@", "ssh://", "git+")) or (source.startswith("https://") and source.endswith(".git"))


@dataclass
class Bundle:
    """Un pack listo para instalar: su carpeta, sus plugins (si vienen en el zip) y de dónde salió."""

    pack_dir: Path
    plugins_dir: Path | None
    origin: str
    meta: dict[str, Any]
    cfg: config.Config
    _tmp: tempfile.TemporaryDirectory | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._tmp:
            self._tmp.cleanup()


def open_bundle(source: str) -> Bundle:
    """Abre (y valida) un pack desde una carpeta, un .zip o una URL git."""
    tmp = None
    path = Path(source).expanduser()
    if path.is_dir():
        root, origin = path.resolve(), str(path.resolve())
    elif path.suffix == ".zip" and path.is_file():
        tmp = tempfile.TemporaryDirectory(prefix="casebook-pack-")
        _safe_extract(path, Path(tmp.name))
        root, origin = Path(tmp.name), str(path.resolve())
    elif _is_git(source):
        tmp = tempfile.TemporaryDirectory(prefix="casebook-pack-")
        url = source.removeprefix("git+")
        got = subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, tmp.name], capture_output=True,
                             text=True, timeout=180, stdin=subprocess.DEVNULL)
        if got.returncode != 0:
            tmp.cleanup()
            raise PackError(f"no pude clonar {url}: {got.stderr.strip() or got.stdout.strip()}")
        root, origin = Path(tmp.name), source
    else:
        raise PackError(f"no encontré {source}: tiene que ser una carpeta, un .zip o una URL git")
    try:
        # El layout de `pack build` (y de un repo de pack): pack/ con el pack y plugins/ al lado.
        pack_dir = root / "pack" if (root / "pack" / "pack.toml").is_file() else root
        plugins_dir = pack_dir.parent / "plugins" if pack_dir != root else root / "plugins"
        plugins_dir = plugins_dir if plugins_dir.is_dir() else None
        meta = manifest(pack_dir)
        _check_tree(pack_dir, PACK_SUFFIXES, "pack", skip=("plugins",))
        if plugins_dir:
            _check_tree(plugins_dir, PLUGIN_SUFFIXES, "plugins")
        try:
            cfg = config.load(Path("/nonexistent/config.toml"), pack_dir / "pack.toml")
        except config.SettingError as exc:
            raise PackError(f"el pack no es válido: {exc}") from None
        for i, plugin in enumerate(meta["plugins"]):
            if not plugin.get("name") or not (plugin.get("path") or plugin.get("pip")):
                raise PackError(f"[[requires.plugins]] #{i + 1}: hacen falta name y path o pip")
            if plugin.get("path") and not (plugins_dir and (plugins_dir.parent / plugin["path"]).resolve().is_relative_to(plugins_dir.resolve())):
                raise PackError(f"[[requires.plugins]] {plugin['name']}: path tiene que estar dentro de plugins/")
        _check_skills(pack_dir)
        _check_knowledge(pack_dir)
    except BaseException:
        if tmp:
            tmp.cleanup()
        raise
    return Bundle(pack_dir, plugins_dir, origin, meta, cfg, tmp)


def _check_skills(pack_dir: Path) -> None:
    skills = pack_dir / "skills"
    if not skills.is_dir():
        return
    for folder in skills.iterdir():
        if folder.is_dir() and not folder.name.startswith(".") and not (folder / "SKILL.md").is_file():
            raise PackError(f"skills/{folder.name}: falta SKILL.md")


def _check_knowledge(pack_dir: Path) -> None:
    for path in sorted((pack_dir / "knowledge").glob("*.json")):
        try:
            entries = json.loads(path.read_text())
        except ValueError as exc:
            raise PackError(f"knowledge/{path.name} no es JSON válido: {exc}") from None
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise PackError(f"knowledge/{path.name}: tiene que ser una lista de entradas")


# --- resumen --------------------------------------------------------------------------------------------------


def skill_names(pack_dir: Path | None) -> list[str]:
    root = pack_dir / "skills" if pack_dir else None
    if not root or not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())


def _domains(cfg: config.Config) -> list[str]:
    from urllib.parse import urlsplit

    out = [e.domain or urlsplit(str(e.options.get("url", ""))).hostname or "" for e in cfg.opensearch.values()]
    # Las URLs de las opciones de CloudWatch (un proveedor con login en el navegador, por ejemplo).
    out += [urlsplit(v).hostname or "" for v in cfg.cw_options.values() if isinstance(v, str) and v.startswith("https://")]
    return sorted({d for d in out if d})


def describe(bundle: Bundle) -> dict[str, Any]:
    """Qué trae el pack y, si ya hay uno con ese nombre, qué cambia."""
    from . import systems

    cfg = bundle.cfg
    current = config.PACKS_DIR / bundle.meta["name"] / "pack.toml"
    info: dict[str, Any] = {
        "name": bundle.meta["name"],
        "display_name": bundle.meta.get("display_name") or bundle.meta["name"],
        "version": str(bundle.meta["version"]),
        "sources": [f"{s.id} ({s.provider})" for s in systems.all_systems(cfg)],
        "skills": skill_names(bundle.pack_dir),
        "knowledge": sum(len(json.loads(p.read_text())) for p in (bundle.pack_dir / "knowledge").glob("*.json")),
        "domains": _domains(cfg),
        "plugins": [],
        "installed_version": None,
        "older": False,
        "changes": [],
    }
    for plugin in bundle.meta["plugins"]:
        missing = [p for p in plugin.get("provides", []) if not any(providers.get(k, p) for k in providers.KINDS)]
        info["plugins"].append({**plugin, "missing": bool(missing) or not plugin.get("provides")})
    if current.is_file():
        old_meta = manifest(current.parent)
        old_cfg = config.load(Path("/nonexistent/config.toml"), current)
        info["installed_version"] = str(old_meta["version"])
        info["older"] = _version(info["version"]) < _version(old_meta["version"])
        old_sources = {f"{s.id} ({s.provider})" for s in systems.all_systems(old_cfg)}
        new_sources = set(info["sources"])
        info["changes"] += [f"fuente nueva: {s}" for s in sorted(new_sources - old_sources)]
        info["changes"] += [f"fuente quitada: {s}" for s in sorted(old_sources - new_sources)]
        old_skills, new_skills = set(skill_names(current.parent)), set(info["skills"])
        info["changes"] += [f"skill nueva: {s}" for s in sorted(new_skills - old_skills)]
        info["changes"] += [f"skill quitada: {s}" for s in sorted(old_skills - new_skills)]
        if current.read_text() != (bundle.pack_dir / "pack.toml").read_text():
            info["changes"].append("pack.toml cambió")
        info["changes"] += [f"skill actualizada: {s}" for s in sorted(new_skills & old_skills)
                            if _digest(current.parent / "skills" / s) != _digest(bundle.pack_dir / "skills" / s)]
    return info


def _digest(folder: Path) -> str:
    from .skills import _digest as digest

    return digest(folder)


def summary_text(info: dict[str, Any]) -> str:
    lines = [f"Pack «{info['display_name']}» {info['version']}"
             + (f" (instalado: {info['installed_version']})" if info["installed_version"] else "")]
    lines.append(f"  Fuentes:      {', '.join(info['sources']) or 'ninguna'}")
    lines.append(f"  Skills:       {', '.join(info['skills']) or 'ninguna'}")
    if info["knowledge"]:
        lines.append(f"  Conocimiento: {info['knowledge']} entradas semilla (pending, no pisan las existentes)")
    if info["domains"]:
        lines.append(f"  Dominios:     {', '.join(info['domains'])} (la extensión pedirá permiso)")
    for p in info["plugins"]:
        state = "falta instalarlo" if p["missing"] else "ya instalado"
        where = f"incluido en el zip ({p['path']})" if p.get("path") else p.get("pip", "")
        lines.append(f"  Plugin:       {p['name']} → {', '.join(p.get('provides', []))}: {state}; {where}")
    if info["changes"]:
        lines.append("  Cambios:")
        lines += [f"    - {c}" for c in info["changes"]]
    if info["older"]:
        lines.append(f"  Ojo: es más viejo que el instalado ({info['installed_version']}).")
    return "\n".join(lines)


# --- instalar -------------------------------------------------------------------------------------------------


def install(source: str | Path, confirm: Callable[[str], bool] | None = None,
            confirm_plugin: Callable[[str], bool] | None = None, out: Callable[[str], None] = lambda s: None) -> config.Pack:
    """Instala y activa un pack. `confirm(resumen)` decide si se aplica (None = sí, salvo un pack más viejo que el
    instalado, que sin confirmación no se instala). `confirm_plugin(nombre)` decide cada plugin que falta (None =
    no se instala ninguno: es código)."""
    bundle = open_bundle(str(source))
    try:
        info = describe(bundle)
        text = summary_text(info)
        if confirm is not None:
            if not confirm(text):
                raise PackError("cancelado: no se cambió nada")
        elif info["older"]:
            raise PackError(f"el pack es más viejo que el instalado ({info['version']} < {info['installed_version']}); "
                            "confírmalo de forma interactiva")
        name = info["name"]
        target = config.PACKS_DIR / name
        tmp = config.PACKS_DIR / f".{name}.tmp"
        config.PACKS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(bundle.pack_dir, tmp, ignore=shutil.ignore_patterns(".*", "__pycache__", "plugins"))
        if bundle.plugins_dir:
            shutil.copytree(bundle.plugins_dir, tmp / "plugins", ignore=shutil.ignore_patterns(".*", "__pycache__"))
        for path in tmp.rglob("*"):
            if path.is_file():
                path.chmod(0o644)  # nada de lo que trae el pack queda ejecutable
        (tmp / ORIGIN_FILE).write_text(json.dumps({"source": bundle.origin, "version": info["version"]}))
        if target.exists():
            shutil.rmtree(target)
        tmp.rename(target)
        (config.PACKS_DIR / "active").write_text(name + "\n")
        seeded = seed_knowledge(target)
        if seeded:
            out(f"Conocimiento semilla: {seeded} entradas nuevas (pending).")
        for plugin in info["plugins"]:
            if not plugin["missing"]:
                continue
            spec = str(target / plugin["path"]) if plugin.get("path") else plugin["pip"]
            if confirm_plugin and confirm_plugin(f"{plugin['name']} ({spec})"):
                install_plugin(spec, out)
            else:
                out(f"Plugin {plugin['name']} sin instalar: sus fuentes quedan deshabilitadas hasta que corras "
                    f"`{sys.executable} -m pip install {spec}`.")
        refreshed = refresh_agent_skills(target)
        if refreshed:
            out(f"Skills actualizadas en tus agentes: {', '.join(refreshed)}.")
        return config.load(Path("/nonexistent/config.toml")).pack
    finally:
        bundle.close()


def install_plugin(spec: str, out: Callable[[str], None] = lambda s: None) -> None:
    got = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", spec], capture_output=True, text=True,
                         timeout=600, stdin=subprocess.DEVNULL)
    if got.returncode != 0:
        raise PackError(f"pip no pudo instalar {spec}: {got.stderr.strip()[-1500:]}")
    providers._registry = None  # que se vean los proveedores nuevos
    out(f"Plugin instalado: {spec}")


def seed_knowledge(pack_dir: Path) -> int:
    """Importa knowledge/*.json como pending. Lo que ya existe (misma fuente, entorno, entidad y tipo) no se toca."""
    files = sorted((pack_dir / "knowledge").glob("*.json"))
    if not files:
        return 0
    from .knowledge import Conflict, KnowledgeError
    from .store import Store

    store = Store(config.load().db_path)
    count = 0
    try:
        for path in files:
            for entry in json.loads(path.read_text()):
                try:
                    store.knowledge.db.write(
                        source=entry["source"], environment=entry["environment"], entity=entry["entity"],
                        kind=entry["kind"], title=entry["title"], content=entry["content"],
                        topics=list(entry.get("topics", [])),
                        evidence={"pack": pack_dir.name, "file": path.name}, status="pending",
                        reason=f"semilla del pack {pack_dir.name}", expected_revision=0, actor="pack",
                    )
                    count += 1
                except Conflict:
                    continue  # ya existe: manda lo del equipo
                except (KeyError, KnowledgeError) as exc:
                    raise PackError(f"knowledge/{path.name}: entrada inválida ({exc})") from None
    finally:
        store.close()
    return count


def refresh_agent_skills(pack_dir: Path) -> list[str]:
    """Actualiza las copias de las skills del pack que ya están instaladas en Claude Code (las nuevas las instala
    el asistente, en los agentes que elijas). Solo toca copias con el mismo name en SKILL.md."""
    from .skills import user_skills_dir

    done = []
    for name in skill_names(pack_dir):
        dst = user_skills_dir() / name
        src = pack_dir / "skills" / name
        if dst.is_symlink() or not dst.is_dir():
            continue  # un link ya apunta al pack (o no es nuestro); sin instalar: lo hace el asistente
        try:
            ours = f"name: {name}" in (dst / "SKILL.md").read_text().splitlines()
        except OSError:
            ours = False
        if ours and _digest(dst) != _digest(src):
            shutil.rmtree(dst)
            shutil.copytree(src, dst)
            done.append(name)
    return done


# --- otros comandos -------------------------------------------------------------------------------------------


def active() -> config.Pack | None:
    return config.load(Path("/nonexistent/config.toml")).pack if config.active_pack() else None


def installed() -> list[dict[str, Any]]:
    current = config.active_pack()
    out = []
    if not config.PACKS_DIR.is_dir():
        return out
    for folder in sorted(config.PACKS_DIR.iterdir()):
        if folder.name.startswith(".") or not (folder / "pack.toml").is_file():
            continue
        try:
            meta = manifest(folder)
        except PackError:
            continue
        origin = json.loads((folder / ORIGIN_FILE).read_text()).get("source") if (folder / ORIGIN_FILE).is_file() else None
        out.append({"name": meta["name"], "version": str(meta["version"]), "display_name": meta.get("display_name"),
                    "active": current is not None and current.parent == folder, "origin": origin})
    return out


def origin(name: str) -> str | None:
    path = config.PACKS_DIR / name / ORIGIN_FILE
    return json.loads(path.read_text()).get("source") if path.is_file() else None


def remove(name: str) -> None:
    folder = config.PACKS_DIR / name
    if not _NAME.match(name) or not (folder / "pack.toml").is_file():
        raise PackError(f"no hay un pack instalado llamado {name}")
    current = config.active_pack()
    if current is not None and current.parent == folder:
        (config.PACKS_DIR / "active").unlink(missing_ok=True)
    shutil.rmtree(folder)


TEMPLATE = '''# Pack de {display}: la configuración del equipo para casebook.
# Mismo formato que config.toml (ver docs/configuracion.md). No pongas credenciales: van en el keyring.

[pack]
name = "{name}"
version = "0.1.0"
display_name = "{display}"
# requires = ">=0.2.0"                 # versión mínima de la app

[texts]
domain = "casos de soporte de {display}"

[card]
prefix = "SUP"
examples = ["SUP-1200", "SUP-1210"]

# [slack]
# channel_id = "C..."
# channel_name = "soporte"

# [doctor]
# shell_aliases = []
# network_hint = "revisa la VPN"

[sources.opensearch.prod]
provider = "direct"                    # o "dashboards-proxy" (domain, tunnel_port, ssh_host)
url = "https://search.ejemplo.internal:9200"
auth = "basic"                         # cada uno guarda usuario y contraseña con `casebook login os-prod`
description = "logs de la app"
hint = "familias: logs-*, users-*"

# [sources.cloudwatch]
# provider = "aws"
# profile = "soporte"
# region = "us-east-1"

# [sources.mysql]
# provider = "mysql-ssh"
# ssh_host = "bastion"
# host = "replica.db.internal"
# database = "app"
# [sources.mysql.credentials]
# kind = "keyring"                     # o "env-file" / "env"

# Un plugin con proveedores propios (código): se instala con pip solo si el usuario lo confirma.
# [[requires.plugins]]
# name = "mi-plugin"
# provides = ["mi-proveedor"]
# path = "plugins/mi-plugin"           # dentro del zip de `pack build`, o: pip = "git+ssh://..."
'''


def init(folder: Path, name: str, display_name: str | None = None) -> Path:
    if not _NAME.match(name):
        raise PackError("el nombre tiene que ser minúsculas, números, - o _")
    if folder.exists() and any(folder.iterdir()):
        raise PackError(f"{folder} ya existe y no está vacía")
    (folder / "skills").mkdir(parents=True, exist_ok=True)
    (folder / "knowledge").mkdir(exist_ok=True)
    (folder / "pack.toml").write_text(TEMPLATE.format(name=name, display=display_name or name.capitalize()))
    (folder / "skills" / "README.md").write_text(
        "Una carpeta por skill, con su SKILL.md. Se instalan en los agentes con `casebook setup`.\n")
    (folder / "knowledge" / "README.md").write_text(
        "Archivos .json con una lista de entradas (source, environment, entity, kind, title, content, topics).\n"
        "Se importan como pending al instalar el pack, sin pisar las que ya existen.\n")
    return folder / "pack.toml"


def build(source: Path, output: Path | None = None) -> Path:
    """Arma el zip para compartir: pack/ (y plugins/ si la carpeta tiene el layout de repo de pack). Valida antes."""
    bundle = open_bundle(str(source))
    try:
        meta = bundle.meta
        output = output or Path.cwd() / f"{meta['name']}-{meta['version']}.zip"
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for base, prefix in ((bundle.pack_dir, "pack"), (bundle.plugins_dir, "plugins")):
                if base is None:
                    continue
                for path in sorted(base.rglob("*")):
                    rel = path.relative_to(base)
                    if path.is_file() and not any(p.startswith(".") or p == "__pycache__" for p in rel.parts):
                        if prefix == "pack" and rel.parts[0] == "plugins":
                            continue
                        zf.write(path, f"{prefix}/{rel.as_posix()}")
        return output
    finally:
        bundle.close()


def export(output: Path, name: str | None = None, version: str = "0.1.0") -> Path:
    """Un zip con la configuración que usas hoy (el pack activo más tu config.toml), sin credenciales (no están en
    la config) ni ajustes personales del visor, más las skills y el conocimiento semilla del pack activo."""
    pack_path = config.active_pack()
    data, meta, _ = config._read_layers(config.CONFIG_PATH, pack_path)
    for personal in ("viewer", "db_path"):
        data.pop(personal, None)
    data.get("slack", {}).pop("watch", None)
    name = name or meta.get("name") or "mi-equipo"
    if not _NAME.match(name):
        raise PackError("el nombre tiene que ser minúsculas, números, - o _")
    pack = {"name": name, "version": version, "display_name": meta.get("display_name") or name}
    with tempfile.TemporaryDirectory(prefix="casebook-export-") as tmp:
        folder = Path(tmp) / "pack"
        if pack_path:
            shutil.copytree(pack_path.parent, folder, ignore=shutil.ignore_patterns(".*", "plugins", "pack.toml"))
        folder.mkdir(exist_ok=True)
        (folder / "pack.toml").write_text(config.dumps({"pack": pack, **data}).replace(
            "# casebook. Se puede editar a mano o desde Ajustes en el visor.",
            f"# Pack exportado de casebook ({name} {version}). Sin credenciales: van en el keyring."))
        return build(folder, output)
