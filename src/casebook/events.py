"""Registro de lo que hace Claude: sesión actual, notas y el ciclo de vida de cada consulta.

Tipos de evento:
    session.started   {name, card}
    session.resumed   {name, card}   (session_resume reactivó una sesión existente)
    note              {text}
    draft             {card, text}   (borrador de comentario para Jira; no se publica)
    health            {checks: [{name, status, detail}]}   (estado de conexiones al abrir la sesión)
    query.started     {tool, params, query, purpose?}
    query.progress    {message, ...}
    query.finished    {duration_ms, rows, result: {columns, rows}, meta}
    query.failed      {duration_ms, error}
    query.cancel_requested {}   (lo escribe el visor; la tool que hace polling lo revisa)
    query.approval_requested {reason, timeout_s}   (consulta cara esperando decisión en el visor)
    query.approved / query.denied {}               (los escribe el visor)
    query.rejected    {tool, params, reason}   (bloqueada por un guard antes de ejecutarse)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

import anyio

from .redact import Redactor
from .store import Store, now

RAW_MAX_BYTES = 2_000_000

# Para qué es la consulta en curso (lo pasa Claude en cada tool; lo fija el decorador de las tools).
current_purpose: ContextVar[str | None] = ContextVar("current_purpose", default=None)


class QueryHandle:
    def __init__(self, recorder: Recorder, query_id: str):
        self._recorder = recorder
        self.query_id = query_id
        self._t0 = time.monotonic()
        self.done = False
        self.start_event: dict | None = None
        self.knowledge: dict = {}

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def cancel_requested(self) -> bool:
        return self._recorder.store.has_event(self.query_id, "query.cancel_requested")

    async def await_approval(self, reason: str, timeout_s: float, poll_s: float = 0.5) -> bool:
        """Deja la consulta esperando aprobación en el visor. True si se aprueba; False si se rechaza, se cancela
        o vence el plazo."""
        self._recorder._emit("query.approval_requested", {"reason": reason, "timeout_s": timeout_s}, self.query_id)
        _notify("casebook: consulta esperando aprobación", reason)
        deadline = time.monotonic() + timeout_s
        decisions = ("query.approved", "query.denied", "query.cancel_requested")
        poll_s = min(poll_s, max(timeout_s / 20, 0.01))
        while True:
            decision = self._recorder.store.first_event_of(self.query_id, decisions)
            if decision:
                return decision == "query.approved"
            if time.monotonic() >= deadline:
                break
            await anyio.sleep(poll_s)
        self._recorder._emit("query.progress", {"message": f"sin respuesta en {timeout_s:.0f} s: no se ejecuta"}, self.query_id)
        return False

    def progress(self, message: str, **extra: Any) -> None:
        self._recorder._emit("query.progress", {"message": message, **extra}, self.query_id)

    def finish(
        self,
        columns: list[str],
        rows: list[list[Any]],
        meta: dict[str, Any] | None = None,
        tables: list[dict[str, Any]] | None = None,
        raw: Any = None,
    ) -> None:
        """`tables` son tablas extra con nombre (por ejemplo una por agregación); `raw` es la respuesta ya
        redactada, para la pestaña JSON del visor."""
        result: dict[str, Any] = {"columns": columns, "rows": rows}
        if tables:
            result["tables"] = tables
        if raw is not None and len(json.dumps(raw, default=str)) <= RAW_MAX_BYTES:
            result["raw"] = raw
        payload = {"duration_ms": self.elapsed_ms(), "rows": len(rows), "result": result, "meta": meta or {}}
        if self.start_event:
            info = self._recorder.store.knowledge.observe(self.start_event,
                {"type": "query.finished", "ts": now(), "payload": payload})
            if info:
                self.knowledge = info
                payload["knowledge"] = info
        self._recorder._emit("query.finished", payload, self.query_id)
        self.done = True


class NoActiveSession(Exception):
    """Algo se iba a registrar sin sesión activa y el Recorder no abre sesiones solo."""


class Recorder:
    def __init__(self, store: Store):
        self.store = store
        self.session_id: int | None = None
        self.redactor = Redactor()
        self.queries_started = 0
        # El MCP lo apaga: tras una reconexión la sesión activa se pierde y abrir otra sola reparte la evidencia.
        self.implicit_sessions = True

    def start_session(self, name: str | None = None, card: str | None = None) -> dict[str, Any]:
        session = self.store.create_session(name, card)
        self.session_id = session["id"]
        self._emit("session.started", {"name": session["name"], "card": session["card"]})
        return session

    def resume_session(self, session_id: int) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise ValueError(f"no existe la sesión {session_id}")
        self.session_id = session_id
        self._emit("session.resumed", {"name": session["name"], "card": session["card"]})
        return session

    def ensure_session(self) -> int:
        if self.session_id is None:
            if not self.implicit_sessions:
                raise NoActiveSession(self._no_session_hint())
            self.start_session()
        return self.session_id

    def _no_session_hint(self) -> str:
        hint = ("no hay sesión activa: este proceso MCP no tiene ninguna (se reinició o reconectó, o aún no se "
                "abrió). No se registró nada. Si seguías una investigación, reanúdala con session_resume")
        last = next(iter(self.store.list_sessions()), None)
        if last:
            hint += f" (la última con actividad es la {last['id']}, «{last['name']}»)"
        return hint + "; si es un caso nuevo, usa session_start."

    def note(self, text: str) -> dict[str, Any]:
        return self._emit("note", {"text": text})

    def slack(self, kind: str, text: str, **extra: Any) -> dict[str, Any]:
        """Lo que deja la vigilancia de Slack: el mensaje ("message"), el análisis ("analysis") o por qué no se
        analizó ("skipped", "error"). `text` es la versión legible (búsqueda, export y session_resume)."""
        return self._emit("slack", {"kind": kind, "text": text, **extra})

    def emit_health(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._emit("health", {"checks": checks})

    def draft(self, card: str, text: str) -> dict[str, Any]:
        return self._emit("draft", {"card": card, "text": text})

    def reject(self, tool: str, params: dict[str, Any], reason: str) -> dict[str, Any]:
        payload = {"tool": tool, "params": params, "reason": reason}
        if current_purpose.get():
            payload["purpose"] = current_purpose.get()
        return self._emit("query.rejected", payload, uuid.uuid4().hex[:12])

    @contextmanager
    def query(
        self, tool: str, params: dict[str, Any], query: str | None = None, cancellable: bool = False
    ) -> Iterator[QueryHandle]:
        handle = QueryHandle(self, uuid.uuid4().hex[:12])
        self.queries_started += 1
        payload = {"tool": tool, "params": params, "query": query}
        if current_purpose.get():
            payload["purpose"] = current_purpose.get()
        if cancellable:
            payload["cancellable"] = True
        handle.start_event = self._emit("query.started", payload, handle.query_id)
        try:
            yield handle
        except Exception as exc:
            failed = {"duration_ms": handle.elapsed_ms(), "error": f"{type(exc).__name__}: {exc}"}
            info = self.store.knowledge.observe(handle.start_event,
                {"type": "query.failed", "ts": now(), "payload": failed})
            if info:
                failed["knowledge"] = info
            self._emit("query.failed", failed, handle.query_id)
            raise
        if not handle.done:
            self._emit("query.failed", {"duration_ms": handle.elapsed_ms(), "error": "sin resultado"}, handle.query_id)

    def _emit(self, type_: str, payload: dict[str, Any], query_id: str | None = None) -> dict[str, Any]:
        return self.store.add_event(self.ensure_session(), type_, payload, query_id)


def _notify(title: str, body: str) -> None:
    """Notificación de escritorio, si hay notify-send. Nunca falla. CASEBOOK_NOTIFY=0 la desactiva."""
    if os.environ.get("CASEBOOK_NOTIFY", "1") == "0" or not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=casebook", title, body[:300]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass
