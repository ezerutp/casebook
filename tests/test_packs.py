"""Config en capas: valores de fábrica neutros, el pack activo y config.toml del usuario."""
import json
import shutil

import anyio
import pytest

from casebook import config, doctor, packs, systems
from casebook.events import Recorder
from casebook.server import build
from casebook.store import Store
from demo_pack import PACK, no_pack


@pytest.fixture
def path(tmp_path):
    return tmp_path / "config.toml"


def test_without_pack_there_are_no_sources(path):
    no_pack()
    cfg = config.load(path)
    assert cfg.pack is None and cfg.opensearch == {} and not cfg.cw_provider and not cfg.db.provider
    assert systems.all_systems(cfg) == [] and cfg.slack_channel_id == ""
    assert (cfg.db.host, cfg.cw_options, cfg.doctor_aliases) == ("", {}, [])


def test_without_pack_doctor_says_what_is_missing(path, monkeypatch):
    no_pack()
    monkeypatch.setattr(doctor, "_shell_aliases", lambda names: pytest.fail("sin alias del equipo no se revisan"))
    checks = doctor.check_prereqs(config.load(path))
    (missing,) = [c for c in checks if c.key == "sources"]
    assert missing.status == doctor.WARN and "pack install" in missing.detail
    assert not any(c.key in ("db-credentials", "aliases") for c in checks)


def test_without_pack_only_session_and_knowledge_tools(path, tmp_path):
    no_pack()
    store = Store(tmp_path / "t.db")
    names = {t.name for t in anyio.run(build(Recorder(store), None, config.load(path)).list_tools)}
    assert "session_start" in names and "knowledge_search" in names
    assert not {n for n in names if n.startswith(("os_", "cw_", "db_"))}
    store.close()


def test_active_pack_is_the_base_layer(path):
    cfg = config.load(path)
    assert (cfg.pack.name, cfg.pack.display_name) == ("acme", "Acme")
    assert [s.id for s in systems.all_systems(cfg)] == ["os", "os-archive", "cw", "db"]
    assert cfg.db.reader_host_pattern == "cluster-ro" and cfg.db.credentials.fix == "db-login"
    assert cfg.doctor_aliases == ["db-login", "os-prod", "os-prod-archive"]


def test_user_config_overrides_the_pack_key_by_key(path):
    path.write_text('[sources.opensearch.prod]\nssh_host = "mi_tunel"\n\n[slack]\nchannel_id = "COTRO"\n')
    cfg = config.load(path)
    prod = cfg.opensearch["prod"]
    assert prod.ssh_host == "mi_tunel" and prod.tunnel_port == 9123 and prod.label == "OpenSearch prod"
    assert cfg.slack_channel_id == "COTRO" and cfg.slack_channel_name == "soporte"


def test_legacy_format_is_still_read(path):
    path.write_text(
        '[opensearch]\nmax_size = 500\n\n[opensearch.envs.prod]\ndomain = "otro.example.com"\ntunnel_port = 9300\n\n'
        '[cloudwatch]\nmax_cost_usd = 1.0\nregion = "eu-west-1"\n\n[db]\nssh_host = "salto"\ndefault_limit = 7\n\n'
        '[db.credentials]\nhint = "pídelas"\n'
    )
    cfg = config.load(path)
    assert cfg.os_max_size == 500 and cfg.opensearch["prod"].domain == "otro.example.com"
    assert cfg.opensearch["prod"].system == "os"  # el resto sale del pack
    assert cfg.cw_options["region"] == "eu-west-1" and cfg.cw_provider == "aws" and cfg.cw_max_cost_usd == 1.0
    assert cfg.db.ssh_host == "salto" and cfg.db.default_limit == 7 and cfg.db.database == "acme"
    assert cfg.db.credentials.hint == "pídelas" and cfg.db.credentials.kind == "env-file"


def test_changing_the_provider_replaces_the_source(path):
    path.write_text('[sources.cloudwatch]\nprovider = "aws"\nprofile = "soporte"\n')
    cfg = config.load(path)
    assert cfg.cw_provider == "aws" and cfg.cw_options["profile"] == "soporte" and "api" not in cfg.cw_options


def test_save_drops_values_equal_to_the_pack(path, tmp_path):
    custom = tmp_path / "pack"
    custom.mkdir()
    (custom / "pack.toml").write_text(PACK.read_text().replace('channel_id = "C0ACME"', 'channel_id = "CPACK"'))
    pack = custom / "pack.toml"
    config.save({"slack.channel_id": "COTRO", "db.default_limit": 50}, path, pack)
    assert "COTRO" in path.read_text()
    config.save({"slack.channel_id": "CPACK"}, path, pack)  # igual al del pack: se borra del archivo
    assert "channel_id" not in path.read_text() and config.load(path, pack).slack_channel_id == "CPACK"


def test_refresh_sees_pack_changes(path):
    cfg = config.load(path)
    pack_file = config.PACKS_DIR / "acme" / "pack.toml"
    pack_file.write_text(pack_file.read_text().replace('channel_id = "C0ACME"', 'channel_id = "CNUEVO"'))
    assert config.refresh(cfg) and cfg.slack_channel_id == "CNUEVO"
    (config.PACKS_DIR / "active").unlink()
    assert config.refresh(cfg) and cfg.opensearch == {} and cfg.pack is None


def test_pack_install_copies_validates_and_activates(tmp_path, capsys):
    from casebook import cli

    no_pack()
    src = tmp_path / "mi-pack"
    src.mkdir()
    (src / "pack.toml").write_text(
        '[pack]\nname = "acme"\nversion = "0.1.0"\ndisplay_name = "Acme"\n\n'
        '[sources.opensearch.logs]\nprovider = "direct"\nurl = "https://search.acme:9200"\n'
    )
    (src / ".git").mkdir()
    assert cli.main(["pack", "install", "--yes", str(src)]) == 0
    assert (config.PACKS_DIR / "active").read_text().strip() == "acme"
    assert (config.PACKS_DIR / "acme" / "pack.toml").exists() and not (config.PACKS_DIR / "acme" / ".git").exists()
    assert [s.id for s in systems.all_systems(config.load(tmp_path / "config.toml"))] == ["os-logs"]
    assert cli.main(["pack", "show"]) == 0
    assert "Acme 0.1.0" in capsys.readouterr().out


@pytest.mark.parametrize(("toml", "error"), [
    ('[sources.opensearch.x]\nprovider = "direct"\n', "falta `url`"),
    ('[pack]\nname = "Mal Nombre"\nversion = "1"\n', "name tiene que ser"),
    ('[pack]\nname = "ok"\n', "falta \\[pack\\] version"),
])
def test_invalid_packs_are_rejected(tmp_path, toml, error):
    src = tmp_path / "p"
    src.mkdir()
    head = '[pack]\nname = "ok"\nversion = "1"\n\n' if not toml.startswith("[pack]") else ""
    (src / "pack.toml").write_text(head + toml)
    with pytest.raises(packs.PackError, match=error):
        packs.install(src)
    assert (config.PACKS_DIR / "active").read_text().strip() == "acme"  # el activo no cambia


def test_demo_pack_in_repo_is_valid(tmp_path):
    shutil.rmtree(config.PACKS_DIR)
    pack = packs.install(PACK.parent)
    assert (pack.name, pack.version) == ("acme", "1.0.0")
    assert json.dumps(sorted(config.load(tmp_path / "x.toml").opensearch)) == '["prod", "prod-archive"]'
