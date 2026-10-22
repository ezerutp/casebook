"""Tools MCP de MySQL (réplica de solo lectura): db_tables, db_describe y db_select."""

from __future__ import annotations

import re
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from .. import guards, summary, systems, texts
from ..backends.errors import BackendError
from ..backends.mysql import MySQLClient, PlanTooExpensive
from ..config import Config
from ..events import QueryHandle, Recorder
from .common import expected_errors

_TABLE = re.compile(r"^[A-Za-z0-9_$]{1,64}$")


def _plan_text(plan: dict[str, Any]) -> str:
    cols = plan["columns"]
    pick = [c for c in ("id", "table", "type", "key", "rows", "Extra") if c in cols]
    lines = [" | ".join(pick)]
    for row in plan["rows"]:
        values = dict(zip(cols, row))
        lines.append(" | ".join(str(values.get(c)) for c in pick))
    return "\n".join(lines)


def register(mcp: MCPServer, recorder: Recorder, cfg: Config, viewer_url: str | None) -> MySQLClient | None:
    client: MySQLClient | None = None

    def get_client() -> MySQLClient:
        nonlocal client
        if client is None:
            system = systems.mysql(cfg)
            provider = systems.provider(cfg, system)
            if provider is None:
                raise guards.GuardError(systems.missing_provider_message(system))
            client = provider.client()
        return client

    def footer(result: dict[str, Any], q: QueryHandle) -> dict[str, Any]:
        result = {"query_id": q.query_id, **result}  # para citar la consulta en las notas
        if q.knowledge:
            result["knowledge"] = q.knowledge
        if viewer_url:
            result["viewer"] = f"{viewer_url}/#/sessions/{recorder.session_id}"
        return result

    tool = texts.tool(mcp, cfg, f"Tablas: {cfg.db.hint}" if cfg.db.hint else "")

    def environment() -> str:
        # Con este valor se guardan y buscan los esquemas en el conocimiento: cambiarlo los deja de encontrar.
        return f"{cfg.db.environment or cfg.db.host}/{cfg.db.database}"

    async def run(sql: str, kind: str, allow_expensive: bool = False) -> dict[str, Any]:
        db = get_client()
        return await anyio.to_thread.run_sync(db.query, sql, kind, allow_expensive)

    @tool
    @expected_errors(recorder, cfg)
    async def db_tables(pattern: str = "%") -> str:
        """Lista las tablas de la base `${database}` cuyo nombre matchea un patrón LIKE (por ejemplo "user%")."""
        if not re.fullmatch(r"[A-Za-z0-9_$%]{1,64}", pattern):
            raise guards.GuardError("patrón inválido: solo letras, dígitos, _, $ y %")
        sql = f"SHOW TABLES LIKE '{pattern}'"
        recorder.ensure_session()
        with recorder.query("db_tables", {"pattern": pattern, "environment": environment()}, sql) as q:
            data = await run(sql, "show")
            rows = data["rows"]
            q.finish(["table"], rows)
        return summary.render(footer({"tables": [r[0] for r in rows]}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def db_describe(table: str) -> str:
        """Columnas e índices de una tabla. Úsalo si knowledge_search/read no ofrece un esquema verificado y vigente de la tabla: los filtros deberían ir por
        columnas indexadas, porque las consultas con full scan sobre tablas grandes se rechazan."""
        if not _TABLE.match(table):
            raise guards.GuardError("nombre de tabla inválido")
        recorder.ensure_session()
        with recorder.query("db_describe", {"table": table, "environment": environment()}, f"SHOW COLUMNS FROM `{table}`;\nSHOW INDEX FROM `{table}`") as q:
            cols = await run(f"SHOW COLUMNS FROM `{table}`", "show")
            idx = await run(f"SHOW INDEX FROM `{table}`", "show")
            ic = idx["columns"]
            index_rows = [
                [r[ic.index("Key_name")], r[ic.index("Seq_in_index")], r[ic.index("Column_name")], r[ic.index("Non_unique")]]
                for r in idx["rows"]
            ]
            index_table = {"name": "índices", "columns": ["key", "seq", "column", "non_unique"], "rows": index_rows}
            q.finish(cols["columns"], cols["rows"], tables=[index_table])

        cc = cols["columns"]
        columns = {r[cc.index("Field")]: r[cc.index("Type")] for r in cols["rows"]}
        indexes: dict[str, list[str]] = {}
        for key, _, column, _ in index_rows:
            indexes.setdefault(key, []).append(column)
        return summary.render(footer({"table": table, "columns": columns, "indexes": indexes}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def db_select(sql: str, limit: int | None = None) -> str:
        """Consulta de solo lectura contra la réplica de MySQL (base `${database}`). Acepta una sola sentencia SELECT,
        SHOW, DESCRIBE o EXPLAIN. Si el SELECT no trae LIMIT se agrega `limit` (100 por defecto y máximo 5000,
        salvo que el ingeniero los cambie en el visor; si te pasas, el error dice el máximo).
        Antes de correr se hace EXPLAIN: un full scan de una tabla grande o un plan muy caro espera hasta 3 min
        a que el ingeniero lo apruebe en el visor; si no, no se corre. Prefiere reescribirla con un índice (usa
        db_describe). Cada consulta se corta a los 15 s igual."""
        limit = limit or cfg.db.default_limit
        kind, final_sql = guards.check_sql(sql, limit, cfg.db.max_limit)
        recorder.ensure_session()
        with recorder.query("db_select", {"limit": limit, "kind": kind, "environment": environment()}, final_sql) as q:
            try:
                data = await run(final_sql, kind)
            except PlanTooExpensive as exc:
                if exc.plan:
                    q.progress("EXPLAIN:\n" + _plan_text(exc.plan))
                if not await q.await_approval(f"db_select: {exc}", cfg.approval_timeout_s):
                    raise BackendError(f"{exc} No se aprobó en el visor.") from exc
                q.progress(f"aprobada en el visor; corre igual con el corte de {cfg.db.max_execution_ms / 1000:g} s")
                data = await run(final_sql, kind, allow_expensive=True)
            rows = recorder.redactor.value(data["rows"])
            columns = data["columns"]
            tables = []
            if data["plan"]:
                tables.append({"name": "EXPLAIN", **data["plan"]})
            truncated = kind == "select" and len(rows) >= limit
            q.finish(columns, rows, {"truncated": truncated}, tables=tables)

        out = {"sql": final_sql, "row_count": len(rows), **summary.table(columns, rows)}
        if truncated:
            out["note"] = f"se alcanzó el límite de {limit} filas: puede haber más"
        return summary.render(footer(out, q))

    return client
