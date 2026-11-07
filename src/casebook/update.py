"""Actualiza la instalación local (la copia en ~/.local/share/casebook/venv) sin pasar por install.sh.

- `casebook update`: instala lo que hay en `main` del remoto del repo de origen. Hace `git fetch` y arma el paquete
  desde ese commit en una carpeta temporal: no toca la rama ni los cambios de tu copia de trabajo.
- `casebook update-dev`: instala tu copia de trabajo tal cual está (rama actual y cambios sin commit), para probar
  cambios locales sin pasar a modo --dev.

En modo --dev no hace falta ninguno de los dos: el paquete es editable y los cambios del repo ya aplican.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from . import native_host

BRANCH = "main"


class UpdateError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise UpdateError("no encontré git en el PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        raise UpdateError(f"git {' '.join(args)} falló: {detail[-1] if detail else exc.returncode}") from exc
    return done.stdout.strip()


def _source_repo(info: dict[str, str]) -> Path:
    source = info.get("source")
    if not source:
        raise UpdateError("install-info no dice de qué repo se instaló; corre ./install.sh desde el repo una vez")
    repo = Path(source)
    if not (repo / "pyproject.toml").is_file():
        raise UpdateError(f"no encuentro el repo en {repo} (¿se movió o se borró?); corre ./install.sh desde el "
                          "repo para registrarlo de nuevo")
    return repo


def _installed_version(python: str) -> str:
    try:
        done = subprocess.run([python, "-c", "from importlib.metadata import version; print(version('casebook'))"],
                              capture_output=True, text=True, check=True)
        return done.stdout.strip()
    except subprocess.CalledProcessError:
        return "?"


def _pip_install(python: str, source: Path, out) -> None:
    # Igual que install.sh: primero las dependencias; después el paquete forzado, para que una misma versión con
    # cambios se reinstale.
    for args, what in (([str(source)], "dependencias"), (["--force-reinstall", "--no-deps", str(source)], "paquete")):
        out(f"  instalando {what}…")
        done = subprocess.run([python, "-m", "pip", "install", "--quiet", *args], capture_output=True, text=True)
        if done.returncode:
            detail = (done.stderr or done.stdout).strip().splitlines()
            raise UpdateError(f"falló pip install ({what}): {detail[-1] if detail else done.returncode}")


def _export_ref(repo: Path, ref: str, dest: Path) -> None:
    """Copia el árbol de `ref` a `dest` sin tocar la copia de trabajo."""
    try:
        data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref],
                              capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise UpdateError(f"git archive {ref} falló: {exc.stderr.decode(errors='replace').strip()}") from exc
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _write_info(info: dict[str, str], built_from: str) -> None:
    info = {**info, "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"), "built_from": built_from}
    native_host.INSTALL_INFO.write_text("".join(f"{k}={v}\n" for k, v in info.items()))


def _refresh_extension(venv: Path, out) -> None:
    # La extensión de Chrome se copia desde el paquete: con el paquete nuevo, se vuelve a copiar con su CLI.
    if not (native_host.DATA_DIR / "native-host").exists():
        return
    done = subprocess.run([str(venv / "bin" / "casebook"), "extension", "install", "--no-abrir"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode == 0:
        out("  extensión de Chrome copiada de nuevo (si cambió, recárgala con ⟳ en chrome://extensions)")


def run(dev: bool, out=print) -> int:
    info = native_host._install_info()
    if info.get("mode") == "dev":
        out(f"Estás en modo desarrollo: el paquete es editable desde {info.get('source', 'el repo')} y tus cambios ya "
            f"aplican. Para traer {BRANCH}, haz git pull en el repo.")
        return 0
    repo = _source_repo(info)
    venv = Path(info.get("venv") or sys.prefix)
    python = str(venv / "bin" / "python")
    if not Path(python).is_file():
        raise UpdateError(f"no encuentro el entorno en {venv}; corre ./install.sh desde el repo")
    before = _installed_version(python)

    if dev:
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        commit = _git(repo, "rev-parse", "--short", "HEAD")
        dirty = [line for line in _git(repo, "status", "--porcelain").splitlines() if line]
        changes = f", {len(dirty)} archivo{'s' if len(dirty) != 1 else ''} con cambios sin commit" if dirty else ""
        out(f"Instalando tu copia de trabajo de {repo} ({branch} @ {commit}{changes})")
        _pip_install(python, repo, out)
        built_from = f"working-tree:{branch}@{commit}{'+cambios' if dirty else ''}"
    else:
        remote = f"origin/{BRANCH}"
        out(f"Trayendo {BRANCH} de origin en {repo}…")
        _git(repo, "fetch", "--quiet", "origin", BRANCH)
        commit = _git(repo, "rev-parse", "--short", remote)
        subject = _git(repo, "log", "-1", "--format=%s", remote)
        out(f"Instalando {remote} @ {commit} ({subject}). Tu rama y tus cambios locales no se tocan.")
        with tempfile.TemporaryDirectory(prefix="casebook-update-") as tmp:
            _export_ref(repo, remote, Path(tmp))
            _pip_install(python, Path(tmp), out)
        built_from = f"{remote}@{commit}"

    _write_info(info, built_from)
    _refresh_extension(venv, out)
    after = _installed_version(python)
    out(f"✓ casebook {after} instalado en {venv}" + (f" (antes {before})" if before != after else ""))
    out("Para que se note: reinicia Claude Code (o /mcp → reconectar casebook) y recarga el visor. "
        "`casebook stop all` detiene lo que siga corriendo con la versión anterior.")
    return 0
