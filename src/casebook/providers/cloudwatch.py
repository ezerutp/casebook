"""Proveedor de CloudWatch Logs Insights `aws`: la API de CloudWatch Logs con boto3 y un perfil de AWS (`aws configure`
/ `aws sso login`). boto3 es una dependencia opcional: `pip install 'casebook[aws]'`.

Otro backend de logs (un proxy propio, por ejemplo) se suma como plugin con un proveedor de kind "logs".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import anyio

from ..backends.errors import BackendError, SessionExpired
from . import Field
from .base import COMPLETE, RUNNING, Estimate, LogGroup, LogsClient, Provider, QueryState


# --- aws ------------------------------------------------------------------------------------------------------


class Aws(Provider):
    kind = "logs"
    name = "aws"
    label = "CloudWatch con un perfil de AWS"
    auth = "external"
    fields = (
        Field("profile", "Perfil de AWS", help="el de ~/.aws/config; vacío = el perfil por defecto"),
        Field("region", "Región", help="vacío = la del perfil"),
        Field("price_per_gb", "Precio por GB escaneado (USD)", float, default=0.005),
        Field("log_group_prefixes", "Log groups permitidos (prefijos)", list, default=[],
              help="vacío = todos los que permita el usuario de AWS"),
    )

    def login_hint(self) -> str:
        profile = self.options.get("profile")
        flag = f" --profile {profile}" if profile else ""
        return f"la sesión es la de tu perfil de AWS: `aws sso login{flag}` (o `aws configure{flag}`)"

    def client(self) -> AwsLogs:
        return AwsLogs(self.options, hint=self.login_hint())

    def doctor_checks(self, heal: bool = True):
        from ..doctor import FAIL, OK, Check

        name = self.system.check
        try:
            who = anyio.run(self.client().identity)
        except BackendError as exc:
            return [Check(name, FAIL, str(exc))]
        return [Check(name, OK, f"perfil de AWS válido ({who})")]


class AwsLogs(LogsClient):
    def __init__(self, options: dict[str, Any], client: Any = None, sts: Any = None, hint: str = ""):
        self.options = options
        self.hint = hint
        self._client, self._sts = client, sts

    def _session(self):
        try:
            import boto3
        except ImportError:
            raise BackendError("falta boto3 para CloudWatch con perfil de AWS: pip install 'casebook[aws]'") from None
        return boto3.Session(profile_name=self.options.get("profile") or None,
                             region_name=self.options.get("region") or None)

    def client(self) -> Any:
        if self._client is None:
            self._client = self._session().client("logs")
        return self._client

    async def _call(self, fn, **kwargs) -> Any:
        try:
            return await anyio.to_thread.run_sync(lambda: fn(**kwargs))
        except BackendError:
            raise
        except Exception as exc:  # botocore: credenciales, permisos, parámetros
            text = f"{type(exc).__name__}: {exc}"
            if any(k in text for k in ("NoCredentials", "ExpiredToken", "UnauthorizedSSO", "SSOTokenLoad",
                                       "TokenRetrieval", "InvalidClientTokenId")):
                raise SessionExpired(f"AWS no aceptó la sesión ({text}). {self.hint}") from exc
            raise BackendError(f"CloudWatch respondió {text}") from exc

    async def identity(self) -> str:
        sts = self._sts or self._session().client("sts")
        who = await self._call(sts.get_caller_identity)
        return who.get("Arn", "?")

    def _allowed(self, name: str) -> None:
        prefixes = self.options.get("log_group_prefixes") or []
        if prefixes and not any(name.startswith(p) for p in prefixes):
            raise BackendError(f"el log group {name} no está permitido (cloudwatch.log_group_prefixes)")

    async def log_groups(self, search: str | None) -> list[LogGroup]:
        args: dict[str, Any] = {"limit": 50}
        if search:
            args["logGroupNamePattern"] = search
        out: list[LogGroup] = []
        token = None
        while len(out) < 500:
            page = await self._call(self.client().describe_log_groups, **args, **({"nextToken": token} if token else {}))
            for g in page.get("logGroups", []):
                prefixes = self.options.get("log_group_prefixes") or []
                if not prefixes or any(g["logGroupName"].startswith(p) for p in prefixes):
                    out.append(LogGroup(g["logGroupName"], None, g.get("storedBytes")))
            token = page.get("nextToken")
            if not token:
                break
        return out

    async def estimate(self, log_groups: list[str], start: str, end: str) -> Estimate:
        """CloudWatch no estima consultas: se supone que lo almacenado se reparte parejo en el tiempo que cubre
        (desde la creación del grupo o lo que dure su retención) y se toma la parte de la ventana."""
        window = (_epoch(end) - _epoch(start)) or 1
        now = time.time()
        total = 0.0
        for name in log_groups:
            self._allowed(name)
            page = await self._call(self.client().describe_log_groups, logGroupNamePrefix=name, limit=5)
            group = next((g for g in page.get("logGroups", []) if g["logGroupName"] == name), None)
            if group is None:
                raise BackendError(f"no existe el log group {name} (o tu usuario de AWS no lo ve)")
            stored = group.get("storedBytes") or 0
            span = now - group.get("creationTime", now * 1000) / 1000
            if group.get("retentionInDays"):
                span = min(span, group["retentionInDays"] * 86_400)
            total += stored * min(1.0, window / max(span, 1))
        return Estimate(total, total / 1e9 * float(self.options.get("price_per_gb") or 0.005))

    async def start_query(self, log_groups: list[str], query: str, start: str, end: str) -> str:
        for name in log_groups:
            self._allowed(name)
        started = await self._call(self.client().start_query, logGroupNames=log_groups, queryString=query,
                                   startTime=int(_epoch(start)), endTime=int(_epoch(end)))
        return started["queryId"]

    async def get_query(self, query_id: str) -> QueryState:
        r = await self._call(self.client().get_query_results, queryId=query_id)
        status = (r.get("status") or "Unknown").upper()
        stats = r.get("statistics") or {}
        scanned = stats.get("bytesScanned")
        rows = [{f["field"]: f.get("value") for f in row} for row in r.get("results", [])]
        return QueryState(
            RUNNING if status in ("SCHEDULED", "RUNNING") else status,
            rows,
            stats.get("recordsMatched"),
            stats.get("recordsScanned"),
            scanned,
            None if scanned is None else scanned / 1e9 * float(self.options.get("price_per_gb") or 0.005),
            error=None if status in ("SCHEDULED", "RUNNING", COMPLETE) else f"CloudWatch dejó la consulta en {status}",
        )

    async def cancel(self, query_id: str) -> None:
        await self._call(self.client().stop_query, queryId=query_id)

    async def surrounding(self, query_id: str, ptr: str, events: int) -> list[dict[str, Any]]:
        record = (await self._call(self.client().get_log_record, logRecordPointer=ptr)).get("logRecord", {})
        group = (record.get("@log") or "").split(":", 1)[-1]
        stream = record.get("@logStream")
        if not group or not stream or not record.get("@timestamp"):
            raise BackendError("CloudWatch no devolvió el stream de ese @ptr")
        self._allowed(group)
        at = int(_epoch(record["@timestamp"]) * 1000)
        common = {"logGroupName": group, "logStreamName": stream}
        before = await self._call(self.client().get_log_events, **common, endTime=at, limit=events, startFromHead=False)
        after = await self._call(self.client().get_log_events, **common, startTime=at, limit=events + 1,
                                 startFromHead=True)
        seen, out = set(), []
        for e in [*before.get("events", []), *after.get("events", [])]:
            key = (e.get("timestamp"), e.get("message"))
            if key not in seen:
                seen.add(key)
                out.append({"timestamp": e.get("timestamp"), "message": e.get("message")})
        return out


def _epoch(value: str) -> float:
    """ISO 8601 (con Z) o '2026-10-14 10:00:00.000' (como lo da CloudWatch, en UTC) a segundos."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()
