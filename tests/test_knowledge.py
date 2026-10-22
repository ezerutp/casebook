import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from casebook.config import Config
from casebook.events import Recorder
from casebook.knowledge import KnowledgeStore, KnowledgeError, Conflict
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "sessions.db")
    yield store
    store.close()


def entry(**kw):
    return dict(source="mysql", environment="prod/acme", entity="sale", kind="relation", title="Ventas",
                content="Relación de ventas y atribución", topics=["ventas"],
                evidence={"summary": "Referencia de código", "code_path": "Sale.java", "code_revision": "abc"},
                reason="Descubrimiento", **kw)


def describe(store, table="sale"):
    rec = Recorder(store)
    with rec.query("db_describe", {"table": table, "environment": "prod/acme"}, f"SHOW COLUMNS FROM `{table}`") as q:
        q.finish(["Field", "Type"], [["id", "bigint"]], tables=[{"name": "índices", "columns": ["key", "column"], "rows": [["PRIMARY", "id"]]}])
    return q


def call(mcp, name, **args):
    return json.loads(anyio.run(lambda: mcp.call_tool(name, args)).content[0].text)


def test_persistence_separate_database_and_capture(store):
    q = describe(store)
    path = store.knowledge.db.path
    assert path.name == "knowledge.db" and path != store.path
    db = KnowledgeStore(path)
    result = db.search("sale", source="mysql", environment="prod/acme")
    record = db.read(result["entries"][0]["id"])
    assert record["status"] == "verified"
    assert record["evidence"]["query_id"] == q.query_id
    assert "PRIMARY" in record["content"]
    assert store.events()[-1]["payload"]["knowledge"]["entries"][0]["id"] == record["id"]
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='entries'").fetchone()[0] == 0


def test_history_conflicts_and_pending_edits(store):
    db = store.knowledge.db
    saved = db.write(**entry())
    edited = db.edit(saved["id"], expected_revision=1, title="Ventas actualizadas", content="Nueva relación", topics=["ventas"], reason="Corregir relación")
    assert edited["revision"] == 2
    with pytest.raises(Conflict):
        db.edit(saved["id"], expected_revision=1, title="Obsoleto", content="Texto", topics=[], reason="Otro agente")
    record = db.read(saved["id"])
    assert record["status"] == "pending" and len(record["history"]) == 2
    assert "content" not in record["history"][0]
    assert db.read(saved["id"], revision=1)["content"] == "Relación de ventas y atribución"


def test_concurrent_creation_does_not_duplicate(store):
    db = store.knowledge.db
    db.search()  # inicialización
    def create(_):
        try:
            return KnowledgeStore(db.path).write(**entry())["changed"]
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(create, range(8))) == 1
    assert db.search()["total"] == 1


def test_search_filters_literal_and_pagination(store):
    db = store.knowledge.db
    for i in range(4):
        fields = entry()
        fields.update(entity=f"sale_{i}", environment="prod/acme" if i < 3 else "other", topics=["ventas"])
        db.write(**fields)
    result = db.search("sale", source="mysql", environment="prod/acme", topic="VENTAS", status="pending", limit=2)
    assert result["total"] == 3 and result["next_offset"] == 2
    assert len(db.search("sale", environment="prod/acme", limit=2, offset=2)["entries"]) == 1
    assert db.search("%_")["total"] == 0


def test_partial_catalog_and_concrete_mapping(store):
    rec = Recorder(store)
    with rec.query("db_tables", {"pattern": "sale%"}) as q:
        q.finish(["table"], [["sale"]])
    with rec.query("os_mapping", {"index": "leads-*", "env": "prod"}) as q:
        q.finish(["field", "type"], [["email", "keyword"]], {"from_index": "leads-1_v-7"})
    entries = store.knowledge.db.search()["entries"]
    assert {r["entity"] for r in entries} == {"sale%", "leads-1_v-7"}
    for r in entries:
        data = json.loads(store.knowledge.db.read(r["id"])["content"])
        assert "scope" in data


def test_normal_results_not_persisted_as_knowledge(store):
    rec = Recorder(store)
    with rec.query("db_select", {}, "SELECT email FROM sale") as q:
        q.finish(["email"], [["customer@example.com"]])
    assert not store.knowledge.db.path.exists()


def test_capture_failure_does_not_fail_query(store, monkeypatch):
    def fail(**kw):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(store.knowledge.db, "write", fail)
    q = describe(store)
    assert q.done
    assert store.events()[-1]["type"] == "query.finished"
    assert "warning" in store.events()[-1]["payload"]["knowledge"]


def test_import_idempotent_does_not_undo_manual_correction(store):
    describe(store)
    db = store.knowledge.db
    old = db.read(db.search()["entries"][0]["id"])
    db.edit(old["id"], expected_revision=old["revision"], title="Corrección", content="Texto revisado", topics=[], reason="Esquema por revisar")
    assert store.knowledge.import_sessions() == {"changed": 0, "warnings": 0}
    assert db.read(old["id"])["title"] == "Corrección"


def test_import_existing_history_without_changing_sessions(store):
    session = store.create_session("anterior")
    store.add_event(session["id"], "query.started", {"tool": "db_describe", "params": {"table": "product"}}, "oldq")
    store.add_event(session["id"], "query.finished", {"result": {"columns": ["Field", "Type"], "rows": [["id", "bigint"]]}}, "oldq")
    previous = store.events()
    assert store.knowledge.import_sessions() == {"changed": 1, "warnings": 0}
    assert store.knowledge.import_sessions() == {"changed": 0, "warnings": 0}
    assert store.events() == previous


@pytest.mark.parametrize("error,expected", [
    ("Table 'acme.sale' doesn't exist", "stale"),
    ("Unknown column 'sale.bad' in field list", "stale"),
    ("Lost connection to MySQL server during query", "verified"),
    ("la consulta superó 15 s", "verified"),
])
def test_only_schema_errors_invalidate(store, error, expected):
    describe(store)
    rec = Recorder(store)
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {"environment": "prod/acme"}, "SELECT bad FROM sale"):
            raise RuntimeError(error)
    saved = store.knowledge.db.search()["entries"][0]
    assert saved["status"] == expected


def test_missing_table_observation_is_not_a_permanent_fact(store):
    rec = Recorder(store)
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {}, "SELECT * FROM user"):
            raise RuntimeError("Table 'acme.user' doesn't exist")
    entry = store.knowledge.db.search("user")["entries"][0]
    assert entry["kind"] == "limitation" and entry["status"] == "pending"


def test_mcp_tools_validate_evidence_and_do_not_promote_agent_claims(store):
    mcp = build(Recorder(store), None, Config(viewer_auto_open=False))
    args = {k: v for k, v in entry().items() if k != "evidence"}
    with pytest.raises(ToolError, match="query_id"):
        call(mcp, "knowledge_record", **args, evidence_summary="Consulta", query_id="invented")
    q = describe(store)
    saved = call(mcp, "knowledge_record", **args, evidence_summary="Relación observada", query_id=q.query_id)
    record = call(mcp, "knowledge_read", entry_id=saved["id"])
    assert record["status"] == "pending"
    assert call(mcp, "knowledge_search", topic="ventas")["total"] == 1
    updated = call(mcp, "knowledge_invalidate", entry_id=saved["id"], expected_revision=1, reason="Relación incorrecta")
    assert updated["status"] == "stale"


def test_redaction_does_not_inherit_session_allowlist(store):
    fields = entry()
    fields["content"] = "email=client@example.com access_token=abcdefghijklmnopqrst"
    saved = store.knowledge.db.write(**fields)
    content = store.knowledge.db.read(saved["id"])["content"]
    assert "client@example.com" not in content and "abcdefghijklmnopqrst" not in content


def test_viewer_actions_protected_and_revision_conflicts(store):
    describe(store)
    c = TestClient(create_app(store))
    item = c.get("/api/knowledge").json()["entries"][0]
    body = dict(expected_revision=item["revision"], title="Corregido", content="Por revisar", topics=["ventas"], reason="Evidencia nueva")
    url = f"/api/knowledge/{item['id']}"
    assert c.put(url, json=body).status_code == 403
    assert c.put(url, json=body, headers={"x-casebook-viewer": "1", "origin": "https://other.test"}).status_code == 403
    assert c.put(url, json=body, headers={"x-casebook-viewer": "1"}).status_code == 200
    assert c.put(url, json=body, headers={"x-casebook-viewer": "1"}).status_code == 409
    assert c.get(url).json()["status"] == "pending"
    assert c.post("/api/knowledge/import").status_code == 403
    assert c.post("/api/knowledge/import", headers={"x-casebook-viewer": "1"}).json()["changed"] == 0
    assert c.get("/api/knowledge/999").status_code == 404


def test_observations_same_millisecond_are_not_lost(store, monkeypatch):
    from casebook import events
    monkeypatch.setattr(events, "now", lambda: "2026-10-17T12:00:00.000+00:00")
    describe(store)
    rec = Recorder(store)
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {"environment": "prod/acme"}, "SELECT * FROM sale"):
            raise RuntimeError("Table 'acme.sale' doesn't exist")
    item = store.knowledge.db.search()["entries"][0]
    assert item["status"] == "stale" and item["revision"] == 2


def test_old_import_cannot_overwrite_newer_observation(store):
    db = store.knowledge.db
    fields = entry()
    db.write(**fields, actor="capture", observed_at="2026-10-17T13:00:00+00:00", observation="new")
    fields["content"] = "Old data"
    out = db.write(**fields, actor="capture", observed_at="2026-10-16T13:00:00+00:00", observation="old")
    assert not out["changed"]
    assert db.read(out["id"])["content"] != "Old data"


def test_new_version_of_knowledge_db_is_not_downgraded(store):
    db = store.knowledge.db
    db.search()
    with sqlite3.connect(db.path) as c:
        c.execute("PRAGMA user_version=2")
    with pytest.raises(KnowledgeError, match="más nueva"):
        db.search()
    with sqlite3.connect(db.path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 2


def test_delete_removes_history_preserves_sessions_and_import_receipts(store):
    describe(store)
    db = store.knowledge.db
    old = db.search()["entries"][0]
    events = store.events()
    assert db.delete(old["id"], old["revision"])["deleted"]
    assert db.search()["total"] == 0
    with pytest.raises(KnowledgeError, match="no encontrada"):
        db.read(old["id"])
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM revisions").fetchone()[0] == 0
    assert store.events() == events
    assert store.knowledge.import_sessions()["changed"] == 0
    describe(store)
    assert db.search()["entries"][0]["id"] > old["id"]


def test_delete_conflicts_do_not_remove_newer_edits(store):
    db = store.knowledge.db
    saved = db.write(**entry())
    db.edit(saved["id"], expected_revision=1, title="Nuevo", content="Nueva evidencia", topics=[], reason="Corrección")
    with pytest.raises(Conflict):
        db.delete(saved["id"], 1)
    assert db.read(saved["id"])["revision"] == 2


def test_delete_endpoint_requires_viewer_and_current_revision(store):
    saved = store.knowledge.db.write(**entry())
    client = TestClient(create_app(store))
    url = f"/api/knowledge/{saved['id']}"
    body = {"expected_revision": 1}
    assert client.request("DELETE", url, json=body).status_code == 403
    assert client.request("DELETE", url, json=body, headers={"x-casebook-viewer": "1", "origin": "https://other.test"}).status_code == 403
    assert client.request("DELETE", url, json={"expected_revision": 0}, headers={"x-casebook-viewer": "1"}).status_code == 409
    assert client.request("DELETE", url, json=body, headers={"x-casebook-viewer": "1"}).status_code == 200
    assert client.request("DELETE", url, json=body, headers={"x-casebook-viewer": "1"}).status_code == 404
