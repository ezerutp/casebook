"""Tools MCP de OpenSearch: os_indices, os_mapping, os_count y os_search."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .. import config, guards, summary, systems, texts
from ..backends.opensearch import aggs_tables, hits_table
from ..config import Config
from ..events import QueryHandle, Recorder
from .common import envs_doc as _envs_doc
from .common import expected_errors, resolve_env as _resolve_env


def envs_doc(cfg: Config) -> str:
    """Los entornos para la descripción de cada tool (salen de config.toml). Con uno solo, `env` es opcional y
    solo se dice qué hay adentro."""
    about = {env: "; ".join(x for x in (t.description, t.hint) if x) for env, t in cfg.opensearch.items()}
    if len(about) == 1:
        (env, text), = about.items()
        return f'Entorno "{env}"' + (f": {text}" if text else "") + "."
    return _envs_doc("el entorno", about)


def register(mcp: MCPServer, recorder: Recorder, cfg: Config, viewer_url: str | None) -> None:
    tool = texts.tool(mcp, cfg, envs_doc(cfg))

    def resolve_env(env: str | None) -> str:
        # Como MySQL y CloudWatch: con varios entornos (prod y prod-archive, por ejemplo) no se elige el primero.
        return _resolve_env("OpenSearch", config.env_names(cfg, "opensearch"), env)

    def footer(result: dict[str, Any], q: QueryHandle) -> dict[str, Any]:
        result = {"query_id": q.query_id, **result}  # para citar la consulta en las notas
        if q.knowledge:
            result["knowledge"] = q.knowledge
        if viewer_url:
            result["viewer"] = f"{viewer_url}/#/sessions/{recorder.session_id}"
        return result

    def prepare(env: str, method: str, path: str, body: dict | None) -> tuple[Any, str]:
        system = systems.opensearch(cfg, env)
        provider = systems.provider(cfg, system)
        if provider is None:
            raise guards.GuardError(systems.missing_provider_message(system))
        client = provider.client()
        query_text = client.describe(method, path, body)
        recorder.ensure_session()
        return client, query_text

    async def fetch(client: Any, method: str, path: str, body: dict | None) -> Any:
        return recorder.redactor.value(await client.request(method, path, body))

    @tool
    @expected_errors(recorder, cfg)
    async def os_indices(pattern: str, env: str | None = None) -> str:
        """Lista los índices que matchean un patrón (por ejemplo "logs-*"), con cantidad de documentos y tamaño.
        Qué familias hay en cada entorno lo dice la lista de entornos."""
        env = resolve_env(env)
        pattern = guards.check_index(pattern)
        path = f"_cat/indices/{pattern}?format=json&h=index,health,docs.count,store.size&s=index"
        client, text = prepare(env, "GET", path, None)
        with recorder.query("os_indices", {"pattern": pattern, "env": env}, text) as q:
            data = await fetch(client, "GET", path, None)
            columns = ["index", "health", "docs.count", "store.size"]
            rows = [[i.get(c) for c in columns] for i in data]
            for r in rows:
                r[2] = int(r[2]) if r[2] not in (None, "") else None
            q.finish(columns, rows, {"env": env})
        return summary.render(footer({"indices": len(rows), **summary.table(columns, rows, limit=100)}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def os_mapping(index: str, env: str | None = None) -> str:
        """Campos y tipos de un índice (o del primero que matchee el patrón). Úsalo si knowledge_search/read no
        ofrece un mapping verificado y adecuado del índice concreto. No adivines nombres de campos."""
        env = resolve_env(env)
        index = guards.check_index(index)
        path = f"{index}/_mapping"
        fields: dict[str, str] = {}

        def walk(props: dict, prefix: str = "") -> None:
            for name, spec in props.items():
                full = f"{prefix}{name}"
                if "properties" in spec:
                    walk(spec["properties"], f"{full}.")
                else:
                    fields.setdefault(full, spec.get("type", "object"))

        client, text = prepare(env, "GET", path, None)
        with recorder.query("os_mapping", {"index": index, "env": env}, text) as q:
            data = await fetch(client, "GET", path, None)
            source_index = next(iter(data), None)
            if source_index:
                walk(data[source_index].get("mappings", {}).get("properties", {}))
            rows = [[k, v] for k, v in sorted(fields.items())]
            q.finish(["field", "type"], rows, {"env": env, "from_index": source_index})
        return summary.render(footer({"from_index": source_index, "fields": dict(rows)}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def os_count(index: str, query: dict | None = None, env: str | None = None) -> str:
        """Cuenta documentos (_count) con un query DSL opcional, por ejemplo {"term": {"productId": 123}}."""
        env = resolve_env(env)
        index = guards.check_index(index)
        body = {"query": query} if query else None
        path = f"{index}/_count"
        client, text = prepare(env, "GET", path, body)
        with recorder.query("os_count", {"index": index, "env": env}, text) as q:
            data = await fetch(client, "GET", path, body)
            q.finish(["count"], [[data.get("count")]], {"env": env})
        return summary.render(footer({"count": data.get("count")}, q))

    @tool
    @expected_errors(recorder, cfg)
    async def os_search(
        index: str,
        query: dict | None = None,
        size: int = 10,
        source: list[str] | None = None,
        sort: list | None = None,
        aggs: dict | None = None,
        track_total_hits: bool = True,
        env: str | None = None,
    ) -> str:
        """_search de solo lectura. `query` es query DSL de OpenSearch; `source` limita los campos devueltos
        (recomendado); `aggs` agregaciones (con size=0 si solo interesan los buckets). size máximo 10000 (el
        ingeniero puede bajarlo; si te pasas, el error dice el máximo).
        Devuelve como mucho 25 filas por tabla; el resultado completo queda en el visor."""
        env = resolve_env(env)
        index = guards.check_index(index)
        body: dict[str, Any] = {"size": size, "track_total_hits": track_total_hits}
        if query:
            body["query"] = query
        if source is not None:
            body["_source"] = source
        if sort:
            body["sort"] = sort
        if aggs:
            body["aggs"] = aggs
        body = guards.check_search_body(body, cfg.os_max_size)
        params = {"index": index, "env": env, "size": size}
        path = f"{index}/_search"
        client, text = prepare(env, "GET", path, body)
        with recorder.query("os_search", params, text) as q:
            data = await fetch(client, "GET", path, body)
            columns, rows = hits_table(data)
            tables = aggs_tables(data)
            total = data.get("hits", {}).get("total")
            total = total.get("value") if isinstance(total, dict) else total
            meta = {"env": env, "total_hits": total, "took_ms": data.get("took"), "timed_out": data.get("timed_out")}
            if not rows and tables:
                # Solo agregaciones: la primera es la tabla principal.
                first = tables.pop(0)
                columns, rows = first["columns"], first["rows"]
                meta["main_table"] = first["name"]
            q.finish(columns, rows, meta, tables=tables, raw=data)

        result: dict[str, Any] = {**meta, **summary.table(columns, rows)}
        if tables:
            result["aggs"] = {t["name"]: summary.table(t["columns"], t["rows"]) for t in tables}
        return summary.render(footer(result, q))
