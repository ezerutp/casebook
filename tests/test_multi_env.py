"""Varias fuentes del mismo tipo: una base de prod y otra de qa, o dos CloudWatch, cada una con su `env`."""
import json

import anyio
import keyring
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook import config, credentials, doctor, server, sources, systems
from casebook.backends import mysql
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store

QA = """
[sources.mysql.qa]
provider = "mysql-ssh"
ssh_host = "bastion-qa"
host = "acme-qa.cluster-ro-xyz.us-east-1.rds.amazonaws.com"
database = "acme_qa"
environment = "qa"
hint = "las mismas tablas que prod"
"""


@pytest.fixture
def path(tmp_path):
    return tmp_path / "config.toml"


def test_pack_base_and_user_qa_live_side_by_side(path):
    path.write_text(QA)
    cfg = config.load(path)
    assert list(cfg.mysql) == ["default", "qa"]  # la del pack (formato de una sola) y la nueva
    by_id = {s.id: s for s in systems.all_systems(cfg)}
    assert [s for s in by_id if s.startswith("db")] == ["db", "db-qa"]
    assert by_id["db"].keyring == systems.MYSQL_KEYRING  # la de siempre: no hay que volver a iniciar sesión
    assert by_id["db-qa"].keyring == "mysql-qa" and by_id["db-qa"].label == "MySQL qa"
    qa = cfg.mysql["qa"]
    assert qa.credentials.kind == "keyring" and "casebook login db-qa" in qa.credentials.hint
    assert cfg.mysql["default"].credentials.kind == "env-file"  # la del pack no cambia
    assert {s["id"]: s["origin"] for s in sources.listing(cfg)}["db-qa"] == "config"


def test_flat_and_named_in_the_same_table(path):
    path.write_text('[sources.cloudwatch]\nprovider = "aws"\nprofile = "prod"\n\n'
                    '[sources.cloudwatch.qa]\nprovider = "aws"\nprofile = "qa"\nregion = "eu-west-1"\n')
    cfg = config.load(path)
    assert {env: src.options["profile"] for env, src in cfg.cloudwatch.items()} == {"default": "prod", "qa": "qa"}
    qa = systems.cloudwatch(cfg, "qa")
    assert (qa.id, qa.keyring, qa.check) == ("cw-qa", "cloudwatch-aws-qa", "cloudwatch:qa (aws)")
    assert systems.cloudwatch(cfg, "default").id == "cw"
    assert systems.provider(cfg, qa).options["region"] == "eu-west-1"  # cada proveedor ve sus opciones


def test_repeated_ids_across_kinds_are_rejected(path):
    path.write_text(QA + 'system = "cw"\n')
    with pytest.raises(config.SettingError, match="ids de sistema repetidos.*cw"):
        config.load(path)


def test_add_and_remove_a_named_source(path):
    system = sources.add("mysql", "staging", "mysql-ssh", {"ssh_host": "b", "host": "h", "database": "d"}, path)
    assert system.id == "db-staging" and "[sources.mysql.staging]" in path.read_text()
    with pytest.raises(config.SettingError, match="entorno"):
        sources.add("mysql", None, "mysql-ssh", {"ssh_host": "b", "host": "h", "database": "d"}, path)
    with pytest.raises(config.SettingError, match="entorno"):
        sources.add("mysql", "credentials", "mysql-ssh", {"ssh_host": "b", "host": "h", "database": "d"}, path)
    cfg = config.load(path)
    assert sources.remove(cfg, "db-staging") == "quitada" and "staging" not in config.load(path).mysql


def test_each_base_has_its_own_keyring_entry(monkeypatch):
    saved = {}
    monkeypatch.setattr(keyring, "set_password", lambda svc, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(keyring, "get_password", lambda svc, key: saved.get(key))
    credentials.set_db_login("ana", "x", "mysql-qa")
    assert credentials.get_db_login(None, "mysql-qa") == ("ana", "x")
    assert credentials.get_db_login() is None  # la de default sigue vacía
    assert credentials.db_login_source(None, "mysql-qa", "db-qa") == "keyring (casebook login db-qa)"


def test_doctor_checks_each_base(path, monkeypatch):
    path.write_text(QA)
    cfg = config.load(path)
    seen = []
    monkeypatch.setattr(doctor, "check_db", lambda cfg, system: seen.append(system.id) or [])
    for system in systems.all_systems(cfg):
        if system.kind == "mysql":
            doctor.check_system(cfg, system)
    assert seen == ["db", "db-qa"]


def test_instructions_ask_for_env_with_several_bases(path):
    path.write_text(QA)
    text = server.instructions(config.load(path))
    assert "varios entornos de MySQL (default, qa)" in text
    assert "varios entornos" not in server.instructions(config.load(path.with_name("otro.toml")))


# --- tools ----------------------------------------------------------------------------------------------------


@pytest.fixture
def mcp(path, tmp_path, monkeypatch):
    path.write_text(QA)
    cfg = config.load(path)
    hosts = []

    def fake_query(self, sql, kind, allow_expensive=False):
        hosts.append(self.source.host)
        return {"columns": ["Tables"], "rows": [["lead"]], "plan": None}

    monkeypatch.setattr(mysql.MySQLClient, "query", fake_query)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    yield build(rec, None, cfg), hosts, store, rec
    store.close()


def call(mcp, name, args):
    async def go():
        return json.loads((await mcp.call_tool(name, args)).content[0].text)

    return anyio.run(go)


def test_tools_require_env_when_there_are_several_bases(mcp):
    server_, hosts, _, _ = mcp
    with pytest.raises(ToolError, match="varios entornos de MySQL.*indica `env`"):
        anyio.run(lambda: server_.call_tool("db_tables", {"purpose": "p", "pattern": "lead%"}))
    with pytest.raises(ToolError, match="entorno desconocido 'prod'"):
        anyio.run(lambda: server_.call_tool("db_tables", {"purpose": "p", "pattern": "lead%", "env": "prod"}))
    assert hosts == []


def test_tools_go_to_the_chosen_base(mcp):
    server_, hosts, store, rec = mcp
    assert call(server_, "db_tables", {"purpose": "p", "pattern": "lead%", "env": "qa"})["tables"] == ["lead"]
    call(server_, "db_tables", {"purpose": "p", "pattern": "lead%", "env": "default"})
    assert hosts == ["acme-qa.cluster-ro-xyz.us-east-1.rds.amazonaws.com",
                     "acme-prod.cluster-ro-abc123.us-east-1.rds.amazonaws.com"]
    started = [e["payload"]["params"] for e in store.events(rec.session_id) if e["type"] == "query.started"]
    # El conocimiento guarda los esquemas por base: qa no pisa los de prod.
    assert [(p["env"], p["environment"]) for p in started] == [("qa", "qa/acme_qa"), ("default", "prod/acme")]


def test_tool_descriptions_list_the_bases(mcp):
    server_ = mcp[0]
    tools = {t.name: t for t in anyio.run(server_.list_tools)}
    description = tools["db_select"].description
    assert '"default" (base acme)' in description and '"qa" (base acme_qa; tablas: las mismas tablas que prod)' in description
    assert "env" in tools["db_select"].input_schema["properties"]
