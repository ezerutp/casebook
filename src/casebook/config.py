"""Configuración en capas: valores de fábrica (neutros, sin fuentes), el pack activo y config.toml del usuario.

- El pack (packs/<nombre>/pack.toml en DATA_DIR, el que dice packs/active) trae lo de una empresa: fuentes,
  Slack, alias y textos del doctor. Tiene el mismo formato que config.toml más una tabla [pack].
- ~/.config/casebook/config.toml pisa al pack, clave por clave.

Las fuentes van en [sources.<tipo>.<env>] (opensearch, cloudwatch o mysql), cada una con su `provider`: puede haber
varias del mismo tipo (prod y qa). Una fuente de CloudWatch o MySQL escrita con sus opciones directo en
[sources.cloudwatch] o [sources.mysql] es el entorno DEFAULT. El formato anterior ([opensearch.envs.<env>],
[cloudwatch] con provider, [db] con ssh_host/host…) se sigue leyendo.

Los ajustes que se pueden cambiar desde el visor están en SETTINGS: de ahí salen la carga, la validación, el
guardado y la API. Cada servidor MCP recarga los archivos cuando cambian (refresh), así que no hace falta
reiniciar salvo para host y puerto del visor.
"""

from __future__ import annotations

import copy
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import providers


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


CONFIG_PATH = _xdg("XDG_CONFIG_HOME", ".config") / "casebook" / "config.toml"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "casebook"
PACKS_DIR = DATA_DIR / "packs"


@dataclass
class OpenSearchEnv:
    domain: str = ""  # dashboards-proxy: el dominio del cluster (va en SNI y Host)
    tunnel_port: int = 0  # dashboards-proxy: el LocalForward del host de ssh
    ssh_host: str = ""  # el Host de ~/.ssh/config que levanta el túnel (os_tunnel_<env>); en direct, opcional
    system: str = ""  # id para `login <id>`; vacío = os-<env>
    label: str = ""  # nombre para mostrar; vacío = OpenSearch <env>
    description: str = ""  # qué hay en el entorno (familias de índices), para las tools y el instalador
    provider: str = "dashboards-proxy"  # ver providers/
    options: dict[str, Any] = field(default_factory=dict)  # el resto de las opciones del proveedor
    hint: str = ""  # qué hay adentro (familias de índices), para la descripción de las tools


@dataclass
class DbCredentials:
    """De dónde salen usuario y contraseña de la base. Siempre se prueba también el keyring (`login db`)."""

    kind: str = "keyring"  # "env-file" (líneas `export X=...`), "env" (variables de entorno) o "keyring"
    path: str = ""  # env-file: el archivo
    user_var: str = ""  # env-file y env: los nombres de las variables
    password_var: str = ""
    hint: str = "guárdalas con `casebook login db`"  # lo que se le dice al usuario si faltan
    fix: str = ""  # el comando que sugieren el doctor y el instalador; vacío = ninguno


# El nombre de la instancia que toma una fuente escrita en el formato de una sola ([sources.mysql] o
# [sources.cloudwatch] con sus opciones directo): conserva los ids `db` y `cw` y sus entradas del keyring.
DEFAULT = "default"


@dataclass
class MySqlSource:
    """Una base MySQL ([sources.mysql.<nombre>]): a dónde se conecta, con qué credenciales y cómo se muestra."""

    provider: str = "mysql-ssh"
    ssh_host: str = ""
    # Endpoint de solo lectura (reader) de la base. Nunca el writer.
    host: str = ""
    port: int = 3306
    database: str = ""
    reader_host_pattern: str = ""  # si no está vacío, el host tiene que contenerlo (p. ej. "cluster-ro" en Aurora)
    hint: str = ""  # qué tablas hay, para la descripción de las tools
    environment: str = ""  # cómo se nombra en el conocimiento ("<environment>/<database>"); vacío = el host
    system: str = ""  # id para `login <id>`; vacío = db (la instancia default) o db-<nombre>
    label: str = ""  # nombre para mostrar; vacío = MySQL <nombre>
    description: str = ""  # qué hay en la base, para las tools y el instalador
    options: dict[str, Any] = field(default_factory=dict)  # las opciones validadas del proveedor (para plugins)
    credentials: DbCredentials = field(default_factory=DbCredentials)


@dataclass
class CloudWatchSource:
    """Un backend de Logs Insights ([sources.cloudwatch.<nombre>]): el proveedor y sus opciones."""

    provider: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    hint: str = ""  # qué log groups hay, para la descripción de las tools
    system: str = ""  # id para `login <id>`; vacío = cw (la instancia default) o cw-<nombre>
    label: str = ""  # nombre para mostrar; vacío = CloudWatch <nombre>
    description: str = ""


@dataclass
class Database:
    """Las guardas de MySQL ([db]): valen para todas las bases configuradas."""

    max_execution_ms: int = 15_000
    # EXPLAIN previo: se rechaza un full scan (type=ALL) sobre más de estas filas estimadas,
    # o cualquier plan cuyo producto de filas estimadas supere el segundo umbral.
    full_scan_max_rows: int = 200_000
    plan_max_rows: int = 5_000_000
    # LIMIT que se agrega a un SELECT sin LIMIT, y el máximo aceptado.
    default_limit: int = 100
    max_limit: int = 5_000


@dataclass
class Triage:
    """El triage automático de Slack: con qué skill, qué resultados puede dar y cómo se escribe la respuesta."""

    skill: str = ""  # la skill que guía el triage; vacía = sin skill
    outcomes: dict[str, str] = field(default_factory=lambda: {
        "explain": "Se responde directo",
        "escalate": "Escalar con una card",
        "need_info": "Falta información",
        "not_issue": "No es un issue",
    })
    escalation: str = "escalate"  # el resultado que pide una card (va con el campo `card` del análisis)
    escalation_label: str = "Card"
    reply_style: str = "en el idioma del hilo, lista para que la persona la pegue"
    # La skill de investigación del pack (método y recetas por tipo de caso): el análisis la carga además de `skill`
    # para sostener el resultado con evidencia de las fuentes; vacía = solo la skill de triage.
    investigation_skill: str = ""
    max_queries: int = 8  # tope de consultas a las fuentes (OpenSearch, MySQL, CloudWatch) por análisis
    # Tools extra de solo lectura que el análisis puede usar (por ejemplo, la de documentación de un conector de
    # claude.ai). Nombres exactos `mcp__...`; lo que el análisis tiene negado no se puede habilitar desde aquí.
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class CodeRepo:
    """Cómo reconocer el repo de código del equipo (lo declara el pack en [slack.code]): el asistente de instalación
    lo busca en la máquina y pide confirmarlo. La ruta en sí es de cada persona (slack.code_paths)."""

    label: str = "el código fuente"
    hint: str = ""  # qué carpeta es (por ejemplo, «la que tiene src/ y README.md»)
    names: list[str] = field(default_factory=list)  # nombres de carpeta que puede tener
    markers: list[str] = field(default_factory=list)  # archivos o carpetas que tiene que contener


@dataclass
class Pack:
    name: str
    version: str
    display_name: str
    path: Path


@dataclass
class Config:
    viewer_host: str = "127.0.0.1"
    viewer_port: int = 8765
    db_path: Path = field(default_factory=lambda: DATA_DIR / "sessions.db")  # DATA_DIR al crear la config
    # Cuánto espera una consulta cara a que el ingeniero la apruebe en el visor.
    approval_timeout_s: float = 180
    # Abrir el visor en el navegador en la primera session_start si no hay ninguna pestaña conectada.
    viewer_auto_open: bool = True
    # Base de los links a las cards (por ejemplo https://<sitio>.atlassian.net/browse/). Sin valor, el visor no
    # muestra el botón "Abrir card".
    jira_base_url: str | None = None
    # CloudWatch: tope de costo por consulta (el max_cost_usd por defecto de cw_query) y ventana máxima.
    cw_max_cost_usd: float = 0.25
    cw_max_window_days: int = 7
    # OpenSearch: size (y from + size) máximo de os_search.
    os_max_size: int = 10_000
    # Vigilancia de Slack (slack_watch.py): escucha las notificaciones de Slack de escritorio y lee el canal con el
    # conector de claude.ai vía `claude -p` solo cuando llega una (o en la revisión de respaldo, si se activa).
    slack_watch: bool = False
    slack_channel_id: str = ""  # el canal que se vigila (lo trae el pack)
    slack_channel_name: str = ""  # como aparece en las notificaciones de Slack
    slack_backup_poll_minutes: int = 0  # 0 = sin revisión de respaldo (solo notificaciones)
    slack_default_lookback_minutes: int = 0  # 0 = solo mensajes en tiempo real al activar
    slack_ask_lookback_on_enable: bool = True  # preguntar ventana al encender en el visor
    slack_poll_model: str = "haiku"
    slack_analysis_model: str = "sonnet"
    slack_max_analyses_per_day: int = 50
    # Carpetas con el código fuente local, separadas por coma: el análisis las puede leer (solo lectura) para entender
    # cómo funciona algo o confirmar un posible bug. Es de cada máquina, así que va en config.toml, no en el pack.
    slack_code_paths: str | None = None
    slack_code: CodeRepo = field(default_factory=CodeRepo)
    db: Database = field(default_factory=Database)  # las guardas de MySQL
    # Las fuentes, por nombre de instancia (el `env` de las tools): [sources.<tipo>.<nombre>].
    opensearch: dict[str, OpenSearchEnv] = field(default_factory=dict)
    mysql: dict[str, MySqlSource] = field(default_factory=dict)
    cloudwatch: dict[str, CloudWatchSource] = field(default_factory=dict)
    # Doctor: alias de shell que el equipo espera tener y qué decir cuando no se llega al servidor SSH.
    doctor_aliases: list[str] = field(default_factory=list)
    network_hint: str = "revisa tu conexión, la VPN o que tu IP esté permitida en el servidor SSH"
    pack: Pack | None = None  # el pack del que salieron los valores base
    # Textos para Claude (texts.py): de qué son los casos y ejemplos de card ([texts] y [card] del pack).
    texts_domain: str = "casos de soporte"
    card_examples: list[str] = field(default_factory=lambda: ["CASO-1234", "CASO-1240"])
    card_prefix: str = ""  # el prefijo de las cards del equipo (el visor filtra por él); vacío = cualquier card
    slack_triage: Triage = field(default_factory=Triage)
    # De qué archivo salió y su mtime al cargarlo (para refresh). Un Config() armado a mano no se recarga.
    source: Path | None = field(default=None, repr=False, compare=False)
    loaded_mtime: Any = field(default=None, repr=False, compare=False)  # _signature() al cargar
    pack_arg: Path | str | None = field(default=None, repr=False, compare=False)  # el `pack` de load()

    # Compatibilidad con plugins escritos para una sola fuente de CloudWatch: la primera instancia. Un proveedor
    # nuevo lee sus opciones de cfg.cloudwatch[self.system.env].
    def _first_cw(self) -> CloudWatchSource:
        return next(iter(self.cloudwatch.values()), CloudWatchSource())

    @property
    def cw_provider(self) -> str:
        return self._first_cw().provider

    @property
    def cw_options(self) -> dict[str, Any]:
        return self._first_cw().options

    @property
    def cw_hint(self) -> str:
        return self._first_cw().hint


# --- ajustes editables ---------------------------------------------------------------------------------------


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    key: str  # sección.clave en el TOML
    attr: str  # atributo de Config ("db.x" para Config.db.x)
    type: type
    min: float | None = None
    max: float | None = None
    restart: bool = False  # el cambio recién toma efecto al reiniciar Claude Code
    choices: tuple[str, ...] | None = None
    optional: bool = False  # admite vacío (None)

    def get(self, cfg: Config) -> Any:
        obj, name = _target(cfg, self.attr)
        return getattr(obj, name)

    def set(self, cfg: Config, value: Any) -> None:
        obj, name = _target(cfg, self.attr)
        setattr(obj, name, value)

    def coerce(self, value: Any, strict: bool = True) -> Any:
        """Valida un valor y lo devuelve con el tipo correcto. strict=False (al leer el archivo) solo revisa el
        tipo: una config escrita a mano antes de que existieran los rangos no debe impedir el arranque."""
        if value is None or value == "":
            if self.optional:
                return None
            raise SettingError(f"{self.key}: no puede quedar vacío")
        if self.type is bool:
            if not isinstance(value, bool):
                raise SettingError(f"{self.key}: tiene que ser verdadero o falso")
            return value
        if self.type in (int, float):
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise SettingError(f"{self.key}: tiene que ser un número")
            try:
                number = float(value)
            except ValueError:
                raise SettingError(f"{self.key}: tiene que ser un número") from None
            if self.type is int:
                if not number.is_integer():
                    raise SettingError(f"{self.key}: tiene que ser un número entero")
                number = int(number)
            out_of_range = self.min is not None and number < self.min or self.max is not None and number > self.max
            if strict and out_of_range:
                raise SettingError(f"{self.key}: tiene que estar entre {self.min:g} y {self.max:g}")
            return number
        text = str(value).strip()
        if strict and self.choices and text not in self.choices:
            raise SettingError(f"{self.key}: tiene que ser uno de {', '.join(self.choices)}")
        if self.key == "slack.code_paths":
            return _code_paths(text, strict)
        if self.key == "jira.base_url":
            if strict and not text.startswith("https://"):
                raise SettingError("jira.base_url: tiene que empezar con https://")
            text = text.rstrip("/") + "/"
        return text


# Verbos de tools que cambian algo. El análisis automático lee texto de Slack (no confiable): si el pack le habilita
# una tool, tiene que ser de lectura. Es una red de seguridad por nombre, no un permiso: la decisión es del pack.
_WRITE_VERBS = {"create", "update", "delete", "remove", "refund", "send", "add", "edit", "write", "post", "upload",
                "transition", "execute", "reply", "react", "publish", "trash", "share", "copy", "move", "set",
                "assign", "merge", "push", "approve", "cancel", "archive", "invite", "complete", "schedule"}


def _looks_like_write(tool: str) -> bool:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tool.rsplit("__", 1)[-1])
    return any(word in _WRITE_VERBS for word in re.findall(r"[a-z0-9]+", name.lower()))


def _private_dirs() -> tuple[Path, ...]:
    return DATA_DIR, CONFIG_PATH.parent, *(Path.home() / d for d in (".ssh", ".aws", ".gnupg", ".claude"))


def code_dirs(value: str | None) -> list[str]:
    """Las carpetas de [slack] code_paths como lista (el valor es texto separado por comas)."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _code_path(raw: str, strict: bool) -> str:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise SettingError(f"slack.code_paths: «{raw}» tiene que ser una ruta absoluta")
    if any(c in str(path) for c in "()*?[]{}"):
        raise SettingError(f"slack.code_paths: «{raw}» no puede llevar ( ) * ? [ ] {{ }}")
    path = path.resolve()
    if strict and not path.is_dir():
        raise SettingError(f"slack.code_paths: «{raw}» no es una carpeta")
    if path in (Path("/"), Path.home().resolve()):
        raise SettingError("slack.code_paths: no puede ser la raíz ni tu carpeta personal; elige la del código")
    for private in _private_dirs():
        private = private.resolve()
        if private == path or path in private.parents:
            raise SettingError(f"slack.code_paths: «{raw}» incluye {private}, que guarda credenciales")
    return str(path)


def _code_paths(text: str, strict: bool) -> str | None:
    """Normaliza las carpetas de código. Se leen con permisos de solo lectura, así que se exige que existan, que no
    sean la raíz ni la carpeta personal y que no incluyan credenciales; sus nombres van dentro de una regla de
    permisos, por eso no pueden llevar paréntesis ni comodines. Al guardar (strict) lo inválido se rechaza con su
    motivo; al leer el archivo se descarta sin dar acceso, para que un valor escrito a mano no impida arrancar."""
    out: list[str] = []
    for raw in code_dirs(text):
        try:
            out.append(_code_path(raw, strict))
        except SettingError:
            if strict:
                raise
    return ", ".join(dict.fromkeys(out)) or None


def _target(cfg: Config, attr: str) -> tuple[Any, str]:
    if attr.startswith("db."):
        return cfg.db, attr[3:]
    return cfg, attr


# Los mínimos y máximos son límites duros: desde el visor se pueden ajustar las guardas, no desactivarlas.
SETTINGS: tuple[Setting, ...] = (
    Setting("viewer.host", "viewer_host", str, restart=True, choices=("127.0.0.1", "::1", "localhost")),
    Setting("viewer.port", "viewer_port", int, 1024, 65535, restart=True),
    Setting("viewer.auto_open", "viewer_auto_open", bool),
    Setting("viewer.approval_timeout_s", "approval_timeout_s", float, 10, 900),
    Setting("jira.base_url", "jira_base_url", str, optional=True),
    Setting("cloudwatch.max_cost_usd", "cw_max_cost_usd", float, 0.01, 5),
    Setting("cloudwatch.max_window_days", "cw_max_window_days", int, 1, 30),
    Setting("opensearch.max_size", "os_max_size", int, 1, 10_000),
    Setting("slack.watch", "slack_watch", bool),
    Setting("slack.channel_id", "slack_channel_id", str),
    Setting("slack.channel_name", "slack_channel_name", str),
    Setting("slack.backup_poll_minutes", "slack_backup_poll_minutes", int, 0, 1440),
    Setting("slack.default_lookback_minutes", "slack_default_lookback_minutes", int, 0, 1440),
    Setting("slack.ask_lookback_on_enable", "slack_ask_lookback_on_enable", bool),
    Setting("slack.poll_model", "slack_poll_model", str, choices=("haiku", "sonnet", "opus")),
    Setting("slack.analysis_model", "slack_analysis_model", str, choices=("haiku", "sonnet", "opus")),
    Setting("slack.max_analyses_per_day", "slack_max_analyses_per_day", int, 1, 500),
    Setting("slack.code_paths", "slack_code_paths", str, optional=True),
    Setting("db.default_limit", "db.default_limit", int, 1, 10_000),
    Setting("db.max_limit", "db.max_limit", int, 1, 10_000),
    Setting("db.max_execution_ms", "db.max_execution_ms", int, 1_000, 120_000),
    Setting("db.full_scan_max_rows", "db.full_scan_max_rows", int, 1_000, 50_000_000),
    Setting("db.plan_max_rows", "db.plan_max_rows", int, 10_000, 1_000_000_000),
)
BY_KEY = {s.key: s for s in SETTINGS}


def _check_consistency(cfg: Config) -> None:
    if cfg.db.default_limit > cfg.db.max_limit:
        raise SettingError("db.default_limit no puede superar a db.max_limit")


# --- carga ---------------------------------------------------------------------------------------------------


def _read(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def _lookup(data: dict[str, Any], key: str) -> tuple[bool, Any]:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


_ACTIVE = "active"  # load(pack=_ACTIVE): el pack activo; pack=None: sin pack; pack=<Path>: ese pack.toml


def active_pack() -> Path | None:
    """El pack.toml del pack activo (PACKS_DIR/active dice cuál), o None si no hay."""
    marker = PACKS_DIR / "active"
    try:
        name = marker.read_text().strip()
    except OSError:
        return None
    path = PACKS_DIR / name / "pack.toml"
    return path if name and path.exists() else None


def _pack_path(pack: Path | str | None) -> Path | None:
    return active_pack() if pack == _ACTIVE else (Path(pack) if pack else None)


def merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """`over` pisa a `base` clave por clave (las tablas se combinan, el resto se reemplaza)."""
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


# Claves de [db] que son de la fuente (van a [sources.mysql]); las demás son guardas (SETTINGS).
_DB_SOURCE_KEYS = ("provider", "ssh_host", "host", "port", "database", "reader_host_pattern", "credentials")
_CW_SETTING_KEYS = ("max_cost_usd", "max_window_days")


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Pasa el formato anterior ([opensearch.envs.*], [cloudwatch] con provider, [db] con ssh_host…) a
    [sources.*]. Si están los dos, manda [sources.*]."""
    data = copy.deepcopy(data)
    legacy: dict[str, Any] = {}
    envs = data.get("opensearch", {}).pop("envs", None)
    if envs:
        legacy["opensearch"] = envs
    cw = data.get("cloudwatch", {})
    cw_source = {k: cw.pop(k) for k in list(cw) if k not in _CW_SETTING_KEYS}
    if cw_source:
        legacy["cloudwatch"] = cw_source
    db = data.get("db", {})
    db_source = {k: db.pop(k) for k in _DB_SOURCE_KEYS if k in db}
    if db_source:
        legacy["mysql"] = db_source
    for section in ("opensearch", "cloudwatch", "db"):
        if section in data and not data[section]:
            data.pop(section)
    if legacy:
        data["sources"] = merge(legacy, data.get("sources", {}))
    for kind in ("cloudwatch", "mysql"):
        table = data.get("sources", {}).get(kind)
        if isinstance(table, dict):
            data["sources"][kind] = _named(table)
    return data


def _named(table: dict[str, Any]) -> dict[str, Any]:
    """[sources.mysql] o [sources.cloudwatch] por instancia. Las opciones escritas directo en la tabla (el formato
    de una sola fuente) son la instancia DEFAULT; cada subtabla, salvo `credentials`, es una instancia con nombre."""
    flat = {k: v for k, v in table.items() if k == "credentials" or not isinstance(v, dict)}
    out = {k: v for k, v in table.items() if k not in flat}
    if flat:
        out[DEFAULT] = merge(out.get(DEFAULT, {}), flat)
    return out


def _layer(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """merge(), salvo que una fuente que cambia de `provider` se reemplaza entera: las opciones del proveedor
    anterior no le sirven al nuevo."""
    out = merge(base, over)
    for kind, instances in over.get("sources", {}).items():
        previous = base.get("sources", {}).get(kind)
        if not isinstance(instances, dict) or not isinstance(previous, dict):
            continue
        for name, src in instances.items():
            old = previous.get(name)
            if isinstance(src, dict) and isinstance(old, dict) and \
                    src.get("provider", old.get("provider")) != old.get("provider"):
                out["sources"][kind][name] = copy.deepcopy(src)
    return out


def _read_layers(path: Path, pack_path: Path | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(datos combinados, la tabla [pack], solo el pack normalizado)."""
    pack_data = _read(pack_path) if pack_path else {}
    meta = pack_data.pop("pack", {})
    base = normalize(pack_data)
    return _layer(base, normalize(_read(path))), meta, base


def load(path: Path | None = None, pack: Path | str | None = _ACTIVE) -> Config:
    path = path or CONFIG_PATH
    pack_path = _pack_path(pack)
    cfg = Config(source=path, pack_arg=pack)
    data, meta, _ = _read_layers(path, pack_path)
    if pack_path:
        cfg.pack = Pack(str(meta.get("name") or pack_path.parent.name), str(meta.get("version", "")),
                        str(meta.get("display_name") or meta.get("name") or pack_path.parent.name), pack_path)
    cfg.loaded_mtime = _signature(path, pack_path)
    for setting in SETTINGS:
        found, value = _lookup(data, setting.key)
        if found:
            setting.set(cfg, setting.coerce(value, strict=False))
    if "db_path" in data:
        cfg.db_path = Path(data["db_path"]).expanduser()
    texts = data.get("texts", {})
    cfg.texts_domain = str(texts.get("domain", cfg.texts_domain))
    cfg.card_prefix = str(data.get("card", {}).get("prefix", cfg.card_prefix))
    examples = [str(c) for c in data.get("card", {}).get("examples", [])]
    if examples:
        cfg.card_examples = (examples * 2)[:2]
    triage = data.get("slack", {}).get("triage", {})
    if triage:
        known = {f.name for f in fields(Triage)}
        unknown = sorted(set(triage) - known)
        if unknown:
            raise SettingError(f"slack.triage: opción desconocida: {', '.join(unknown)}")
        cfg.slack_triage = Triage(**{**vars(Triage()), **triage})
        if cfg.slack_triage.escalation not in cfg.slack_triage.outcomes:
            raise SettingError("slack.triage.escalation tiene que ser uno de los outcomes")
        extra = cfg.slack_triage.allowed_tools
        if not isinstance(extra, list) or any(
                not isinstance(t, str) or not t.startswith("mcp__") or "*" in t for t in extra):
            raise SettingError("slack.triage.allowed_tools tiene que ser una lista de nombres exactos mcp__… "
                               "(sin comodines)")
        writes = [t for t in extra if _looks_like_write(t)]
        if writes:
            raise SettingError(f"slack.triage.allowed_tools: {', '.join(writes)} parece una tool que escribe; el "
                               "análisis automático lee contenido que no es confiable y solo admite tools de lectura")
    code = data.get("slack", {}).get("code", {})
    if code:
        known = {f.name for f in fields(CodeRepo)}
        unknown = sorted(set(code) - known)
        if unknown:
            raise SettingError(f"slack.code: opción desconocida: {', '.join(unknown)}")
        cfg.slack_code = CodeRepo(**{**vars(CodeRepo()), **code})
        for key in ("names", "markers"):
            value = getattr(cfg.slack_code, key)
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise SettingError(f"slack.code.{key} tiene que ser una lista de textos")
    doctor = data.get("doctor", {})
    cfg.doctor_aliases = [str(a) for a in doctor.get("shell_aliases", cfg.doctor_aliases)]
    cfg.network_hint = str(doctor.get("network_hint", cfg.network_hint))
    try:
        _load_sources(cfg, data.get("sources", {}))
    except providers.ProviderError as exc:
        raise SettingError(str(exc)) from None
    ids = [system_id(kind, name, src.system) for kind in SOURCE_KINDS for name, src in getattr(cfg, kind).items()]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise SettingError(f"sources: ids de sistema repetidos ({', '.join(repeated)}); cambia `system` o el nombre")
    _check_consistency(cfg)
    return cfg


def _signature(path: Path, pack_path: Path | None) -> Any:
    """Lo que cambia si hay que recargar: los mtime de config.toml y del pack, y cuál es el pack."""
    def mtime(p: Path | None) -> float | None:
        try:
            return p.stat().st_mtime if p and p.exists() else None
        except OSError:
            return None

    return (mtime(path), str(pack_path) if pack_path else None, mtime(pack_path))


# Claves de una fuente que no son opciones del proveedor.
_SOURCE_META = ("system", "label", "description", "provider", "hint")


def _options(kind: str, name: str, values: dict[str, Any], where: str) -> dict[str, Any] | None:
    """Las opciones validadas contra el esquema del proveedor; None si el proveedor no está instalado (la fuente
    queda configurada pero deshabilitada, y el doctor lo dice)."""
    cls = providers.get(kind, name)
    if cls is None:
        return None
    return providers.validate(cls, values, where)


# El prefijo del id de sistema de cada tipo de fuente (`login <id>`, `source test <id>`).
ID_PREFIX = {"opensearch": "os", "cloudwatch": "cw", "mysql": "db"}


def system_id(kind: str, name: str, explicit: str = "") -> str:
    """El id de una fuente: el `system` que fija la config o, si no, <prefijo>-<nombre>. La instancia DEFAULT de
    CloudWatch y MySQL (la del formato de una sola fuente) conserva `cw` y `db`."""
    if explicit:
        return explicit
    prefix = ID_PREFIX[kind]
    return prefix if kind != "opensearch" and name == DEFAULT else f"{prefix}-{name}"


def _where(kind: str, name: str) -> str:
    return f"sources.{kind}" if name == DEFAULT else f"sources.{kind}.{name}"


def _instances(sources: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    table = sources.get(kind, {})
    if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
        raise SettingError(f"sources.{kind}: cada fuente va en su tabla, [sources.{kind}.<nombre>]")
    return table


def _load_sources(cfg: Config, sources: dict[str, Any]) -> None:
    for env, values in _instances(sources, "opensearch").items():
        name = str(values.get("provider", "dashboards-proxy"))
        raw = {k: v for k, v in values.items() if k not in _SOURCE_META}
        opts = _options("opensearch", name, raw, f"sources.opensearch.{env}")
        if opts is None:
            opts = dict(raw)
        opts = {k: v.format(env=env) if isinstance(v, str) else v for k, v in opts.items()}
        cfg.opensearch[env] = OpenSearchEnv(
            str(opts.pop("domain", "") or ""),
            int(opts.pop("tunnel_port", 0) or 0),
            str(opts.pop("ssh_host", "") or ""),
            str(values.get("system", "")),
            str(values.get("label", "")),
            str(values.get("description", "")),
            name,
            opts,
            str(values.get("hint", "")),
        )

    for env, values in _instances(sources, "cloudwatch").items():
        where = _where("cloudwatch", env)
        if not values.get("provider"):
            raise SettingError(f"{where}: falta `provider` (por ejemplo \"aws\")")
        provider = str(values["provider"])
        raw = {k: v for k, v in values.items() if k not in _SOURCE_META}
        opts = _options("logs", provider, raw, where)
        cfg.cloudwatch[env] = CloudWatchSource(provider, raw if opts is None else opts, str(values.get("hint", "")),
                                               str(values.get("system", "")), str(values.get("label", "")),
                                               str(values.get("description", "")))

    for env, values in _instances(sources, "mysql").items():
        where = _where("mysql", env)
        src = MySqlSource(provider=str(values.get("provider", "mysql-ssh")), hint=str(values.get("hint", "")),
                          environment=str(values.get("environment", "")), system=str(values.get("system", "")),
                          label=str(values.get("label", "")), description=str(values.get("description", "")))
        cls = providers.get("sql", src.provider)
        if cls is not None:
            raw = {k: v for k, v in values.items() if k not in (*_SOURCE_META, "credentials", "environment")}
            src.options = providers.validate(cls, raw, where)
            for key, value in src.options.items():
                if value is not None and key in _MYSQL_FIELDS:
                    setattr(src, key, value)
        creds = values.get("credentials", {})
        login = system_id("mysql", env, src.system)
        known = {f.name for f in fields(DbCredentials)}
        unknown = sorted(set(creds) - known)
        if unknown:
            raise SettingError(f"{where}.credentials: opción desconocida: {', '.join(unknown)}")
        merged = DbCredentials(**{**vars(DbCredentials()), "hint": f"guárdalas con `casebook login {login}`",
                                  **{k: str(v) for k, v in creds.items()}})
        if merged.kind not in ("env-file", "env", "keyring"):
            raise SettingError(f"{where}.credentials.kind tiene que ser env-file, env o keyring")
        if merged.kind == "env-file" and not merged.path:
            raise SettingError(f"{where}.credentials: con kind = \"env-file\" falta `path`")
        if merged.kind != "keyring" and not (merged.user_var and merged.password_var):
            raise SettingError(f"{where}.credentials: faltan `user_var` y `password_var`")
        src.credentials = merged
        cfg.mysql[env] = src


_MYSQL_FIELDS = {f.name for f in fields(MySqlSource)} - {"options", "credentials"}


# Lo que no cambia en caliente: el visor ya escucha en un host y puerto, y la base de sesiones está abierta.
_NOT_LIVE = {"viewer_host", "viewer_port", "db_path", "source", "loaded_mtime", "pack_arg"}


def refresh(cfg: Config) -> bool:
    """Si config.toml o el pack cambiaron desde la última carga (o se activó otro pack), copia los valores nuevos
    sobre `cfg` (en el lugar, así los clientes que guardan una referencia a cfg o a cfg.db ven el cambio). Un
    archivo inválido se ignora y quedan los valores anteriores. Devuelve True si cambió algo."""
    path = cfg.source
    if path is None:
        return False
    signature = _signature(path, _pack_path(cfg.pack_arg))
    if signature == cfg.loaded_mtime:
        return False
    try:
        fresh = load(path, cfg.pack_arg)
    except (OSError, ValueError, KeyError, TypeError):  # tomllib.TOMLDecodeError y SettingError son ValueError
        return False
    for f in fields(Config):
        if f.name in _NOT_LIVE:
            continue
        if f.name == "db":
            for g in fields(Database):
                setattr(cfg.db, g.name, getattr(fresh.db, g.name))
        elif f.name in SOURCE_KINDS:
            # En el lugar: las tools guardan una referencia al diccionario. Una fuente que cambió es otro objeto, y
            # así las tools saben que tienen que rehacer su cliente.
            current, new = getattr(cfg, f.name), getattr(fresh, f.name)
            for env in [e for e in current if e not in new]:
                del current[env]
            for env, src in new.items():
                if current.get(env) != src:
                    current[env] = src
        else:
            setattr(cfg, f.name, getattr(fresh, f.name))
    cfg.loaded_mtime = fresh.loaded_mtime
    return True


# --- guardado ------------------------------------------------------------------------------------------------


def base(pack: Path | str | None = _ACTIVE) -> Config:
    """Los valores sin config.toml del usuario: los de fábrica más los del pack. Un ajuste igual a esto no se
    escribe en config.toml (así un cambio del pack le llega a quien no lo tocó)."""
    cfg = Config()
    pack_path = _pack_path(pack)
    if not pack_path:
        return cfg
    _, _, data = _read_layers(Path("/nonexistent/config.toml"), pack_path)
    for setting in SETTINGS:
        found, value = _lookup(data, setting.key)
        if found:
            setting.set(cfg, setting.coerce(value, strict=False))
    return cfg


def save(values: dict[str, Any], path: Path | None = None, pack: Path | str | None = _ACTIVE) -> dict[str, Any]:
    """Valida `values` ({clave: valor}, claves de SETTINGS) y los escribe en config.toml, conservando las demás
    claves del archivo. Un valor igual al de fábrica (o al del pack) se borra del archivo. Antes de reescribir deja
    una copia en config.toml.bak (el archivo se regenera, así que los comentarios no sobreviven). Devuelve lo que
    se aplicó."""
    path = path or CONFIG_PATH
    unknown = sorted(set(values) - set(BY_KEY))
    if unknown:
        raise SettingError(f"ajuste desconocido: {', '.join(unknown)}")
    defaults = base(pack)
    clean = {key: BY_KEY[key].coerce(value) for key, value in values.items()}

    data = _read(path)
    probe = load(path, pack)  # la config actual + los cambios, para revisar que quede coherente
    for key, value in clean.items():
        BY_KEY[key].set(probe, value)
    _check_consistency(probe)

    for key, value in clean.items():
        section, name = key.split(".", 1)
        table = data.setdefault(section, {})
        if value is None or value == BY_KEY[key].get(defaults):
            table.pop(name, None)
        else:
            table[name] = value
        if not table:
            data.pop(section, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + ".bak").write_text(path.read_text())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(data))
    tmp.replace(path)
    return clean


def _write_user(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.with_name(path.name + ".bak").write_text(path.read_text())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps(data))
    tmp.replace(path)


SOURCE_KINDS = ("opensearch", "cloudwatch", "mysql")


def set_source(kind: str, env: str, values: dict[str, Any], path: Path | None = None,
               pack: Path | str | None = _ACTIVE) -> None:
    """Agrega o reemplaza una fuente en config.toml ([sources.<tipo>.<env>]). Se valida cargando la config
    resultante antes de escribir."""
    path = path or CONFIG_PATH
    if kind not in SOURCE_KINDS or not env:
        raise SettingError(f"fuente inválida: el tipo es uno de {', '.join(SOURCE_KINDS)} y lleva un nombre")
    data = normalize(_read(path))
    data.setdefault("sources", {}).setdefault(kind, {})[env] = values
    path.parent.mkdir(parents=True, exist_ok=True)  # la primera fuente de una instalación nueva
    probe = path.with_name(path.name + ".probe")
    probe.write_text(dumps(data))
    try:
        load(probe, pack)
    finally:
        probe.unlink(missing_ok=True)
    _write_user(data, path)


def remove_source(kind: str, env: str, path: Path | None = None) -> bool:
    """Quita una fuente de config.toml. Devuelve False si ahí no estaba (por ejemplo, porque es del pack)."""
    path = path or CONFIG_PATH
    data = normalize(_read(path))
    sources = data.get("sources", {})
    container = sources.get(kind, {})
    if env not in container:
        return False
    del container[env]
    if not container:
        sources.pop(kind, None)
    if not sources:
        data.pop("sources", None)
    _write_user(data, path)
    return True


def dumps(data: dict[str, Any]) -> str:
    """TOML mínimo para lo que tiene este archivo (y un pack): tablas anidadas, valores simples y listas de tablas
    ([[requires.plugins]])."""
    out = ["# casebook. Se puede editar a mano o desde Ajustes en el visor.", ""]

    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        raise TypeError(f"no sé escribir {type(v).__name__} en TOML")

    def is_tables(v: Any) -> bool:
        return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)

    def table(prefix: str, node: dict[str, Any], header: str | None = None) -> None:
        scalars = {k: v for k, v in node.items() if not isinstance(v, dict) and not is_tables(v)}
        if header or (prefix and scalars):
            out.append(header or f"[{prefix}]")
        for k, v in scalars.items():
            out.append(f"{_key(k)} = {value(v)}")
        if scalars or header:
            out.append("")
        for k, v in node.items():
            name = f"{prefix}.{_key(k)}" if prefix else _key(k)
            if isinstance(v, dict):
                table(name, v)
            elif is_tables(v):
                for item in v:
                    table(name, item, header=f"[[{name}]]")

    table("", data)
    return "\n".join(out).rstrip() + "\n"


def _key(k: str) -> str:
    return k if k.replace("_", "").replace("-", "").isalnum() else '"' + k.replace('"', '\\"') + '"'
