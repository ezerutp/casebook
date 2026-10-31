"""Tools MCP de MySQL (réplica de solo lectura): db_tables, db_describe y db_select."""

from __future__ import annotations

import re
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from .. import config, guards, summary, systems, texts
from ..backends.errors import BackendError
from ..backends.mysql import MySQLClient, PlanTooExpensive
from ..config import Config
from ..events import QueryHandle, Recorder
from .common import envs_doc, expected_errors, resolve_env

_TABLE = re.compile(r"^[A-Za-z0-9_$]{1,64}$")


def _plan_text(plan: dict[str, Any]) -> str:
    cols = plan["columns"]
    pick = [c for c in ("id", "table", "type", "key", "rows", "Extra") if c in cols]
    lines = [" | ".join(pick)]
    for row in plan["rows"]:
        values = dict(zip(cols, row))
        lines.append(" | ".join(str(values.get(c)) for c in pick))
    return "\n".join(lines)


def _about(cfg: Config) -> dict[str, str]:
    """Qué hay en cada base, para la descripción de `env`."""
    out = {}
    for env, src in cfg.mysql.items():
        parts = [f"base {src.database}" if src.database else "", src.description, f"tablas: {src.hint}" if src.hint else ""]
        out[config.env_name("mysql", env, src)] = "; ".join(p for p in parts if p)
    return out


def register(mcp: MCPServer, recorder: Recorder, cfg: Config, viewer_url: str | None) -> None:
    # Un cliente por base. Si la fuente cambia en config.toml (refresh la reemplaza por otro objeto), se rehace.
    clients: dict[str, tuple[object, MySQLClient]] = {}

    def pick(env: str | None) -> str:
        return resolve_env("MySQL", config.env_names(cfg, "mysql"), env)

    def shown(env: str) -> str:
        return config.env_name("mysql", env, cfg.mysql[env])

    def get_client(env: str) -> MySQLClient:
        src = cfg.mysql[env]
        cached = clients.get(env)
        if cached and cached[0] is src:
            return cached[1]
        if cached:
            cached[1].close()
        system = systems.mysql(cfg, env)
        provider = systems.provider(cfg, system)
        if provider is None:
            raise guards.GuardError(systems.missing_provider_message(system))
        client = provider.client()
        clients[env] = (src, client)
        return client

    def footer(result: dict[str, Any], q: QueryHandle) -> dict[str, Any]:
        result = {"query_id": q.query_id, **result}  # para citar la consulta en las notas
        if q.knowledge:
            result["knowledge"] = q.knowledge
        if viewer_url:
            result["viewer"] = f"{viewer_url}/#/sessions/{recorder.session_id}"
        return result

    only = next(iter(cfg.mysql.values()), None) if len(cfg.mysql) == 1 else None
    extra = envs_doc("la base", _about(cfg)) or (f"Tablas: {only.hint}" if only and only.hint else "")
    tool = texts.tool(mcp, cfg, extra)

    def environment(env: str) -> str:
        # Con este valor se guardan y buscan los esquemas en el conocimiento: cambiarlo los deja de encontrar.
        src = cfg.mysql[env]
        return f"{src.environment or src.host}/{src.database}"

    def params(env: str, **values: Any) -> dict[str, Any]:
        # `env` solo cuando hay varias bases: con una sola, la consulta queda igual que antes.
        return {**values, **({"env": shown(env)} if len(cfg.mysql) > 1 else {}), "environment": environment(env)}

    def meta(env: str, **values: Any) -> dict[str, Any]:
        # El visor muestra meta.env en la tarjeta de la consulta: con varias bases, se ve contra cuál corrió.
        return {**values, **({"env": shown(env)} if len(cfg.mysql) > 1 else {})}

    async def run(env: str, sql: str, kind: str, allow_expensive: bool = False) -> dict[str, Any]:
        db = get_client(env)
        return await anyio.to_thread.run_sync(db.query, sql, kind, allow_expensive)

    @tool
    @expected_errors(recorder, cfg)
    async def db_tables(pattern: str = "%", env: str | None = None) -> str:
        """Lista las tablas de la base ${database} cuyo nombre matchea un patrón LIKE (por ejemplo "user%")."""
        if not re.fullmatch(r"[A-Za-z0-9_$%]{1,64}", pattern):
            raise guards.GuardError("patrón inválido: solo letras, dígitos, _, $ y %")
        env = pick(env)
        sql = f"SHOW TABLES LIKE '{pattern}'"
        recorder.ensure_session()
        with recorder.query("db_tables", params(env, pattern=pattern), sql) as q:
            data = await run(env, sql, "show")
            rows = data["rows"]
            q.finish(["table"], rows, meta(env))
        return summary.render(footer({"tables": [r[0] for r in rows]}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def db_describe(table: str, env: str | None = None) -> str:
        """Columnas e índices de una tabla. Úsalo si knowledge_search/read no ofrece un esquema verificado y vigente de la tabla: los filtros deberían ir por
        columnas indexadas, porque las consultas con full scan sobre tablas grandes se rechazan."""
        if not _TABLE.match(table):
            raise guards.GuardError("nombre de tabla inválido")
        env = pick(env)
        recorder.ensure_session()
        with recorder.query("db_describe", params(env, table=table), f"SHOW COLUMNS FROM `{table}`;\nSHOW INDEX FROM `{table}`") as q:
            cols = await run(env, f"SHOW COLUMNS FROM `{table}`", "show")
            idx = await run(env, f"SHOW INDEX FROM `{table}`", "show")
            ic = idx["columns"]
            index_rows = [
                [r[ic.index("Key_name")], r[ic.index("Seq_in_index")], r[ic.index("Column_name")], r[ic.index("Non_unique")]]
                for r in idx["rows"]
            ]
            index_table = {"name": "índices", "columns": ["key", "seq", "column", "non_unique"], "rows": index_rows}
            q.finish(cols["columns"], cols["rows"], meta(env), tables=[index_table])

        cc = cols["columns"]
        columns = {r[cc.index("Field")]: r[cc.index("Type")] for r in cols["rows"]}
        indexes: dict[str, list[str]] = {}
        for key, _, column, _ in index_rows:
            indexes.setdefault(key, []).append(column)
        return summary.render(footer({"table": table, "columns": columns, "indexes": indexes}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def db_select(sql: str, limit: int | None = None, env: str | None = None) -> str:
        """Consulta de solo lectura contra la réplica de MySQL (base ${database}). Acepta una sola sentencia SELECT,
        SHOW, DESCRIBE o EXPLAIN. Si el SELECT no trae LIMIT se agrega `limit` (100 por defecto y máximo 5000,
        salvo que el ingeniero los cambie en el visor; si te pasas, el error dice el máximo).
        Antes de correr se hace EXPLAIN: un full scan de una tabla grande o un plan muy caro espera hasta 3 min
        a que el ingeniero lo apruebe en el visor; si no, no se corre. Prefiere reescribirla con un índice (usa
        db_describe). Cada consulta se corta a los 15 s igual."""
        env = pick(env)
        limit = limit or cfg.db.default_limit
        kind, final_sql = guards.check_sql(sql, limit, cfg.db.max_limit)
        recorder.ensure_session()
        with recorder.query("db_select", params(env, limit=limit, kind=kind), final_sql) as q:
            try:
                data = await run(env, final_sql, kind)
            except PlanTooExpensive as exc:
                if exc.plan:
                    q.progress("EXPLAIN:\n" + _plan_text(exc.plan))
                if not await q.await_approval(f"db_select: {exc}", cfg.approval_timeout_s):
                    raise BackendError(f"{exc} No se aprobó en el visor.") from exc
                q.progress(f"aprobada en el visor; corre igual con el corte de {cfg.db.max_execution_ms / 1000:g} s")
                data = await run(env, final_sql, kind, allow_expensive=True)
            rows = recorder.redactor.value(data["rows"])
            columns = data["columns"]
            tables = []
            if data["plan"]:
                tables.append({"name": "EXPLAIN", **data["plan"]})
            truncated = kind == "select" and len(rows) >= limit
            q.finish(columns, rows, meta(env, truncated=truncated), tables=tables)

        out = {"sql": final_sql, "row_count": len(rows), **summary.table(columns, rows)}
        if truncated:
            out["note"] = f"se alcanzó el límite de {limit} filas: puede haber más"
        return summary.render(footer(out, q))

