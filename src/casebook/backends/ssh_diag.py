"""Diagnóstico de ssh: opciones comunes de los túneles y traducción de los errores de ssh a qué hacer.

Con BatchMode ssh nunca pregunta nada, así que cuando falla deja en stderr una línea que dice por qué. Los túneles
la guardan en un log y estos helpers la convierten en un mensaje accionable.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

CONNECT_TIMEOUT = 10  # segundos: sin esto, una IP bloqueada deja a ssh esperando el timeout del sistema

# Opciones de los túneles: nunca preguntar, fallar si no se puede abrir el forward, cortar rápido si no conecta y
# salir (en vez de quedar colgado) si se pierde la conexión.
TUNNEL_OPTIONS = [
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
]

# Fragmento del error de ssh (en minúsculas) -> qué significa para el usuario.
_HINTS = [
    (("connection timed out", "operation timed out", "no route to host", "network is unreachable"),
     "tu máquina no llega al servidor SSH: lo más común es que falte la VPN o que tu IP no esté permitida"),
    (("connection refused",),
     "el servidor SSH rechazó la conexión: revisa HostName y Port del host en ~/.ssh/config"),
    (("could not resolve hostname", "name or service not known", "temporary failure in name resolution"),
     "no se resuelve el nombre del servidor SSH: revisa HostName en ~/.ssh/config o tu DNS/VPN"),
    (("permission denied",),
     "el servidor SSH no aceptó tu clave: cárgala con `ssh-add` o revisa que sea la registrada (IdentityFile)"),
    (("host key verification failed", "remote host identification has changed"),
     "falta aceptar (o cambió) la huella del servidor SSH: conéctate una vez a mano con `ssh <host>`"),
    (("could not request local forwarding", "address already in use", "cannot listen to port"),
     "el puerto local del túnel ya está ocupado por otro proceso"),
    (("bad configuration option", "no such file or directory", "can't open user config"),
     "hay un error en ~/.ssh/config"),
]

_NOISE = ("warning: permanently added", "pseudo-terminal", "authenticated to", "debug")


def last_error_line(log: Path) -> str | None:
    """La última línea útil que ssh dejó en el log (sin avisos que no son errores)."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if line and not line.lower().startswith(_NOISE):
            return line
    return None


def explain(line: str | None, host: str) -> str:
    """'<línea de ssh> → <qué significa>' o, sin línea, la causa más probable de un ssh que no termina de conectar."""
    if not line:
        return (f"`ssh {host}` no terminó de conectar y no dejó error: lo más común es que tu máquina no llegue al "
                "servidor SSH (VPN o IP no permitida)")
    low = line.lower()
    for needles, hint in _HINTS:
        if any(n in low for n in needles):
            return f"{line} → {hint.replace('<host>', host)}"
    return line


def ssh_config(host: str) -> dict[str, list[str]]:
    """La config efectiva de un host según `ssh -G` (clave en minúsculas -> valores)."""
    try:
        out = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    config: dict[str, list[str]] = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key.lower(), []).append(value.strip())
    return config


def host_defined(host: str, config: dict[str, list[str]]) -> bool:
    """`ssh -G` responde para cualquier nombre: un host no definido queda con HostName igual al alias."""
    return bool(config) and config.get("hostname", [host])[0] != host


def tcp_reachable(hostname: str, port: int, timeout: float = 5) -> tuple[bool, str]:
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True, f"{hostname}:{port} responde"
    except socket.timeout:
        return False, f"{hostname}:{port} no respondió en {timeout:g} s"
    except OSError as exc:
        return False, f"{hostname}:{port}: {exc.strerror or exc}"
