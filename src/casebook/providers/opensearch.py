"""Proveedores de OpenSearch.

- dashboards-proxy: la consola de OpenSearch Dashboards (`/api/console/proxy`), por un túnel de ~/.ssh/config, con
  la cookie de la sesión del navegador. Es lo que hay cuando el cluster solo se abre desde Dashboards.
- direct: la API REST del cluster (una URL), con usuario y contraseña o API key guardados en el keyring, y un
  túnel SSH opcional (`ssh -L` al host de la URL).
"""

from __future__ import annotations

import json
import threading
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from .. import credentials
from ..backends.errors import BackendError, SessionExpired
from ..backends.opensearch import OpenSearchClient, OpenSearchError
from ..backends.tunnel import SshTunnel
from . import Field
from .base import Provider


class DashboardsProxy(Provider):
    kind = "opensearch"
    name = "dashboards-proxy"
    label = "OpenSearch Dashboards (consola, por túnel SSH)"
    auth = "browser"
    fields = (
        Field("domain", "Dominio del cluster", required=True, help="va en SNI y en Host; el certificado se verifica"),
        Field("tunnel_port", "Puerto local del túnel", int, required=True, help="el LocalForward del host de ssh"),
        Field("ssh_host", "Host de ~/.ssh/config", default="os_tunnel_{env}",
              help="el que levanta el túnel con LocalForward <puerto> <dominio>:443"),
    )

    def client(self) -> OpenSearchClient:
        return OpenSearchClient(self.cfg, self.system.env)

    def login_target(self):
        from ..login import BrowserTarget, validate_os

        env = self.system.env
        domain = self.cfg.opensearch[env].domain
        return BrowserTarget(
            f"https://{domain}/_dashboards/app/dev_tools#/console",
            f"https://{domain}/_dashboards/api/v1/configuration/account",
            lambda c: validate_os(self.cfg, env, c),
        )

    def prepare_login(self) -> str | None:
        from ..backends import os_tunnel

        target = self.cfg.opensearch[self.system.env]
        state, action = os_tunnel.ensure(target.ssh_host, target.tunnel_port, target.domain)
        return None if state == os_tunnel.UP else f"No se pudo preparar el túnel: {action}."

    def doctor_checks(self, heal: bool = True):
        from ..doctor import check_opensearch

        return check_opensearch(self.cfg, self.system.env, heal)


class Direct(Provider):
    kind = "opensearch"
    name = "direct"
    label = "OpenSearch directo (API REST)"
    auth = "secret"
    fields = (
        Field("url", "URL del cluster", required=True, help="por ejemplo https://search.miempresa.internal:9200"),
        Field("auth", "Autenticación", default="basic", choices=("basic", "api-key", "none")),
        Field("ssh_host", "Túnel SSH (opcional)", help="un host de ~/.ssh/config: se abre `ssh -L` hacia la URL"),
        Field("verify_tls", "Verificar el certificado", bool, default=True),
    )

    @classmethod
    def auth_for(cls, options: dict[str, Any]) -> str:
        return "none" if options.get("auth") == "none" else "secret"

    def secret_fields(self) -> tuple[Field, ...]:
        if self.cfg.opensearch[self.system.env].options.get("auth") == "api-key":
            return (Field("api_key", "API key", required=True),)
        return (Field("user", "Usuario", required=True), Field("password", "Contraseña", required=True))

    def client(self) -> DirectClient:
        return DirectClient(self.cfg, self.system)

    def doctor_checks(self, heal: bool = True):
        from ..doctor import FAIL, OK, Check

        name = self.system.check
        try:
            info = anyio.run(lambda: self.client().request("GET", "", None, 15))
        except BackendError as exc:
            return [Check(name, FAIL, str(exc))]
        version = (info.get("version") or {}).get("number") if isinstance(info, dict) else None
        cluster = info.get("cluster_name") if isinstance(info, dict) else None
        return [Check(name, OK, f"conectado a {cluster or 'el cluster'}" + (f" (OpenSearch {version})" if version else ""))]


# Un túnel por destino, compartido por las consultas del proceso.
_tunnels: dict[tuple[str, str, int], SshTunnel] = {}
_tunnels_lock = threading.Lock()


def _tunnel(ssh_host: str, host: str, port: int) -> SshTunnel:
    with _tunnels_lock:
        key = (ssh_host, host, port)
        if key not in _tunnels:
            _tunnels[key] = SshTunnel(ssh_host, host, port)
        return _tunnels[key]


class DirectClient:
    def __init__(self, cfg, system):
        target = cfg.opensearch[system.env]
        self.system = system
        self.env = system.env
        self.options = target.options
        self.url = str(self.options["url"]).rstrip("/")
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BackendError(f"opensearch.envs.{self.env}.url no es una URL válida: {self.url}")
        self.scheme, self.host = parts.scheme, parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.ssh_host = target.ssh_host

    def describe(self, method: str, path: str, body: dict | None) -> str:
        text = f"{method} {path}"
        if body is not None:
            text += "\n" + json.dumps(body, indent=2, ensure_ascii=False)
        return text

    def _auth(self) -> tuple[dict[str, str], tuple[str, str] | None]:
        kind = self.options.get("auth", "basic")
        if kind == "none":
            return {}, None
        secret = credentials.get_secret(self.system)
        if not secret:
            raise SessionExpired(f"no hay credenciales guardadas para {self.env}. Corre `casebook login {self.system.id}`")
        if kind == "api-key":
            return {"authorization": f"ApiKey {secret.get('api_key', '')}"}, None
        return {}, (secret.get("user", ""), secret.get("password", ""))

    async def request(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> Any:
        headers, basic = self._auth()
        headers["content-type"] = "application/json"
        base, extensions = self.url, {}
        if self.ssh_host:
            port = await anyio.to_thread.run_sync(_tunnel(self.ssh_host, self.host, self.port).ensure)
            base = f"{self.scheme}://127.0.0.1:{port}"
            headers["host"] = self.host if self.port in (80, 443) else f"{self.host}:{self.port}"
            extensions["sni_hostname"] = self.host
        async with httpx.AsyncClient(timeout=timeout, verify=bool(self.options.get("verify_tls", True)),
                                     auth=basic) as client:
            try:
                resp = await client.request(
                    method, f"{base}/{path.lstrip('/')}", headers=headers,
                    content=json.dumps(body) if body is not None else None, extensions=extensions,
                )
            except httpx.TimeoutException as exc:
                raise BackendError(f"OpenSearch no respondió en {timeout:.0f} s; acota la consulta") from exc
            except httpx.HTTPError as exc:
                raise BackendError(f"no se pudo conectar con OpenSearch {self.env} ({self.url}): {exc}") from exc
        if resp.status_code in (401, 403):
            raise SessionExpired(
                f"OpenSearch {self.env} rechazó las credenciales (HTTP {resp.status_code}). "
                f"Corre `casebook login {self.system.id}`"
            )
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            raise OpenSearchError(resp.status_code, data)
        return data
