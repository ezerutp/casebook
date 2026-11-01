"""Native host de la extensión: protocolo, validación antes de guardar e instalación, sin Chrome ni keyring reales."""
import io
import json
import struct

import httpx
import pytest

from casebook import credentials, native_host
from casebook.config import Config
import fake_web_logs
from casebook.config import CloudWatchSource
from demo_pack import acme as _acme


@pytest.fixture
def keyring_(monkeypatch):
    store = {}
    monkeypatch.setattr(credentials, "set_cookie", lambda system, value: store.__setitem__(system.id, value))
    return store


@pytest.fixture(autouse=True)
def _web_logs(monkeypatch):
    monkeypatch.setitem(fake_web_logs.providers.BUILTIN, ("logs", "web-logs"), "fake_web_logs:WebLogsProvider")
    monkeypatch.setattr(fake_web_logs.providers, "_registry", None)


def acme():
    """El pack de prueba con CloudWatch en un proveedor que inicia sesión en el navegador."""
    cfg = _acme()
    cfg.cloudwatch = {"default": CloudWatchSource("web-logs", {"api": fake_web_logs.API, "origin": fake_web_logs.ORIGIN})}
    return cfg


@pytest.fixture
def web_logs(monkeypatch):
    real = httpx.Client
    monkeypatch.setattr(fake_web_logs.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(fake_web_logs.handler())))


def frame(message):
    data = json.dumps(message).encode()
    return struct.pack("=I", len(data)) + data


def replies(raw):
    out, stream = [], io.BytesIO(raw)
    while (message := native_host.read_message(stream)) is not None:
        out.append(message)
    return out


def test_targets_come_from_config_and_mark_the_extension_connected():
    assert not native_host.connected()
    reply = native_host.handle(acme(), {"type": "targets"})
    by_system = {t["system"]: t for t in reply["targets"]}
    assert set(by_system) == {"os", "os-archive", "cw"}
    assert by_system["cw"] == {"system": "cw", "app_host": "logs.acme.example",
                               "cookie_url": "https://api-logs.acme.example/me",
                               "origins": ["https://*.acme.example/*", "https://api-logs.acme.example/*", "https://logs.acme.example/*"]}
    assert by_system["os"]["app_host"] == acme().opensearch["prod"].domain
    assert native_host.connected()


def test_valid_cookie_is_saved_and_rejected_one_keeps_previous(keyring_, web_logs):
    reply = native_host.handle(acme(), {"type": "cookies", "system": "cw", "cookie": "osid=viejo"})
    assert reply["ok"] is False and keyring_ == {}
    assert native_host.last_result("cw")["ok"] is False
    reply = native_host.handle(acme(), {"type": "cookies", "system": "cw", "cookie": "osid=ok"})
    assert reply == {"ok": True, "detail": "sesión guardada"}
    assert keyring_ == {"cw": "osid=ok"}
    result = native_host.last_result("cw")
    assert result["ok"] and result["info"] == ["Sesión de Ana Dev"]
    assert "osid" not in native_host.STATUS_PATH.read_text()


def test_status_reports_results_without_account_info(keyring_, web_logs):
    native_host.handle(acme(), {"type": "cookies", "system": "cw", "cookie": "osid=ok"})
    reply = native_host.handle(acme(), {"type": "status"})
    assert reply["systems"]["cw"]["ok"] is True
    assert set(reply["systems"]["cw"]) == {"ok", "detail", "at"}


def test_extension_files_referenced_by_the_manifest_exist():
    from pathlib import Path

    root = Path(native_host.__file__).resolve().parents[2] / "chrome-extension"
    manifest = json.loads((root / "manifest.json").read_text())
    files = [manifest["background"]["service_worker"], manifest["action"]["default_popup"],
             *manifest["icons"].values(), *manifest["action"]["default_icon"].values()]
    assert all((root / f).is_file() for f in files)


@pytest.mark.parametrize("message", [
    {"type": "cookies", "system": "db", "cookie": "a=b"},
    {"type": "cookies", "system": "cw", "cookie": 3},
    {"type": "cookies", "system": "cw", "cookie": "sin-igual"},
    {"type": "otro"},
])
def test_invalid_messages_save_nothing(keyring_, message):
    assert native_host.handle(acme(), message)["ok"] is False
    assert keyring_ == {}


def test_main_speaks_native_messaging_and_rejects_other_origins(monkeypatch, capsys):
    stdout = io.BytesIO()
    monkeypatch.setattr("sys.stdin", type("S", (), {"buffer": io.BytesIO(frame({"type": "targets"}))})())
    monkeypatch.setattr("sys.stdout", type("S", (), {"buffer": stdout})())
    assert native_host.main([native_host.ORIGIN]) == 0
    assert "targets" in replies(stdout.getvalue())[0]
    assert native_host.main(["chrome-extension://otra/"]) == 1
    assert native_host.main([]) == 1


def test_oversized_message_is_refused():
    with pytest.raises(ValueError):
        native_host.read_message(io.BytesIO(struct.pack("=I", native_host.MAX_MESSAGE + 1)))


def test_install_registers_manifest_launcher_and_extension_copy(tmp_path, monkeypatch):
    (tmp_path / "config" / "google-chrome").mkdir(parents=True)
    written = native_host.install()
    manifest = json.loads(written[0].read_text())
    assert written[0].parent == tmp_path / "config" / "google-chrome" / "NativeMessagingHosts"
    assert manifest["allowed_origins"] == [native_host.ORIGIN]
    assert manifest["path"] == str(native_host.LAUNCHER_PATH)
    assert "casebook.native_host" in native_host.LAUNCHER_PATH.read_text()
    assert native_host.installed()
    assert native_host.uninstall() == written
    assert not native_host.installed()


def _install_info(text):
    native_host.INSTALL_INFO.parent.mkdir(parents=True, exist_ok=True)
    native_host.INSTALL_INFO.write_text(text)


def test_dev_mode_loads_the_extension_from_the_repo():
    from pathlib import Path

    repo = Path(native_host.__file__).resolve().parents[2]
    _install_info(f"mode=dev\nsource={repo}\n")
    native_host.install()
    assert native_host.extension_dir() == repo / "chrome-extension"
    assert not native_host.EXTENSION_COPY.exists()


def test_local_install_copies_the_extension_like_the_package(tmp_path):
    clone = tmp_path / "clon"
    (clone / "chrome-extension").mkdir(parents=True)
    (clone / "chrome-extension" / "manifest.json").write_text('{"version": "9"}')
    _install_info(f"mode=copia\nsource={clone}\n")
    assert native_host.extension_dir() is None  # todavía no se copió
    native_host.install()
    assert native_host.extension_dir() == native_host.EXTENSION_COPY
    assert (native_host.EXTENSION_COPY / "manifest.json").read_text() == '{"version": "9"}'
    import shutil

    shutil.rmtree(clone)  # el repo se puede borrar
    assert native_host.extension_dir() == native_host.EXTENSION_COPY
    native_host.uninstall()
    assert not native_host.EXTENSION_COPY.exists()


def test_extension_key_matches_the_allowed_id():
    import base64
    import hashlib
    from pathlib import Path

    manifest = json.loads((Path(native_host.__file__).resolve().parents[2] / "chrome-extension" / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    assert "".join(chr(ord("a") + int(c, 16)) for c in digest) == native_host.EXTENSION_ID
    assert "nativeMessaging" in manifest["permissions"] and "cookies" in manifest["permissions"]
    # Sin dominios fijos: la extensión pide permiso para los del pack, desde el popup.
    assert "host_permissions" not in manifest and manifest["optional_host_permissions"] == ["https://*/*"]
    text = json.dumps(manifest)
    assert "acme.com" not in text and "amazonaws" not in text


def test_chrome_profiles_are_read_from_local_state(tmp_path):
    chrome = tmp_path / "config" / "google-chrome"
    chrome.mkdir(parents=True)
    (chrome / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Profile 2": {"name": "acme.com"}, "Default": {"name": "Personal"}}}}))
    assert native_host.chrome_profiles() == [("Default", "Personal"), ("Profile 2", "acme.com")]
    native_host.set_profiles(["Profile 2"])
    assert native_host.login_profile() == "Profile 2"


def test_missing_permissions_are_reported_to_doctor_and_installer():
    from casebook import doctor
    from casebook.setup import steps

    native_host.handle(acme(), {"type": "targets"})
    reply = native_host.handle(acme(), {"type": "permissions", "missing": ["https://logs.acme.example/*"]})
    assert reply["ok"] and native_host.missing_permissions() == ["https://logs.acme.example/*"]
    native_host.install()
    check = doctor.check_extension()
    assert check.status == doctor.WARN and "logs.acme.example" in check.detail and "Dar permiso" in check.detail
    state = steps.extension_state([{"name": "Default", "state": next(iter(steps.WORKING))}])
    assert not state["ok"] and "Dar permiso" in state["subtitle"]
    native_host.handle(acme(), {"type": "permissions", "missing": []})
    assert doctor.check_extension().status == doctor.OK and steps.extension_state([])["ok"]
    assert native_host.handle(acme(), {"type": "permissions", "missing": "todo"})["ok"] is False
