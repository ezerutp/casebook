"""Túneles SSH de OpenSearch (los `os_tunnel_<env>` de ~/.ssh/config, los mismos que usa `os-prod`).

Detecta si el túnel está arriba, caído o colgado y lo repara:
- caído: lo levanta con `ssh -N <host>`;
- colgado (el puerto acepta conexiones pero nada responde): termina los procesos ssh de ESE host y lo levanta.

El túnel que se levanta aquí queda independiente del proceso (como el de `os-prod`) y usa ServerAliveInterval, así
que si se corta la conexión con el servidor SSH, ssh sale en vez de quedar colgado.
"""

from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import time
from pathlib import Path

from ..config import DATA_DIR
from . import ssh_diag

UP, DOWN, HUNG = "up", "down", "hung"
PROBE_TIMEOUT = 4
START_TIMEOUT = 20


def state(port: int, domain: str) -> str:
    """Un handshake TLS a través del túnel: si OpenSearch contesta, el túnel funciona de punta a punta."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=1)
    except OSError:
        return DOWN
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # solo se prueba que del otro lado responda alguien
    try:
        sock.settimeout(PROBE_TIMEOUT)
        with ctx.wrap_socket(sock, server_hostname=domain):
            return UP
    except (OSError, ssl.SSLError):
        return HUNG
    finally:
        sock.close()


def ssh_pids(host: str) -> list[int]:
    """PIDs de los procesos `ssh ... <host> ...` (coincidencia exacta del alias, no por prefijo)."""
    try:
        out = subprocess.run(["pgrep", "-a", "-x", "ssh"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.partition(" ")
        if host in cmd.split():
            pids.append(int(pid))
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Un proceso zombie (terminado pero sin recoger) ya no ocupa el puerto.
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (OSError, IndexError):
        return True


def _terminate(pids: list[int], grace: float = 3) -> None:
    """SIGTERM y, si no salen a tiempo, SIGKILL (un proceso detenido no atiende SIGTERM)."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.1)
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _start(host: str, log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as fh:  # un log por intento: la última línea es siempre la de este intento
        return subprocess.Popen(
            ["ssh", "-N", *ssh_diag.TUNNEL_OPTIONS, host],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=fh,
            start_new_session=True,  # sobrevive al proceso, como el túnel de os-prod
        )


def ensure(host: str, port: int, domain: str) -> tuple[str, str]:
    """Deja el túnel funcionando si se puede. Devuelve (estado final, qué se hizo)."""
    current = state(port, domain)
    if current == UP:
        return UP, "túnel arriba"

    action = "estaba caído"
    if current == HUNG:
        pids = ssh_pids(host)
        _terminate(pids)
        action = f"estaba colgado (se terminó ssh pid {', '.join(map(str, pids))})" if pids else "estaba colgado"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and state(port, domain) != DOWN:
            time.sleep(0.2)
        if state(port, domain) != DOWN:
            return HUNG, f"{action}, pero el puerto {port} sigue ocupado por otro proceso"

    log = DATA_DIR / f"tunnel-{host}.log"
    proc = _start(host, log)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            why = ssh_diag.explain(ssh_diag.last_error_line(log), host)
            return DOWN, f"{action}; no se pudo levantar `ssh {host}`: {why}"
        if state(port, domain) == UP:
            return UP, f"{action}; se levantó de nuevo"
        time.sleep(0.3)
    why = ssh_diag.explain(ssh_diag.last_error_line(log), host)
    return state(port, domain), f"{action}; el túnel nuevo no respondió en {START_TIMEOUT} s: {why}"
