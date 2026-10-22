import pytest

from casebook.backends import opensearch, os_tunnel


@pytest.fixture(autouse=True)
def _sin_pack_ni_home_reales(monkeypatch, tmp_path):
    """Ningún test usa el pack activo de la máquina ni lee archivos del home real (como el de credenciales que
    nombra el pack de Acme). En los tests el pack activo es el de Acme del repo."""
    import shutil

    from casebook import config
    from demo_pack import PACK

    packs = tmp_path / "packs"
    shutil.copytree(PACK.parent, packs / "acme")  # pack.toml y sus skills
    (packs / "active").write_text("acme\n")  # como antes los valores de fábrica; sin pack: no_pack()
    monkeypatch.setattr(config, "PACKS_DIR", packs)
    # Ni config.toml ni las bases (sesiones, conocimiento) reales: config.load() y Config() van al test.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "user-config" / "config.toml")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _slack_aislado(monkeypatch, tmp_path):
    """El estado de la vigilancia de Slack va a una carpeta del test, nunca a ~/.local/share."""
    from casebook import slack_watch

    monkeypatch.setattr(slack_watch, "STATE_PATH", tmp_path / "slack-watch.json")
    monkeypatch.setattr(slack_watch, "WORK_DIR", tmp_path / "slack-work")


@pytest.fixture(autouse=True)
def _sin_notificaciones(monkeypatch):
    """Los tests de aprobación no deben mandar notificaciones de escritorio de verdad."""
    monkeypatch.setenv("CASEBOOK_NOTIFY", "0")


@pytest.fixture(autouse=True)
def _sin_tuneles_reales(monkeypatch):
    """Ningún test toca túneles reales: por defecto el túnel 'está arriba'."""
    monkeypatch.setattr(os_tunnel, "state", lambda port, domain: os_tunnel.UP)
    monkeypatch.setattr(os_tunnel, "ensure", lambda host, port, domain: (os_tunnel.UP, "túnel arriba"))
    opensearch._last_ok.clear()


@pytest.fixture(autouse=True)
def _sin_chequeos_reales(monkeypatch):
    """session_start corre doctor (MySQL, CloudWatch, túneles): en los tests no hay chequeos reales."""
    from casebook import doctor

    monkeypatch.setattr(doctor, "checks", lambda cfg, heal=True: [])


@pytest.fixture(autouse=True)
def _sin_extension_real(monkeypatch, tmp_path):
    """Ningún test lee ni escribe el estado real de la extensión de Chrome ni sus manifests."""
    from casebook import native_host

    data = tmp_path / "data"
    monkeypatch.setattr(native_host, "STATUS_PATH", data / "chrome-extension.json")
    monkeypatch.setattr(native_host, "INSTALL_INFO", data / "install-info")
    monkeypatch.setattr(native_host, "EXTENSION_COPY", data / "chrome-extension")
    monkeypatch.setattr(native_host, "LAUNCHER_PATH", data / "native-host")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # manifests y perfiles de Chrome
