"""Consultas manuales desde el visor: repetir, editar, correr nuevas, fijar y que el agente se entere."""

import json
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from casebook import manual
from casebook.backends import opensearch as backend
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme

H = {"x-casebook-viewer": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = []
    counts = iter(range(7, 100))

    async def fake_request(self, method, path, body=None, timeout=60):
        calls.append({"env": self.env, "path": path, "body": body})
        if path.endswith("_count"):
            return {"count": next(counts)}
        if path.endswith("_search"):
            return {"took": 3, "timed_out": False, "hits": {"total": {"value": 2}, "hits": [
                {"_index": "leads-1", "_id": "a", "_source": {"email": "x@y.com"}},
                {"_index": "leads-1", "_id": "b", "_source": {"email": "z@y.com"}}]}}
        raise AssertionError(path)

    monkeypatch.setattr(backend.OpenSearchClient, "request", fake_request)
    store = Store(tmp_path / "t.db")
    cfg = acme()
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, "http://v", cfg)
    yield mcp, rec, store, cfg, calls
    store.close()


def call(mcp, name, args):
    async def go():
        result = await mcp.call_tool(name, args)
        return result.content[0].text

    return anyio.run(go)


def wait_finished(store, query_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        types = [e["type"] for e in store.query_events(query_id)]
        if "query.finished" in types or "query.failed" in types:
            return types
        time.sleep(0.02)
    raise AssertionError(f"la consulta {query_id} no terminó")


def started(store, query_id):
    return manual.started_event(store, query_id)["payload"]


def test_agent_queries_keep_their_args(env):
    mcp, rec, store, _, _ = env
    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*", "query": {"term": {"a": 1}}})
    p = store.events(rec.session_id)[-2]["payload"]
    assert p["args"] == {"index": "leads-*", "query": {"term": {"a": 1}}, "env": "prod"}
    assert "origin" not in p


def test_repeat_records_a_manual_run_under_the_original(env):
    mcp, rec, store, cfg, calls = env
    call(mcp, "os_count", {"purpose": "contar leads", "env": "prod", "index": "leads-*"})
    original = store.events(rec.session_id)[-1]["query_id"]

    with TestClient(create_app(store, cfg)) as client:
        r = client.post(f"/api/queries/{original}/repeat", json={}, headers=H)
        assert r.status_code == 202, r.text
        first = r.json()["query_id"]
        wait_finished(store, first)
        # Repetir la repetición agrupa bajo la original, no encadena.
        second = client.post(f"/api/queries/{first}/repeat", json={}, headers=H).json()["query_id"]
        wait_finished(store, second)

    p = started(store, first)
    assert p["origin"] == "manual" and p["run_mode"] == "repeat" and p["parent_query_id"] == original
    assert p["purpose"] == "contar leads" and p["params"]["env"] == "prod"
    assert started(store, second)["parent_query_id"] == original
    assert [c["path"] for c in calls] == ["leads-*/_count"] * 3
    assert store.query_session(first) == rec.session_id


def test_repeat_rebuilds_args_of_old_events_without_args(env):
    _, _, store, _, _ = env
    start = {"query_id": "q1", "payload": {"tool": "os_search", "params": {"index": "leads-*", "env": "prod", "size": 5},
             "query": 'GET leads-*/_search\n{"size": 5, "track_total_hits": true, "query": {"term": {"a": 1}}, '
                      '"_source": ["email"], "timeout": "30s"}'}}
    assert manual.args_of(start) == {"index": "leads-*", "query": {"term": {"a": 1}}, "size": 5,
                                     "source": ["email"], "track_total_hits": True, "env": "prod"}
    sql = {"query_id": "q2", "payload": {"tool": "db_select", "params": {"limit": 100, "env": "prod"},
           "query": "SELECT 1 LIMIT 100"}}
    assert manual.args_of(sql) == {"sql": "SELECT 1 LIMIT 100", "limit": 100, "env": "prod"}
    cw = {"query_id": "q3", "payload": {"tool": "cw_query", "args": {"log_group": "g", "query": "fields x",
          "start": "-2h"}, "params": {"log_group": "g", "start": "2026-11-14T08:00:00Z",
          "end": "2026-11-14T10:00:00Z", "max_cost_usd": 0.25, "env": "prod"}, "query": "fields x"}}
    args = manual.args_of(cw)
    assert args["start"] == "2026-11-14T08:00:00Z" and args["end"] == "2026-11-14T10:00:00Z"
    from datetime import UTC, datetime

    shifted = manual.shift_window(args, now=datetime(2026, 11, 14, 15, 0, tzinfo=UTC))
    assert shifted["start"].startswith("2026-11-14T13:00:00") and shifted["end"].startswith("2026-11-14T15:00:00")


def test_discovery_tools_are_not_repeatable(env):
    mcp, rec, store, cfg, _ = env
    store.add_event(rec.session_id, "query.started", {"tool": "os_mapping", "params": {"index": "x"}}, "qmap")
    with TestClient(create_app(store, cfg)) as client:
        r = client.post("/api/queries/qmap/repeat", json={}, headers=H)
    assert r.status_code == 422 and "no se repite" in r.json()["detail"]


def test_new_manual_query_and_guard_errors_go_to_the_form(env):
    _, rec, store, cfg, calls = env
    with TestClient(create_app(store, cfg)) as client:
        bad = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H,
                          json={"tool": "os_search", "purpose": "x", "args": {"index": ".security", "env": "prod"}})
        assert bad.status_code == 422 and "no permitido" in bad.json()["detail"]
        nopurpose = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H,
                                json={"tool": "os_count", "purpose": " ", "args": {"index": "leads-*", "env": "prod"}})
        assert nopurpose.status_code == 422
        notallowed = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H,
                                 json={"tool": "session_note", "purpose": "x", "args": {}})
        assert notallowed.status_code == 422
        ok = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H, json={
            "tool": "os_search", "purpose": "Buscar el lead", "wait": True,
            "args": {"index": "leads-*", "env": "prod", "query": {"term": {"email": "x@y.com"}}, "size": 5}})
        assert ok.status_code == 202, ok.text
    qid = ok.json()["query_id"]
    assert wait_finished(store, qid)[-1] == "query.finished"
    p = started(store, qid)
    assert p["origin"] == "manual" and p["run_mode"] == "new" and "parent_query_id" not in p
    # El guard bloqueó antes de arrancar: ni request ni tarjeta de rechazo.
    types = [e["type"] for e in store.events(rec.session_id)]
    assert "query.rejected" not in types
    assert len(calls) == 1 and calls[0]["body"]["size"] == 5


def test_edit_requires_parent_in_same_session(env):
    mcp, rec, store, cfg, _ = env
    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*"})
    original = store.events(rec.session_id)[-1]["query_id"]
    other = store.create_session("otra")["id"]
    with TestClient(create_app(store, cfg)) as client:
        args = client.get(f"/api/queries/{original}/args").json()
        assert args["tool"] == "os_count" and args["args"]["index"] == "leads-*"
        r = client.post(f"/api/sessions/{other}/queries", headers=H, json={
            "tool": "os_count", "purpose": "x", "run_mode": "edit", "parent_query_id": original, "args": args["args"]})
        assert r.status_code == 422
        r = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H, json={
            "tool": "os_count", "purpose": "sin filtro", "run_mode": "edit", "parent_query_id": original,
            "args": {**args["args"], "query": {"match_all": {}}}, "wait": True})
        assert r.status_code == 202
    p = started(store, r.json()["query_id"])
    assert p["run_mode"] == "edit" and p["parent_query_id"] == original


def test_manual_queries_skip_the_review_gate(env, monkeypatch):
    mcp, rec, store, cfg, _ = env
    from casebook.review import ReviewError

    def closed(sid):
        raise ReviewError("la sesión está cerrada")

    monkeypatch.setattr(store.review, "query_gate", closed)
    with TestClient(create_app(store, cfg)) as client:
        r = client.post(f"/api/sessions/{rec.session_id}/queries", headers=H, json={
            "tool": "os_count", "purpose": "después del cierre", "args": {"index": "leads-*", "env": "prod"}, "wait": True})
    assert r.status_code == 202, r.text


def test_pin_and_unpin(env):
    mcp, rec, store, cfg, _ = env
    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*"})
    qid = store.events(rec.session_id)[-1]["query_id"]
    with TestClient(create_app(store, cfg)) as client:
        assert client.post(f"/api/queries/{qid}/pin", json={"pinned": True}, headers=H).json() == {"pinned": [qid]}
        client.post(f"/api/queries/{qid}/pin", json={"pinned": True}, headers=H)  # idempotente
        assert client.post(f"/api/queries/{qid}/pin", json={"pinned": False}, headers=H).json() == {"pinned": []}
        assert client.post("/api/queries/nope/pin", json={"pinned": True}, headers=H).status_code == 404
    assert [e["type"] for e in store.query_events(qid)][-2:] == ["query.pinned", "query.unpinned"]


def test_agent_hears_about_engineer_activity_once(env):
    mcp, rec, store, cfg, _ = env
    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*"})
    original = store.events(rec.session_id)[-1]["query_id"]
    with TestClient(create_app(store, cfg)) as client:
        qid = client.post(f"/api/queries/{original}/repeat", json={}, headers=H).json()["query_id"]
        wait_finished(store, qid)
        client.post(f"/api/queries/{qid}/pin", json={"pinned": True}, headers=H)

    out = json.loads(call(mcp, "os_count", {"purpose": "otra", "env": "prod", "index": "leads-*"}))
    activity = out["engineer_activity"]
    assert activity["pinned"] == [qid]
    [q] = activity["queries"]
    assert q["query_id"] == qid and q["run_mode"] == "repeat" and q["status"] == "ok"
    assert q["parent_query_id"] == original and q["rows_delta_vs_parent"] == 0
    # Ya se le contó: la siguiente respuesta no lo repite.
    assert "engineer_activity" not in json.loads(call(mcp, "os_count", {"purpose": "y otra", "env": "prod", "index": "leads-*"}))


def test_resume_reports_manual_queries_after_the_agent_stopped(env):
    mcp, rec, store, cfg, _ = env
    sid = rec.session_id
    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*"})
    call(mcp, "session_note", {"text": "conclusión: hay 7"})
    with TestClient(create_app(store, cfg)) as client:
        r = client.post(f"/api/sessions/{sid}/queries", headers=H, json={
            "tool": "os_count", "purpose": "volver a contar", "args": {"index": "leads-*", "env": "prod"}, "wait": True})
    qid = r.json()["query_id"]

    # Otro proceso MCP (se reinició) reanuda la sesión.
    rec2 = Recorder(store)
    mcp2 = build(rec2, "http://v", cfg)
    out = json.loads(call(mcp2, "session_resume", {"session_id": sid}))
    assert [q["query_id"] for q in out["engineer_activity"]["queries"]] == [qid]
    assert out["engineer_activity"]["queries"][0]["rows"] == 1


def test_running_manual_query_is_reported_again_when_it_finishes(env):
    _, rec, store, _, _ = env
    sid = rec.session_id
    store.add_event(sid, "query.started", {"tool": "cw_query", "params": {}, "origin": "manual", "run_mode": "new",
                                           "purpose": "logs"}, "qrun")
    report, cursor = manual.activity(store, sid, 0)
    assert report["queries"][0]["status"] == "running"
    store.add_event(sid, "query.finished", {"rows": 3, "result": {"columns": [], "rows": []}}, "qrun")
    report, _ = manual.activity(store, sid, cursor)
    assert report["queries"][0]["status"] == "ok" and report["queries"][0]["rows"] == 3


def test_similar_queries_from_other_sessions(env):
    mcp, rec, store, _, _ = env
    call(mcp, "os_search", {"purpose": "buscar", "env": "prod", "index": "leads-*", "size": 2})
    other = store.create_session("otra")["id"]
    found = store.similar_queries("os_search", "index", "leads-*", exclude_session=other)
    assert found and found[0]["args"]["index"] == "leads-*" and found[0]["rows"] == 2
    assert store.similar_queries("os_search", "index", "leads-*", exclude_session=rec.session_id) == []


def test_export_marks_manual_and_pinned_queries(env):
    mcp, rec, store, cfg, _ = env
    from casebook import export

    call(mcp, "os_count", {"purpose": "contar", "env": "prod", "index": "leads-*"})
    original = store.events(rec.session_id)[-1]["query_id"]
    with TestClient(create_app(store, cfg)) as client:
        qid = client.post(f"/api/queries/{original}/repeat", json={}, headers=H).json()["query_id"]
        wait_finished(store, qid)
        client.post(f"/api/queries/{qid}/pin", json={"pinned": True}, headers=H)
    md = export.render(store, rec.session_id)
    assert "2 consultas (1 del ingeniero)" in md
    assert "## Evidencia fijada por el ingeniero" in md and f"- `{qid}`" in md
    assert f"del ingeniero (repetida de `{original}`) · fijada" in md
