import json
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from casebook import credentials
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def test_recorder_query_lifecycle(store):
    rec = Recorder(store)
    rec.start_session("SUP-1", card="SUP-1")
    rec.note("hola")
    with rec.query("os_search", {"index": "leads-*"}, "GET leads-*/_search") as q:
        q.progress("corriendo")
        q.finish(["a", "b"], [[1, "x"], [2, "y"]], {"cost_usd": 0.01})

    types = [e["type"] for e in store.events(rec.session_id)]
    assert types == ["session.started", "note", "query.started", "query.progress", "query.finished"]
    finished = store.events(rec.session_id)[-1]
    assert finished["payload"]["rows"] == 2
    assert finished["payload"]["result"]["columns"] == ["a", "b"]


def test_recorder_query_failure_is_recorded_and_reraised(store):
    rec = Recorder(store)
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {}, "SELECT 1"):
            raise RuntimeError("túnel caído")
    last = store.events(rec.session_id)[-1]
    assert last["type"] == "query.failed"
    assert "túnel caído" in last["payload"]["error"]


def test_query_without_finish_is_marked_failed(store):
    rec = Recorder(store)
    with rec.query("cw_query", {}, "fields @message"):
        pass
    assert store.events(rec.session_id)[-1]["type"] == "query.failed"


def test_events_without_session_create_one(store):
    rec = Recorder(store)
    rec.note("sin sesión")
    sessions = store.list_sessions()
    assert len(sessions) == 1 and sessions[0]["name"] == f"Sin identificar · {sessions[0]['id']}"


def test_viewer_api(store):
    rec = Recorder(store)
    rec.start_session("SUP-2")
    rec.note("nota")
    client = TestClient(create_app(store))

    assert client.get("/api/state").json()["latest_event_id"] == 2
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["name"] == "SUP-2" and sessions[0]["events"] == 2
    events = client.get(f"/api/sessions/{rec.session_id}/events").json()
    assert [e["type"] for e in events] == ["session.started", "note"]
    assert client.get("/api/sessions/999/events").status_code == 404
    assert "casebook" in client.get("/").text
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_viewer_stream_sends_new_events(store):
    rec = Recorder(store)
    rec.start_session("SUP-3")
    server = uvicorn.Server(uvicorn.Config(create_app(store), host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/stream?after=1", timeout=5) as resp:
            rec.note("en vivo")  # escrito después de conectar: tiene que llegar por SSE
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    break
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert event["type"] == "note" and event["payload"]["text"] == "en vivo"
    assert not thread.is_alive()


def test_mcp_tools(store):
    rec = Recorder(store)
    mcp = build(rec, "http://127.0.0.1:8765")

    async def go():
        tools = {t.name for t in await mcp.list_tools()}
        assert {"session_start", "session_note"} <= tools
        await mcp.call_tool("session_start", {"name": "SUP-4", "card": "SUP-4"})
        await mcp.call_tool("session_note", {"text": "conclusión"})

    anyio.run(go)
    session = store.list_sessions()[0]
    assert session["name"] == "SUP-4"
    assert [e["type"] for e in store.events(session["id"])] == ["session.started", "health", "note"]


def test_normalize_cookie():
    assert credentials.normalize_cookie("cookie: a=1; b=2\n") == "a=1; b=2"
    assert credentials.normalize_cookie("a=1") == "a=1"
    assert credentials.cookie_names("a=1; security_authentication=xyz") == ["a", "security_authentication"]


def test_merge_cookie_keeps_unrotated_and_replaces_rotated():
    merged = credentials.merge_cookie("A=1; B=2; STATE=s", {"A": "9", "C": "3"})
    assert credentials.parse_cookie(merged) == {"A": "9", "B": "2", "STATE": "s", "C": "3"}


def test_search_endpoint(store):
    rec = Recorder(store)
    rec.start_session("SUP-7", card="SUP-7")
    rec.note("El gclid Cj0KCQ venció antes del upload")
    with rec.query("db_select", {"sql": "x"}, "SELECT * FROM `lead` WHERE email = 'a@b.com'") as q:
        q.finish(["lead_id"], [[123]])
    client = TestClient(create_app(store))
    hits = client.get("/api/search", params={"q": "venció"}).json()
    assert len(hits) == 1 and hits[0]["type"] == "note" and hits[0]["session"] == "SUP-7"
    assert "venció" in hits[0]["snippet"]
    hits = client.get("/api/search", params={"q": "a@b.com"}).json()
    assert hits[0]["type"] == "query.started" and hits[0]["query_id"]
    assert client.get("/api/search", params={"q": "x"}).json() == []


def test_purpose_is_required_and_recorded(store):
    from mcp.server.mcpserver.exceptions import ToolError

    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, None)

    async def go():
        schema = {t.name: t.input_schema for t in await mcp.list_tools()}
        assert "purpose" in schema["db_select"]["required"] and "purpose" not in schema["session_note"]["properties"]
        try:
            await mcp.call_tool("db_select", {"sql": "SELECT 1"})
        except Exception as exc:  # falta purpose: el SDK lo rechaza al validar
            assert "purpose" in str(exc)
        else:
            raise AssertionError("debería exigir purpose")
        try:
            await mcp.call_tool("db_select", {"sql": "DELETE FROM t", "purpose": "Probar el guard"})
        except ToolError:
            pass

    anyio.run(go)
    rejected = store.events(rec.session_id)[-1]
    assert rejected["type"] == "query.rejected"
    assert rejected["payload"]["purpose"] == "Probar el guard" and "purpose" not in rejected["payload"]["params"]


@pytest.mark.parametrize(("clients", "opens"), [(0, True), (1, False), (None, False)])
def test_session_start_opens_viewer_once_when_no_tab(store, monkeypatch, clients, opens):
    from casebook import server

    opened = []

    async def fake_clients(url):
        return clients

    monkeypatch.setattr(server, "_viewer_clients", fake_clients)
    monkeypatch.setattr(server, "_open_browser", lambda url: opened.append(url) or True)
    mcp = build(Recorder(store), "http://127.0.0.1:8765")

    async def go():
        first = (await mcp.call_tool("session_start", {"name": "SUP-1"})).content[0].text
        second = (await mcp.call_tool("session_start", {"name": "SUP-2"})).content[0].text
        return first, second

    first, second = anyio.run(go)
    assert "http://127.0.0.1:8765/#/sessions/" in first and "Dile al usuario" in first
    assert opened == (["http://127.0.0.1:8765/#/sessions/1"] if opens else [])
    assert ("Se abrió en su navegador" in first) == opens
    assert "Se abrió" not in second  # solo la primera vez por proceso


def test_session_resume_opens_viewer_when_no_tab(store, monkeypatch):
    """Retomar una investigación en un proceso nuevo también abre el visor en esa sesión."""
    from casebook import server

    opened = []

    async def fake_clients(url):
        return 0

    monkeypatch.setattr(server, "_viewer_clients", fake_clients)
    monkeypatch.setattr(server, "_open_browser", lambda url: opened.append(url) or True)
    session = store.create_session("SUP-1", "SUP-1")
    mcp = build(Recorder(store), "http://127.0.0.1:8765")

    async def go():
        return (await mcp.call_tool("session_resume", {"session_id": session["id"]})).content[0].text

    result = json.loads(anyio.run(go))
    assert opened == [f"http://127.0.0.1:8765/#/sessions/{session['id']}"]
    assert "Se abrió en su navegador" in result["viewer"]


def test_mcp_without_active_session_records_nothing_and_points_to_resume(store):
    """Tras reconectar el MCP no hay sesión activa: nada abre una «Sin identificar» sola."""
    from mcp.server.mcpserver.exceptions import ToolError

    previous = store.create_session("SUP-13536", "SUP-13536")
    store.add_event(previous["id"], "note", {"text": "Pendiente: verificar el UPDATE"})
    rec = Recorder(store)
    mcp = build(rec, None)

    async def go(tool, args):
        with pytest.raises(ToolError) as exc:
            await mcp.call_tool(tool, args)
        return str(exc.value)

    for tool, args in [
        ("db_select", {"sql": "SELECT 1", "purpose": "Verificar"}),
        ("db_select", {"sql": "DELETE FROM t", "purpose": "Probar el guard"}),
        ("session_note", {"text": "Hallazgo"}),
        ("jira_comment_draft", {"card": "SUP-13536", "text": "Borrador"}),
        ("session_update", {"card": "SUP-13536"}),
        ("session_export", {}),
    ]:
        message = anyio.run(go, tool, args)
        if args.get("sql") != "DELETE FROM t":
            assert "session_resume" in message and f"la {previous['id']}, «SUP-13536»" in message
    assert [s["id"] for s in store.list_sessions()] == [previous["id"]]
    assert len(store.events(previous["id"])) == 1 and rec.session_id is None


def test_state_counts_connected_viewer_tabs(store):
    rec = Recorder(store)
    rec.start_session("SUP-3")
    app = create_app(store)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    try:
        assert httpx.get(f"{base}/api/state").json()["clients"] == 0
        with httpx.stream("GET", f"{base}/api/stream", timeout=5) as resp:
            lines = resp.iter_lines()  # hay que conservar el iterador: si se descarta, cierra la conexión
            next(lines)
            assert httpx.get(f"{base}/api/state").json()["clients"] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_health_summary_and_event(store, monkeypatch):
    from casebook import doctor, server

    checks = [
        doctor.Check("opensearch:prod", doctor.WARN, "estaba caído; se levantó de nuevo (puerto 9123)"),
        doctor.Check("opensearch:prod", doctor.OK, "sesión vigente"),
        doctor.Check("cloudwatch (aws)", doctor.FAIL, "sesión vencida. Corre `casebook login cw`"),
        doctor.Check("mysql (reader)", doctor.OK, "conectado al reader"),
    ]

    async def fake_health(cfg):
        return checks

    monkeypatch.setattr(server, "_health", fake_health)
    rec = Recorder(store)
    mcp = build(rec, None)
    text = anyio.run(lambda: mcp.call_tool("session_start", {"name": "SUP-9"})).content[0].text
    assert "opensearch:prod, mysql (reader) OK" in text
    assert "Fallan: cloudwatch (aws): sesión vencida. Corre `casebook login cw`" in text
    health = [e for e in store.events(rec.session_id) if e["type"] == "health"][0]
    assert [c["status"] for c in health["payload"]["checks"]] == ["warn", "ok", "fail", "ok"]


def test_health_runs_checks_in_parallel_with_timeout(monkeypatch):
    import time as _time

    from casebook import doctor, server
    from casebook.config import Config

    monkeypatch.setattr(server, "HEALTH_TIMEOUT_S", 0.5)
    monkeypatch.setattr(
        doctor,
        "checks",
        lambda cfg, heal=True: [
            lambda: [doctor.Check("mysql (reader)", doctor.OK, "ok")],
            lambda: _time.sleep(2) or [doctor.Check("cloudwatch (aws)", doctor.OK, "tarde")],
        ],
    )
    t0 = _time.monotonic()
    result = anyio.run(server._health, acme())
    assert _time.monotonic() - t0 < 1.5  # no espera al chequeo lento
    assert [c.name for c in result] == ["mysql (reader)", "chequeo"]
