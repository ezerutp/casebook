"""Visor: FastAPI + SSE. Lee los eventos de SQLite, así que sirve tanto en vivo como para sesiones viejas."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from typing import Any

from .. import __version__, config, export, skills
from ..config import Config
from ..store import Store
from ..knowledge import KnowledgeError, Conflict

STATIC = Path(__file__).parent / "static"
POLL_SECONDS = 0.4
KEEPALIVE_SECONDS = 15
# Las acciones que cambian algo (aprobar, cancelar) exigen este header: un formulario o un fetch "simple" desde
# otra página no puede mandarlo sin preflight CORS, y este servidor no responde preflights.
ACTION_HEADER = "x-casebook-viewer"
DECISIONS = ("query.approved", "query.denied", "query.cancel_requested")


def require_viewer(request: Request) -> None:
    if request.headers.get(ACTION_HEADER) != "1":
        raise HTTPException(403, "falta el header del visor")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(403, "origen no permitido")


class Approval(BaseModel):
    approve: bool


class SlackWatchEnable(BaseModel):
    lookback_minutes: int = 0


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class SourceNew(BaseModel):
    kind: str
    env: str | None = None
    provider: str
    values: dict[str, Any] = {}


class KnowledgeDelete(BaseModel):
    expected_revision: int


class KnowledgeEdit(BaseModel):
    expected_revision: int
    title: str
    content: str
    topics: list[str]
    reason: str
    stale: bool = False


def create_app(store: Store, cfg: Config | None = None, served_by: str = "viewer") -> FastAPI:
    """served_by: "viewer" (la terminal de start/viewer) o "mcp" (Claude Code); lo usa `stop` para saber qué detener."""
    cfg = cfg or Config()
    app = FastAPI(title="casebook", docs_url=None, redoc_url=None)
    app.state.clients = 0  # pestañas del visor conectadas por SSE
    app.state.stopping = False  # lo prende ViewerServer al apagar, para que los SSE terminen solos
    app.state.store, app.state.cfg = store, cfg  # para el vigilante de Slack que arranca ViewerServer
    app.state.watcher = None  # el SlackWatcher, mientras un ViewerServer sirve esta app

    @app.middleware("http")
    async def no_stale_assets(request: Request, call_next):
        # El visor se actualiza junto con el paquete: que el navegador revalide siempre HTML, JS y CSS.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["cache-control"] = "no-cache"
        return response
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state() -> dict:
        return {"latest_event_id": store.latest_event_id(), "clients": app.state.clients,
                "served_by": served_by, "pid": os.getpid()}

    @app.get("/api/meta")
    def meta() -> dict:
        config.refresh(cfg)
        from .. import systems

        return {
            "version": __version__,
            "jira_base_url": cfg.jira_base_url,
            # Textos del pack: el prefijo de las cards (filtro de sesiones) y un ejemplo para las ayudas.
            "pack": cfg.pack.display_name if cfg.pack else None,
            "card_prefix": cfg.card_prefix,
            "card_example": cfg.card_examples[0],
            # Los sistemas de config.toml, para el panel de conexiones y Ajustes → Credenciales.
            "systems": [
                {"id": s.id, "check": s.check, "label": s.label, "short": s.short,
                 "cmd": f"casebook login {s.id}"}
                for s in systems.all_systems(cfg)
            ],
        }

    @app.get("/api/health")
    def health() -> dict | None:
        """El estado de conexiones más nuevo: el del último session_start o el del último chequeo del visor."""
        last = store.latest_of_type("health")
        stored = {"ts": last["ts"], "checks": last["payload"]["checks"]} if last else None
        checked = store.viewer_health()
        if checked and (not stored or checked["ts"] >= stored["ts"]):
            return checked
        return stored

    @app.post("/api/health", dependencies=[Depends(require_viewer)])
    def recheck() -> dict:
        """Corre doctor sin reparar túneles (eso queda para session_start y el CLI). Va a un thread de FastAPI."""
        from .. import doctor
        from ..store import now

        checked = {"ts": now(), "checks": [vars(c) for c in doctor.run(cfg, heal=False)]}
        store.save_viewer_health(checked)
        return checked

    def settings_payload() -> dict:
        """Lo que muestra Ajustes. `value` es lo que dice el archivo; `running` es lo que usa este proceso (difieren
        en host y puerto hasta reiniciar). Los valores salen de cfg recién recargado."""
        config.refresh(cfg)
        error = None
        try:
            on_disk = config.load(cfg.source, cfg.pack_arg) if cfg.source else cfg
        except (OSError, ValueError, KeyError, TypeError) as exc:  # archivo editado a mano con un error
            on_disk, error = cfg, f"config.toml no se pudo leer: {exc}"
        defaults = config.base(cfg.pack_arg)
        return {
            "path": str(cfg.source) if cfg.source else None,
            "error": error,
            "exists": bool(cfg.source and cfg.source.exists()),
            "settings": [
                {
                    "key": s.key,
                    "type": s.type.__name__,
                    "value": s.get(on_disk),
                    "running": s.get(cfg),
                    "default": s.get(defaults),
                    "min": s.min,
                    "max": s.max,
                    "restart": s.restart,
                    "choices": list(s.choices) if s.choices else None,
                    "optional": s.optional,
                }
                for s in config.SETTINGS
            ],
            "readonly": {
                "db_path": str(cfg.db_path),
                "mysql": [
                    {"env": env, "ssh_host": src.ssh_host, "host": src.host, "database": src.database}
                    for env, src in cfg.mysql.items()
                ],
                "cloudwatch": [
                    {"env": env, "provider": src.provider, "profile": src.options.get("profile", "")}
                    for env, src in cfg.cloudwatch.items()
                ],
                "opensearch": [
                    {"env": env, "domain": e.domain or e.options.get("url", ""), "tunnel_port": e.tunnel_port or "",
                     "ssh_host": e.ssh_host, "provider": e.provider}
                    for env, e in cfg.opensearch.items()
                ],
            },
        }

    # --- fuentes (Ajustes → Fuentes) -------------------------------------------------------------------------

    def sources_payload() -> dict:
        from .. import sources

        config.refresh(cfg)
        return {
            "sources": sources.listing(cfg),
            "catalog": sources.catalog(),
            "pack": {"name": cfg.pack.display_name, "version": cfg.pack.version} if cfg.pack else None,
            "writable": cfg.source is not None,
        }

    @app.get("/api/sources")
    def list_sources() -> dict:
        return sources_payload()

    @app.post("/api/sources", dependencies=[Depends(require_viewer)])
    def add_source(body: SourceNew) -> dict:
        from .. import sources

        if cfg.source is None:
            raise HTTPException(409, "este visor no tiene un config.toml asociado")
        try:
            system = sources.add(body.kind, body.env, body.provider, body.values, cfg.source, cfg.pack_arg)
        except config.SettingError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**sources_payload(), "added": system.id}

    @app.delete("/api/sources/{system_id}", dependencies=[Depends(require_viewer)])
    def remove_source(system_id: str) -> dict:
        from .. import sources

        config.refresh(cfg)
        try:
            result = sources.remove(cfg, system_id)
        except KeyError:
            raise HTTPException(404, f"no hay una fuente {system_id}") from None
        except config.SettingError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**sources_payload(), "result": result}

    @app.post("/api/sources/{system_id}/test", dependencies=[Depends(require_viewer)])
    def test_source(system_id: str) -> dict:
        """Los chequeos del doctor para esa fuente, sin reparar túneles. Va a un thread de FastAPI."""
        from .. import sources

        config.refresh(cfg)
        try:
            checks = sources.test(cfg, system_id)
        except KeyError:
            raise HTTPException(404, f"no hay una fuente {system_id}") from None
        return {"checks": [vars(c) for c in checks]}

    @app.get("/api/slack")
    def slack_status() -> dict:
        from .. import slack_watch

        config.refresh(cfg)
        return slack_watch.status(cfg)

    @app.post("/api/slack/analyze-latest", dependencies=[Depends(require_viewer)], status_code=202)
    def slack_analyze_latest() -> dict:
        """Análisis manual del último pedido del canal (funciona con la vigilancia apagada). No repite uno que ya
        está analizado o en curso: en ese caso el estado apunta a esa sesión."""
        watcher = getattr(app.state, "watcher", None)
        if watcher is None:
            raise HTTPException(503, "el vigilante de Slack no corre en este proceso; reinicia el visor")
        if not watcher.analyze_latest():
            raise HTTPException(409, "ya estoy buscando el último mensaje")
        return {"started": True}

    @app.get("/api/slack/thread/{session_id}")
    def slack_thread(session_id: int) -> dict:
        """Si la sesión viene de un hilo de Slack y cómo terminó su última «Revisar respuestas del hilo»."""
        from .. import slack_watch

        info = slack_watch.thread_info(store, session_id)
        check = slack_watch.read_state().get("thread_checks", {}).get(str(session_id))
        return {"has_thread": info is not None, "check": check}

    @app.post("/api/slack/thread/{session_id}/check", dependencies=[Depends(require_viewer)], status_code=202)
    def slack_thread_check(session_id: int, force: bool = False) -> dict:
        """Lee las respuestas nuevas del hilo de la sesión y, si hay, las analiza en la misma sesión. force: aunque
        solo sean agradecimientos o confirmaciones."""
        from .. import slack_watch

        watcher = getattr(app.state, "watcher", None)
        if watcher is None:
            raise HTTPException(503, "el vigilante de Slack no corre en este proceso; reinicia el visor")
        if slack_watch.thread_info(store, session_id) is None:
            raise HTTPException(404, "esta sesión no viene de un mensaje de Slack")
        if not watcher.check_thread(session_id, force=force):
            raise HTTPException(409, "ya estoy revisando el hilo de esta sesión")
        return {"started": True}

    @app.post("/api/slack/poll-now", dependencies=[Depends(require_viewer)], status_code=202)
    def slack_poll_now() -> dict:
        """«Reintentar» del panel: lee el canal ya, sin esperar a la próxima notificación."""
        watcher = getattr(app.state, "watcher", None)
        if watcher is None:
            raise HTTPException(503, "el vigilante de Slack no corre en este proceso; reinicia el visor")
        if not watcher.request_poll():
            raise HTTPException(409, "la vigilancia está apagada")
        return {"started": True}

    @app.post("/api/slack/watch/enable", dependencies=[Depends(require_viewer)])
    def slack_watch_enable(body: SlackWatchEnable | None = None) -> dict:
        """Activa la vigilancia de Slack fijando la ventana de inicio (lookback_minutes)."""
        watcher = getattr(app.state, "watcher", None)
        if watcher is None:
            raise HTTPException(503, "el vigilante de Slack no corre en este proceso; reinicia el visor")
        minutes = max(0, min(1440, body.lookback_minutes if body else 0))
        watcher.enable_watch(lookback_minutes=minutes)
        return settings_payload()

    @app.post("/api/slack/watch/disable", dependencies=[Depends(require_viewer)])
    def slack_watch_disable() -> dict:
        """Apaga la vigilancia de Slack."""
        watcher = getattr(app.state, "watcher", None)
        if watcher is None:
            raise HTTPException(503, "el vigilante de Slack no corre en este proceso; reinicia el visor")
        watcher.disable_watch()
        return settings_payload()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings_payload()

    @app.put("/api/settings", dependencies=[Depends(require_viewer)])
    def put_settings(body: SettingsUpdate) -> dict:
        if cfg.source is None:
            raise HTTPException(409, "este visor no tiene un config.toml asociado")
        values = dict(body.values)
        watcher = getattr(app.state, "watcher", None)
        if watcher is not None and isinstance(values.get("slack.watch"), bool):
            # El switch de Ajustes pasa por el vigilante, igual que el del panel: así fija desde dónde vigilar
            # (con la ventana por defecto) y no retoma un cursor viejo.
            if values.pop("slack.watch"):
                watcher.enable_watch()
            else:
                watcher.disable_watch()
        try:
            config.save(values, cfg.source, cfg.pack_arg)
        except config.SettingError as exc:
            raise HTTPException(422, str(exc)) from exc
        return settings_payload()

    def skill_call(fn, name: str) -> dict:
        try:
            return fn(name)
        except skills.SkillError as exc:
            raise HTTPException(404 if str(exc).startswith("no hay") else 409, str(exc)) from exc

    @app.get("/api/skills")
    def list_skills() -> dict:
        from ..server import instructions  # import tardío: server importa este módulo

        return {**skills.listing(), "server_instructions": instructions(cfg)}

    @app.get("/api/skills/{name}")
    def skill_detail(name: str) -> dict:
        return skill_call(skills.detail, name)

    @app.post("/api/skills/{name}/install", dependencies=[Depends(require_viewer)])
    def skill_install(name: str) -> dict:
        return skill_call(skills.install, name)

    @app.post("/api/skills/{name}/uninstall", dependencies=[Depends(require_viewer)])
    def skill_uninstall(name: str) -> dict:
        return skill_call(skills.uninstall, name)

    def knowledge_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except KnowledgeError as exc:
            raise HTTPException(404 if str(exc) == "entrada no encontrada" else 422, str(exc)) from exc
        except (sqlite3.Error, OSError) as exc:
            raise HTTPException(503, "Conocimiento local no disponible; las sesiones siguen disponibles") from exc

    @app.get("/api/knowledge")
    def knowledge_list(q: str = "", source: str | None = None, environment: str | None = None,
                       topic: str | None = None, status: str | None = None, limit: int = 20, offset: int = 0):
        return knowledge_call(store.knowledge.db.search, q, source, environment, topic, status, limit, offset)

    @app.post("/api/knowledge/import", dependencies=[Depends(require_viewer)])
    def knowledge_import():
        return knowledge_call(store.knowledge.import_sessions)

    @app.get("/api/knowledge/{entry_id}")
    def knowledge_read(entry_id: int, history_offset: int = 0, revision: int | None = None):
        return knowledge_call(store.knowledge.db.read, entry_id, history_offset, revision)

    @app.delete("/api/knowledge/{entry_id}", dependencies=[Depends(require_viewer)])
    def knowledge_delete(entry_id: int, body: KnowledgeDelete):
        return knowledge_call(store.knowledge.db.delete, entry_id, body.expected_revision)

    @app.put("/api/knowledge/{entry_id}", dependencies=[Depends(require_viewer)])
    def knowledge_edit(entry_id: int, body: KnowledgeEdit):
        return knowledge_call(store.knowledge.db.edit, entry_id, **body.model_dump())

    @app.get("/api/sessions")
    def sessions() -> list[dict]:
        return store.list_sessions()

    @app.get("/api/sessions/{session_id}/events")
    def session_events(session_id: int, after: int = 0) -> list[dict]:
        if not store.get_session(session_id):
            raise HTTPException(404, "sesión no encontrada")
        return store.events(session_id, after=after, limit=5000)

    @app.get("/api/sessions/{session_id}/export.md")
    def session_export(session_id: int, rows: int = 20, download: bool = False) -> PlainTextResponse:
        session = store.get_session(session_id)
        if not session:
            raise HTTPException(404, "sesión no encontrada")
        headers = {}
        if download:
            name = f"{export.slug(session['card'] or session['name'])}-{session_id}.md"
            headers["content-disposition"] = f'attachment; filename="{name}"'
        return PlainTextResponse(
            export.render(store, session_id, max_rows=max(1, min(rows, 500))),
            media_type="text/markdown; charset=utf-8",
            headers=headers,
        )

    @app.get("/api/search")
    def search(q: str) -> list[dict]:
        text = q.strip()
        if len(text) < 2:
            return []
        return [
            {
                "event_id": e["id"],
                "session_id": e["session_id"],
                "session": e["name"],
                "card": e["card"],
                "query_id": e["query_id"],
                "type": e["type"],
                "ts": e["ts"],
                "snippet": _snippet(e["payload"], text),
            }
            for e in store.search(text)
        ]

    @app.post("/api/queries/{query_id}/approval", status_code=202, dependencies=[Depends(require_viewer)])
    def decide(query_id: str, body: Approval) -> dict:
        session_id = store.query_session(query_id)
        if session_id is None or not store.has_event(query_id, "query.approval_requested"):
            raise HTTPException(404, "no hay una aprobación pendiente para esa consulta")
        if store.first_event_of(query_id, DECISIONS + ("query.finished", "query.failed")):
            raise HTTPException(409, "la consulta ya se decidió o terminó")
        store.add_event(session_id, "query.approved" if body.approve else "query.denied", {}, query_id)
        return {"ok": True}

    @app.post("/api/queries/{query_id}/cancel", status_code=202, dependencies=[Depends(require_viewer)])
    def cancel_query(query_id: str) -> dict:
        session_id = store.query_session(query_id)
        if session_id is None:
            raise HTTPException(404, "consulta no encontrada")
        if store.has_event(query_id, "query.finished") or store.has_event(query_id, "query.failed"):
            raise HTTPException(409, "la consulta ya terminó")
        if not store.has_event(query_id, "query.cancel_requested"):
            store.add_event(session_id, "query.cancel_requested", {}, query_id)
        return {"ok": True}

    @app.get("/api/stream")
    async def stream(request: Request, after: int | None = None) -> StreamingResponse:
        last_id = request.headers.get("last-event-id")
        cursor = int(last_id) if last_id else (after if after is not None else store.latest_event_id())

        async def gen():
            app.state.clients += 1
            try:
                async for chunk in _events():
                    yield chunk
            finally:
                app.state.clients -= 1

        async def _events():
            nonlocal cursor
            idle = 0.0
            yield "retry: 2000\n\n"
            while not app.state.stopping and not await request.is_disconnected():
                batch = store.events(after=cursor, limit=200)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {event['id']}\ndata: {json.dumps(event, default=str)}\n\n"
                if batch:
                    idle = 0.0
                    continue
                await asyncio.sleep(POLL_SECONDS)
                idle += POLL_SECONDS
                if idle >= KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"}
        )

    return app


class ViewerServer(uvicorn.Server):
    """uvicorn.Server que avisa al visor antes de apagarse y corre el vigilante de Slack mientras sirve.

    Los SSE de las pestañas abiertas no terminan nunca por su cuenta: sin el aviso, uvicorn los cancela al vencer
    timeout_graceful_shutdown y deja un traceback por cada pestaña. El vigilante va acá porque solo un proceso
    sirve el puerto: así nunca hay dos revisando el canal.
    """

    watcher = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets)
        if self.started:
            # La vigilancia de Slack es opcional: si falla al arrancar, el visor sigue funcionando sin ella.
            try:
                from ..slack_watch import SlackWatcher

                state = self.config.app.state
                self.watcher = state.watcher = SlackWatcher(state.store, state.cfg)
                if state.cfg.slack_watch:  # el programa se apagó sin avisar (kill, corte): no se retoma sola
                    self.watcher.disable_watch()
                self.watcher.start()
            except Exception:
                logging.getLogger("casebook").exception("no pude arrancar la vigilancia de Slack")
                self.watcher = None

    async def serve(self, sockets=None) -> None:
        # ViewerKeeper reutiliza la app entre servidores: cada uno arranca con el aviso apagado.
        self.config.app.state.stopping = False
        await super().serve(sockets)

    async def shutdown(self, sockets=None) -> None:
        self.config.app.state.stopping = True
        if self.watcher is not None:
            self.watcher.disable_watch()  # con el programa apagado no hay vigilancia: se enciende de nuevo a mano
            self.watcher.stop()
            self.config.app.state.watcher = None
        await super().shutdown(sockets)


def _snippet(payload: dict, text: str, width: int = 80) -> str:
    """Un pedazo legible alrededor de la coincidencia."""
    flat = " ".join(
        str(v) for v in (payload.get("text"), payload.get("query"), payload.get("error"), payload.get("reason")) if v
    ) or json.dumps(payload.get("result", payload), ensure_ascii=False, default=str)
    flat = " ".join(flat.split())
    i = flat.lower().find(text.lower())
    if i < 0:
        return flat[: width * 2]
    start = max(0, i - width)
    return ("…" if start else "") + flat[start : i + len(text) + width] + ("…" if i + len(text) + width < len(flat) else "")
