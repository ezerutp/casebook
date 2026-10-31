"""Revisa que cada conexión esté lista: túneles (y los repara) y sesiones del navegador.

Nunca imprime valores de cookies: solo estados y, como mucho, nombres de cookies.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from . import config, credentials, systems
from .backends import os_tunnel
from .config import Config

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    # Para el instalador: dónde agruparlo, una clave estable (para recordar un aviso ignorado) y el comando que
    # lo arregla, si hay uno.
    group: str = ""
    key: str = ""
    fix: str = ""


def set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {c.name: c.value for c in resp.cookies.jar}


def session_status(resp: httpx.Response) -> tuple[str, str]:
    if resp.status_code == 200:
        return OK, "sesión vigente"
    if resp.status_code in (401, 403) or resp.is_redirect:
        return FAIL, f"sesión vencida o inválida (HTTP {resp.status_code})"
    return FAIL, f"respuesta inesperada (HTTP {resp.status_code})"


def check_opensearch(cfg: Config, env: str, heal: bool = True) -> list[Check]:
    target = cfg.opensearch[env]
    system = systems.opensearch(cfg, env)
    name = system.check
    cookie = credentials.get_cookie(system)
    if not cookie:
        return [Check(name, FAIL, f"sin cookie guardada. Corre `casebook login {system.id}`")]
    checks = []

    if heal:
        result, action = os_tunnel.ensure(target.ssh_host, target.tunnel_port, target.domain)
    else:
        result = os_tunnel.state(target.tunnel_port, target.domain)
        action = {os_tunnel.UP: "túnel arriba", os_tunnel.DOWN: "túnel caído", os_tunnel.HUNG: "túnel colgado"}[result]
    if result != os_tunnel.UP:
        checks.append(Check(name, FAIL, f"{action}. Revisa que `ssh {target.ssh_host}` conecte sin pedir nada"))
        return checks
    checks.append(Check(name, OK if action == "túnel arriba" else WARN, f"{action} (puerto {target.tunnel_port})"))

    url = f"https://127.0.0.1:{target.tunnel_port}/_dashboards/api/v1/configuration/account"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                url, headers={"host": target.domain, "cookie": cookie}, extensions={"sni_hostname": target.domain}
            )
    except httpx.TimeoutException:
        checks.append(Check(name, FAIL, f"OpenSearch no respondió a tiempo por el túnel `{target.ssh_host}`; corre `casebook doctor` de nuevo"))
        return checks
    except httpx.HTTPError as exc:
        checks.append(Check(name, FAIL, f"error de conexión: {exc}"))
        return checks
    credentials.refresh_from_response(system, cookie, set_cookies(resp))
    status, detail = session_status(resp)
    if status != OK:
        detail += f". Haz login en Dashboards y corre `casebook login {system.id}`"
    checks.append(Check(name, status, detail))
    return checks


def check_db(cfg: Config, system=None) -> list[Check]:
    from .backends.errors import BackendError
    from .backends.mysql import MySQLClient

    system = system or systems.mysql(cfg)
    name = system.check
    source = cfg.mysql[system.env].credentials
    if not credentials.get_db_login(source, system.keyring):
        return [Check(name, FAIL, f"sin credenciales en {credentials.describe_source(source, system.id)}: "
                      f"{source.hint}")]
    try:
        client = MySQLClient(cfg.mysql[system.env], cfg.db, system.keyring, system.id)
    except BackendError as exc:
        return [Check(name, FAIL, str(exc))]
    try:
        info = client.server_info()
    except BackendError as exc:
        return [Check(name, FAIL, str(exc))]
    finally:
        client.close()
    return [
        Check(
            name,
            OK,
            f"conectado al reader (MySQL {info['version']}, innodb_read_only={info['innodb_read_only']}) "
            f"con las credenciales de {credentials.db_login_source(source, system.keyring, system.id)}",
        )
    ]


def checks(cfg: Config, heal: bool = True) -> list:
    """Los chequeos por sistema, para correrlos en serie (CLI) o en paralelo (session_start)."""
    return [lambda system=system: check_system(cfg, system, heal) for system in systems.all_systems(cfg)]


def check_system(cfg: Config, system, heal: bool = True) -> list[Check]:
    """Los chequeos que define el proveedor del sistema; si no está instalado, eso mismo es la falla."""
    provider = systems.provider(cfg, system)
    if provider is None:
        return [Check(system.check, FAIL, systems.missing_provider_message(system))]
    return provider.doctor_checks(heal)


def run(cfg: Config, heal: bool = True) -> list[Check]:
    return [c for check in checks(cfg, heal) for c in check()]


# --- requisitos ----------------------------------------------------------------------------------------------
# Lo que tiene que estar en su sitio en la máquina, antes de cualquier sesión. Lo usan `doctor` y el instalador;
# no corre en session_start (abrir la shell para leer los alias tarda unos segundos).

def _shell_aliases(names: tuple[str, ...] | list[str]) -> dict[str, bool] | None:
    """Qué alias existen en la shell interactiva del usuario (None si no se pudo preguntar)."""
    import os
    import subprocess

    shell = os.environ.get("SHELL") or "/bin/bash"
    try:
        out = subprocess.run(
            [shell, "-ic", "alias"], capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    defined = set()
    for line in out.splitlines():
        line = line.removeprefix("alias ").strip()
        defined.add(line.partition("=")[0].strip("'\""))
    return {name: name in defined for name in names}


def _ago(ts: float) -> str:
    import time

    minutes = int((time.time() - ts) // 60)
    if minutes < 1:
        return "hace menos de un minuto"
    if minutes < 120:
        return f"hace {minutes} min"
    hours = minutes // 60
    return f"hace {hours} h" if hours < 48 else f"hace {hours // 24} días"


def check_extension() -> Check:
    """La extensión de Chrome es opcional: sin ella el login abre una ventana aparte."""
    from . import native_host

    name = "requisitos"
    if not native_host.installed():
        return Check(name, WARN, "extensión de Chrome sin registrar (opcional; entrega las sesiones sola): "
                     "corre `casebook extension`")
    status = native_host.read_status()
    if not status.get("last_seen"):
        folder = native_host.extension_dir() or "la carpeta que indica `casebook extension`"
        return Check(name, WARN, "extensión de Chrome registrada pero nunca conectada: cárgala en chrome://extensions "
                     f"(Modo de desarrollador → Cargar descomprimida → {folder})")
    parts = []
    for system, result in sorted((status.get("systems") or {}).items()):
        state = "entregó la sesión" if result.get("ok") else "la última cookie no validó"
        parts.append(f"{system}: {state} {_ago(result['at'])}")
    detail = f"extensión de Chrome conectada ({_ago(status['last_seen'])})"
    missing = native_host.missing_permissions()
    if missing:
        hosts = ", ".join(m.removeprefix("https://").removesuffix("/*") for m in missing)
        return Check(name, WARN, f"{detail}, pero le falta permiso para {hosts}: ábrela (ícono de la extensión) y "
                     "toca «Dar permiso»")
    return Check(name, OK, detail + (f"; {', '.join(parts)}" if parts else ""))


def check_prereqs(cfg: Config) -> list[Check]:
    import shutil

    from .backends import ssh_diag

    name = "requisitos"
    out: list[Check] = []
    if not shutil.which("ssh"):
        return [Check(name, FAIL, "no está instalado ssh (los túneles de OpenSearch y MySQL lo necesitan)",
                      "Conexión", "ssh")]

    # Hosts de ~/.ssh/config: los túneles de OpenSearch y el de MySQL.
    hosts = [
        *[(target.ssh_host, f"OpenSearch {env}", target.tunnel_port if target.provider == "dashboards-proxy" else None)
          for env, target in cfg.opensearch.items() if target.ssh_host],
        *[(src.ssh_host, "MySQL" if env == config.DEFAULT else f"MySQL {env}", None)
          for env, src in cfg.mysql.items() if src.ssh_host],
    ]
    if not cfg.opensearch and not cfg.cloudwatch and not cfg.mysql:
        where = f"el pack {cfg.pack.name}" if cfg.pack else "ningún pack"
        out.append(Check(name, WARN, f"no hay fuentes configuradas ({where} ni config.toml): instala el pack de tu "
                         "equipo con `casebook pack install <carpeta>` o agrega [sources.*] en config.toml",
                         "Sistema", "sources"))
    servers: dict[tuple[str, int], str] = {}
    seen: set[tuple[str, int | None]] = set()
    for host, role, forward_port in hosts:
        if (host, forward_port) in seen:  # dos bases (prod y qa) detrás del mismo bastión: un solo chequeo
            continue
        seen.add((host, forward_port))
        conf = ssh_diag.ssh_config(host)
        if not ssh_diag.host_defined(host, conf):
            out.append(Check(name, FAIL, f"falta el host `{host}` en ~/.ssh/config ({role})", "Conexión",
                             f"ssh-host:{host}"))
            continue
        hostname, port = conf["hostname"][0], int(conf.get("port", ["22"])[0])
        # `ssh -G` da "9123 [dominio]:443" (o "127.0.0.1:9123 ..."): el puerto local es el final del primer campo.
        local_ports = {f.split()[0].rsplit(":", 1)[-1] for f in conf.get("localforward", []) if f.strip()}
        if forward_port and str(forward_port) not in local_ports:
            out.append(Check(name, FAIL, f"el host `{host}` no tiene `LocalForward {forward_port} <dominio de OpenSearch>:443`",
                             "Conexión", f"ssh-host:{host}"))
        else:
            out.append(Check(name, OK, f"host `{host}` en ~/.ssh/config → {hostname} ({role})", "Conexión",
                             f"ssh-host:{host}"))
        proxied = any(v and v != "none" for v in conf.get("proxyjump", []) + conf.get("proxycommand", []))
        if not proxied:
            servers[(hostname, port)] = host

    # La prueba directa de VPN / IP permitida: ¿abre el puerto de ssh del servidor?
    for (hostname, port), host in servers.items():
        ok, detail = ssh_diag.tcp_reachable(hostname, port)
        if ok:
            out.append(Check(name, OK, f"servidor SSH {detail}", "Conexión", f"ssh-server:{hostname}"))
        else:
            out.append(Check(name, FAIL, f"servidor SSH {detail} → tu máquina no llega: {cfg.network_hint}",
                             "Conexión", f"ssh-server:{hostname}"))

    for env, src in cfg.mysql.items():
        source = src.credentials
        if source.kind == "keyring":
            continue
        where = credentials.describe_source(source)
        base = "la base" if env == config.DEFAULT else f"la base {env}"
        key = "db-credentials" if env == config.DEFAULT else f"db-credentials:{env}"
        if credentials.external_db_login(source):
            out.append(Check(name, OK, f"credenciales de {base} en {where}", "Credenciales", key))
        else:
            out.append(Check(name, FAIL, f"no hay credenciales de {base} en {where}: {source.hint}",
                             "Credenciales", key, source.fix))

    aliases = _shell_aliases(cfg.doctor_aliases) if cfg.doctor_aliases else {}
    if aliases is None:
        out.append(Check(name, WARN, "no pude leer los alias de tu shell", "Sistema", "aliases"))
    else:
        missing = [a for a, present in aliases.items() if not present]
        if missing:
            out.append(Check(name, WARN, f"no encontré los alias {', '.join(missing)} en tu shell (el paquete no los "
                             "necesita, pero usa sus mismos hosts y credenciales: revisa el setup del equipo)",
                             "Sistema", "aliases"))
        elif cfg.doctor_aliases:
            out.append(Check(name, OK, f"alias del equipo: {', '.join(cfg.doctor_aliases)}", "Sistema", "aliases"))

    out.append(check_extension())

    try:
        import keyring

        backend = type(keyring.get_keyring()).__name__
        if "fail" in backend.lower() or "null" in backend.lower():
            out.append(Check(name, FAIL, f"no hay un keyring usable ({backend}): instala gnome-keyring o kwallet",
                             "Credenciales", "keyring"))
        else:
            out.append(Check(name, OK, f"keyring: {backend}", "Credenciales", "keyring"))
    except Exception as exc:  # cualquier problema del backend de keyring
        out.append(Check(name, FAIL, f"keyring: {exc}", "Credenciales", "keyring"))
    return out
