import datetime as dt
import decimal
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook import guards
from casebook.backends import mysql
from casebook.backends.errors import BackendError
from casebook.config import Config, Database, MySqlSource
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from demo_pack import acme


def approval_timeout(cfg, seconds):
    cfg.approval_timeout_s = seconds
    return cfg

# --- guards de SQL --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind", "expected"),
    [
        ("SELECT id FROM `lead` WHERE email = 'a@b.com'", "select", "SELECT id FROM `lead` WHERE email = 'a@b.com' LIMIT 100"),
        ("select * from t limit 10;", "select", "SELECT * FROM t LIMIT 10"),
        ("SELECT 1 UNION SELECT 2", "select", "SELECT 1 UNION SELECT 2 LIMIT 100"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "select", "WITH x AS (SELECT 1) SELECT * FROM x LIMIT 100"),
        ("SHOW TABLES LIKE 'lead%'", "show", "SHOW TABLES LIKE 'lead%'"),
        ("DESCRIBE `lead`", "describe", "DESCRIBE `lead`"),
        ("EXPLAIN SELECT * FROM t", "describe", "EXPLAIN SELECT * FROM t"),
    ],
)
def test_check_sql_accepts(sql, kind, expected):
    assert guards.check_sql(sql) == (kind, expected)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("UPDATE t SET a = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("/* hola */ DROP TABLE t", "DROP"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("select 1; delete from t", "exactamente una"),
        ("SELECT * FROM t FOR UPDATE", "FOR UPDATE"),
        ("SELECT * FROM t LOCK IN SHARE MODE", "FOR UPDATE"),
        ("SELECT * FROM (SELECT * FROM t FOR UPDATE) x", "FOR UPDATE"),
        ("SELECT a INTO @v FROM t", "INTO"),
        ("SELECT * INTO OUTFILE '/tmp/x' FROM t", "no se pudo validar"),
        ("SELECT SLEEP(10)", "SLEEP"),
        ("SELECT BENCHMARK(1000000, MD5('a'))", "BENCHMARK"),
        ("SELECT LOAD_FILE('/etc/passwd')", "LOAD_FILE"),
        ("EXPLAIN ANALYZE SELECT * FROM t", "ANALYZE"),
        ("SELECT * FROM t LIMIT 100000", "supera el máximo"),
        ("SET SESSION TRANSACTION READ WRITE", "no"),
    ],
)
def test_check_sql_rejects(sql, reason):
    with pytest.raises(guards.GuardError, match=reason):
        guards.check_sql(sql)


def test_check_sql_custom_limit_bounds():
    assert guards.check_sql("SELECT 1", limit=5)[1] == "SELECT 1 LIMIT 5"
    with pytest.raises(guards.GuardError):
        guards.check_sql("SELECT 1", limit=0)


# --- plan y tipos ---------------------------------------------------------------------------------------------

PLAN_COLS = ["id", "select_type", "table", "type", "key", "rows", "Extra"]


def test_check_plan():
    db = Database(full_scan_max_rows=1000, plan_max_rows=10_000)
    mysql.check_plan(PLAN_COLS, [(1, "SIMPLE", "lead", "ref", "idx_email", 3, None)], db)
    mysql.check_plan(PLAN_COLS, [(1, "SIMPLE", "small", "ALL", None, 500, None)], db)
    with pytest.raises(mysql.PlanTooExpensive, match="full scan de `lead`"):
        mysql.check_plan(PLAN_COLS, [(1, "SIMPLE", "lead", "ALL", None, 5000, None)], db)
    with pytest.raises(mysql.PlanTooExpensive, match="examinar"):
        mysql.check_plan(PLAN_COLS, [(1, "SIMPLE", "a", "ref", "k", 200, None), (1, "SIMPLE", "b", "ref", "k", 100, None)], db)


def test_to_json():
    assert mysql.to_json(dt.datetime(2026, 10, 14, 10, 0)) == "2026-10-14 10:00:00"
    assert mysql.to_json(decimal.Decimal("12.50")) == "12.50"
    assert mysql.to_json(2**60) == str(2**60) and mysql.to_json(42) == 42
    assert mysql.to_json(b"hola") == "hola" and mysql.to_json(b"\xff\x00") == "0xff00"


def test_client_refuses_writer_endpoint():
    with pytest.raises(BackendError, match="reader"):
        mysql.MySQLClient(MySqlSource(host="db.cluster-abc.example.com", reader_host_pattern="cluster-ro"))
    with pytest.raises(BackendError, match="no hay base configurada"):
        mysql.MySQLClient(MySqlSource())
    mysql.MySQLClient(MySqlSource(host="replica.example.com"))  # sin patrón: alcanzan las otras capas


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def execute(self, sql):
        self.conn.executed.append(sql)
        if sql == "SELECT @@innodb_read_only, @@super_read_only":
            self._rows = [self.conn.read_only]

    def fetchone(self):
        return self._rows[0]


class FakeConn:
    def __init__(self, read_only):
        self.read_only = read_only
        self.executed = []
        self.closed = False
        self.open = True

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


# (innodb_read_only, super_read_only): Aurora reader, réplica con super_read_only, instancia que acepta escrituras.
@pytest.mark.parametrize("read_only", [(1, 0), (0, 1), (0, 0)])
def test_connect_verifies_reader_and_sets_session(monkeypatch, read_only):
    conn = FakeConn(read_only)
    monkeypatch.setattr(mysql.credentials, "get_db_login", lambda *a: ("u", "p"))
    monkeypatch.setattr(mysql.SshTunnel, "ensure", lambda self: 4321)
    monkeypatch.setattr(mysql.pymysql, "connect", lambda **kw: conn)
    client = mysql.MySQLClient(MySqlSource(host="replica.example.com"))
    if any(read_only):
        assert client._connect() is conn
        assert conn.executed == ["SELECT @@innodb_read_only, @@super_read_only", "SET SESSION MAX_EXECUTION_TIME = 15000"]
    else:
        with pytest.raises(BackendError, match="acepta escrituras"):
            client._connect()
        assert conn.closed and len(conn.executed) == 1


# --- tools ----------------------------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path, monkeypatch):
    calls = []
    responses = {}

    def fake_query(self, sql, kind, allow_expensive=False):
        calls.append((sql, kind) if not allow_expensive else (sql, kind, "aprobada"))
        for prefix, resp in responses.items():
            if sql.startswith(prefix):
                if isinstance(resp, Exception):
                    if allow_expensive and "aprobada" in responses:
                        return responses["aprobada"]
                    raise resp
                return resp
        raise AssertionError(f"sin respuesta para {sql}")

    monkeypatch.setattr(mysql.MySQLClient, "query", fake_query)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, "http://v", approval_timeout(acme(), 1.0))
    yield mcp, rec, store, calls, responses
    store.close()


def call(mcp, name, args):
    async def go():
        return json.loads((await mcp.call_tool(name, args)).content[0].text)

    return anyio.run(go)


def test_db_select_end_to_end(server):
    mcp, rec, store, calls, responses = server
    responses["SELECT"] = {
        "columns": ["id", "email", "notes"],
        "rows": [[1, "a@b.com", "token=ya29.abcdefghijklmnopq"], [2, "otro@gmail.com", "ok"]],
        "plan": {"columns": PLAN_COLS, "rows": [[1, "SIMPLE", "lead", "ref", "idx", 2, None]]},
    }
    out = call(mcp, "db_select", {"purpose": "prueba", "sql": "SELECT id, email, notes FROM `lead` WHERE email IN ('a@b.com', 'otro@gmail.com')", "limit": 2})

    assert calls == [("SELECT id, email, notes FROM `lead` WHERE email IN ('a@b.com', 'otro@gmail.com') LIMIT 2", "select")]
    assert out["row_count"] == 2 and "puede haber más" in out["note"]
    text = json.dumps(out)
    assert "ya29" not in text and "a@b.com" in text and "otro@gmail.com" in text
    finished = store.events(rec.session_id)[-1]
    assert finished["type"] == "query.finished" and out["query_id"] == finished["query_id"]
    assert finished["payload"]["result"]["tables"][0]["name"] == "EXPLAIN"


def test_db_select_rejects_writes_without_touching_db(server):
    mcp, rec, store, calls, _ = server
    for sql in ["DELETE FROM `lead`", "UPDATE `lead` SET email = 'x'", "SELECT SLEEP(5)"]:
        with pytest.raises(ToolError):
            anyio.run(lambda: mcp.call_tool("db_select", {"purpose": "prueba", "sql": sql}))
    assert calls == []
    rejected = [e for e in store.events(rec.session_id) if e["type"] == "query.rejected"]
    assert [r["payload"]["params"]["sql"] for r in rejected] == ["DELETE FROM `lead`", "UPDATE `lead` SET email = 'x'", "SELECT SLEEP(5)"]
    assert "query.started" not in [e["type"] for e in store.events(rec.session_id)]


def test_db_select_expensive_plan_is_recorded_with_plan(server):
    mcp, rec, store, _, responses = server
    exc = mysql.PlanTooExpensive("el plan hace un full scan de `lead`")
    exc.plan = {"columns": PLAN_COLS, "rows": [[1, "SIMPLE", "lead", "ALL", None, 90_000_000, None]]}
    responses["SELECT"] = exc
    with pytest.raises(ToolError, match="full scan.*No se aprobó"):
        anyio.run(lambda: mcp.call_tool("db_select", {"purpose": "prueba", "sql": "SELECT * FROM `lead` WHERE notes LIKE '%x%'"}))
    types = [e["type"] for e in store.events(rec.session_id)]
    assert types[-4:] == ["query.progress", "query.approval_requested", "query.progress", "query.failed"]
    plan_msg = [e for e in store.events(rec.session_id) if e["type"] == "query.progress"][0]["payload"]["message"]
    assert "90000000" in plan_msg


def test_db_select_expensive_plan_approved_runs_with_plan_check_skipped(server):
    from test_cloudwatch import decide_when_requested

    mcp, rec, store, calls, responses = server
    responses["SELECT"] = mysql.PlanTooExpensive("el plan hace un full scan de `lead`")
    responses["aprobada"] = {"columns": ["n"], "rows": [[0]], "plan": {"columns": PLAN_COLS, "rows": []}}
    decide_when_requested(store, True)
    out = call(mcp, "db_select", {"purpose": "prueba", "sql": "SELECT COUNT(*) AS n FROM `lead` WHERE first_name = 'x'"})
    assert out["rows"] == [[0]]
    assert calls[-1][2] == "aprobada"
    assert "query.approved" in [e["type"] for e in store.events(rec.session_id)]


def test_db_describe_and_tables(server):
    mcp, _, _, calls, responses = server
    responses["SHOW COLUMNS"] = {"columns": ["Field", "Type", "Null", "Key"], "rows": [["id", "bigint", "NO", "PRI"], ["email", "varchar(255)", "YES", "MUL"]], "plan": None}
    responses["SHOW INDEX"] = {
        "columns": ["Table", "Non_unique", "Key_name", "Seq_in_index", "Column_name"],
        "rows": [["lead", 0, "PRIMARY", 1, "id"], ["lead", 1, "idx_email", 1, "email"]],
        "plan": None,
    }
    responses["SHOW TABLES"] = {"columns": ["Tables_in_acme (lead%)"], "rows": [["lead"], ["lead_stage"]], "plan": None}

    out = call(mcp, "db_describe", {"purpose": "prueba", "table": "lead"})
    assert out["columns"] == {"id": "bigint", "email": "varchar(255)"}
    assert out["indexes"] == {"PRIMARY": ["id"], "idx_email": ["email"]}
    assert call(mcp, "db_tables", {"purpose": "prueba", "pattern": "lead%"})["tables"] == ["lead", "lead_stage"]
    for bad in [("db_describe", {"purpose": "prueba", "table": "lead; DROP TABLE x"}), ("db_tables", {"purpose": "prueba", "pattern": "x' OR '1"})]:
        with pytest.raises(ToolError):
            anyio.run(lambda: mcp.call_tool(*bad))


def test_query_runs_inside_read_only_transaction(monkeypatch):
    executed = []
    client = mysql.MySQLClient(MySqlSource(host="replica.example.com"))

    def fake_execute(sql):
        executed.append(sql)
        if sql.startswith("EXPLAIN"):
            return PLAN_COLS, [[1, "SIMPLE", "lead", "const", "PRIMARY", 1, None]]
        return ["lead_id"], [[1]]

    monkeypatch.setattr(client, "_execute", fake_execute)
    client._conn = object()
    out = client.query("SELECT lead_id FROM `lead` WHERE lead_id = 1 LIMIT 1", "select")
    assert executed == [
        "EXPLAIN SELECT lead_id FROM `lead` WHERE lead_id = 1 LIMIT 1",
        "START TRANSACTION READ ONLY",
        "SELECT lead_id FROM `lead` WHERE lead_id = 1 LIMIT 1",
        "ROLLBACK",
    ]
    assert out["rows"] == [[1]]
