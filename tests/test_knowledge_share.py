"""Conocimiento compartido en una rama git: pull, push y la configuración del pack."""
import json
import subprocess

import pytest

from casebook import cli, config, knowledge_share, packs
from casebook.knowledge import KnowledgeError, KnowledgeStore
from casebook.knowledge_share import Remote
from casebook.store import Store

PACK = '[pack]\nname = "acme"\nversion = "2.0.0"\n{extra}\n[sources.opensearch.logs]\nprovider = "direct"\nurl = "https://s.acme:9200"\n'


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    for key, value in (("GIT_AUTHOR_NAME", "Test"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                       ("GIT_COMMITTER_NAME", "Test"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(key, value)


@pytest.fixture
def target(tmp_path):
    repo = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
    return Remote(str(repo), "knowledge")


def db(tmp_path, name):
    return KnowledgeStore(tmp_path / name / "knowledge.db")


def fill(store, n, start=0):
    for i in range(start, start + n):
        store.write(source="mysql", environment="prod/acme", entity=f"tabla_{i}", kind="relation", title=f"Tabla {i}",
                    content=f"Relación {i}", topics=["ventas"], evidence={"summary": "Evidencia"}, reason="Descubrimiento")


def test_push_creates_the_branch_and_pull_only_adds_what_is_missing(tmp_path, target):
    mine, friend = db(tmp_path, "yo"), db(tmp_path, "amigo")
    with pytest.raises(KnowledgeError, match="todavía no existe"):
        knowledge_share.pull(friend, target)
    fill(mine, 30)
    asked = []
    assert knowledge_share.push(mine, target, lambda text: asked.append(text) or True, interactive=False) == {
        "new": 30, "updated": 0, "pushed": True, "total": 30}
    assert "30 nuevas" in asked[0]
    assert knowledge_share.pull(friend, target)["new"] == 30
    fill(mine, 3, start=30)
    assert knowledge_share.push(mine, target, interactive=False)["new"] == 3
    assert knowledge_share.push(mine, target, interactive=False)["pushed"] is False
    result = knowledge_share.pull(friend, target)
    assert (result["new"], result["existing"], result["files"]) == (3, 30, 1)
    files = subprocess.run(["git", "-C", target.repo, "ls-tree", "--name-only", "knowledge"], capture_output=True, text=True)
    assert files.stdout.split() == ["knowledge.json"]  # la rama solo trae conocimiento


def test_push_merges_with_what_others_pushed_and_can_be_cancelled(tmp_path, target):
    mine, friend = db(tmp_path, "yo"), db(tmp_path, "amigo")
    fill(mine, 2)
    fill(friend, 2, start=1)  # tabla_1 la tienen los dos, con el mismo contenido
    with pytest.raises(KnowledgeError, match="cancelado"):
        knowledge_share.push(mine, target, lambda text: False, interactive=False)
    knowledge_share.push(mine, target, interactive=False)
    assert knowledge_share.push(friend, target, interactive=False) == {"new": 1, "updated": 0, "pushed": True, "total": 3}
    assert knowledge_share.pull(mine, target)["new"] == 1
    friend.edit(friend.search("tabla_1")["entries"][0]["id"], expected_revision=1, title="Tabla 1",
                content="Corregida", topics=[], reason="Corrección")
    assert knowledge_share.push(friend, target, interactive=False)["updated"] == 1
    assert knowledge_share.pull(mine, target)["updated"] == 1
    assert mine.read(mine.search("tabla_1")["entries"][0]["id"])["content"] == "Corregida"


def test_push_retries_when_someone_else_pushed_first(tmp_path, target, monkeypatch):
    mine, friend = db(tmp_path, "yo"), db(tmp_path, "amigo")
    fill(mine, 1)
    fill(friend, 1, start=5)
    knowledge_share.push(mine, target, interactive=False)
    fill(mine, 1, start=1)
    real, raced = knowledge_share._git, []

    def racing(*args, **kw):
        if args[0] == "push" and not raced:
            raced.append(True)
            knowledge_share.push(friend, target, interactive=False)  # alguien sube justo antes
        return real(*args, **kw)

    monkeypatch.setattr(knowledge_share, "_git", racing)
    assert knowledge_share.push(mine, target, interactive=False)["pushed"]
    assert knowledge_share.pull(db(tmp_path, "tercero"), target)["new"] == 3


def test_pull_ignores_bad_files_and_rejects_bad_remotes(tmp_path, target):
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "--quiet", target.repo, str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "--orphan", "knowledge"], check=True)
    (work / "roto.json").write_text("{")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m", "x"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "--quiet", "origin", "knowledge"], check=True)
    with pytest.raises(KnowledgeError, match="roto.json"):
        knowledge_share.pull(db(tmp_path, "yo"), target)
    for bad in (Remote("--upload-pack=x", "knowledge"), Remote("ext::sh -c x", "knowledge"),
                Remote(target.repo, "-x"), Remote(target.repo, "a..b")):
        with pytest.raises(KnowledgeError, match="inválido"):
            knowledge_share.check(bad)


def test_pack_declares_the_branch_and_the_repo(tmp_path):
    def install(extra):
        root = tmp_path / f"pack{len(list(tmp_path.iterdir()))}"
        root.mkdir()
        (root / "pack.toml").write_text(PACK.format(extra=extra))
        return root

    for extra in ('repo = "file:///tmp/x"', '[pack.knowledge]\nrepo = "/tmp/x"', 'knowledge = "rama"'):
        with pytest.raises(packs.PackError, match="pack"):
            packs.open_bundle(str(install(extra)))
    cfg = config.load(pack=install('repo = "git@example.com:acme/pack.git"\n[pack.knowledge]\n') / "pack.toml")
    assert knowledge_share.remote(cfg) == Remote("git@example.com:acme/pack.git", "knowledge")
    cfg = config.load(pack=install('[pack.knowledge]\nrepo = "ssh://git@example.com/k.git"\nbranch = "conocimiento"\n') / "pack.toml")
    assert knowledge_share.remote(cfg) == Remote("ssh://git@example.com/k.git", "conocimiento")
    assert knowledge_share.remote(config.load(pack=install("") / "pack.toml")) is None
    with pytest.raises(KnowledgeError, match="no dice el repo"):
        knowledge_share.remote(config.load(pack=install("[pack.knowledge]\n") / "pack.toml"))


def test_pack_install_pulls_and_survives_a_failing_remote(tmp_path, target, monkeypatch):
    fill(db(tmp_path, "yo"), 2)
    knowledge_share.push(db(tmp_path, "yo"), target, interactive=False)
    root = tmp_path / "acme"
    root.mkdir()
    (root / "pack.toml").write_text(PACK.format(extra="[pack.knowledge]\n"))
    monkeypatch.setattr(knowledge_share, "remote", lambda cfg=None: target)
    out = []
    packs.install(root, out=out.append)
    assert any("Conocimiento del equipo: 2 nuevas" in line for line in out)
    store = Store(config.load().db_path)
    try:
        assert store.knowledge.db.search()["total"] == 2
    finally:
        store.close()
    monkeypatch.setattr(knowledge_share, "remote", lambda cfg=None: Remote(str(tmp_path / "no-existe.git"), "knowledge"))
    out.clear()
    assert packs.install(root, out=out.append).name == "acme"
    assert any("No pude traer el conocimiento" in line for line in out)


def test_pack_update_falls_back_to_the_declared_repo(tmp_path, monkeypatch):
    root = tmp_path / "acme"
    root.mkdir()
    (root / "pack.toml").write_text(PACK.format(extra='repo = "git@example.com:acme/pack.git"'))
    zipped = packs.build(root, tmp_path / "acme.zip")
    packs.install(zipped)
    zipped.unlink()
    seen = []
    monkeypatch.setattr(packs, "install", lambda source, *a, **k: seen.append(source) or packs.active())
    assert cli.main(["pack", "update", "--yes"]) == 0
    assert seen == ["git@example.com:acme/pack.git"]


def test_cli_pull_and_push_with_repo_override(tmp_path, target, capsys):
    store = Store(config.load().db_path)
    fill(store.knowledge.db, 2)
    store.close()
    assert cli.main(["knowledge", "push", "--repo", target.repo, "--yes"]) == 0
    assert "2 nuevas" in capsys.readouterr().out
    assert cli.main(["knowledge", "push", "--repo", target.repo, "--yes"]) == 0
    assert "Nada que subir" in capsys.readouterr().out
    assert cli.main(["knowledge", "pull", "--repo", target.repo]) == 0
    assert "2 ya las tenías" in capsys.readouterr().out
    listing = json.loads(subprocess.run(["git", "-C", target.repo, "show", "knowledge:knowledge.json"],
                                        capture_output=True, text=True, check=True).stdout)
    assert len(listing["entries"]) == 2 and "exported_at" not in listing


def test_pack_seed_does_not_overwrite_nor_restore_deleted_entries(tmp_path):
    root = tmp_path / "acme"
    (root / "knowledge").mkdir(parents=True)
    (root / "pack.toml").write_text(PACK.format(extra=""))
    seed = {"source": "mysql", "environment": "prod/app", "entity": "users", "kind": "schema", "title": "Usuarios",
            "content": "id, email", "topics": ["usuarios"], "status": "verified", "updated_at": "2999-01-01T00:00:00+00:00"}
    (root / "knowledge" / "tablas.json").write_text(json.dumps([seed]))
    out = []
    packs.install(root, out=out.append)
    assert any("1 entradas nuevas" in line for line in out)
    store = Store(config.load().db_path)
    try:
        item = store.knowledge.db.search()["entries"][0]
        assert item["status"] == "pending"  # una semilla no entra como verificada, diga lo que diga
        store.knowledge.db.edit(item["id"], expected_revision=1, title="Usuarios", content="Corregido", topics=[], reason="Local")
        packs.install(root)
        assert store.knowledge.db.read(item["id"])["content"] == "Corregido"
        store.knowledge.db.delete(item["id"], 2)
        packs.install(root)
        assert store.knowledge.db.search()["total"] == 0
    finally:
        store.close()
