"""OpenSearch vía el proxy de la consola de Dashboards, por el túnel SSH.

Solo lectura: las rutas las arma este módulo a partir de operaciones conocidas; nunca se reenvía una ruta
arbitraria. La cookie de sesión rota en cada respuesta y se guarda la versión nueva.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import anyio
import httpx

from .. import credentials, systems
from ..config import Config
from ..doctor import set_cookies
from . import os_tunnel
from .errors import BackendError, SessionExpired

TUNNEL_RECHECK_S = 60
_last_ok: dict[str, float] = {}  # env -> última vez que una consulta pasó por el túnel
log = logging.getLogger("casebook")


class OpenSearchError(BackendError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        reason = body
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            err = body["error"]
            reason = f"{err.get('type')}: {err.get('reason')}"
        super().__init__(f"HTTP {status}: {reason}")


class OpenSearchClient:
    def __init__(self, cfg: Config, env: str):
        if env not in cfg.opensearch:
            raise ValueError(f"entorno desconocido {env!r}; opciones: {sorted(cfg.opensearch)}")
        self.cfg = cfg
        self.env = env
        self.system = systems.opensearch(cfg, env)
        self.target = cfg.opensearch[env]
        # Directo al túnel (127.0.0.1:<puerto>), con el dominio real en SNI y Host: el certificado de AWS se verifica
        # con el trust store normal. No hace falta el nginx local ni /etc/hosts (esos son para el navegador).
        self.base = f"https://127.0.0.1:{self.target.tunnel_port}/_dashboards"

    def describe(self, method: str, path: str, body: dict | None) -> str:
        text = f"{method} {path}"
        if body is not None:
            text += "\n" + json.dumps(body, indent=2, ensure_ascii=False)
        return text

    async def request(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> Any:
        cookie = credentials.get_cookie(self.system)
        if not cookie:
            raise SessionExpired(f"no hay sesión guardada para {self.env}. Corre `casebook login {self.system.id}`")
        # Si hace rato que no se usa, se prueba el túnel antes (un handshake): un túnel colgado se repara ya,
        # en vez de esperar el timeout de la consulta.
        if time.monotonic() - _last_ok.get(self.env, 0) > TUNNEL_RECHECK_S:
            await self._ensure_tunnel()
        try:
            resp = await self._post(method, path, body, cookie, timeout)
        except _TunnelProblem:
            await self._ensure_tunnel()
            resp = await self._post(method, path, body, cookie, timeout)  # un solo reintento
        _last_ok[self.env] = time.monotonic()

        credentials.refresh_from_response(self.system, cookie, set_cookies(resp))
        if resp.status_code == 401 or resp.is_redirect:
            raise SessionExpired(
                f"la sesión de OpenSearch {self.env} venció. Haz login en Dashboards y corre "
                f"`casebook login {self.system.id}`"
            )
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            raise OpenSearchError(resp.status_code, data)
        return data

    async def _post(self, method: str, path: str, body: dict | None, cookie: str, timeout: float) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base}/api/console/proxy?path={quote(path, safe='')}&method={method}&dataSourceId=",
                    headers={
                        "host": self.target.domain,
                        "cookie": cookie,
                        "osd-xsrf": "true",
                        "content-type": "application/json",
                    },
                    content=json.dumps(body) if body is not None else None,
                    extensions={"sni_hostname": self.target.domain},
                )
            except httpx.ConnectError as exc:
                raise _TunnelProblem() from exc
            except httpx.TimeoutException as exc:
                if os_tunnel.state(self.target.tunnel_port, self.target.domain) != os_tunnel.UP:
                    raise _TunnelProblem() from exc
                raise BackendError(f"OpenSearch no respondió en {timeout:.0f} s; acota la consulta") from exc
        return resp

    async def _ensure_tunnel(self) -> None:
        result, action = await anyio.to_thread.run_sync(
            os_tunnel.ensure, self.target.ssh_host, self.target.tunnel_port, self.target.domain
        )
        if result != os_tunnel.UP:
            raise BackendError(
                f"el túnel de OpenSearch {self.env} no funciona ({action}). Pídele al usuario que revise que "
                f"`ssh {self.target.ssh_host}` conecte sin pedir nada"
            )
        if action != "túnel arriba":
            log.warning("túnel %s: %s", self.env, action)


class _TunnelProblem(Exception):
    pass


# --- tablas --------------------------------------------------------------------------------------------------


def _flatten(doc: Any, prefix: str = "", out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, dict):
                _flatten(v, f"{prefix}{k}.", out)
            else:
                out[f"{prefix}{k}"] = v
    return out


def table_from_records(records: list[dict[str, Any]]) -> tuple[list[str], list[list[Any]]]:
    columns: list[str] = []
    seen: set[str] = set()
    for r in records:
        for k in r:
            if k not in seen:
                seen.add(k)
                columns.append(k)
    return columns, [[r.get(c) for c in columns] for r in records]


def hits_table(response: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    records = []
    for hit in response.get("hits", {}).get("hits", []):
        record = {"_index": hit.get("_index"), "_id": hit.get("_id")}
        record.update(_flatten(hit.get("_source") or {}))
        for k, v in (hit.get("fields") or {}).items():
            record.setdefault(k, v[0] if isinstance(v, list) and len(v) == 1 else v)
        records.append(record)
    return table_from_records(records)


def _bucket_rows(agg: dict[str, Any], path: dict[str, Any]) -> list[dict[str, Any]]:
    """Aplana buckets anidados: cada fila lleva las claves de los buckets padres y las métricas hoja."""
    buckets = agg.get("buckets")
    if isinstance(buckets, dict):  # filters agg con keyed=true
        buckets = [{"key": k, **v} for k, v in buckets.items()]
    if not isinstance(buckets, list):
        return [{**path, **_metrics(agg)}] if path or _metrics(agg) else []
    rows = []
    for b in buckets:
        key = b.get("key_as_string", b.get("key"))
        here = {**path, _key_name(path): key, _count_name(path): b.get("doc_count")}
        children = {k: v for k, v in b.items() if isinstance(v, dict) and _is_agg(v)}
        nested = [k for k, v in children.items() if "buckets" in v]
        metrics: dict[str, Any] = {}
        for k, v in children.items():
            if "buckets" in v:
                continue
            if "doc_count" in v and "value" not in v and "values" not in v:
                metrics.update(_single_bucket(k, v))
            else:
                metrics[k] = _metric_value(v)
        if nested:
            for name in nested:
                for row in _bucket_rows(children[name], here):
                    rows.append({**row, **metrics})
        else:
            rows.append({**here, **metrics})
    return rows


def _key_name(path: dict[str, Any]) -> str:
    level = len(path) // 2
    return "key" if level == 0 else f"key_{level + 1}"


def _count_name(path: dict[str, Any]) -> str:
    level = len(path) // 2
    return "doc_count" if level == 0 else f"doc_count_{level + 1}"


def _is_agg(v: dict[str, Any]) -> bool:
    return "buckets" in v or "value" in v or "values" in v or "doc_count" in v


def _single_bucket(name: str, agg: dict[str, Any]) -> dict[str, Any]:
    """Aggs de un solo bucket (filter, missing, nested, global): su doc_count y las métricas que tengan adentro."""
    out = {name: agg.get("doc_count")}
    for k, v in agg.items():
        if isinstance(v, dict) and ("value" in v or "values" in v):
            out[f"{name}.{k}"] = _metric_value(v)
    return out


def _metric_value(agg: dict[str, Any]) -> Any:
    if "value_as_string" in agg:
        return agg["value_as_string"]
    if "value" in agg:
        return agg["value"]
    return agg.get("values")


def _metrics(agg: dict[str, Any]) -> dict[str, Any]:
    if "value" in agg or "values" in agg:
        return {"value": _metric_value(agg)}
    return {k: v for k, v in agg.items() if isinstance(v, (int, float, str)) and k != "meta"}


def aggs_tables(response: dict[str, Any]) -> list[dict[str, Any]]:
    tables = []
    for name, agg in (response.get("aggregations") or {}).items():
        if not isinstance(agg, dict):
            continue
        columns, rows = table_from_records(_bucket_rows(agg, {}))
        tables.append({"name": f"aggs.{name}", "columns": columns, "rows": rows})
    return tables
