"""Proveedores: registro (internos y plugins), esquema de opciones, OpenSearch directo, CloudWatch con AWS,
proveedor faltante y fuentes de credenciales de la base."""
import json

import anyio
import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook import config, credentials, doctor, providers, systems
from casebook.backends.errors import BackendError, SessionExpired
from casebook.config import Config, DbCredentials
from casebook.events import Recorder
from casebook.providers import Field, cloudwatch
from casebook.providers import opensearch as os_providers
from casebook.providers.base import Provider
from casebook.server import build
from casebook.store import Store


def load(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return config.load(path)


# --- registro y esquema ---------------------------------------------------------------------------------------


def test_builtin_providers():
    assert providers.names("opensearch") == ["dashboards-proxy", "direct"]
    assert providers.names("logs") == ["aws"]
    assert providers.get("sql", "mysql-ssh").auth == "external"
    assert providers.get("logs", "nada") is None


class FakeLogs(Provider):
    kind, name, auth = "logs", "fake", "none"
    fields = (Field("endpoint", "Endpoint", required=True),)


def test_plugin_providers_come_from_entry_points(monkeypatch):
    class EP:
        name, value = "fake", f"{__name__}:FakeLogs"

        def load(self):
            return FakeLogs

    class Broken:
        name, value = "roto", "no.existe:Nada"

        def load(self):
            raise ImportError("no existe")

    monkeypatch.setattr(providers, "entry_points", lambda group: [EP(), Broken()])
    monkeypatch.setattr(providers, "_registry", None)
    assert providers.get("logs", "fake") is FakeLogs  # el roto no impide cargar el resto
    assert "fake" in providers.names("logs")


def test_validate_options():
    cls = os_providers.Direct
    assert providers.validate(cls, {"url": "https://x"}, "e") == {
        "url": "https://x", "auth": "basic", "ssh_host": None, "verify_tls": True}
    with pytest.raises(providers.ProviderError, match="falta `url`"):
        providers.validate(cls, {}, "e")
    with pytest.raises(providers.ProviderError, match="desconocida.*clave"):
        providers.validate(cls, {"url": "https://x", "clave": 1}, "e")
    with pytest.raises(providers.ProviderError, match="basic, api-key, none"):
        providers.validate(cls, {"url": "https://x", "auth": "token"}, "e")
    with pytest.raises(providers.ProviderError, match="verdadero o falso"):
        providers.validate(cls, {"url": "https://x", "verify_tls": "si"}, "e")


def test_config_errors_name_the_source(tmp_path):
    with pytest.raises(config.SettingError, match="sources.opensearch.dev: falta `tunnel_port`"):
        load(tmp_path, '[opensearch.envs.dev]\ndomain = "x"\n')
    with pytest.raises(config.SettingError, match="sources.cloudwatch: opción desconocida"):
        load(tmp_path, '[cloudwatch]\nprovider = "aws"\nperfil = "x"\n')
    with pytest.raises(config.SettingError, match="sources.mysql.credentials.kind"):
        load(tmp_path, '[db.credentials]\nkind = "vault"\n')


# --- proveedor faltante ---------------------------------------------------------------------------------------


MISSING = '[opensearch.envs.logs]\nprovider = "elastic-cloud"\ncloud_id = "abc"\n'


def test_missing_provider_keeps_the_source_disabled(tmp_path):
    cfg = load(tmp_path, MISSING)
    system = systems.opensearch(cfg, "logs")
    assert system.auth == "missing" and not system.browser
    assert cfg.opensearch["logs"].options == {"cloud_id": "abc"}  # sin validar: no se sabe su esquema
    (check,) = doctor.check_system(cfg, system)
    assert check.status == doctor.FAIL and "«elastic-cloud»" in check.detail and "dashboards-proxy" in check.detail


def test_missing_provider_in_tools_is_a_clear_error(tmp_path):
    cfg = load(tmp_path, MISSING)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, None, cfg)
    with pytest.raises(ToolError, match="no está instalado"):
        anyio.run(lambda: mcp.call_tool("os_count", {"purpose": "prueba", "index": "a", "env": "logs"}))
    store.close()


# --- OpenSearch directo ---------------------------------------------------------------------------------------


DIRECT = '[opensearch.envs.lab]\nprovider = "direct"\nurl = "https://search.lab:9200"\nauth = "{auth}"\n'


@pytest.fixture
def direct(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.headers.get("authorization") in (None, "Basic Ym9iOm1hbA=="):  # bob:mal
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"cluster_name": "lab", "version": {"number": "2.11.0"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(os_providers.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), auth=kw.get("auth")))
    secrets = {}
    monkeypatch.setattr(credentials, "get_secret", lambda system: secrets.get(system.id))

    def make(auth="basic"):
        cfg = load(tmp_path, DIRECT.format(auth=auth))
        return cfg, systems.opensearch(cfg, "lab"), secrets, seen

    return make


def test_direct_basic_auth(direct):
    cfg, system, secrets, seen = direct("basic")
    assert system.auth == "secret" and system.id == "os-lab"
    client = systems.provider(cfg, system).client()
    with pytest.raises(SessionExpired, match="login os-lab"):
        anyio.run(client.request, "GET", "_cat/indices")
    secrets["os-lab"] = {"user": "bob", "password": "bien"}
    assert anyio.run(client.request, "GET", "_count", {"query": {"match_all": {}}})["cluster_name"] == "lab"
    assert str(seen[-1].url) == "https://search.lab:9200/_count" and json.loads(seen[-1].content) == {"query": {"match_all": {}}}
    secrets["os-lab"] = {"user": "bob", "password": "mal"}
    with pytest.raises(SessionExpired, match="rechazó las credenciales"):
        anyio.run(client.request, "GET", "_count")


def test_direct_api_key_and_doctor(direct):
    cfg, system, secrets, seen = direct("api-key")
    provider = systems.provider(cfg, system)
    assert [f.key for f in provider.secret_fields()] == ["api_key"]
    secrets["os-lab"] = {"api_key": "k1"}
    (check,) = provider.doctor_checks()
    assert check.status == doctor.OK and "OpenSearch 2.11.0" in check.detail
    assert seen[-1].headers["authorization"] == "ApiKey k1"


def test_direct_without_auth_needs_no_login(direct):
    cfg, system, _, _ = direct("none")
    assert system.auth == "none"


def test_cli_login_saves_a_secret(direct, monkeypatch):
    from casebook import cli

    cfg, *_ = direct("basic")
    saved = {}
    monkeypatch.setattr(cli.config, "load", lambda *a: cfg)
    monkeypatch.setattr(cli.credentials, "set_secret", lambda system, values: saved.update({system.id: values}))
    monkeypatch.setattr("builtins.input", lambda prompt="": "bob")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "bien")
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    assert cli.main(["login", "os-lab"]) == 0
    assert saved == {"os-lab": {"user": "bob", "password": "bien"}}


# --- CloudWatch con AWS ---------------------------------------------------------------------------------------


class FakeAwsLogs:
    """Las llamadas de boto3 que usa el proveedor, con respuestas fijas."""

    def __init__(self):
        self.calls = []
        self.polls = [{"status": "Running"}, {
            "status": "Complete",
            "results": [[{"field": "@timestamp", "value": "2026-10-14 10:00:00.000"},
                         {"field": "@message", "value": "ok accessToken=ya29.abcdefghijklmnop"}, {"field": "@ptr", "value": "P1"}]],
            "statistics": {"recordsMatched": 1, "recordsScanned": 10, "bytesScanned": 2e9},
        }]

    def describe_log_groups(self, **kw):
        self.calls.append(("describe", kw))
        now_ms = 1_800_000_000_000
        return {"logGroups": [
            {"logGroupName": "/app/api", "storedBytes": 70e9, "creationTime": now_ms - 70 * 86_400_000,
             "retentionInDays": 7},
            {"logGroupName": "/otro/x", "storedBytes": 1},
        ]}

    def start_query(self, **kw):
        self.calls.append(("start", kw))
        return {"queryId": "q-123456789"}

    def get_query_results(self, queryId):
        self.calls.append(("get", queryId))
        return self.polls.pop(0)

    def stop_query(self, queryId):
        self.calls.append(("stop", queryId))

    def get_log_record(self, logRecordPointer):
        return {"logRecord": {"@log": "123:/app/api", "@logStream": "s1", "@timestamp": "2026-10-14 10:00:00.000"}}

    def get_log_events(self, **kw):
        self.calls.append(("events", kw))
        if kw.get("startFromHead"):
            return {"events": [{"timestamp": 2, "message": "en el momento"}, {"timestamp": 3, "message": "después"}]}
        return {"events": [{"timestamp": 1, "message": "antes"}, {"timestamp": 2, "message": "en el momento"}]}


AWS = '[cloudwatch]\nprovider = "aws"\nprofile = "lab"\nlog_group_prefixes = ["/app/"]\n'


def test_aws_logs_client(monkeypatch):
    fake = FakeAwsLogs()
    monkeypatch.setattr(cloudwatch.time, "time", lambda: 1_800_000_000)
    client = cloudwatch.AwsLogs({"log_group_prefixes": ["/app/"], "price_per_gb": 0.005}, client=fake)
    assert [g.name for g in anyio.run(client.log_groups, None)] == ["/app/api"]
    # 70 GB en 7 días de retención: una ventana de 1 día son ~10 GB.
    est = anyio.run(client.estimate, ["/app/api"], "2026-10-13T00:00:00Z", "2026-10-14T00:00:00Z")
    assert round(est.bytes / 1e9) == 10 and round(est.cost_usd, 3) == 0.05
    with pytest.raises(BackendError, match="no está permitido"):
        anyio.run(client.start_query, ["/otro/x"], "fields @message", "2026-10-13T00:00:00Z", "2026-10-14T00:00:00Z")
    context = anyio.run(client.surrounding, "q", "P1", 2)
    assert [e["message"] for e in context] == ["antes", "en el momento", "después"]


def test_aws_login_is_external(tmp_path):
    cfg = load(tmp_path, AWS)
    system = systems.cloudwatch(cfg)
    assert system.auth == "external" and system.check == "cloudwatch (aws)"
    assert "aws sso login --profile lab" in systems.provider(cfg, system).login_hint()
    assert "cw" not in [s.id for s in systems.browser_systems(cfg)]


def test_cw_query_end_to_end_with_aws(tmp_path, monkeypatch):
    from casebook.tools import cloudwatch as cw_tools

    cfg = load(tmp_path, AWS)
    fake = FakeAwsLogs()
    monkeypatch.setattr(cw_tools, "POLL_SECONDS", 0)
    monkeypatch.setattr(cloudwatch.time, "time", lambda: 1_800_000_000)
    monkeypatch.setattr(cloudwatch.Aws, "client", lambda self: cloudwatch.AwsLogs(cfg.cw_options, client=fake))
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    mcp = build(rec, None, cfg)

    async def go():
        args = {"purpose": "prueba", "log_group": "/app/api", "query": "fields @message", "start": "-1h"}
        return json.loads((await mcp.call_tool("cw_query", args)).content[0].text)

    out = anyio.run(go)
    assert out["status"] == "COMPLETE" and out["external_id"] == "q-123456789" and out["records_matched"] == 1
    assert out["cost_usd"] == pytest.approx(0.01) and "ya29" not in json.dumps(out)
    start = next(kw for name, kw in fake.calls if name == "start")
    assert start["logGroupNames"] == ["/app/api"] and start["endTime"] - start["startTime"] == 3600
    store.close()


# --- credenciales de la base ----------------------------------------------------------------------------------


def test_db_credentials_from_env(tmp_path, monkeypatch):
    cfg = load(tmp_path, '[db.credentials]\nkind = "env"\nuser_var = "DB_U"\npassword_var = "DB_P"\nhint = "pídelas"\n')
    source = cfg.mysql["default"].credentials
    assert credentials.external_db_login(source) is None
    monkeypatch.setenv("DB_U", "ana")
    monkeypatch.setenv("DB_P", "x")
    assert credentials.get_db_login(source) == ("ana", "x")
    assert credentials.db_login_source(source) == "las variables DB_U y DB_P"


def test_db_credentials_from_another_file(tmp_path):
    creds = tmp_path / "db.env"
    creds.write_text("export MY_USER=ana\nexport MY_PASS='s3 cr3t'\n")
    source = DbCredentials(kind="env-file", path=str(creds), user_var="MY_USER", password_var="MY_PASS")
    assert credentials.get_db_login(source) == ("ana", "s3 cr3t")
    assert credentials.describe_source(source) == str(creds)
