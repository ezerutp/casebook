import stat

import pytest

from casebook import cli, doctor
from casebook.backends import os_tunnel, ssh_diag, tunnel
from casebook.backends.errors import BackendError
from casebook.backends.os_tunnel import ensure as real_ensure  # antes de que conftest lo reemplace
from casebook.config import Config
from demo_pack import acme

TIMEOUT = "ssh: connect to host bastion.acme.example port 22: Connection timed out"


@pytest.mark.parametrize(
    ("line", "hint"),
    [
        (TIMEOUT, "VPN"),
        ("ssh: connect to host bastion.acme.example port 22: No route to host", "VPN"),
        ("usuario@bastion.acme.example: Permission denied (publickey).", "ssh-add"),
        ("Host key verification failed.", "ssh os_tunnel_prod"),
        ("ssh: Could not resolve hostname bastion.acme.example: Name or service not known", "DNS"),
        ("bind [127.0.0.1]:9123: Address already in use", "puerto local"),
        ("algo raro que no conocemos", "algo raro que no conocemos"),
    ],
)
def test_explain(line, hint):
    assert hint in ssh_diag.explain(line, "os_tunnel_prod")


def test_explain_without_output_points_to_network():
    assert "VPN" in ssh_diag.explain(None, "db-bastion") and "`ssh db-bastion`" in ssh_diag.explain(None, "db-bastion")


def test_last_error_line_skips_noise(tmp_path):
    log = tmp_path / "l"
    log.write_text(f"{TIMEOUT}\nWarning: Permanently added 'x' to the list of known hosts.\n\n")
    assert ssh_diag.last_error_line(log) == TIMEOUT
    assert ssh_diag.last_error_line(tmp_path / "no-existe") is None


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Un `ssh` falso en el PATH que imprime `message` en stderr y sale con 255, como ssh real al fallar."""

    def install(message):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "ssh"
        script.write_text(f"#!/bin/sh\necho '{message}' >&2\nexit 255\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
        monkeypatch.setattr(os_tunnel, "DATA_DIR", tmp_path / "data")
        monkeypatch.setattr(tunnel, "DATA_DIR", tmp_path / "data")

    return install


def test_os_tunnel_reports_the_ssh_cause(fake_ssh, monkeypatch):
    fake_ssh(TIMEOUT)
    monkeypatch.setattr(os_tunnel, "state", lambda port, domain: os_tunnel.DOWN)
    result, detail = real_ensure("os_tunnel_prod", 9123, "x.example")
    assert result == os_tunnel.DOWN
    assert "Connection timed out" in detail and "VPN" in detail


def test_mysql_tunnel_reports_the_ssh_cause(fake_ssh):
    fake_ssh("usuario@bastion.acme.example: Permission denied (publickey).")
    t = tunnel.SshTunnel("db-bastion", "db.example", 3306, startup_timeout=5)
    with pytest.raises(BackendError, match="Permission denied.*ssh-add"):
        t.ensure()


def test_ssh_options_fail_fast():
    opts = " ".join(ssh_diag.TUNNEL_OPTIONS)
    assert "BatchMode=yes" in opts and f"ConnectTimeout={ssh_diag.CONNECT_TIMEOUT}" in opts


# --- requisitos ----------------------------------------------------------------------------------------------

GOOD_HOSTS = {
    "os_tunnel_prod": {"hostname": ["bastion.acme.example"], "port": ["22"],
                       "localforward": ["9123 [vpc-x.es.amazonaws.com]:443"]},
    "os_tunnel_prod-archive": {"hostname": ["bastion.acme.example"], "port": ["22"],
                                   "localforward": ["9126 [vpc-y.es.amazonaws.com]:443"]},
    "db-bastion": {"hostname": ["bastion.acme.example"], "port": ["22"]},
}


@pytest.fixture
def machine(monkeypatch):
    """La máquina de alguien del equipo, configurable por test."""
    state = {"hosts": dict(GOOD_HOSTS), "reachable": True, "aliases": {"db-login": True, "os-prod": True, "os-prod-archive": True},
             "creds": {"DB_USER": "u", "DB_PASSWORD": "p"}}
    monkeypatch.setattr(ssh_diag, "ssh_config", lambda host: state["hosts"].get(host, {"hostname": [host]}))
    monkeypatch.setattr(
        ssh_diag, "tcp_reachable",
        lambda h, p, timeout=5: (True, f"{h}:{p} responde") if state["reachable"] else (False, f"{h}:{p} no respondió en 5 s"),
    )
    monkeypatch.setattr(doctor, "_shell_aliases", lambda names: state["aliases"])
    monkeypatch.setattr(doctor.credentials, "external_db_login",
                        lambda source=None: ("u", "p") if state["creds"].get("DB_PASSWORD") else None)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    return state


def fails(checks):
    return [c.detail for c in checks if c.status == doctor.FAIL]


def test_prereqs_all_good(machine):
    checks = doctor.check_prereqs(acme())
    assert fails(checks) == []
    assert any("bastion.acme.example:22 responde" in c.detail for c in checks)
    assert sum("servidor SSH" in c.detail for c in checks) == 1  # todos los hosts usan el mismo servidor: se prueba una vez


def test_prereqs_friend_without_vpn(machine):
    machine["reachable"] = False
    (problem,) = fails(doctor.check_prereqs(acme()))
    assert "no respondió" in problem and "VPN" in problem and "whitelist" in problem


def test_prereqs_missing_host_and_forward(machine):
    machine["hosts"] = {"os_tunnel_prod": {"hostname": ["bastion.acme.example"], "port": ["22"], "localforward": []}}
    problems = fails(doctor.check_prereqs(acme()))
    assert any("LocalForward 9123" in p for p in problems)
    assert any("falta el host `db-bastion`" in p for p in problems)


def test_prereqs_missing_db_credentials_and_alias(machine):
    machine["creds"] = {}
    machine["aliases"] = {"db-login": False, "os-prod": True}
    checks = doctor.check_prereqs(acme())
    assert any("corre `db-login` una vez" in p for p in fails(checks))
    assert any(c.status == doctor.WARN and "db-login" in c.detail for c in checks)


def test_doctor_requisitos_cli(machine, capsys):
    assert cli.main(["doctor", "--requisitos"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Requisitos") and "Conexiones" not in out
    machine["reachable"] = False
    assert cli.main(["doctor", "--requisitos"]) == 1
