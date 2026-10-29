"""Los pasos del instalador, sin interfaz: los usan el asistente de terminal y el gráfico.

install.sh ya creó el venv e instaló el paquete; aquí sigue lo demás (comando en el PATH, agentes, requisitos,
extensión de Chrome, sesiones). Cada paso devuelve Items (estado + texto) y nunca imprime. Con dry_run no cambia
nada: muestra lo que haría.

Las reglas de qué se puede tocar son las de siempre: solo se reemplaza o quita lo que apunta a este repo o a una
instalación de casebook; lo demás se avisa y se deja.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config, native_host, systems

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
from .. import MCP_NAME  # noqa: E402  (se reexporta para el instalador)


def skill_names() -> list[str]:
    """Las skills del pack activo, que se instalan en cada agente."""
    from .. import packs

    pack = config.active_pack()
    return packs.skill_names(pack.parent) if pack else []
AGENTS = (("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode"))


@dataclass
class Item:
    status: str
    text: str
    detail: str = ""
    group: str = ""  # requisitos: Conexión, Credenciales o Sistema
    key: str = ""  # estable entre corridas: para recordar un aviso ignorado
    fix: str = ""  # el comando que lo arregla, para copiarlo
    link: str = ""  # dónde resolverlo, si es una página


@dataclass
class Context:
    repo: Path | None  # el repo desde el que se instaló (skills); puede no existir más en modo copia
    dev: bool
    venv: Path
    agents: list[str] | None = None  # --agents=...: sin preguntar
    skip_mcp: bool = False
    skip_skill: bool = False
    no_login: bool = False
    dry_run: bool = False
    home: Path = field(default_factory=Path.home)

    @property
    def app_dir(self) -> Path:
        return config.DATA_DIR

    @property
    def cli_target(self) -> Path:
        return self.venv / "bin" / "casebook"

    @property
    def cli_link(self) -> Path:
        return self.home / ".local" / "bin" / "casebook"

    @property
    def mode_label(self) -> str:
        return "modo desarrollo" if self.dev else "instalación local"


def as_dicts(items: list[Item]) -> list[dict]:
    return [asdict(i) for i in items]


# --- entorno -------------------------------------------------------------------------------------------------


def environment(ctx: Context) -> list[Item]:
    from importlib.metadata import version

    v = sys.version_info
    items = [
        Item(OK, f"Python {v.major}.{v.minor}.{v.micro}", sys.executable),
        Item(OK, f"casebook {version('casebook')}", str(ctx.venv)),
    ]
    if ctx.dev:
        items.append(Item(INFO, "Modo desarrollo: editable desde el repo", "tus cambios aplican sin reinstalar"))
    else:
        items.append(Item(INFO, "Instalación local: copia propia", "el repo se puede mover o borrar"))
    return items


def link_cli(ctx: Context) -> list[Item]:
    link, target = ctx.cli_link, ctx.cli_target
    items = []
    if link.is_symlink() and link.resolve() == target.resolve():
        items.append(Item(OK, "Comando casebook listo", f"{link} → {target}"))
    elif (link.exists() or link.is_symlink()) and not _ours_link(ctx, link):
        items.append(Item(WARN, f"{link} ya existe y no es de esta instalación; no lo toco",
                          "quítalo y vuelve a correr el instalador"))
    else:
        if not ctx.dry_run:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.unlink(missing_ok=True)
            link.symlink_to(target)
        items.append(Item(OK, "Comando casebook en tu PATH", f"{link} → {target}"))
    if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        items.append(Item(WARN, f"{link.parent} no está en tu PATH",
                          'agrégalo a tu shell: export PATH="$HOME/.local/bin:$PATH"'))
    return items


# --- agentes -------------------------------------------------------------------------------------------------


def agents_available() -> list[dict]:
    return [{"id": a, "label": label, "installed": shutil.which(a) is not None} for a, label in AGENTS]


def agent_version(agent: str) -> str | None:
    """La versión de un agente instalado (`<agente> --version`), o None si no está o no la dice."""
    if not shutil.which(agent):
        return None
    try:
        out = subprocess.run([agent, "--version"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"\d+\.\d+(?:\.\d+)?", out)
    return found.group(0) if found else None


def default_agents(ctx: Context) -> list[str]:
    if ctx.agents is not None:
        return ctx.agents
    return [a["id"] for a in agents_available() if a["installed"]]


def _ours_link(ctx: Context, path: Path) -> bool:
    if not path.is_symlink():
        return False
    target = os.readlink(path)
    roots = [str(ctx.app_dir)] + ([str(ctx.repo)] if ctx.repo else [])
    return any(target.startswith(r) for r in roots) or "/casebook/" in target


def _ours_skill(ctx: Context, path: Path, name: str) -> bool:
    """El symlink del modo --dev, o una copia con nuestro SKILL.md."""
    if _ours_link(ctx, path):
        return True
    try:
        return f"name: {name}" in (path / "SKILL.md").read_text().splitlines()
    except OSError:
        return False


def _ours_agent_skill(ctx: Context, path: Path, name: str) -> bool:
    """Además, el symlink a la skill de Claude Code de una versión anterior del instalador."""
    claude_skill = ctx.home / ".claude" / "skills" / name
    return (path.is_symlink() and os.readlink(path) == str(claude_skill)) or _ours_skill(ctx, path, name)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


def _skill_source(ctx: Context, name: str) -> Path | None:
    """La skill del pack activo. En --dev, la del repo (packs/<nombre>/skills) si está, así los cambios aplican
    sin reinstalar el pack."""
    pack = config.active_pack()
    if pack is None:
        return None
    candidates = [pack.parent / "skills" / name]
    if ctx.dev and ctx.repo:
        candidates.insert(0, ctx.repo / "packs" / pack.parent.name / "skills" / name)
    return next((c for c in candidates if (c / "SKILL.md").is_file()), None)


def _shared_skill(ctx: Context, name: str) -> Path | None:
    """Lo que enlazan Codex y OpenCode: el repo en --dev, o una copia en el directorio de la app."""
    source = _skill_source(ctx, name)
    if source is None:
        return None
    if ctx.dev:
        return source
    shared = ctx.app_dir / "skills" / name
    if not ctx.dry_run:
        shutil.rmtree(shared, ignore_errors=True)
        shared.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, shared)
    return shared


def _link_skill(ctx: Context, dst: Path, shared: Path) -> Item:
    if dst.is_symlink() and os.readlink(dst) == str(shared):
        return Item(OK, "Skill ya enlazada", str(dst))
    if (dst.exists() or dst.is_symlink()) and not _ours_agent_skill(ctx, dst, dst.name):
        return Item(WARN, f"{dst} ya existe y no es de esta instalación; no lo toco")
    if not ctx.dry_run:
        _remove(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(shared)
    return Item(OK, "Skill enlazada", f"{dst} → {shared}")


def _claude(ctx: Context) -> list[Item]:
    items = []
    target = str(ctx.cli_target)
    if not ctx.skip_mcp:
        if not shutil.which("claude"):
            items.append(Item(WARN, "No encontré `claude`",
                              f"cuando lo instales: claude mcp add -s user {MCP_NAME} -- {target} mcp"))
        else:
            current = _run("claude", "mcp", "get", MCP_NAME)
            out = current.stdout if current.returncode == 0 else ""
            if f"Command: {target}" in out and "User config" in out:
                items.append(Item(OK, "Servidor MCP ya registrado (scope user)", target))
            elif ctx.dry_run:
                items.append(Item(OK, "Registraría el servidor MCP (scope user)", target))
            else:
                if out:
                    _run("claude", "mcp", "remove", "-s", "user", MCP_NAME)
                added = _run("claude", "mcp", "add", "-s", "user", MCP_NAME, "--", target, "mcp")
                if added.returncode == 0:
                    items.append(Item(OK, "Servidor MCP registrado (scope user)",
                                      target + (" · reconecta con /mcp" if out else "")))
                else:
                    items.append(Item(WARN, "No pude registrar el servidor MCP",
                                      f"corre: claude mcp add -s user {MCP_NAME} -- {target} mcp"))
    for name in () if ctx.skip_skill else skill_names():
        source, dst = _skill_source(ctx, name), ctx.home / ".claude" / "skills" / name
        if source is None:
            items.append(Item(WARN, f"No encontré la skill {name} en el pack", "reinstala el pack con `pack install`"))
        elif (dst.exists() or dst.is_symlink()) and not _ours_skill(ctx, dst, name):
            items.append(Item(WARN, f"{dst} ya existe y no es de esta instalación; no lo toco"))
        else:
            if not ctx.dry_run:
                _remove(dst) if (dst.exists() or dst.is_symlink()) else None
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.symlink_to(source) if ctx.dev else shutil.copytree(source, dst)
            items.append(Item(OK, "Skill enlazada desde el repo" if ctx.dev else "Skill copiada del pack", str(dst)))
    return items


def _codex(ctx: Context) -> list[Item]:
    items = []
    target = str(ctx.cli_target)
    if not ctx.skip_mcp:
        if not shutil.which("codex"):
            items.append(Item(WARN, "No encontré `codex`", f"cuando lo instales: codex mcp add {MCP_NAME} -- {target} mcp"))
        else:
            got = _run("codex", "mcp", "get", MCP_NAME, "--json")
            try:
                current = json.loads(got.stdout)["transport"].get("command") or "" if got.returncode == 0 else ""
            except (ValueError, KeyError, TypeError):
                current = ""
            if current == target:
                items.append(Item(OK, "Servidor MCP ya registrado", target))
            elif ctx.dry_run:
                items.append(Item(OK, "Registraría el servidor MCP", target))
            else:
                if got.returncode == 0:
                    _run("codex", "mcp", "remove", MCP_NAME)
                added = _run("codex", "mcp", "add", MCP_NAME, "--", target, "mcp")
                if added.returncode == 0:
                    items.append(Item(OK, "Servidor MCP registrado", target + (" · reinicia Codex" if current else "")))
                else:
                    items.append(Item(WARN, "No pude registrar el servidor MCP",
                                      f"corre: codex mcp add {MCP_NAME} -- {target} mcp"))
    for name in () if ctx.skip_skill else skill_names():
        old = Path(os.environ.get("CODEX_HOME") or ctx.home / ".codex") / "skills" / name
        if old.exists() or old.is_symlink():
            if _ours_agent_skill(ctx, old, name):
                if not ctx.dry_run:
                    _remove(old)
                items.append(Item(OK, "Quitada la copia anterior de la skill", str(old)))
            else:
                items.append(Item(WARN, f"{old} no es de esta instalación; no lo toco", "puede verse duplicada"))
        shared = _shared_skill(ctx, name)
        if shared is None:
            items.append(Item(WARN, f"No encontré la skill {name} en el pack", "reinstala el pack con `pack install`"))
        else:
            items.append(_link_skill(ctx, ctx.home / ".agents" / "skills" / name, shared))
    return items


def _opencode(ctx: Context) -> list[Item]:
    items = []
    base = Path(os.environ.get("XDG_CONFIG_HOME") or ctx.home / ".config") / "opencode"
    path = base / "opencode.json"
    target = str(ctx.cli_target)
    if not ctx.skip_mcp:
        try:
            data = json.loads(path.read_text()) if path.exists() else {"$schema": "https://opencode.ai/config.json"}
        except ValueError:
            data = None
        if data is None:
            items.append(Item(WARN, f"No pude leer {path} (¿tiene comentarios?)",
                              f'agrega en "mcp": "{MCP_NAME}": {{"type": "local", "command": ["{target}", "mcp"]}}'))
        else:
            mcp = data.get("mcp") or {}
            if (mcp.get(MCP_NAME) or {}).get("command") == [target, "mcp"]:
                items.append(Item(OK, "Servidor MCP ya registrado", target))
            else:
                if not ctx.dry_run:
                    mcp[MCP_NAME] = {"type": "local", "command": [target, "mcp"], "enabled": True}
                    data["mcp"] = mcp
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
                    os.replace(tmp, path)
                items.append(Item(OK, "Servidor MCP registrado", str(path)))
        if not shutil.which("opencode"):
            items.append(Item(WARN, "No encontré `opencode`", "la configuración queda lista para cuando lo instales"))
    for name in () if ctx.skip_skill else skill_names():
        shared = _shared_skill(ctx, name)
        if shared is None:
            items.append(Item(WARN, f"No encontré la skill {name} en el pack", "reinstala el pack con `pack install`"))
        else:
            items.append(_link_skill(ctx, base / "skills" / name, shared))
    return items


def register_agents(ctx: Context, selected: list[str]) -> dict[str, list[Item]]:
    """Por agente elegido: el servidor MCP y las skills."""
    run = {"claude": _claude, "codex": _codex, "opencode": _opencode}
    return {agent: run[agent](ctx) for agent, _ in AGENTS if agent in selected}


# --- requisitos ----------------------------------------------------------------------------------------------


def prereqs() -> list[Item]:
    from .. import doctor

    cfg = config.load()
    status = {doctor.OK: OK, doctor.WARN: WARN, doctor.FAIL: FAIL}
    items = [Item(status[c.status], c.detail, group=c.group or "Sistema", key=c.key, fix=c.fix)
             for c in doctor.check_prereqs(cfg) if "extensión de Chrome" not in c.detail]
    if shutil.which("notify-send"):
        items.append(Item(OK, "notify-send (avisos para aprobar consultas)", group="Sistema", key="notify-send"))
    else:
        items.append(Item(WARN, "Sin notify-send: no habrá aviso de escritorio para aprobar consultas (opcional)",
                          group="Sistema", key="notify-send"))
    if shutil.which("xdg-open") or shutil.which("open"):
        items.append(Item(OK, "xdg-open (abrir el visor y el login solos)", group="Sistema", key="xdg-open"))
    else:
        items.append(Item(WARN, "Sin xdg-open: tendrás que abrir los links a mano (opcional)",
                          group="Sistema", key="xdg-open"))
    return items


PREREQ_GROUPS = ("Conexión", "Credenciales", "Sistema")


def _ignored_path() -> Path:
    return config.DATA_DIR / "setup-ignorados.json"


def ignored() -> set[str]:
    """Las claves de los avisos que se eligió ignorar en el instalador (no vuelven como pendientes)."""
    try:
        return set(json.loads(_ignored_path().read_text()))
    except (OSError, ValueError, TypeError):
        return set()


def set_ignored(key: str, on: bool = True) -> None:
    keys = ignored()
    keys = keys | {key} if on else keys - {key}
    path = _ignored_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(keys)))


# --- extensión de Chrome -------------------------------------------------------------------------------------


# --- Slack (opcional) ----------------------------------------------------------------------------------------

SLACK_CONNECTOR = "claude.ai Slack"
SLACK_DOWNLOAD = "https://slack.com/downloads/linux"
SLACK_CONNECTORS = "https://claude.ai/settings/connectors"


def _slack_desktop() -> str | None:
    """Dónde está Slack de escritorio (comando, snap, flatpak o acceso directo), o None."""
    if found := shutil.which("slack"):
        return found
    home = Path.home()
    candidates = [
        Path("/snap/bin/slack"),
        Path("/var/lib/flatpak/exports/share/applications/com.slack.Slack.desktop"),
        home / ".local/share/flatpak/exports/share/applications/com.slack.Slack.desktop",
        Path("/usr/share/applications/slack.desktop"),
        home / ".local/share/applications/slack.desktop",
        Path("/Applications/Slack.app"),
    ]
    return next((str(c) for c in candidates if c.exists()), None)


def _slack_running() -> bool:
    try:
        return _run("pgrep", "-x", "slack").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def slack_checks() -> list[Item]:
    """Lo que necesita la vigilancia del canal de soporte. Es opcional: lo que falta va como INFO (cómo
    resolverlo), no como pendiente de la instalación. Solo se revisa; nada se cambia."""
    items = []
    desktop = _slack_desktop()
    if not desktop:
        items.append(Item(INFO, "Slack de escritorio no está instalado",
                          f"descárgalo de {SLACK_DOWNLOAD} (o: sudo snap install slack)", key="slack-desktop",
                          link=SLACK_DOWNLOAD))
    elif sys.platform != "linux":
        items.append(Item(INFO, "Slack de escritorio instalado, pero escuchar sus notificaciones solo funciona en "
                                "Linux", "activa la revisión de respaldo en Ajustes → Slack"))
    elif _slack_running():
        items.append(Item(OK, "Slack de escritorio instalado y abierto", desktop, key="slack-desktop"))
    else:
        items.append(Item(INFO, "Slack de escritorio instalado pero cerrado",
                          "ábrelo y déjalo abierto: la vigilancia escucha sus notificaciones", key="slack-desktop"))

    if not shutil.which("claude"):
        items.append(Item(INFO, "No encontré `claude`: el conector de Slack se usa desde Claude Code"))
    else:
        try:
            got = _run("claude", "mcp", "get", SLACK_CONNECTOR)
            out = got.stdout if got.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "Connected" in out:
            items.append(Item(OK, "Conector de Slack de claude.ai conectado", key="slack-connector"))
        elif out:
            items.append(Item(INFO, "El conector de Slack de claude.ai falta autorizarlo",
                              "en Claude Code: /mcp → claude.ai Slack → Authenticate (sin aprobación del admin)",
                              key="slack-connector", fix="/mcp"))
        else:
            items.append(Item(INFO, "No encontré el conector de Slack de claude.ai",
                              "actívalo en claude.ai → Connectors → Slack; después /mcp en Claude Code",
                              key="slack-connector", link=SLACK_CONNECTORS))
    channel = config.load().slack_channel_name
    items.append(Item(INFO, f"En Slack, #{channel or 'el canal de soporte'} → Notificaciones: «Todos los mensajes nuevos»",
                      "luego prende el toggle del panel Slack del visor", key="slack-notifications"))
    return items


def slack_watch() -> bool:
    return config.load().slack_watch


def set_slack_watch(on: bool) -> None:
    """Prende o apaga la vigilancia del canal: el mismo ajuste que el toggle del panel Slack del visor."""
    config.save({"slack.watch": on})


# --- código local ----------------------------------------------------------------------------------------------

# Carpetas que no se recorren al buscar el repo: ocultas (salvo que sean el destino), dependencias y compilados.
_NOISE = {"node_modules", "target", "build", "dist", "venv", "site-packages", "__pycache__", "snap", "Library"}
SEARCH_DEPTH = 6  # niveles bajo la carpeta personal
SEARCH_BUDGET_S = 4.0  # la búsqueda no puede demorar el asistente
SEARCH_MAX_DIRS = 40_000


def code_repo() -> config.CodeRepo:
    return config.load().slack_code


def code_paths() -> str | None:
    return config.load().slack_code_paths


def missing_markers(path: str, repo: config.CodeRepo) -> list[str]:
    """Lo que el pack espera encontrar en la carpeta y no está: sirve para avisar «¿seguro que es esta?»."""
    return [m for m in repo.markers if not (Path(path).expanduser() / m).exists()]


def find_code_candidates(repo: config.CodeRepo, home: Path | None = None, limit: int = 5) -> list[str]:
    """Busca bajo la carpeta personal las carpetas con alguno de los nombres del pack que contengan lo que el pack
    espera. Recorre por niveles (lo cercano primero), con tope de tiempo y de carpetas; sin nombres no busca."""
    wanted = {n.lower() for n in repo.names}
    if not wanted:
        return []
    home = home or Path.home()
    found: list[str] = []
    queue: deque[tuple[Path, int]] = deque([(home, 0)])
    deadline = time.monotonic() + SEARCH_BUDGET_S
    visited = 0
    while queue and len(found) < limit and visited < SEARCH_MAX_DIRS and time.monotonic() < deadline:
        directory, depth = queue.popleft()
        try:
            children = sorted((e for e in os.scandir(directory) if e.is_dir(follow_symlinks=False)),
                              key=lambda e: e.name)
        except OSError:
            continue
        for entry in children:
            visited += 1
            if entry.name.lower() in wanted and not missing_markers(entry.path, repo):
                found.append(entry.path)
                continue  # el repo encontrado no se recorre por dentro
            if depth + 1 < SEARCH_DEPTH and not entry.name.startswith(".") and entry.name not in _NOISE:
                queue.append((Path(entry.path), depth + 1))
    return found[:limit]


def set_code_paths(value: str) -> str | None:
    """Guarda la carpeta (o carpetas) de código con las mismas validaciones que Ajustes. Vacío la quita. Devuelve lo
    guardado, ya normalizado, o lanza config.SettingError si no sirve."""
    saved = config.BY_KEY["slack.code_paths"].coerce(value)
    config.save({"slack.code_paths": saved or ""})
    return saved


def code_status() -> Item:
    """Cómo está el código local: configurado (y si existe) o pendiente."""
    current = code_paths()
    repo = code_repo()
    if not current:
        return Item(INFO, "Código local: sin configurar",
                    f"el análisis de Slack no lee código hasta que elijas {repo.label} (opcional; también en "
                    "Ajustes → Slack → Código local)", key="code-paths")
    gone = [d for d in config.code_dirs(current) if not Path(d).is_dir()]
    if gone:
        return Item(WARN, f"No encuentro {', '.join(gone)}", "revisa la ruta en Ajustes → Slack → Código local",
                    key="code-paths")
    return Item(OK, f"El análisis puede leer {repo.label} (solo lectura)", current, key="code-paths")


def extension_install(ctx: Context) -> list[Item]:
    if ctx.dry_run:
        return [Item(OK, "Registraría el native host para todos los perfiles"),
                Item(OK, "Dejaría la extensión lista para cargar", str(native_host.extension_dir() or ""))]
    written = native_host.install()
    folder = native_host.extension_dir()
    items = [Item(OK, "Native host registrado", ", ".join(str(p.parent) for p in written))]
    if folder:
        items.append(Item(OK, "Extensión desde el repo" if ctx.dev else "Extensión copiada", str(folder)))
    else:
        items.append(Item(FAIL, "No encontré la carpeta de la extensión", "reinstala con ./install.sh"))
    return items


def extension_profiles() -> list[dict]:
    """Cada perfil de Chrome y si tiene la extensión cargada (desde qué carpeta), según sus Preferences."""
    expected = native_host.extension_dir()
    chosen = native_host.read_status().get("profiles") or []
    out = []
    for directory, name in native_host.chrome_profiles():
        loaded = native_host.extension_in_profile(directory)
        if loaded is None:
            state = "missing"
        elif not Path(loaded["path"]).is_dir():
            # Por ejemplo la copia de la instalación local, que se borra al pasar a --dev o al desinstalar.
            state = "folder_gone"
        elif not loaded["enabled"]:
            state = "disabled"
        elif expected and Path(loaded["path"]).resolve() != expected.resolve():
            state = "other_folder"
        else:
            state = "loaded"
        out.append({"dir": directory, "name": name, "state": state, "path": loaded and loaded["path"],
                    "login_profile": bool(chosen) and chosen[0] == directory})
    return out


WORKING = ("loaded", "other_folder")  # desde otra carpeta funciona igual: el ID es el mismo


def extension_state(profiles: list[dict] | None = None) -> dict:
    """Si la extensión está lista: basta con un perfil (o con que ya se haya conectado alguna vez)."""
    profiles = extension_profiles() if profiles is None else profiles
    loaded = [p["name"] for p in profiles if p["state"] in WORKING]
    # Que se haya conectado alguna vez no sirve si la carpeta desde la que la carga Chrome ya no existe.
    connected = native_host.connected() and not any(p["state"] == "folder_gone" for p in profiles)
    if loaded:
        subtitle = f"cargada en {', '.join(loaded)}"
    elif connected:
        subtitle = "conectada"
    else:
        subtitle = "falta cargarla"
    ok = bool(loaded) or connected
    missing = native_host.missing_permissions()
    if ok and missing:
        # Cargada pero sin permiso para los dominios de las fuentes: no puede leer las sesiones hasta que se dé.
        ok, subtitle = False, "falta dar permiso: ábrela y toca «Dar permiso»"
    return {"ok": ok, "subtitle": subtitle, "loaded": loaded, "connected": connected, "missing_permissions": missing}


def copy_to_clipboard(text: str) -> bool:
    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"]):
        if shutil.which(command[0]):
            try:
                subprocess.run(command, input=text, text=True, timeout=5, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def remember_login_profile(directory: str) -> None:
    profiles = [p for p in native_host.read_status().get("profiles") or [] if p != directory]
    native_host.set_profiles([directory, *profiles])


# --- sesiones ------------------------------------------------------------------------------------------------


def sessions() -> list[dict]:
    """Si cada servicio tiene una sesión guardada que la API acepta (sin levantar túneles)."""
    from .. import credentials, login

    cfg = config.load()
    out = []
    for system in systems.browser_systems(cfg):
        saved = credentials.get_cookie(system)
        extra = ""
        if not saved:
            status, detail = "missing", "sin sesión guardada"
        else:
            result = login.browser_target(cfg, system).check(saved)
            if result.ok:
                if result.cookie and result.cookie != saved:
                    credentials.set_cookie(system, result.cookie)
                # La primera línea (de quién es la sesión) en la tarjeta; el presupuesto y demás, aparte.
                status, detail = "ok", result.info[0] if result.info else "sesión vigente"
                extra = ". ".join(result.info[1:])
            elif not result.retry:
                status, detail = "unknown", result.detail
            else:
                status, detail = "expired", result.detail
        out.append({"system": system.id, "label": system.label, "description": system.description, "status": status, "detail": detail,
                    "extra": extra})
    return out


def team_name() -> str:
    """El nombre del pack activo (en minúsculas), para recomendar el perfil de Chrome del trabajo."""
    pack = config.load().pack
    return pack.name.lower() if pack else ""


def card_example() -> str:
    return config.load().card_examples[0]


def login_systems() -> list[systems.System]:
    """Los sistemas que se inician con el navegador, en el orden de config.toml."""
    return systems.browser_systems(config.load())


def login(system_id: str, timeout_s: int = 300, **kwargs) -> int:
    from .. import login as login_

    cfg = config.load()
    return login_.login_browser(cfg, systems.get(cfg, system_id), timeout_s, **kwargs)


def login_command(system: str) -> str:
    return f"casebook login {system}"
