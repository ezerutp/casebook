import os
import tomllib

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from casebook import config
from casebook.backends import mysql
from casebook.config import Config
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme

HEADERS = {"x-casebook-viewer": "1"}


@pytest.fixture
def path(tmp_path):
    return tmp_path / "config.toml"


def bump_mtime(p):
    """Algunos sistemas de archivos tienen mtime de baja resolución: se fuerza uno distinto."""
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_save_writes_only_changes_and_keeps_other_keys(path):
    path.write_text('# comentario\ndb_path = "/tmp/s.db"\n[opensearch.envs.prod]\ndomain = "d"\ntunnel_port = 9\n'
                    '[db]\nssh_host = "otro"\nmax_limit = 300\n')
    config.save({"cloudwatch.max_cost_usd": "0.5", "db.default_limit": 50, "db.max_limit": 5000}, path)

    data = tomllib.loads(path.read_text())
    assert data["cloudwatch"] == {"max_cost_usd": 0.5}
    assert data["db"] == {"ssh_host": "otro", "default_limit": 50}  # max_limit volvió al de fábrica: se borra
    assert data["db_path"] == "/tmp/s.db" and data["opensearch"]["envs"]["prod"]["tunnel_port"] == 9
    assert "# comentario" in (path.parent / "config.toml.bak").read_text()

    cfg = config.load(path)
    assert (cfg.cw_max_cost_usd, cfg.db.default_limit, cfg.db.max_limit, cfg.db.ssh_host) == (0.5, 50, 5000, "otro")
    assert cfg.opensearch["prod"].tunnel_port == 9


@pytest.mark.parametrize(
    "values, message",
    [
        ({"cloudwatch.max_cost_usd": 50}, "entre 0.01 y 5"),
        ({"db.max_limit": 2.5}, "entero"),
        ({"viewer.host": "0.0.0.0"}, "uno de"),
        ({"viewer.auto_open": "sí"}, "verdadero o falso"),
        ({"jira.base_url": "http://x"}, "https://"),
        ({"db.default_limit": 900, "db.max_limit": 800}, "no puede superar"),
        ({"db.password": "x"}, "desconocido"),
    ],
)
def test_save_rejects_invalid_values(path, values, message):
    with pytest.raises(config.SettingError, match=message):
        config.save(values, path)
    assert not path.exists()


def test_load_tolerates_old_values_out_of_range(path):
    path.write_text("[db]\nmax_execution_ms = 300000\n")  # escrito a mano antes de que existieran los rangos
    assert config.load(path).db.max_execution_ms == 300_000


def test_refresh_updates_in_place_except_host_and_port(path):
    cfg = config.load(path)  # todavía no existe el archivo
    db = cfg.db
    assert not config.refresh(cfg)
    config.save({"db.max_execution_ms": 20_000, "viewer.port": 9999, "jira.base_url": "https://x.net/browse"}, path)
    assert config.refresh(cfg)
    assert cfg.db is db and db.max_execution_ms == 20_000  # los clientes con referencia a cfg.db lo ven
    assert cfg.jira_base_url == "https://x.net/browse/"
    assert cfg.viewer_port == 8765  # el visor ya escucha: se toma al reiniciar
    assert not config.refresh(cfg)  # sin cambios en el archivo

    path.write_text("esto no es toml [")
    bump_mtime(path)
    assert not config.refresh(cfg) and db.max_execution_ms == 20_000  # un archivo roto no pisa nada


def test_config_without_file_never_refreshes():
    assert not config.refresh(acme())


def test_dumps_roundtrip():
    data = {"a": 1, "s": 'con "comillas"', "t": {"b": True, "f": 0.25, "n": {"x-y": [1, 2]}}}
    assert tomllib.loads(config.dumps(data)) == data


# --- API -----------------------------------------------------------------------------------------------------


def test_settings_api(path):
    cfg = config.load(path)
    client = TestClient(create_app(Store(path.parent / "s.db"), cfg))
    data = client.get("/api/settings").json()
    port = next(s for s in data["settings"] if s["key"] == "viewer.port")
    assert port == {**port, "value": 8765, "running": 8765, "default": 8765, "restart": True}
    assert data["exists"] is False and data["readonly"]["opensearch"][0]["env"] == "prod"

    assert client.put("/api/settings", json={"values": {"viewer.port": 9000}}).status_code == 403
    bad = client.put("/api/settings", json={"values": {"cloudwatch.max_cost_usd": 99}}, headers=HEADERS)
    assert bad.status_code == 422 and "entre" in bad.json()["detail"]

    ok = client.put("/api/settings", json={"values": {"viewer.port": 9000, "cloudwatch.max_cost_usd": 1}},
                    headers=HEADERS).json()
    by_key = {s["key"]: s for s in ok["settings"]}
    assert by_key["viewer.port"]["value"] == 9000 and by_key["viewer.port"]["running"] == 8765
    assert by_key["cloudwatch.max_cost_usd"]["running"] == 1  # este proceso ya lo usa
    assert cfg.cw_max_cost_usd == 1


def test_settings_api_without_config_file_is_read_only(tmp_path):
    client = TestClient(create_app(Store(tmp_path / "s.db"), acme()))
    assert client.get("/api/settings").json()["path"] is None
    assert client.put("/api/settings", json={"values": {}}, headers=HEADERS).status_code == 409


def test_settings_api_reports_broken_file(path):
    cfg = config.load(path)
    path.write_text("[db\n")
    client = TestClient(create_app(Store(path.parent / "s.db"), cfg))
    assert "no se pudo leer" in client.get("/api/settings").json()["error"]


# --- las tools toman los cambios sin reiniciar ---------------------------------------------------------------


def test_db_select_uses_limits_saved_after_start(path, tmp_path, monkeypatch):
    calls = []

    def fake_query(self, sql, kind, allow_expensive=False):
        calls.append(sql)
        return {"columns": ["x"], "rows": [[1]], "plan": None}

    monkeypatch.setattr(mysql.MySQLClient, "query", fake_query)
    cfg = config.load(path)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, None, cfg)

    def call(args):
        async def go():
            return await mcp.call_tool("db_select", {"purpose": "prueba", **args})

        return anyio.run(go)

    call({"sql": "SELECT 1"})
    config.save({"db.default_limit": 7, "db.max_limit": 10}, path)
    call({"sql": "SELECT 2"})
    assert calls == ["SELECT 1 LIMIT 100", "SELECT 2 LIMIT 7"]
    with pytest.raises(ToolError, match="supera el máximo de 10"):
        call({"sql": "SELECT 3 LIMIT 50"})
    assert len(calls) == 2
    store.close()
