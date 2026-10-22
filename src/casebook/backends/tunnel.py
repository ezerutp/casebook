"""Túnel SSH propio (ssh -N -L) hacia un servidor SSH, levantado bajo demanda y bajado al salir del proceso.

stdin y stdout de ssh van a /dev/null: en el servidor MCP esos son el canal con Claude Code. BatchMode evita que
ssh se quede esperando una contraseña o una confirmación interactiva.
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import threading
import time

from ..config import DATA_DIR
from . import ssh_diag
from .errors import BackendError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class SshTunnel:
    def __init__(self, ssh_host: str, remote_host: str, remote_port: int, startup_timeout: float = 20):
        self.ssh_host = ssh_host
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.startup_timeout = startup_timeout
        self.port: int | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        atexit.register(self.close)

    def ensure(self) -> int:
        """Devuelve el puerto local del túnel, levantándolo si hace falta."""
        with self._lock:
            if self._proc and self._proc.poll() is None and self.port and _port_open(self.port):
                return self.port
            self._stop()
            port = _free_port()
            log = DATA_DIR / f"tunnel-mysql-{self.ssh_host}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "wb") as fh:  # un log por intento: la última línea es siempre la de este intento
                self._proc = subprocess.Popen(
                    [
                        "ssh", "-N", *ssh_diag.TUNNEL_OPTIONS,
                        "-L", f"127.0.0.1:{port}:{self.remote_host}:{self.remote_port}",
                        self.ssh_host,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=fh,
                )
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    self._proc = None
                    why = ssh_diag.explain(ssh_diag.last_error_line(log), self.ssh_host)
                    raise BackendError(f"no se pudo abrir el túnel SSH a {self.ssh_host}: {why}")
                if _port_open(port):
                    self.port = port
                    return port
                time.sleep(0.2)
            self._stop()
            why = ssh_diag.explain(ssh_diag.last_error_line(log), self.ssh_host)
            raise BackendError(f"el túnel SSH a {self.ssh_host} no respondió en {self.startup_timeout:.0f} s: {why}")

    def _stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.port = None

    def close(self) -> None:
        with self._lock:
            self._stop()
