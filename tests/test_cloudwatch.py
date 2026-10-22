import json
import threading
import time
from datetime import UTC, datetime

import anyio
import pytest

import fake_logs
from fake_logs import QUERY_ID
from casebook.backends.errors import BackendError
from casebook.providers.base import QueryState
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from casebook import guards, timerange
from casebook.config import Config
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from casebook.tools import cloudwatch
from casebook.viewer.app import create_app
from demo_pack import acme


def approval_timeout(cfg, seconds):
    cfg.approval_timeout_s = seconds
    return cfg

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# --- rangos de tiempo -----------------------------------------------------------------------------------------


def test_timerange_parse():
    assert timerange.parse("now", NOW) == NOW
    assert timerange.parse("-2h", NOW) == datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    assert timerange.parse("-3d", NOW) == datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert timerange.parse("2026-10-14T09:30:00Z", NOW) == datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
    assert timerange.parse("2026-10-14T09:30:00", NOW).tzinfo == UTC
    assert timerange.iso(datetime(2026, 10, 14, 9, 30, tzinfo=UTC)) == "2026-10-14T09:30:00.000Z"
    with pytest.raises(guards.GuardError):
        timerange.parse("ayer", NOW)


def test_timerange_window_limits():
    with pytest.raises(guards.GuardError, match="anterior"):
        timerange.window("now", "-1h", 7, NOW)
    with pytest.raises(guards.GuardError, match="7 días"):
        timerange.window("-8d", "now", 7, NOW)
    s, e = timerange.window("-1h", "now", 7, NOW)
    assert e - s == NOW - timerange.parse("-1h", NOW)


# --- tools ----------------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudwatch, "POLL_SECONDS", 0)
    fake = fake_logs.install(monkeypatch)
    cfg = approval_timeout(acme(), 1.0)
    cfg.cw_provider, cfg.cw_options = "fake", {}
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, "http://v", cfg)
    yield mcp, rec, store, fake
    store.close()


def call(mcp, name, args):
    async def go():
        result = await mcp.call_tool(name, args)
        return json.loads(result.content[0].text)

    return anyio.run(go)


def test_cw_query_end_to_end(env):
    mcp, rec, store, fake = env
    out = call(mcp, "cw_query", {"purpose": "prueba", 
        "log_group": "/ecs/acme-api",
        "query": "fields @timestamp, @message | filter @message like /cliente@x.com/",
        "start": "-1h",
    })

    assert [c[0] for c in fake.calls] == ["estimate", "start", "get", "get"]
    assert out["status"] == "COMPLETE" and out["records_matched"] == 2 and out["cost_usd"] == 0.015
    assert out["columns"] == ["@timestamp", "@message", "@ptr"]
    text = json.dumps(out)
    assert "ya29" not in text and "cliente@x.com" in text and "otro@gmail.com" in text
    assert out["external_id"] == QUERY_ID

    events = store.events(rec.session_id)
    assert [e["type"] for e in events] == [
        "session.started", "query.started", "query.progress", "query.progress", "query.finished"
    ]
    assert events[1]["payload"]["cancellable"] is True
    assert out["query_id"] == events[-1]["query_id"]
    assert events[-1]["payload"]["meta"]["cost_usd"] == 0.015
    assert "ya29" not in json.dumps(events)


def test_cw_query_over_max_cost_waits_for_approval_and_expires(env):
    mcp, rec, store, fake = env
    fake.estimate_resp.cost_usd = 0.2  # 0.2 * 1.5 > 0.25
    with pytest.raises(ToolError, match="no se aprobó"):
        anyio.run(lambda: mcp.call_tool("cw_query", {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message", "start": "-6h"}))
    assert [c[0] for c in fake.calls] == ["estimate"]
    types = [e["type"] for e in store.events(rec.session_id)]
    assert "query.approval_requested" in types and types[-1] == "query.failed"


def decide_when_requested(store, approve):
    """Simula al ingeniero: cuando aparece el pedido de aprobación, responde desde el visor."""
    viewer = TestClient(create_app(store))
    deadline = time.monotonic() + 5

    def run():
        while time.monotonic() < deadline:
            pending = [e for e in store.events() if e["type"] == "query.approval_requested"]
            if pending:
                viewer.post(
                    f"/api/queries/{pending[-1]['query_id']}/approval",
                    json={"approve": approve},
                    headers={"x-casebook-viewer": "1"},
                )
                return
            time.sleep(0.02)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("approve", [True, False])
def test_cw_query_over_max_cost_approved_or_denied_in_viewer(env, approve):
    mcp, rec, store, fake = env
    fake.estimate_resp.cost_usd = 0.2
    decide_when_requested(store, approve)
    args = {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message", "start": "-6h"}
    if approve:
        out = call(mcp, "cw_query", args)
        assert out["status"] == "COMPLETE"
        assert "start" in [c[0] for c in fake.calls]
    else:
        with pytest.raises(ToolError, match="no se aprobó"):
            anyio.run(lambda: mcp.call_tool("cw_query", args))
        assert "start" not in [c[0] for c in fake.calls]
    assert ("query.approved" if approve else "query.denied") in [e["type"] for e in store.events(rec.session_id)]


def test_cw_query_refuses_when_backend_limit_exceeded(env):
    mcp, _, _, fake = env
    fake.estimate_resp.within_limit = False
    with pytest.raises(ToolError, match="no permite"):
        anyio.run(lambda: mcp.call_tool("cw_query", {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message", "start": "-1h", "max_cost_usd": 5}))
    assert "start" not in [c[0] for c in fake.calls]


def test_cw_query_failed_status_reaches_claude(env):
    mcp, _, _, fake = env
    fake.polls = [QueryState("FAILED", error="MalformedQueryException: unexpected symbol")]
    with pytest.raises(ToolError, match="MalformedQuery"):
        anyio.run(lambda: mcp.call_tool("cw_query", {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message |", "start": "-1h"}))


def test_cw_query_cancel_from_viewer(env, monkeypatch):
    mcp, rec, store, fake = env
    fake.polls = [QueryState("RUNNING")]  # nunca termina
    viewer = TestClient(create_app(store))

    original = fake_logs.FakeLogsClient.get_query

    async def get_and_request_cancel(self, external_id):
        started = [e for e in store.events() if e["type"] == "query.started"][-1]
        assert viewer.post(f"/api/queries/{started['query_id']}/cancel", headers={"x-casebook-viewer": "1"}).status_code == 202
        return await original(self, external_id)

    monkeypatch.setattr(fake_logs.FakeLogsClient, "get_query", get_and_request_cancel)
    with pytest.raises(ToolError, match="cancelada"):
        anyio.run(lambda: mcp.call_tool("cw_query", {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message", "start": "-1h"}))
    assert ("cancel", QUERY_ID) in fake.calls
    types = [e["type"] for e in store.events(rec.session_id)]
    assert "query.cancel_requested" in types and types[-1] == "query.failed"


def test_viewer_cancel_endpoint_states(env):
    _, rec, store, _ = env
    viewer = TestClient(create_app(store))
    h = {"x-casebook-viewer": "1"}
    assert viewer.post("/api/queries/nope/cancel", headers=h).status_code == 404
    with rec.query("cw_query", {}, "q", cancellable=True) as q:
        q.finish(["a"], [[1]])
    assert viewer.post(f"/api/queries/{q.query_id}/cancel", headers=h).status_code == 409


@pytest.mark.parametrize("log_group", ["/ecs/acme-*", "", "a b", "x" * 600])
def test_cw_rejects_bad_log_groups(env, log_group):
    mcp, rec, store, fake = env
    with pytest.raises(ToolError, match="log group"):
        anyio.run(lambda: mcp.call_tool("cw_estimate", {"purpose": "prueba", "log_group": log_group, "start": "-1h"}))
    assert fake.calls == []
    rejected = store.events(rec.session_id)[-1]
    assert rejected["type"] == "query.rejected" and rejected["payload"]["tool"] == "cw_estimate"


def test_cw_estimate_and_log_groups(env):
    mcp, _, _, _ = env
    out = call(mcp, "cw_estimate", {"purpose": "prueba", "log_group": "/ecs/acme-api", "start": "-1h"})
    assert out["estimated_gb"] == 2.0 and out["within_backend_limit"] is True
    assert call(mcp, "cw_log_groups", {"purpose": "prueba", })["log_groups"] == ["/ecs/acme-api"]


def test_cw_context(env):
    mcp, _, _, fake = env
    out = call(mcp, "cw_context", {"purpose": "prueba", "external_id": QUERY_ID, "ptr": "PTR1", "events": 5})
    assert fake.calls[-1] == ("surrounding", QUERY_ID, "PTR1", 5)
    assert out["columns"] == ["timestamp", "message"] and "abcdefghijklmnop" not in json.dumps(out)
    with pytest.raises(ToolError, match="external_id"):
        anyio.run(lambda: mcp.call_tool("cw_context", {"purpose": "prueba", "external_id": "../admin", "ptr": "x"}))


def test_cw_context_unavailable_is_a_clear_error(env):
    mcp, _, _, fake = env
    fake.surrounding_resp = BackendError("no pudo traer el contexto: This record is no longer in its log stream")
    with pytest.raises(ToolError, match="no longer in its log stream"):
        anyio.run(lambda: mcp.call_tool("cw_context", {"purpose": "prueba", "external_id": QUERY_ID, "ptr": "p"}))


def test_cw_query_cancelled_before_start_never_starts(env, monkeypatch):
    mcp, _, store, fake = env
    viewer = TestClient(create_app(store))
    original = fake_logs.FakeLogsClient.estimate

    async def estimate_then_cancel(self, *args):
        started = [e for e in store.events() if e["type"] == "query.started"][-1]
        viewer.post(f"/api/queries/{started['query_id']}/cancel", headers={"x-casebook-viewer": "1"})
        return await original(self, *args)

    monkeypatch.setattr(fake_logs.FakeLogsClient, "estimate", estimate_then_cancel)
    with pytest.raises(ToolError, match="antes de empezar"):
        anyio.run(lambda: mcp.call_tool("cw_query", {"purpose": "prueba", "log_group": "/ecs/acme-api", "query": "fields @message", "start": "-1h"}))
    assert "start" not in [c[0] for c in fake.calls]


def test_viewer_action_protection(env):
    _, rec, store, _ = env
    viewer = TestClient(create_app(store))
    with rec.query("cw_query", {}, "q", cancellable=True) as q:
        anyio.run(lambda: q.await_approval("cara", timeout_s=0.01, poll_s=0.01))
        url = f"/api/queries/{q.query_id}/approval"
        assert viewer.post(url, json={"approve": True}).status_code == 403  # sin header
        assert viewer.post(url, json={"approve": True}, headers={"x-casebook-viewer": "1", "origin": "https://evil.example"}).status_code == 403
        assert viewer.post(f"/api/queries/{q.query_id}/cancel").status_code == 403
        ok = viewer.post(url, json={"approve": True}, headers={"x-casebook-viewer": "1", "origin": "http://testserver"})
        assert ok.status_code == 202
        assert viewer.post(url, json={"approve": False}, headers={"x-casebook-viewer": "1"}).status_code == 409
        q.finish(["a"], [[1]])
    assert viewer.post("/api/queries/nope/approval", json={"approve": True}, headers={"x-casebook-viewer": "1"}).status_code == 404
