"""Consultas manuales: el ingeniero corre consultas desde el visor y quedan en la sesión como las del agente.

No hay una segunda implementación de las consultas: el visor llama a las mismas tools del MCP (con sus guards,
redacción, estimados, aprobaciones y captura de conocimiento) a través de un Recorder que escribe en la sesión
elegida y marca cada query.started con `origin: "manual"`, `run_mode` ("repeat", "edit" o "new") y, si viene de
otra consulta, `parent_query_id`. Así session_read, el export, la búsqueda y el conocimiento las tratan igual, y
el agente se entera con `engineer_activity` (ver Recorder.engineer_activity).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .events import Recorder
from .store import Store

# Las tools que el ingeniero puede correr desde el visor y cuáles tienen sentido repetir (las de descubrimiento
# no: no son evidencia del caso y el conocimiento ya las guarda).
MANUAL_TOOLS = {"os_search", "os_count", "os_indices", "os_mapping", "db_select", "db_tables", "db_describe",
                "cw_query", "cw_log_groups"}
REPEATABLE = {"os_search", "os_count", "db_select", "cw_query"}
RUN_MODES = {"repeat", "edit", "new"}
# Cuánto se espera a que una consulta arranque (pase los guards) antes de responder al visor.
START_TIMEOUT_S = 60


class ManualError(ValueError):
    """La consulta no se pudo lanzar (un guard la bloqueó, falta la fuente...). El texto va al visor."""


@dataclass
class _Run:
    session_id: int
    extra: dict[str, Any]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    query_id: str | None = None
    error: str | None = None


_current: ContextVar[_Run | None] = ContextVar("manual_run", default=None)


class ManualRecorder(Recorder):
    """Un Recorder cuya sesión y marcas salen de la corrida en curso (un ContextVar), así varias consultas
    manuales en sesiones distintas pueden correr a la vez sobre las mismas tools."""

    gated = False  # el gate de la revisión controla al agente, no al ingeniero

    def __init__(self, store: Store):
        super().__init__(store)
        self.implicit_sessions = False

    @property
    def session_id(self) -> int | None:  # type: ignore[override]
        run = _current.get()
        return run.session_id if run else None

    @session_id.setter
    def session_id(self, value: int | None) -> None:
        pass  # la fija cada corrida

    def query_extra(self) -> dict[str, Any]:
        run = _current.get()
        return dict(run.extra) if run else {}

    def engineer_activity(self, since_agent: bool = False) -> None:
        return None  # es el ingeniero: no hace falta contarle lo suyo

    def reject(self, tool: str, params: dict[str, Any], reason: str) -> dict[str, Any]:
        return {}  # un guard que bloquea va como error al formulario, no como tarjeta en la sesión

    def _emit(self, type_: str, payload: dict[str, Any], query_id: str | None = None) -> dict[str, Any]:
        event = super()._emit(type_, payload, query_id)
        run = _current.get()
        if run is not None and type_ == "query.started" and run.query_id is None:
            run.query_id = query_id
            run.started.set()
        return event


class ManualRunner:
    """Las tools de las fuentes, registradas en un servidor MCP propio (no se expone: solo se llama a call_tool)."""

    def __init__(self, store: Store, cfg: Config):
        from mcp.server.mcpserver import MCPServer

        from .tools import cloudwatch, database, opensearch

        self.store = store
        self.cfg = cfg
        self.recorder = ManualRecorder(store)
        self.mcp = MCPServer("casebook-manual")
        # Todas, aunque una fuente no esté configurada todavía: la tool responde que falta (y si se agrega desde
        # Ajustes, ya funciona sin reiniciar).
        opensearch.register(self.mcp, self.recorder, cfg, None)
        cloudwatch.register(self.mcp, self.recorder, cfg, None)
        database.register(self.mcp, self.recorder, cfg, None)
        self._tasks: set[asyncio.Task] = set()

    async def run(self, session_id: int, tool: str, args: dict[str, Any], purpose: str, *, run_mode: str,
                  parent_query_id: str | None = None, wait: bool = False) -> dict[str, Any]:
        """Lanza la consulta y responde cuando arrancó (o terminó, con `wait`): {query_id, run_id}. Si un guard
        la bloquea antes de arrancar, ManualError con el motivo. La consulta sigue corriendo en segundo plano."""
        if tool not in MANUAL_TOOLS:
            raise ManualError(f"{tool} no se puede correr desde el visor")
        if run_mode not in RUN_MODES:
            raise ManualError(f"run_mode inválido: {run_mode}")
        purpose = (purpose or "").strip()
        if not purpose:
            raise ManualError("escribe para qué es la consulta (es el título de la tarjeta y lo que lee el agente)")
        if self.store.get_session(session_id) is None:
            raise ManualError(f"no existe la sesión {session_id}")
        extra: dict[str, Any] = {"origin": "manual", "run_mode": run_mode, "run_id": uuid.uuid4().hex[:12]}
        if parent_query_id:
            extra["parent_query_id"] = parent_query_id
        run = _Run(session_id, extra)
        arguments = {k: v for k, v in args.items() if k != "purpose"}
        arguments["purpose"] = purpose[:300]

        token = _current.set(run)
        try:
            task = asyncio.create_task(self._call(tool, arguments, run))
        finally:
            _current.reset(token)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        waiter = run.done if wait else run.started
        first = asyncio.create_task(waiter.wait())
        finished = asyncio.create_task(run.done.wait())
        try:
            await asyncio.wait({first, finished}, timeout=START_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
            finished.cancel()
        if run.query_id is None:
            raise ManualError(run.error or f"la consulta no arrancó en {START_TIMEOUT_S} s")
        return {"query_id": run.query_id, "run_id": extra["run_id"]}

    async def _call(self, tool: str, arguments: dict[str, Any], run: _Run) -> None:
        try:
            await self.mcp.call_tool(tool, arguments)
        except Exception as exc:  # noqa: BLE001 - el error ya quedó como query.failed o va al formulario
            run.error = _clean_error(exc)
        finally:
            run.done.set()


def _clean_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    # pydantic: "1 validation error for os_searchArguments\nsize\n  Input should be a valid integer..."
    if "validation error" in text:
        lines = [line.strip() for line in text.splitlines()[1:] if line.strip() and not line.strip().startswith("For further")]
        text = "argumentos inválidos: " + "; ".join(lines[:4])
    return text


# --- repetir ---------------------------------------------------------------------------------------------------


def started_event(store: Store, query_id: str) -> dict[str, Any] | None:
    return next((e for e in store.query_events(query_id) if e["type"] == "query.started"), None)


def args_of(start: dict[str, Any]) -> dict[str, Any]:
    """Los argumentos para volver a correr una consulta: los que guardó query.started o, en eventos anteriores a
    eso, reconstruidos de params y del texto de la consulta."""
    p = start["payload"]
    tool, params, query = p["tool"], p.get("params") or {}, p.get("query") or ""
    if p.get("args") is not None:
        args = dict(p["args"])
    elif tool == "db_select":
        args = {"sql": query, "limit": params.get("limit")}
    elif tool == "cw_query":
        args = {"log_group": params.get("log_group"), "query": query}
    elif tool in ("os_search", "os_count"):
        body = _os_body(query)
        args = {"index": params.get("index")}
        if body.get("query"):
            args["query"] = body["query"]
        if tool == "os_search":
            for key, arg in (("size", "size"), ("_source", "source"), ("sort", "sort"), ("aggs", "aggs"),
                             ("track_total_hits", "track_total_hits")):
                if key in body:
                    args[arg] = body[key]
    else:
        args = {k: v for k, v in params.items() if k not in ("environment",)}
    if tool == "cw_query":
        # La ventana resuelta (ISO), no la relativa ("-2h"): repetir es ver lo mismo que vio la original.
        args.update({k: params[k] for k in ("start", "end", "max_cost_usd") if params.get(k) is not None})
    if params.get("env") is not None:
        args["env"] = params["env"]
    return {k: v for k, v in args.items() if v is not None}


def _os_body(text: str) -> dict[str, Any]:
    """El body de una consulta de OpenSearch a partir de su texto ("GET path\\n{json}")."""
    _, _, body = text.partition("\n")
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def shift_window(args: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """La misma duración de ventana, terminando ahora («¿sigue pasando?»)."""
    from . import timerange

    now = now or datetime.now(UTC)
    start, end = timerange.parse(args["start"], now), timerange.parse(args.get("end", "now"), now)
    return {**args, "start": timerange.iso(now - (end - start)), "end": timerange.iso(now)}


def root_of(start: dict[str, Any]) -> str:
    """La consulta original de una cadena de repeticiones: las ejecuciones se agrupan bajo ella."""
    p = start["payload"]
    if p.get("origin") == "manual" and p.get("run_mode") == "repeat" and p.get("parent_query_id"):
        return p["parent_query_id"]
    return start["query_id"]


# --- lo que se le cuenta al agente -------------------------------------------------------------------------------


def _outcome(store: Store, query_id: str) -> dict[str, Any]:
    for e in reversed(store.query_events(query_id)):
        if e["type"] == "query.finished":
            return {"status": "ok", "rows": e["payload"].get("rows")}
        if e["type"] == "query.failed":
            return {"status": "failed", "error": str(e["payload"].get("error", ""))[:300]}
    return {"status": "running"}


def activity(store: Store, session_id: int, after: int) -> tuple[dict[str, Any] | None, int]:
    """(resumen de la actividad del ingeniero después de `after`, nuevo cursor). Las consultas que siguen
    corriendo se informan como "running" y el cursor queda antes de ellas, para volver a contarlas al terminar."""
    events = store.manual_events(session_id, after)
    if not events:
        return None, after
    queries, pinned, unpinned = [], [], []
    cursor = events[-1]["id"]
    first_running = None
    for e in events:
        p = e["payload"]
        if e["type"] == "query.pinned":
            pinned.append(e["query_id"])
            continue
        if e["type"] == "query.unpinned":
            unpinned.append(e["query_id"])
            continue
        item = {"query_id": e["query_id"], "tool": p.get("tool"), "purpose": p.get("purpose"),
                "env": (p.get("params") or {}).get("env"), "run_mode": p.get("run_mode"),
                **_outcome(store, e["query_id"])}
        if p.get("parent_query_id"):
            item["parent_query_id"] = p["parent_query_id"]
            parent = _outcome(store, p["parent_query_id"])
            if item.get("rows") is not None and parent.get("rows") is not None:
                item["rows_delta_vs_parent"] = item["rows"] - parent["rows"]
        if item["status"] == "running" and first_running is None:
            first_running = e["id"]
        queries.append(item)
    if first_running is not None:
        cursor = first_running - 1
    report: dict[str, Any] = {
        "note": "El ingeniero trabajó en el visor. Sus consultas son evidencia de la sesión: tenlas en cuenta, "
                "cítalas por query_id, léelas con session_read y no las repitas si ya responden la pregunta. "
                "Una consulta fijada es evidencia que el ingeniero considera clave.",
    }
    if queries:
        report["queries"] = queries
    if pinned:
        report["pinned"] = pinned
    if unpinned:
        report["unpinned"] = unpinned
    return report, cursor
