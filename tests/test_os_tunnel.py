import socket
import subprocess

from casebook.backends import os_tunnel
from casebook.backends.os_tunnel import ssh_pids as real_ssh_pids
from casebook.backends.os_tunnel import state as real_state  # antes de que conftest lo reemplace
from casebook.doctor import checks as real_checks
from demo_pack import acme


def test_state_down_when_port_closed():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert real_state(port, "x.example") == os_tunnel.DOWN


def test_state_hung_when_port_accepts_but_nothing_answers(monkeypatch):
    monkeypatch.setattr(os_tunnel, "PROBE_TIMEOUT", 0.3)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        assert real_state(server.getsockname()[1], "x.example") == os_tunnel.HUNG


def test_ssh_pids_matches_exact_host_only(monkeypatch):
    out = "\n".join([
        "100 ssh os_tunnel_prod -N",
        "101 ssh -N -o BatchMode=yes os_tunnel_prod",
        "102 ssh os_tunnel_prod-archive -N",
        "103 ssh db-bastion",
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out))
    assert real_ssh_pids("os_tunnel_prod") == [100, 101]


def test_doctor_checks_opensearch_session_through_tunnel(monkeypatch):
    import httpx

    from casebook import doctor
    from casebook.config import Config

    seen = {}

    def handler(request):
        seen["url"], seen["host"] = str(request.url), request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, json={})

    real = httpx.Client
    monkeypatch.setattr(doctor.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(doctor.credentials, "get_cookie", lambda system: "a=1")
    monkeypatch.setattr(doctor.credentials, "refresh_from_response", lambda *a: None)
    checks = doctor.check_opensearch(acme(), "prod")
    assert [c.status for c in checks] == ["ok", "ok"]
    assert seen["url"].startswith("https://127.0.0.1:9123/_dashboards/api/v1/configuration/account")
    assert seen["host"] == seen["sni"] == acme().opensearch["prod"].domain


def test_doctor_reports_missing_conversions_login_without_opening_tunnel(monkeypatch):
    from casebook import doctor
    from casebook.config import Config

    monkeypatch.setattr(doctor, "checks", real_checks)
    monkeypatch.setattr(doctor.credentials, "get_cookie", lambda system: None)

    def unexpected_tunnel(*args):
        raise AssertionError("no debe abrir un túnel sin credenciales")

    monkeypatch.setattr(os_tunnel, "ensure", unexpected_tunnel)
    monkeypatch.setattr(doctor, "check_db", lambda *a: [])
    from casebook.providers import cloudwatch

    monkeypatch.setattr(cloudwatch.Aws, "doctor_checks", lambda self, heal=True: [])  # nada de AWS real
    checks = doctor.run(acme())
    assert {c.name for c in checks} == {"opensearch:prod", "opensearch:prod-archive"}
    conversions = next(c for c in checks if c.name == "opensearch:prod-archive")
    assert conversions.status == doctor.FAIL
    assert "casebook login os-archive" in conversions.detail
