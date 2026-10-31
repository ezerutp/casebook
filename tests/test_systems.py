"""Registro de sistemas: los entornos de OpenSearch salen de config.toml, sin listas fijas en el código."""
import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from casebook import config, native_host, systems
from casebook.backends import opensearch as backend
from casebook.config import Config
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from demo_pack import acme

STAGING = """
[opensearch.envs.staging]
domain = "vpc-staging.example.es.amazonaws.com"
tunnel_port = 9200
description = "índices de prueba"
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(STAGING)
    return config.load(path)


def test_factory_systems_keep_their_ids_and_keyring_entries():
    by_id = {s.id: s for s in systems.all_systems(acme())}
    assert list(by_id) == ["os", "os-archive", "cw", "db"]
    assert by_id["os"].keyring == "opensearch-prod" and by_id["os-archive"].keyring == "opensearch-prod-archive"
    assert by_id["cw"].keyring == "cloudwatch-aws" and by_id["db"].keyring == "mysql-prod-reader"
    assert [s.id for s in systems.browser_systems(acme())] == ["os", "os-archive"]  # aws: perfil, no navegador


def test_new_env_in_config_is_a_new_system(cfg):
    staging = systems.get(cfg, "os-staging")
    assert (staging.kind, staging.env, staging.keyring, staging.check) == (
        "opensearch", "staging", "opensearch-staging", "opensearch:staging")
    assert staging.label == "OpenSearch staging" and staging.description == "índices de prueba"
    assert cfg.opensearch["staging"].ssh_host == "os_tunnel_staging"
    assert [s.check for s in systems.all_systems(cfg)] == [
        "opensearch:prod", "opensearch:prod-archive", "opensearch:staging", "cloudwatch (aws)", "mysql (reader)"]


def test_redefined_factory_env_keeps_its_id(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[opensearch.envs.prod]\ndomain = "otro.es.amazonaws.com"\ntunnel_port = 9300\n')
    prod = systems.opensearch(config.load(path), "prod")
    assert (prod.id, prod.label) == ("os", "OpenSearch prod")


@pytest.mark.parametrize("extra", ['system = "os"', 'system = "cw"'])
def test_repeated_or_reserved_ids_are_rejected(tmp_path, extra):
    path = tmp_path / "config.toml"
    path.write_text(STAGING + extra + "\n")
    with pytest.raises(config.SettingError, match="ids de sistema"):
        config.load(path)


def test_extension_targets_include_new_env(cfg):
    targets = {t["system"]: t for t in native_host.handle(cfg, {"type": "targets"})["targets"]}
    assert set(targets) == {"os", "os-archive", "os-staging"}
    assert targets["os-staging"]["app_host"] == "vpc-staging.example.es.amazonaws.com"
    labels = native_host.handle(cfg, {"type": "status"})["labels"]
    assert labels["os-staging"] == "OpenSearch staging"


def test_unknown_system_in_cli_lists_the_options(capsys, monkeypatch, cfg):
    from casebook import cli

    monkeypatch.setattr(cli.config, "load", lambda *a: cfg)
    assert cli.main(["login", "os-nada"]) == 1
    assert "os-staging" in capsys.readouterr().err


@pytest.fixture
def tools(tmp_path, monkeypatch, cfg):
    calls = []

    async def fake_request(self, method, path, body=None, timeout=60):
        calls.append(self.env)
        return {"count": 7}

    monkeypatch.setattr(backend.OpenSearchClient, "request", fake_request)
    store = Store(tmp_path / "t.db")
    rec = Recorder(store)
    rec.start_session("SUP-1")
    yield build(rec, "http://v", cfg), calls
    store.close()


def test_tools_take_any_configured_env(tools):
    mcp, calls = tools

    async def go(args):
        return json.loads((await mcp.call_tool("os_count", {"purpose": "prueba", "index": "a", **args})).content[0].text)

    assert anyio.run(go, {"env": "staging"})["count"] == 7
    anyio.run(go, {"env": "prod"})
    assert calls == ["staging", "prod"]
    with pytest.raises(ToolError, match="opciones: prod, prod-archive, staging"):
        anyio.run(go, {"env": "nada"})
    with pytest.raises(ToolError, match=r"varios entornos de OpenSearch \(prod, prod-archive, staging\): indica `env`"):
        anyio.run(go, {})  # con varios, no se elige el primero
    assert calls == ["staging", "prod"]


def test_tool_description_lists_the_envs(tools):
    mcp, _ = tools
    tool = next(t for t in anyio.run(mcp.list_tools) if t.name == "os_search")
    assert '"staging" (índices de prueba)' in tool.description
    assert tool.input_schema["properties"]["env"]["default"] is None
