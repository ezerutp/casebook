import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook import guards
from casebook.backends import opensearch as backend
from casebook.config import Config
from casebook.events import Recorder
from casebook.redact import Redactor
from casebook.server import build
from casebook.store import Store
from demo_pack import acme

# --- guards ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("index", ["leads-*", "leads-3_v-2", "orders-pending-*,leads-1_v-*"])
def test_check_index_accepts(index):
    assert guards.check_index(index)


@pytest.mark.parametrize(
    "index",
    ["*", ".kibana", "_all", "leads-*/_delete_by_query", "leads?refresh=true", "leads-*,.security", "Leads", ""],
)
def test_check_index_rejects(index):
    with pytest.raises(guards.GuardError):
        guards.check_index(index)


def test_check_search_body():
    body = guards.check_search_body({"size": 0, "query": {"match_all": {}}})
    assert body["timeout"] == guards.OS_SERVER_TIMEOUT
    for bad in ({"size": 10_001}, {"size": 5000, "from": 6000}, {"scroll": "1m"}, {"script_fields": {}}):
        with pytest.raises(guards.GuardError):
            guards.check_search_body(bad)


# --- redacción ------------------------------------------------------------------------------------------------


def test_redact_tokens_and_pii():
    r = Redactor()
    line = (
        "upload gclid=Cj0K accessToken=ya29.a0AfB_byC123456789 Bearer abcdefghijkl "
        "from otro@gmail.com to cliente@example.com phone +1 (555) 123-4567 at 2026-10-14 10:00:00 id 123456789012"
    )
    out = r.text(line)
    assert "ya29" not in out and "abcdefghijkl" not in out
    assert "accessToken=[REDACTED]" in out
    assert "otro@gmail.com" in out and "cliente@example.com" in out
    assert "555" not in out and "***67" in out
    assert "2026-10-14 10:00:00" in out and "123456789012" in out
    assert "gclid=Cj0K" in out


def test_redact_sensitive_keys_in_structures():
    out = Redactor().value({"refresh_token": "abc", "nested": [{"apiKey": "k", "name": "ok"}], "n": 3})
    assert out == {"refresh_token": "[REDACTED]", "nested": [{"apiKey": "[REDACTED]", "name": "ok"}], "n": 3}


# --- tablas ---------------------------------------------------------------------------------------------------

SEARCH = {
    "took": 12,
    "timed_out": False,
    "hits": {
        "total": {"value": 2, "relation": "eq"},
        "hits": [
            {"_index": "leads-1_v-2", "_id": "a", "_source": {"email": "x@y.com", "attr": {"source": "fb"}}},
            {"_index": "leads-1_v-2", "_id": "b", "_source": {"email": "z@y.com", "score": 3}},
        ],
    },
    "aggregations": {
        "por_mes": {
            "buckets": [
                {"key_as_string": "2026-09", "key": 1, "doc_count": 5, "total": {"value": 10.5},
                 "por_fuente": {"buckets": [{"key": "fb", "doc_count": 3}, {"key": "gg", "doc_count": 2}]}},
                {"key_as_string": "2026-10", "key": 2, "doc_count": 1, "total": {"value": 1.0},
                 "por_fuente": {"buckets": []}},
            ]
        },
        "suma": {"value": 11.5},
    },
}


def test_hits_table_flattens_source():
    columns, rows = backend.hits_table(SEARCH)
    assert columns == ["_index", "_id", "email", "attr.source", "score"]
    assert rows[0] == ["leads-1_v-2", "a", "x@y.com", "fb", None]


def test_aggs_tables_nested_buckets_and_metrics():
    tables = {t["name"]: t for t in backend.aggs_tables(SEARCH)}
    mes = tables["aggs.por_mes"]
    assert mes["columns"] == ["key", "doc_count", "key_2", "doc_count_2", "total"]
    assert mes["rows"][0] == ["2026-09", 5, "fb", 3, 10.5]
    assert len(mes["rows"]) == 2  # el bucket con sub-buckets vacíos no genera filas
    assert tables["aggs.suma"]["rows"] == [[11.5]]


# --- tools ----------------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    calls = []
    responses = {}

    async def fake_request(self, method, path, body=None, timeout=60):
        calls.append({"env": self.env, "method": method, "path": path, "body": body})
        for prefix, resp in responses.items():
            if path.startswith(prefix) or prefix in path:
                return resp
        raise AssertionError(f"sin respuesta para {path}")

    monkeypatch.setattr(backend.OpenSearchClient, "request", fake_request)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, "http://v", acme())
    yield mcp, rec, store, calls, responses
    store.close()


def call(mcp, name, args):
    async def go():
        result = await mcp.call_tool(name, args)
        return json.loads(result.content[0].text)

    return anyio.run(go)


def test_os_search_end_to_end(server):
    mcp, rec, store, calls, responses = server
    leak = dict(SEARCH)
    leak["hits"] = {
        "total": {"value": 1},
        "hits": [{"_index": "i", "_id": "1", "_source": {"msg": "token accessToken=ya29.secretsecretsecret", "email": "a@b.com"}}],
    }
    responses["_search"] = leak

    out = call(mcp, "os_search", {"purpose": "prueba", "index": "leads-*", "query": {"term": {"email": "a@b.com"}}, "size": 5, "source": ["msg", "email"]})

    assert calls[0]["path"] == "leads-*/_search" and calls[0]["body"]["timeout"] == "30s"
    assert calls[0]["body"]["_source"] == ["msg", "email"]
    assert out["total_hits"] == 1 and out["columns"] == ["_index", "_id", "msg", "email"]
    assert "ya29" not in json.dumps(out)
    assert out["rows"][0][3] == "a@b.com"  # el email que Claude consultó no se oculta
    assert set(out["aggs"]) == {"aggs.por_mes", "aggs.suma"}
    assert out["viewer"] == f"http://v/#/sessions/{rec.session_id}"

    types = [e["type"] for e in store.events(rec.session_id)]
    assert types == ["session.started", "query.started", "query.finished"]
    finished = store.events(rec.session_id)[-1]
    assert "ya29" not in json.dumps(finished)
    assert finished["payload"]["result"]["raw"]["took"] == 12


def test_os_search_aggs_only_uses_first_agg_as_main_table(server):
    mcp, _, _, _, responses = server
    responses["_search"] = {**SEARCH, "hits": {"total": {"value": 6}, "hits": []}}
    out = call(mcp, "os_search", {"purpose": "prueba", "index": "leads-*", "size": 0, "aggs": {"x": {}}})
    assert out["main_table"] == "aggs.por_mes" and out["columns"][0] == "key"
    assert list(out["aggs"]) == ["aggs.suma"]


def test_os_search_guard_blocks_before_any_request(server):
    mcp, rec, store, calls, _ = server
    with pytest.raises(ToolError, match="no permitido"):
        anyio.run(lambda: mcp.call_tool("os_search", {"purpose": "prueba", "index": ".security"}))
    assert calls == []


def test_backend_errors_reach_claude(server, monkeypatch):
    mcp, rec, store, _, _ = server

    async def expired(self, *a, **k):
        raise backend.SessionExpired("la sesión de OpenSearch prod venció")

    monkeypatch.setattr(backend.OpenSearchClient, "request", expired)
    with pytest.raises(ToolError, match="venció"):
        anyio.run(lambda: mcp.call_tool("os_count", {"purpose": "prueba", "index": "leads-*"}))
    assert store.events(rec.session_id)[-1]["type"] == "query.failed"


def test_os_indices_and_count(server):
    mcp, rec, store, calls, responses = server
    responses["_cat/indices"] = [{"index": "leads-1_v-2", "health": "green", "docs.count": "42", "store.size": "1gb"}]
    responses["_count"] = {"count": 7}
    out = call(mcp, "os_indices", {"purpose": "prueba", "pattern": "leads-*", "env": "prod-archive"})
    assert out["rows"] == [["leads-1_v-2", "green", 42, "1gb"]]
    assert calls[0]["env"] == "prod-archive" and calls[0]["path"].startswith("_cat/indices/leads-*?format=json")
    counted = call(mcp, "os_count", {"purpose": "prueba", "index": "leads-*", "query": {"match_all": {}}})
    # El query_id que ve Claude es el mismo que queda en la sesión, para citarlo en las notas.
    assert counted == {"query_id": store.events(rec.session_id)[-1]["query_id"], "count": 7, "viewer": out["viewer"]}


def test_os_mapping(server):
    mcp, _, _, _, responses = server
    responses["_mapping"] = {
        "leads-1_v-2": {"mappings": {"properties": {"email": {"type": "keyword"}, "attr": {"properties": {"source": {"type": "keyword"}}}}}}
    }
    out = call(mcp, "os_mapping", {"purpose": "prueba", "index": "leads-*"})
    assert out["fields"] == {"attr.source": "keyword", "email": "keyword"}


def test_successful_tools_never_record_failures(server):
    mcp, rec, store, _, responses = server
    responses["_cat/indices"] = [{"index": "a", "health": "green", "docs.count": "1", "store.size": "1kb"}]
    responses["_count"] = {"count": 1}
    responses["_mapping"] = {"a": {"mappings": {"properties": {}}}}
    responses["_search"] = SEARCH
    for name, args in [("os_indices", {"purpose": "prueba", "pattern": "a*"}), ("os_count", {"purpose": "prueba", "index": "a"}),
                       ("os_mapping", {"purpose": "prueba", "index": "a"}), ("os_search", {"purpose": "prueba", "index": "a"})]:
        call(mcp, name, args)
    types = [e["type"] for e in store.events(rec.session_id)]
    assert "query.failed" not in types and types.count("query.finished") == 4


def test_session_expired_message(monkeypatch):
    monkeypatch.setattr(backend.credentials, "get_cookie", lambda system: None)
    client = backend.OpenSearchClient(acme(), "prod")
    with pytest.raises(backend.SessionExpired, match="login os"):
        anyio.run(client.request, "GET", "_cat/indices")


def test_aggs_tables_single_bucket_sub_aggs():
    resp = {"aggregations": {"por_dia": {"buckets": [
        {"key_as_string": "2026-10-14", "doc_count": 10, "con_error": {"doc_count": 3, "valor": {"value": 1.5}}},
        {"key_as_string": "2026-10-13", "doc_count": 8, "con_error": {"doc_count": 0, "valor": {"value": None}}},
    ]}}}
    t = backend.aggs_tables(resp)[0]
    assert t["columns"] == ["key", "doc_count", "con_error", "con_error.valor"]
    assert t["rows"] == [["2026-10-14", 10, 3, 1.5], ["2026-10-13", 8, 0, None]]


def _client_with_responses(monkeypatch, responses):
    """Cliente de OpenSearch con un transport falso que devuelve `responses` en orden."""
    import httpx

    monkeypatch.setattr(backend.credentials, "get_cookie", lambda system: "a=1")
    monkeypatch.setattr(backend.credentials, "refresh_from_response", lambda *a: None)
    queue = list(responses)

    def handler(request):
        assert request.url.host == "127.0.0.1" and request.headers["host"].startswith("vpc-acme-prod")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    monkeypatch.setattr(backend.httpx, "AsyncClient", lambda **kw: real(transport=transport))
    return backend.OpenSearchClient(acme(), "prod")


def test_connection_refused_repairs_tunnel_and_retries(monkeypatch):
    import httpx

    repairs = []
    monkeypatch.setattr(backend.os_tunnel, "ensure", lambda *a: repairs.append(a) or (backend.os_tunnel.UP, "estaba caído; se levantó de nuevo"))
    client = _client_with_responses(monkeypatch, [httpx.ConnectError("refused"), httpx.Response(200, json=[{"index": "a"}])])
    assert anyio.run(client.request, "GET", "_cat/indices") == [{"index": "a"}]
    assert [r[0] for r in repairs] == ["os_tunnel_prod", "os_tunnel_prod"]  # chequeo previo + reparación


def test_tunnel_that_cannot_be_repaired_is_a_clear_error(monkeypatch):
    import httpx

    monkeypatch.setattr(backend.os_tunnel, "ensure", lambda *a: (backend.os_tunnel.DOWN, "no se pudo levantar `ssh os_tunnel_prod`"))
    client = _client_with_responses(monkeypatch, [httpx.ConnectError("refused")])
    with pytest.raises(backend.BackendError, match="túnel de OpenSearch prod no funciona.*ssh os_tunnel_prod"):
        anyio.run(client.request, "GET", "_cat/indices")


def test_recent_success_skips_tunnel_probe(monkeypatch):
    import httpx

    probes = []
    monkeypatch.setattr(backend.os_tunnel, "ensure", lambda *a: probes.append(a) or (backend.os_tunnel.UP, "túnel arriba"))
    client = _client_with_responses(monkeypatch, [httpx.Response(200, json={}), httpx.Response(200, json={})])
    anyio.run(client.request, "GET", "_cat/indices")
    anyio.run(client.request, "GET", "_cat/indices")
    assert len(probes) == 1


def test_emails_remain_distinguishable_without_allowlist():
    result = Redactor().value({
        'rows': [['ana@example.com'], ['alberto@example.com']],
        'message': 'Contact ana@example.com; access_token=super-secret-value',
        'password': 'private',
    })
    assert result['rows'] == [['ana@example.com'], ['alberto@example.com']]
    assert result['message'] == 'Contact ana@example.com; access_token=[REDACTED]'
    assert result['password'] == '[REDACTED]'
