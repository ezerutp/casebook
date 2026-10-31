"""Tools MCP de CloudWatch Logs Insights: cw_log_groups, cw_estimate, cw_query y cw_context."""

from __future__ import annotations

import re
import time
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from .. import guards, summary, systems, texts, timerange
from ..backends.errors import BackendError
from ..backends.opensearch import table_from_records
from ..config import Config
from ..events import QueryHandle, Recorder
from ..providers.base import COMPLETE, RUNNING, LogsClient
from .common import envs_doc, expected_errors, resolve_env

# En la prueba real el escaneo superó al estimado en ~45 %: se compara el tope contra el estimado con margen.
ESTIMATE_MARGIN = 1.5
POLL_SECONDS = 1.5
QUERY_DEADLINE_SECONDS = 600

_LOG_GROUP = re.compile(r"^[A-Za-z0-9_./#-]{1,512}$")
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def _gb(n: Any) -> str:
    return f"{(n or 0) / 1e9:.2f} GB"


def register(mcp: MCPServer, recorder: Recorder, cfg: Config, viewer_url: str | None) -> None:
    def footer(result: dict[str, Any], q: QueryHandle) -> dict[str, Any]:
        result = {"query_id": q.query_id, **result}  # para citar la consulta en las notas
        if viewer_url:
            result["viewer"] = f"{viewer_url}/#/sessions/{recorder.session_id}"
        return result

    about = {env: "; ".join(p for p in (src.description, f"log groups: {src.hint}" if src.hint else "") if p)
             for env, src in cfg.cloudwatch.items()}
    only = next(iter(cfg.cloudwatch.values()), None) if len(cfg.cloudwatch) == 1 else None
    extra = envs_doc("el backend de logs", about) or (f"Log groups: {only.hint}" if only and only.hint else "")
    tool = texts.tool(mcp, cfg, extra)

    def pick(env: str | None) -> str:
        return resolve_env("CloudWatch", list(cfg.cloudwatch), env)

    def with_env(env: str, values: dict[str, Any]) -> dict[str, Any]:
        # `env` solo cuando hay varios: con uno solo, la consulta queda igual que antes.
        return {**values, "env": env} if len(cfg.cloudwatch) > 1 else values

    def logs_client(env: str) -> tuple[LogsClient, str]:
        """El cliente del proveedor de ese CloudWatch y su nombre (para los mensajes)."""
        system = systems.cloudwatch(cfg, env)
        provider = systems.provider(cfg, system)
        if provider is None:
            raise guards.GuardError(systems.missing_provider_message(system))
        return provider.client(), provider.name

    def check_log_group(log_group: str) -> str:
        if not _LOG_GROUP.match(log_group) or "*" in log_group:
            raise guards.GuardError(f"log group inválido: {log_group!r} (un solo nombre exacto, sin comodines)")
        return log_group

    @tool
    @expected_errors(recorder, cfg)
    async def cw_log_groups(search: str | None = None, env: str | None = None) -> str:
        """Lista los log groups que se pueden consultar, con su tamaño almacenado. `search` filtra por nombre."""
        env = pick(env)
        client, _ = logs_client(env)
        recorder.ensure_session()
        with recorder.query("cw_log_groups", with_env(env, {"search": search}), None) as q:
            groups = await client.log_groups(search)
            rows = [[g.name, g.label, g.stored_bytes] for g in groups]
            q.finish(["name", "label", "storedBytes"], rows, with_env(env, {}))
        return summary.render(footer({"log_groups": [r[0] for r in rows]}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def cw_estimate(log_group: str, start: str, end: str = "now", env: str | None = None) -> str:
        """Estima cuántos GB escanearía una consulta y cuánto costaría, sin correrla. `start`/`end` en ISO 8601
        (UTC por defecto) o relativos: "-2h", "-3d", "now"."""
        check_log_group(log_group)
        s, e = timerange.window(start, end, cfg.cw_max_window_days)
        env = pick(env)
        client, _ = logs_client(env)
        recorder.ensure_session()
        params = with_env(env, {"log_group": log_group, "start": timerange.iso(s), "end": timerange.iso(e)})
        with recorder.query("cw_estimate", params, None) as q:
            est = await client.estimate([log_group], timerange.iso(s), timerange.iso(e))
            q.finish(
                ["log_group", "bytes", "cost_usd", "within_limit"],
                [[log_group, est.bytes, est.cost_usd, est.within_limit]],
                with_env(env, {"estimated_cost_usd": est.cost_usd}),
            )
        return summary.render(
            footer(
                {
                    "estimated_gb": round(est.bytes / 1e9, 3),
                    "estimated_cost_usd": round(est.cost_usd, 5),
                    "within_backend_limit": est.within_limit,
                    "backend_max_per_query_usd": est.limit_usd,
                    "note": "el costo real suele superar al estimado (hasta ~50 %)",
                },
                q,
            )
        )

    @tool
    @expected_errors(recorder, cfg)
    async def cw_query(
        log_group: str,
        query: str,
        start: str,
        end: str = "now",
        max_cost_usd: float | None = None,
        env: str | None = None,
    ) -> str:
        """Corre una consulta de Logs Insights sobre UN log group y espera el resultado.
        `query` es la sintaxis de Logs Insights sin SOURCE, por ejemplo:
        "fields @timestamp, @message | filter @message like /gclid=abc/ | sort @timestamp desc | limit 50".
        `start`/`end` en ISO 8601 (UTC) o relativos ("-2h", "now"); ventana máxima 7 días (o la que fije el ingeniero).
        Antes de correr se estima el costo: si el estimado (con margen) supera `max_cost_usd` (por defecto el tope
        que fijó el ingeniero, de fábrica $0.25),
        la consulta espera hasta 3 min a que el ingeniero la apruebe en el visor; si no, no se corre. Avísale
        antes de lanzar algo caro. Cada fila trae @ptr, que sirve para cw_context."""
        check_log_group(log_group)
        query = query.strip()
        if not query:
            raise guards.GuardError("la consulta está vacía")
        if max_cost_usd is None:
            max_cost_usd = cfg.cw_max_cost_usd
        if max_cost_usd <= 0:
            raise guards.GuardError("max_cost_usd tiene que ser mayor a 0")
        s, e = timerange.window(start, end, cfg.cw_max_window_days)
        start_iso, end_iso = timerange.iso(s), timerange.iso(e)
        env = pick(env)
        client, provider_name = logs_client(env)
        recorder.ensure_session()
        params = with_env(env, {"log_group": log_group, "start": start_iso, "end": end_iso,
                                "max_cost_usd": max_cost_usd})

        with recorder.query("cw_query", params, query, cancellable=True) as q:
            est = await client.estimate([log_group], start_iso, end_iso)
            est_cost = est.cost_usd
            q.progress(f"estimado {_gb(est.bytes)} · ${est_cost:.4f}")
            if not est.within_limit:
                raise BackendError(
                    f"el proveedor {provider_name} no permite esta consulta: estimado ${est_cost:.4f} supera su "
                    f"límite de ${est.limit_usd}. Acota la ventana."
                )
            if est_cost * ESTIMATE_MARGIN > max_cost_usd:
                reason = (
                    f"cw_query sobre {log_group}: estimado ${est_cost:.4f} ({_gb(est.bytes)}), con margen "
                    f"${est_cost * ESTIMATE_MARGIN:.4f}, supera el tope de ${max_cost_usd}"
                )
                if not await q.await_approval(reason, cfg.approval_timeout_s):
                    raise BackendError(
                        f"{reason}; no se aprobó en el visor. Acota la ventana o filtra más antes de reintentar."
                    )
                q.progress("aprobada en el visor")

            if q.cancel_requested():
                raise BackendError("consulta cancelada desde el visor antes de empezar")
            external_id = await client.start_query([log_group], query, start_iso, end_iso)

            result = await _poll(client, external_id, q)

            columns, rows = table_from_records(recorder.redactor.value(result.rows))
            meta = {
                "external_id": external_id,
                "log_group": log_group,
                "records_matched": result.records_matched,
                "records_scanned": result.records_scanned,
                "bytes_scanned": result.bytes_scanned,
                "cost_usd": result.cost_usd,
                "estimated_cost_usd": result.estimated_cost_usd if result.estimated_cost_usd is not None else est_cost,
                "backend_duration_ms": result.duration_ms,
            }
            q.finish(columns, rows, with_env(env, meta))

        # @ptr es largo y solo sirve para cw_context: va al final de cada fila.
        if "@ptr" in columns:
            i = columns.index("@ptr")
            columns = columns[:i] + columns[i + 1 :] + ["@ptr"]
            rows = [r[:i] + r[i + 1 :] + [r[i]] for r in rows]
        out = {
            "status": "COMPLETE",
            "external_id": external_id,
            **({"env": env} if len(cfg.cloudwatch) > 1 else {}),  # cw_context lo necesita
            "records_matched": meta["records_matched"],
            "gb_scanned": round((meta["bytes_scanned"] or 0) / 1e9, 3),
            "cost_usd": meta["cost_usd"],
            **summary.table(columns, rows),
        }
        return summary.render(footer(out, q))

    async def _poll(client: LogsClient, external_id: str, q):
        deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
        last_status = None
        while True:
            if q.cancel_requested():
                await client.cancel(external_id)
                raise BackendError("consulta cancelada desde el visor")
            result = await client.get_query(external_id)
            if result.status != RUNNING:
                if result.status != COMPLETE:
                    raise BackendError(f"la consulta terminó con estado {result.status}: {result.error or 'sin detalle'}")
                if result.expired:
                    raise BackendError("los resultados de la consulta expiraron en el backend")
                return result
            if result.status != last_status:
                q.progress(result.status)
                last_status = result.status
            if time.monotonic() > deadline:
                await client.cancel(external_id)
                raise BackendError(f"la consulta superó {QUERY_DEADLINE_SECONDS // 60} min y se canceló; acótala")
            await anyio.sleep(POLL_SECONDS)

    @tool
    @expected_errors(recorder, cfg)
    async def cw_context(external_id: str, ptr: str, events: int = 20, env: str | None = None) -> str:
        """Trae las líneas de log alrededor de un resultado de cw_query (las de antes y después en el mismo
        stream). `external_id` (y `env`, si hay varios) vienen en la respuesta de cw_query y `ptr` es el @ptr de
        la fila."""
        if not _EXTERNAL_ID.match(external_id):
            raise guards.GuardError("external_id inválido")
        if not 1 <= events <= 200:
            raise guards.GuardError("events tiene que estar entre 1 y 200")
        env = pick(env)
        client, _ = logs_client(env)
        recorder.ensure_session()
        params = with_env(env, {"external_id": external_id, "events": events})
        with recorder.query("cw_context", params, f"surrounding-logs de {external_id}") as q:
            records = recorder.redactor.value(await client.surrounding(external_id, ptr, events))
            columns, rows = table_from_records(records)
            q.finish(columns, rows, with_env(env, {}), raw=records)
        return summary.render(footer(summary.table(columns, rows, limit=60), q))

