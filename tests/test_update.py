import subprocess
import sys
from pathlib import Path

import pytest

from casebook import native_host, update


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def install(tmp_path, monkeypatch):
    """Un remoto con main, un clon (el repo de origen) y el install-info de una instalación local."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.email", "t@example.com")
    git(seed, "config", "user.name", "t")
    (seed / "pyproject.toml").write_text("[project]\nname = 'casebook'\n")
    (seed / "marca.txt").write_text("main v1\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "v1")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(remote)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True)

    data = tmp_path / "data"
    data.mkdir()
    info = data / "install-info"
    venv = Path(sys.prefix)
    info.write_text(f"mode=copia\nvenv={venv}\nsource={repo}\ninstalled_at=antes\n")
    monkeypatch.setattr(native_host, "INSTALL_INFO", info)
    monkeypatch.setattr(native_host, "DATA_DIR", data)

    installed = []

    def fake_pip(python, source, out):
        installed.append((source / "marca.txt").read_text())

    monkeypatch.setattr(update, "_pip_install", fake_pip)
    monkeypatch.setattr(update, "_installed_version", lambda python: "0.0.0")
    return {"seed": seed, "repo": repo, "info": info, "installed": installed}


def test_update_installs_remote_main_without_touching_the_working_copy(install):
    seed, repo = install["seed"], install["repo"]
    # main avanza en el remoto; en el repo local hay otra rama con cambios sin commit.
    (seed / "marca.txt").write_text("main v2\n")
    git(seed, "commit", "-qam", "v2")
    git(seed, "push", "-q", str(repo.parent / "remote.git"), "main")
    git(repo, "checkout", "-q", "-b", "mi-rama")
    (repo / "marca.txt").write_text("cambio local\n")

    assert update.run(dev=False, out=lambda *_: None) == 0

    assert install["installed"] == ["main v2\n"]
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "mi-rama"
    assert (repo / "marca.txt").read_text() == "cambio local\n"
    assert "built_from=origin/main@" in install["info"].read_text()


def test_update_dev_installs_the_working_copy_as_is(install):
    repo = install["repo"]
    (repo / "marca.txt").write_text("cambio local\n")

    assert update.run(dev=True, out=lambda *_: None) == 0

    assert install["installed"] == ["cambio local\n"]
    assert "built_from=working-tree:main@" in install["info"].read_text()
    assert "+cambios" in install["info"].read_text()


def test_dev_mode_needs_no_update(install):
    install["info"].write_text(f"mode=dev\nsource={install['repo']}\n")
    lines = []
    assert update.run(dev=False, out=lines.append) == 0
    assert install["installed"] == []
    assert "modo desarrollo" in lines[0]


def test_missing_repo_is_a_clear_error(install, tmp_path):
    install["info"].write_text(f"mode=copia\nvenv={sys.prefix}\nsource={tmp_path / 'no-existe'}\n")
    with pytest.raises(update.UpdateError, match="no encuentro el repo"):
        update.run(dev=False, out=lambda *_: None)
