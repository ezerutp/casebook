"""Native host de la extensión de Chrome: recibe las cookies de un servicio, las valida y las guarda en el keyring.

Lo lanza Chrome (Native Messaging): un mensaje JSON con prefijo de 4 bytes por stdin y la respuesta por stdout.
Solo acepta la extensión de este paquete. Nunca devuelve ni registra valores de cookies: en el archivo de
estado quedan el resultado de la última validación por sistema y cuándo se conectó la extensión.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import BinaryIO

from .config import DATA_DIR

NAME = "casebook.native_host"
EXTENSION_ID = "cikoklfokekkadpoihbgnjopgjhdkhka"  # fijo por la "key" de chrome-extension/manifest.json
ORIGIN = f"chrome-extension://{EXTENSION_ID}/"
STATUS_PATH = DATA_DIR / "chrome-extension.json"
INSTALL_INFO = DATA_DIR / "install-info"  # lo escribe install.sh: mode=dev|copia y source= (el repo)
EXTENSION_COPY = DATA_DIR / "chrome-extension"  # en la instalación local, la copia que se carga en Chrome
LAUNCHER_PATH = DATA_DIR / "native-host"
MAX_MESSAGE = 1024 * 1024  # Chrome manda hasta 4 GB; una cookie no pasa de unos KB


# --- estado (lo leen login y doctor) -------------------------------------------------------------------------


def read_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _write_status(status: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATUS_PATH)


def _update_status(system: str | None = None, **result) -> None:
    status = read_status()
    status["last_seen"] = time.time()
    if system:
        status.setdefault("systems", {})[system] = {**result, "at": time.time()}
    _write_status(status)


def missing_permissions() -> list[str]:
    """Los dominios de las fuentes para los que la extensión todavía no tiene permiso (lo informa ella misma)."""
    return list(read_status().get("missing_permissions") or [])


def connected() -> bool:
    """La extensión habló con el host alguna vez."""
    return bool(read_status().get("last_seen"))


def last_result(system: str) -> dict | None:
    return (read_status().get("systems") or {}).get(system)


def set_profiles(directories: list[str]) -> None:
    """Perfiles donde se cargó la extensión: `login` abre la página en el primero."""
    status = read_status()
    status["profiles"] = directories
    _write_status(status)


def login_profile() -> str | None:
    profiles = read_status().get("profiles") or []
    return profiles[0] if profiles else None


# --- protocolo -----------------------------------------------------------------------------------------------


def read_message(stream: BinaryIO) -> dict | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    if length > MAX_MESSAGE:
        raise ValueError("mensaje demasiado grande")
    return json.loads(stream.read(length))


def write_message(stream: BinaryIO, message: dict) -> None:
    data = json.dumps(message, ensure_ascii=False).encode()
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def handle(cfg, message: dict) -> dict:
    from . import credentials, login, systems

    kind = message.get("type")
    if kind == "targets":
        _update_status()
        out = []
        for system in systems.browser_systems(cfg):
            target = login.browser_target(cfg, system)
            app_host, cookie_host = target.url.split("/")[2], target.cookie_url.split("/")[2]
            # Los permisos que tiene que tener la extensión para este sistema (se piden desde el popup).
            origins = sorted({f"https://{app_host}/*", f"https://{cookie_host}/*",
                              *(f"https://*.{d.lstrip('.')}/*" for d in target.cookie_domains)})
            out.append({"system": system.id, "app_host": app_host, "cookie_url": target.cookie_url, "origins": origins})
        return {"targets": out}
    if kind == "status":
        # Para el popup: resultados y horas, sin cookies ni datos de la cuenta.
        _update_status()
        results = read_status().get("systems") or {}
        labels = {s.id: s.label for s in systems.browser_systems(cfg)}
        return {"systems": {s: {k: r.get(k) for k in ("ok", "detail", "at")} for s, r in results.items()},
                "labels": labels}
    if kind == "permissions":
        # La extensión informa qué dominios de las fuentes le faltan (sin permiso no puede leer esas sesiones).
        missing = message.get("missing")
        if not isinstance(missing, list) or not all(isinstance(m, str) and len(m) < 300 for m in missing):
            return {"ok": False, "detail": "mensaje inválido"}
        status = read_status()
        status["last_seen"] = time.time()
        status["missing_permissions"] = sorted(set(missing))[:50]
        _write_status(status)
        return {"ok": True, "detail": "anotado"}
    if kind == "cookies":
        system_id, cookie = message.get("system"), message.get("cookie")
        system = next((s for s in systems.browser_systems(cfg) if s.id == system_id), None)
        if system is None or not isinstance(cookie, str) or "=" not in cookie:
            return {"ok": False, "detail": "mensaje inválido"}
        result = login.browser_target(cfg, system).check(cookie)
        if result.ok:
            credentials.set_cookie(system, result.cookie or cookie)
            _update_status(system_id, ok=True, detail="sesión guardada", info=list(result.info))
            return {"ok": True, "detail": "sesión guardada"}
        # Una cookie rechazada no pisa lo guardado: puede ser un login a medio completar.
        _update_status(system_id, ok=False, detail=result.detail)
        return {"ok": False, "detail": result.detail}
    return {"ok": False, "detail": f"tipo de mensaje desconocido: {kind}"}


def main(argv: list[str] | None = None) -> int:
    from . import config

    argv = sys.argv[1:] if argv is None else argv
    # Chrome pasa el origen de quien llama como primer argumento.
    if not argv or argv[0] != ORIGIN:
        print("casebook: este programa lo lanza Chrome para la extensión; no se usa a mano.", file=sys.stderr)
        return 1
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # cualquier print accidental rompería el protocolo
    cfg = config.load()
    while True:
        try:
            message = read_message(stdin)
        except ValueError as exc:
            write_message(stdout, {"ok": False, "detail": str(exc)})
            return 1
        if message is None:
            return 0
        try:
            reply = handle(cfg, message)
        except Exception as exc:  # que la extensión reciba un error y no un host caído; sin detalles con datos
            reply = {"ok": False, "detail": f"error en el host ({type(exc).__name__})"}
        write_message(stdout, reply)


# --- instalación ---------------------------------------------------------------------------------------------


def manifest_dirs() -> list[Path]:
    """Dónde busca Chrome los manifests de native hosts del usuario (las variantes instaladas)."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        browsers = ["Google/Chrome", "Google/Chrome Beta", "Chromium"]
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        browsers = ["google-chrome", "google-chrome-beta", "chromium"]
    dirs = [base / b / "NativeMessagingHosts" for b in browsers if (base / b).is_dir()]
    return dirs or [base / browsers[0] / "NativeMessagingHosts"]


def _install_info() -> dict[str, str]:
    try:
        lines = INSTALL_INFO.read_text().splitlines()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in lines if "=" in line)


def _repo_extension() -> Path | None:
    info = _install_info()
    candidates = [Path(info["source"]) / "chrome-extension"] if info.get("source") else []
    candidates.append(Path(__file__).resolve().parents[2] / "chrome-extension")  # editable desde el repo
    return next((c for c in candidates if (c / "manifest.json").is_file()), None)


def _packaged_extension() -> Path | None:
    """La copia que trae el paquete instalado (force-include en pyproject.toml)."""
    path = Path(__file__).parent / "chrome_extension"
    return path if (path / "manifest.json").is_file() else None


def dev_mode() -> bool:
    mode = _install_info().get("mode")
    return mode == "dev" if mode else _packaged_extension() is None


def extension_dir() -> Path | None:
    """La carpeta que se carga en Chrome. Igual que el paquete: en --dev, chrome-extension/ del repo (los cambios
    aplican con ⟳); en la instalación local, una copia propia, así el repo se puede mover o borrar."""
    if dev_mode():
        return _repo_extension()
    return EXTENSION_COPY if (EXTENSION_COPY / "manifest.json").is_file() else None


def _copy_extension() -> None:
    import shutil

    source = _packaged_extension() or _repo_extension()
    if source is None:
        raise FileNotFoundError("no encontré la extensión en el paquete ni en el repo")
    tmp = EXTENSION_COPY.with_name(EXTENSION_COPY.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(source, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(EXTENSION_COPY, ignore_errors=True)
    tmp.rename(EXTENSION_COPY)


def install() -> list[Path]:
    """Registra el native host para todos los perfiles y, en la instalación local, copia la extensión."""
    if dev_mode():
        import shutil

        shutil.rmtree(EXTENSION_COPY, ignore_errors=True)  # una copia vieja confundiría al cargarla
    else:
        _copy_extension()
    LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_PATH.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m casebook.native_host "$@"\n')
    os.chmod(LAUNCHER_PATH, 0o755)
    manifest = {
        "name": NAME,
        "description": "casebook: guarda en el keyring la sesión que entrega la extensión",
        "path": str(LAUNCHER_PATH),
        "type": "stdio",
        "allowed_origins": [ORIGIN],
    }
    written = []
    for directory in manifest_dirs():
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{NAME}.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n")
        written.append(path)
    return written


def uninstall() -> list[Path]:
    removed = [d / f"{NAME}.json" for d in manifest_dirs() if (d / f"{NAME}.json").exists()]
    for path in removed:
        path.unlink()
    for path in (LAUNCHER_PATH, STATUS_PATH):
        path.unlink(missing_ok=True)
    import shutil

    shutil.rmtree(EXTENSION_COPY, ignore_errors=True)
    return removed


def chrome_profiles() -> list[tuple[str, str]]:
    """(directorio, nombre) de los perfiles de Google Chrome, desde su «Local State»."""
    home = Path.home()
    if sys.platform == "darwin":
        local_state = home / "Library" / "Application Support" / "Google" / "Chrome" / "Local State"
    else:
        local_state = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "google-chrome" / "Local State"
    try:
        cache = json.loads(local_state.read_text())["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return sorted(((directory, info.get("name") or directory) for directory, info in cache.items()),
                  key=lambda p: (p[0] != "Default", p[0]))


def _chrome_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "google-chrome"


def extension_in_profile(profile_dir: str) -> dict | None:
    """Si el perfil tiene cargada esta extensión: {"path", "enabled"} según sus Preferences, o None.

    Chrome escribe Preferences cada pocos segundos, así que un cambio recién hecho tarda un momento en verse.
    """
    for name in ("Secure Preferences", "Preferences"):
        try:
            prefs = json.loads((_chrome_dir() / profile_dir / name).read_text())
        except (OSError, ValueError):
            continue
        entry = ((prefs.get("extensions") or {}).get("settings") or {}).get(EXTENSION_ID)
        if entry and entry.get("path"):
            reasons = entry.get("disable_reasons")
            enabled = entry.get("state", 1) != 0 and not reasons
            return {"path": entry["path"], "enabled": enabled}
    return None


def open_extensions_page(profile_dir: str) -> bool:
    """Abre chrome://extensions en ese perfil (en el Chrome abierto, si lo hay). El usuario carga la extensión."""
    import shutil
    import subprocess

    if sys.platform == "darwin":
        command = ["open", "-na", "Google Chrome", "--args", f"--profile-directory={profile_dir}", "chrome://extensions"]
    else:
        binary = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        if not binary:
            return False
        command = [binary, f"--profile-directory={profile_dir}", "chrome://extensions"]
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def installed() -> bool:
    return LAUNCHER_PATH.exists() and any((d / f"{NAME}.json").exists() for d in manifest_dirs())


if __name__ == "__main__":
    sys.exit(main())
