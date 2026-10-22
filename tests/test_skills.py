import pytest
from fastapi.testclient import TestClient

from casebook import skills
from casebook.store import Store
from casebook.viewer.app import create_app

SKILL = "---\nname: mi-skill\ndescription: Hace algo útil.\n---\n\n# Mi skill\n\nPaso 1.\n"


@pytest.fixture
def dirs(tmp_path):
    repo = tmp_path / "repo" / "skills"
    (repo / "mi-skill").mkdir(parents=True)
    (repo / "mi-skill" / "SKILL.md").write_text(SKILL)
    (repo / "sin-skill-md").mkdir()  # no es una skill
    user = tmp_path / "home" / ".claude" / "skills"
    return repo, user


def state(repo, user):
    return skills.listing(repo, user)["repo"][0]


def test_install_links_and_uninstall_removes_only_the_link(dirs):
    repo, user = dirs
    listing = skills.listing(repo, user)
    assert [s["name"] for s in listing["repo"]] == ["mi-skill"] and listing["others"] == []
    s = listing["repo"][0]
    assert (s["status"], s["can_install"], s["description"], s["files"]) == ("missing", True, "Hace algo útil.", ["SKILL.md"])

    assert skills.install("mi-skill", repo, user)["status"] == "linked"
    assert (user / "mi-skill").is_symlink() and (user / "mi-skill").resolve() == (repo / "mi-skill").resolve()
    assert skills.install("mi-skill", repo, user)["status"] == "linked"  # idempotente

    assert skills.uninstall("mi-skill", repo, user)["status"] == "missing"
    assert not (user / "mi-skill").exists() and (repo / "mi-skill" / "SKILL.md").exists()


def test_broken_link_into_repo_is_repaired(dirs, tmp_path):
    repo, user = dirs
    user.mkdir(parents=True)
    (user / "mi-skill").symlink_to(repo.parent / "skills-viejo" / "mi-skill")  # el repo se reorganizó
    assert state(repo, user)["status"] == "broken"
    assert skills.install("mi-skill", repo, user)["status"] == "linked"


def test_foreign_link_and_copy_are_never_touched(dirs, tmp_path):
    repo, user = dirs
    user.mkdir(parents=True)
    other = tmp_path / "otra" / "mi-skill"
    other.mkdir(parents=True)
    (other / "SKILL.md").write_text(SKILL)
    (user / "mi-skill").symlink_to(other)
    s = state(repo, user)
    assert (s["status"], s["can_install"], s["can_uninstall"]) == ("foreign_link", False, False)
    for action in (skills.install, skills.uninstall):
        with pytest.raises(skills.SkillError, match="no lo toco"):
            action("mi-skill", repo, user)
    assert (user / "mi-skill").resolve() == other.resolve()

    (user / "mi-skill").unlink()
    (user / "mi-skill").mkdir()
    (user / "mi-skill" / "SKILL.md").write_text(SKILL.replace("Paso 1.", "Paso viejo."))
    s = state(repo, user)
    assert (s["status"], s["same_as_repo"], s["can_install"]) == ("copy", False, False)
    diff = skills.detail("mi-skill", repo, user)["diff"]
    assert "-Paso viejo." in diff and "+Paso 1." in diff
    with pytest.raises(skills.SkillError):
        skills.install("mi-skill", repo, user)
    assert (user / "mi-skill" / "SKILL.md").read_text().endswith("Paso viejo.\n")


def test_other_user_skills_are_listed_read_only(dirs, tmp_path):
    repo, user = dirs
    (user / "daily").mkdir(parents=True)
    (user / "daily" / "SKILL.md").write_text("---\nname: daily\ndescription: 'La daily.'\n---\nTexto\n")
    (user / ".trash" / "vieja").mkdir(parents=True)
    (user / "synced").mkdir()
    others = skills.listing(repo, user)["others"]
    assert [(o["name"], o["description"]) for o in others] == [("daily", "La daily.")]
    assert skills.detail("daily", repo, user)["skill_md"].endswith("Texto\n")
    with pytest.raises(skills.SkillError, match="de este repo"):
        skills.uninstall("daily", repo, user)
    for bad in ("../etc", "x/y", ".trash"):
        with pytest.raises(skills.SkillError):
            skills.detail(bad, repo, user)


def test_skills_api(dirs, tmp_path, monkeypatch):
    repo, user = dirs
    monkeypatch.setattr(skills, "pack_skills_dir", lambda: repo)
    monkeypatch.setattr(skills, "user_skills_dir", lambda: user)
    client = TestClient(create_app(Store(tmp_path / "s.db")))
    data = client.get("/api/skills").json()
    assert data["repo"][0]["status"] == "missing" and "session_start" in data["server_instructions"]
    assert client.get("/api/skills/mi-skill").json()["skill_md"] == SKILL
    assert client.get("/api/skills/nada").status_code == 404

    assert client.post("/api/skills/mi-skill/install").status_code == 403  # sin el header del visor
    assert client.post("/api/skills/mi-skill/install", headers={"x-casebook-viewer": "1"}).json()["status"] == "linked"
    assert client.post("/api/skills/mi-skill/uninstall", headers={"x-casebook-viewer": "1"}).json()["status"] == "missing"
