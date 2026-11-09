from fastapi.testclient import TestClient

from casebook import __version__, doctor
from casebook.config import Config
from casebook.events import Recorder
from casebook.store import Store
from casebook.viewer.app import create_app

import pytest
from demo_pack import acme


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    yield s
    s.close()


def test_sessions_count_errors(store):
    rec = Recorder(store)
    rec.start_session("SUP-1")
    with pytest.raises(RuntimeError):
        with rec.query("db_select", {"sql": "x"}, "SELECT 1"):
            raise RuntimeError("falló")
    rec.reject("db_select", {"sql": "DELETE FROM x"}, "solo lectura")
    rec.start_session("SUP-2")
    errors = {s["name"]: s["errors"] for s in TestClient(create_app(store)).get("/api/sessions").json()}
    assert errors == {"SUP-1": 2, "SUP-2": 0}


def test_meta_exposes_version_and_jira_url(store):
    cfg = acme()
    meta = TestClient(create_app(store, cfg)).get("/api/meta").json()
    assert meta["version"] == __version__ and meta["jira_base_url"] is None
    assert [(s["id"], s["check"], s["cmd"]) for s in meta["systems"]][-2:] == [
        ("cw", "cloudwatch (aws)", "casebook login cw"), ("db", "mysql (reader)", "casebook login db")]
    cfg.jira_base_url = "https://ejemplo.atlassian.net/browse/"
    assert TestClient(create_app(store, cfg)).get("/api/meta").json()["jira_base_url"] == cfg.jira_base_url


def test_health_returns_newest_of_session_and_viewer_check(store, monkeypatch):
    client = TestClient(create_app(store))
    assert client.get("/api/health").json() is None

    rec = Recorder(store)
    rec.start_session("SUP-3")
    rec.emit_health([{"name": "mysql (reader)", "status": "fail", "detail": "sin credenciales"}])
    assert client.get("/api/health").json()["checks"][0]["status"] == "fail"

    calls = []

    def fake_run(cfg, heal=True):
        calls.append(heal)
        return [doctor.Check("mysql (reader)", doctor.OK, "conectado")]

    monkeypatch.setattr(doctor, "run", fake_run)
    assert client.post("/api/health").status_code == 403  # sin el header del visor
    fresh = client.post("/api/health", headers={"x-casebook-viewer": "1"}).json()
    assert fresh["checks"] == [{"name": "mysql (reader)", "status": "ok", "detail": "conectado", "group": "",
                                "key": "", "fix": ""}]
    assert calls == [False]  # desde el visor no se reparan túneles
    assert client.get("/api/health").json() == fresh

    rec.emit_health([{"name": "mysql (reader)", "status": "warn", "detail": "lento"}])
    assert client.get("/api/health").json()["checks"][0]["status"] == "warn"


def test_viewer_health_survives_restart_without_adding_session_events(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    monkeypatch.setattr(doctor, "run", lambda cfg, heal=True: [
        doctor.Check("opensearch:prod-archive", doctor.OK, "sesión vigente")
    ])
    with_store = Store(path)
    client = TestClient(create_app(with_store))
    fresh = client.post("/api/health", headers={"x-casebook-viewer": "1"}).json()
    assert with_store.list_sessions() == []
    assert with_store.latest_event_id() == 0
    with_store.close()

    reopened = Store(path)
    try:
        assert TestClient(create_app(reopened)).get("/api/health").json() == fresh
    finally:
        reopened.close()


def test_recheck_survives_a_system_whose_check_raises(store, monkeypatch):
    """Un MCP lanzado sin DBUS_SESSION_BUS_ADDRESS no llega al keyring: antes eso tumbaba el POST con un 500 y el
    botón de recargar del visor parecía no hacer nada."""
    import keyring.errors

    cfg = acme()

    class Broken:
        def doctor_checks(self, heal):
            raise keyring.errors.NoKeyringError("No recommended backend was available")

    class Fine:
        def __init__(self, system):
            self.system = system

        def doctor_checks(self, heal):
            return [doctor.Check(self.system.check, doctor.OK, "listo")]

    systems_ = doctor.systems.all_systems(cfg)
    first = systems_[0]
    monkeypatch.setattr(doctor, "checks", lambda cfg, heal=True: [  # el conftest lo deja vacío
        lambda s=s: doctor.check_system(cfg, s, heal) for s in systems_])
    monkeypatch.setattr(doctor.systems, "provider",
                        lambda cfg, system: Broken() if system.id == first.id else Fine(system))
    resp = TestClient(create_app(store, cfg)).post("/api/health", headers={"x-casebook-viewer": "1"})
    assert resp.status_code == 200
    checks = {c["name"]: c for c in resp.json()["checks"]}
    assert checks[first.check]["status"] == "fail" and "keyring" in checks[first.check]["detail"]
    assert all(c["status"] == "ok" for name, c in checks.items() if name != first.check)
