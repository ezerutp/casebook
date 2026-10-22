"""Packs (fase 5): zip, build, export, init, conocimiento semilla, plugins, skills y fuentes a mano."""
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from casebook import config, packs, skills, sources, systems
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import PACK, no_pack

ACME = '[pack]\nname = "acme"\nversion = "{version}"\n\n[sources.opensearch.logs]\nprovider = "direct"\nurl = "https://s.acme:9200"\n'
HEADERS = {"x-casebook-viewer": "1"}


def make_pack(root, version="1.0.0", extra=""):
    (root / "skills" / "acme-triage").mkdir(parents=True)
    (root / "skills" / "acme-triage" / "SKILL.md").write_text("---\nname: acme-triage\ndescription: x\n---\nhola\n")
    (root / "knowledge").mkdir()
    (root / "knowledge" / "tablas.json").write_text(json.dumps([{
        "source": "mysql", "environment": "prod/app", "entity": "users", "kind": "schema", "title": "Usuarios",
        "content": "id, email", "topics": ["usuarios"]}]))
    (root / "pack.toml").write_text(ACME.format(version=version) + extra)
    return root


def zip_with(tmp_path, files: dict[str, bytes], name="p.zip", symlink: str | None = None):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for arc, data in files.items():
            zf.writestr(arc, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return path


@pytest.mark.parametrize(("files", "symlink", "error"), [
    ({"../evil/pack.toml": b"x"}, None, "ruta no permitida"),
    ({"/abs/pack.toml": b"x"}, None, "ruta no permitida"),
    ({"pack.toml": ACME.format(version="1").encode()}, "skills/link", "symlinks"),
    ({"pack.toml": ACME.format(version="1").encode(), "tool.exe": b"MZ"}, None, "tipo de archivo"),
    ({"pack.toml": ACME.format(version="1").encode(), "skills/x/notas.md": b"x"}, None, "falta SKILL.md"),
    ({"pack.toml": ACME.format(version="1").encode(), "knowledge/a.json": b"{"}, None, "JSON"),
])
def test_dangerous_or_broken_zips_are_rejected(tmp_path, files, symlink, error):
    with pytest.raises(packs.PackError, match=error):
        packs.install(zip_with(tmp_path, files, symlink=symlink))
    assert (config.PACKS_DIR / "active").read_text().strip() == "acme"


def test_plugin_path_must_stay_inside_plugins(tmp_path):
    extra = '\n[[requires.plugins]]\nname = "x"\nprovides = ["x"]\npath = "../../x"\n'
    with pytest.raises(packs.PackError, match="dentro de plugins"):
        packs.install(make_pack(tmp_path / "acme", extra=extra))


def test_requires_newer_app_is_rejected(tmp_path):
    root = make_pack(tmp_path / "acme")
    (root / "pack.toml").write_text((root / "pack.toml").read_text().replace(
        'version = "1.0.0"', 'version = "1.0.0"\nrequires = ">=99.0"'))
    with pytest.raises(packs.PackError, match="actualiza la app"):
        packs.install(root)


def test_build_and_install_roundtrip_with_plugin_and_knowledge(tmp_path, monkeypatch):
    repo = tmp_path / "acme-repo"
    make_pack(repo / "pack", extra='\n[[requires.plugins]]\nname = "acme-logs"\nprovides = ["acme"]\n'
                                   'path = "plugins/acme-logs"\n')
    (repo / "plugins" / "acme-logs").mkdir(parents=True)
    (repo / "plugins" / "acme-logs" / "pyproject.toml").write_text('[project]\nname = "acme-logs"\n')
    zipped = packs.build(repo, tmp_path / "acme.zip")
    names = zipfile.ZipFile(zipped).namelist()
    assert "pack/pack.toml" in names and "plugins/acme-logs/pyproject.toml" in names

    pip_calls, summaries, out = [], [], []
    monkeypatch.setattr(packs, "install_plugin", lambda spec, out=None: pip_calls.append(spec))
    pack = packs.install(zipped, confirm=lambda text: summaries.append(text) or True,
                         confirm_plugin=lambda what: True, out=out.append)
    assert (pack.name, pack.version) == ("acme", "1.0.0")
    assert "Plugin:       acme-logs → acme: falta instalarlo" in summaries[0]
    assert pip_calls == [str(config.PACKS_DIR / "acme" / "plugins" / "acme-logs")]
    installed = config.PACKS_DIR / "acme"
    assert (installed / "skills" / "acme-triage" / "SKILL.md").exists()
    assert not any(p.stat().st_mode & 0o111 for p in installed.rglob("*") if p.is_file())
    assert any("1 entradas nuevas" in line for line in out)
    db = Store(config.load().db_path).knowledge.db
    assert db.search("usuarios")["entries"][0]["status"] == "pending"
    assert packs.seed_knowledge(installed) == 0  # la segunda vez no pisa nada


def test_plugins_are_not_installed_without_consent(tmp_path, monkeypatch):
    root = make_pack(tmp_path / "acme", extra='\n[[requires.plugins]]\nname = "x"\nprovides = ["x"]\npip = "x-plugin"\n')
    monkeypatch.setattr(packs, "install_plugin", lambda *a, **k: pytest.fail("no se pidió permiso"))
    out = []
    packs.install(root, out=out.append)
    assert any("sin instalar" in line and "x-plugin" in line for line in out)


def test_older_pack_needs_confirmation_and_changes_are_listed(tmp_path):
    packs.install(make_pack(tmp_path / "v2", version="2.0.0"))
    old = make_pack(tmp_path / "v1", version="1.0.0")
    with pytest.raises(packs.PackError, match="más viejo"):
        packs.install(old)
    newer = make_pack(tmp_path / "v3", version="3.0.0", extra='\n[slack]\nchannel_id = "C3"\n')
    (newer / "skills" / "acme-triage" / "SKILL.md").write_text("---\nname: acme-triage\n---\ncambió\n")
    seen = []
    packs.install(newer, confirm=lambda text: seen.append(text) or True)
    assert "(instalado: 2.0.0)" in seen[0] and "pack.toml cambió" in seen[0] and "skill actualizada: acme-triage" in seen[0]
    with pytest.raises(packs.PackError, match="cancelado"):
        packs.install(make_pack(tmp_path / "v4", version="4.0.0"), confirm=lambda text: False)
    assert packs.active().version == "3.0.0"


def test_installed_copies_of_skills_are_refreshed(tmp_path, monkeypatch):
    user = tmp_path / "claude-skills"
    monkeypatch.setattr(skills, "user_skills_dir", lambda: user)
    packs.install(make_pack(tmp_path / "a"))
    (user / "acme-triage").mkdir(parents=True)
    (user / "acme-triage" / "SKILL.md").write_text("---\nname: acme-triage\n---\nvieja\n")
    newer = make_pack(tmp_path / "b", version="1.1.0")
    (newer / "skills" / "acme-triage" / "SKILL.md").write_text("---\nname: acme-triage\n---\nnueva\n")
    out = []
    packs.install(newer, out=out.append)
    assert "nueva" in (user / "acme-triage" / "SKILL.md").read_text() and any("acme-triage" in o for o in out)


def test_init_creates_an_installable_pack(tmp_path):
    path = packs.init(tmp_path / "nuevo", "nuevo")
    assert packs.install(path.parent).name == "nuevo"
    assert [s.id for s in systems.all_systems(config.load(tmp_path / "x.toml"))] == ["os-prod"]


def test_export_roundtrip_keeps_sources_and_skills_without_personal_settings(tmp_path, monkeypatch):
    user = tmp_path / "config.toml"
    user.write_text('[viewer]\nport = 9999\n\n[slack]\nwatch = true\n\n[sources.opensearch.lab]\nprovider = "direct"\n'
                    'url = "https://lab:9200"\nhint = "línea 1\\nlínea 2"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", user)
    zipped = packs.export(tmp_path / "out.zip", "equipo", "0.2.0")
    names = zipfile.ZipFile(zipped).namelist()
    assert "pack/skills/acme-investigation/SKILL.md" in names
    text = zipfile.ZipFile(zipped).read("pack/pack.toml").decode()
    assert 'name = "equipo"' in text and "port = 9999" not in text and "watch = " not in text
    no_pack()
    pack = packs.install(zipped)
    cfg = config.load(tmp_path / "vacio.toml")
    assert pack.name == "equipo" and {"prod", "prod-archive", "lab"} <= set(cfg.opensearch)
    assert cfg.opensearch["lab"].hint == "línea 1\nlínea 2"


def test_pack_remove(tmp_path):
    packs.install(make_pack(tmp_path / "acme"))
    packs.remove("acme")
    # El pack del test se llama igual que el de los fixtures: lo reemplazó, así que no queda ninguno.
    assert config.active_pack() is None and packs.installed() == []


# --- fuentes a mano -------------------------------------------------------------------------------------------


def test_add_test_and_remove_a_source(tmp_path):
    path = tmp_path / "config.toml"
    system = sources.add("opensearch", "lab", "direct", {"url": "https://lab:9200", "auth": "none",
                                                          "verify_tls": "no", "hint": "logs-*"}, path)
    assert (system.id, system.auth) == ("os-lab", "none")
    cfg = config.load(path)
    assert cfg.opensearch["lab"].options["verify_tls"] is False and cfg.opensearch["lab"].hint == "logs-*"
    assert {s["id"]: s["origin"] for s in sources.listing(cfg)}["os-lab"] == "config"
    with pytest.raises(config.SettingError, match="falta `url`"):
        sources.add("opensearch", "otro", "direct", {}, path)
    with pytest.raises(config.SettingError, match="viene del pack"):
        sources.remove(cfg, "os")
    assert sources.remove(cfg, "os-lab") == "quitada" and "lab" not in config.load(path).opensearch


def test_overriding_a_pack_source_and_going_back(tmp_path):
    path = tmp_path / "config.toml"
    sources.add("cloudwatch", None, "aws", {"profile": "soporte", "log_group_prefixes": "/ecs/, /app/"}, path)
    cfg = config.load(path)
    assert cfg.cw_provider == "aws" and cfg.cw_options["log_group_prefixes"] == ["/ecs/", "/app/"]
    assert {s["id"]: s["origin"] for s in sources.listing(cfg)}["cw"] == "pack+config"
    assert sources.remove(cfg, "cw") == "quitados tus cambios; queda la del pack"
    assert config.load(path).cw_provider == "aws"


def test_sources_api(tmp_path):
    path = tmp_path / "config.toml"
    cfg = config.load(path)
    client = TestClient(create_app(Store(tmp_path / "s.db"), cfg))
    data = client.get("/api/sources").json()
    assert data["pack"]["name"] == "Acme" and [s["id"] for s in data["sources"]] == ["os", "os-archive", "cw", "db"]
    direct = next(p for p in data["catalog"]["opensearch"]["providers"] if p["name"] == "direct")
    assert [f["key"] for f in direct["fields"]] == ["url", "auth", "ssh_host", "verify_tls"]
    body = {"kind": "opensearch", "env": "lab", "provider": "direct", "values": {"url": "https://lab:9200"}}
    assert client.post("/api/sources", json=body).status_code == 403  # sin el header del visor
    added = client.post("/api/sources", json=body, headers=HEADERS)
    assert added.status_code == 200 and added.json()["added"] == "os-lab" and "lab" in cfg.opensearch
    bad = client.post("/api/sources", json={**body, "values": {}}, headers=HEADERS)
    assert bad.status_code == 422 and "url" in bad.json()["detail"]
    assert client.delete("/api/sources/os", headers=HEADERS).status_code == 409
    assert client.delete("/api/sources/os-lab", headers=HEADERS).json()["result"] == "quitada"


def test_cli_source_add_never_touches_the_real_config(tmp_path, monkeypatch, capsys):
    from casebook import cli

    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    real_load = config.load
    monkeypatch.setattr(cli.config, "load", lambda *a, **k: real_load(path))
    code = cli.main(["source", "add", "mysql", "--provider", "mysql-ssh", "--set", "ssh_host=bastion",
                     "--set", "host=replica", "--set", "database=app"])
    assert code == 0 and 'host = "replica"' in path.read_text()
    assert cli.main(["source", "list"]) == 0 and "db" in capsys.readouterr().out
    assert cli.main(["source", "remove", "nada"]) == 1


def test_build_rejects_an_invalid_pack(tmp_path):
    root = tmp_path / "x"
    root.mkdir()
    (root / "pack.toml").write_text('[pack]\nname = "x"\nversion = "1"\n\n[sources.cloudwatch]\nprovider = "aws"\nnada = 1\n')
    with pytest.raises(packs.PackError, match="opción desconocida"):
        packs.build(root, tmp_path / "x.zip")
