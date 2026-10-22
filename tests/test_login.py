import httpx
import pytest

from casebook import login, systems
from casebook.backends import os_tunnel
from casebook.config import Config
from demo_pack import acme as _acme
import fake_web_logs


@pytest.fixture(autouse=True)
def _web_logs(monkeypatch):
    monkeypatch.setitem(fake_web_logs.providers.BUILTIN, ("logs", "web-logs"), "fake_web_logs:WebLogsProvider")
    monkeypatch.setattr(fake_web_logs.providers, "_registry", None)


def acme():
    """El pack de prueba con CloudWatch en un proveedor que inicia sesión en el navegador."""
    cfg = _acme()
    cfg.cw_provider, cfg.cw_options = "web-logs", {"api": fake_web_logs.API, "origin": fake_web_logs.ORIGIN}
    return cfg


def system(system_id):
    return systems.get(acme(), system_id)

GOOD = "ACCESS-TOKEN=a; ID-TOKEN=i; REFRESH-TOKEN=r; security_authentication=s; STATE-TOKEN=t"


@pytest.fixture
def saved(monkeypatch):
    out = {}
    monkeypatch.setattr(login.credentials, "set_cookie", lambda system, value: out.__setitem__(system.id, value))
    monkeypatch.setattr(login.credentials, "get_cookie", lambda system: out.get(system.id))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")  # el "presiona Enter"
    return out


def api(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(login.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


def web_logs(good="osid=ok", reachable=True):
    return fake_web_logs.handler(good, reachable)


def test_browser_cw_captures_validates_and_saves_only_api_cookie(monkeypatch, saved, capsys):
    from casebook import browser_login

    api(monkeypatch, web_logs())

    def capture(url, cookie_url, accept, timeout_s, cancel=None):
        assert url == 'https://logs.acme.example/login'
        assert cookie_url == 'https://api-logs.acme.example/me'
        assert timeout_s == 90
        assert not accept('tracker=not-the-api-session')
        assert not saved
        assert accept('osid=ok')
        return True

    monkeypatch.setattr(browser_login, 'capture_session', capture)
    assert login.login_browser(acme(), system('cw'), 90) == 0
    assert saved == {'cw': 'osid=ok'}
    output = capsys.readouterr().out
    assert 'keyring' in output and 'osid=ok' not in output


def test_browser_os_saves_rotated_cookie(monkeypatch, saved):
    from casebook import browser_login

    def handler(request):
        assert request.headers['cookie'] == GOOD
        return httpx.Response(200, headers={'set-cookie': 'ACCESS-TOKEN=rotated; Path=/'})

    api(monkeypatch, handler)
    monkeypatch.setattr(browser_login, 'capture_session', lambda url, cookie_url, accept, **kw: accept(GOOD))
    assert login.login_browser(acme(), system('os-archive')) == 0
    assert login.credentials.parse_cookie(saved['os-archive'])['ACCESS-TOKEN'] == 'rotated'


@pytest.mark.parametrize('failure', ['timeout', 'cancel', 'browser'])
def test_browser_failure_keeps_previous_session(monkeypatch, saved, capsys, failure):
    from casebook import browser_login

    saved['cw'] = 'osid=old-private-value'
    api(monkeypatch, web_logs())

    def capture(*args, **kwargs):
        if failure == 'cancel':
            raise KeyboardInterrupt
        if failure == 'browser':
            raise browser_login.BrowserLoginError('Chrome no disponible')
        return False

    monkeypatch.setattr(browser_login, 'capture_session', capture)
    assert login.login_browser(acme(), system('cw')) == 1
    assert saved == {'cw': 'osid=old-private-value'}
    assert 'old-private-value' not in capsys.readouterr().out


def test_existing_os_cookie_rotation_is_saved_even_when_kept(monkeypatch, saved):
    saved['os'] = GOOD
    api(monkeypatch, lambda request: httpx.Response(200, headers={'set-cookie': 'ACCESS-TOKEN=fresh; Path=/'}))
    monkeypatch.setattr('builtins.input', lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert login.login_browser(acme(), system('os')) == 0
    assert login.credentials.parse_cookie(saved['os'])['ACCESS-TOKEN'] == 'fresh'


@pytest.mark.parametrize("system", ["os", "os-archive", "cw"])
def test_cli_login_uses_browser_by_default(monkeypatch, system):
    from casebook import cli

    calls = []
    monkeypatch.setattr(login, "login_browser",
                        lambda cfg, target, timeout, mode: calls.append((target, timeout, mode)) or 0)
    monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)
    cfg = acme()
    monkeypatch.setattr(cli.config, "load", lambda *a, **k: cfg)
    assert cli.main(["login", system]) == 0
    assert cli.main(["login", system, "--ventana-separada"]) == 0
    assert [(t.id, timeout, mode) for t, timeout, mode in calls] == [(system, 300, "auto"), (system, 300, "window")]


def test_cli_rejects_manual_cookie_mode():
    from casebook import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["login", "cw", "--no-browser"])
    assert exc.value.code == 2


def test_browser_stops_if_tunnel_cannot_start(monkeypatch, saved):
    monkeypatch.setattr(os_tunnel, "ensure", lambda *a: (os_tunnel.DOWN, "túnel caído"))
    assert login.login_browser(acme(), system("os")) == 1
    assert saved == {}


def test_browser_stops_if_web_logs_unreachable(monkeypatch, saved):
    api(monkeypatch, web_logs(reachable=False))
    assert login.login_browser(acme(), system("cw")) == 1
    assert saved == {}


def test_connected_extension_opens_normal_chrome_and_waits_for_its_session(monkeypatch, saved, capsys):
    from casebook import browser_login, native_host

    api(monkeypatch, web_logs())
    native_host.set_profiles(["Profile 2"])
    native_host.handle(acme(), {"type": "targets"})
    opened = []

    def open_in_chrome(url, profile):
        opened.append((url, profile))
        # Lo que haría la extensión al cargar la página: el host valida y guarda.
        native_host.handle(acme(), {"type": "cookies", "system": "cw", "cookie": "tracker=x"})
        native_host.handle(acme(), {"type": "cookies", "system": "cw", "cookie": "osid=ok"})
        return True

    monkeypatch.setattr(login, "_open_in_chrome", open_in_chrome)
    monkeypatch.setattr(browser_login, "capture_session", lambda *a, **kw: pytest.fail("no debe abrir Playwright"))
    assert login.login_browser(acme(), system("cw")) == 0
    assert opened == [("https://logs.acme.example/login", "Profile 2")]
    assert saved == {"cw": "osid=ok"}
    out = capsys.readouterr().out
    assert "La extensión entregó la sesión" in out and "Ana Dev" in out and "osid=ok" not in out


def test_extension_login_times_out_without_a_valid_session(monkeypatch, saved, capsys):
    from casebook import native_host

    api(monkeypatch, web_logs())
    native_host.handle(acme(), {"type": "targets"})
    monkeypatch.setattr(login, "_open_in_chrome", lambda url, profile: True)
    monkeypatch.setattr(login.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(login.time, "time", lambda: next(clock))
    assert login.login_browser(acme(), system("cw"), 60) == 1
    assert saved == {}
    assert "--ventana-separada" in capsys.readouterr().out


def test_login_with_extension_stops_when_cancelled(monkeypatch, capsys):
    from casebook import native_host

    monkeypatch.setattr(login, '_open_in_chrome', lambda url, profile=None: True)
    monkeypatch.setattr(native_host, 'login_profile', lambda: None)
    monkeypatch.setattr(native_host, 'last_result', lambda system: None)
    assert login._login_with_extension('cw', 'https://logs.acme.example', 300, cancel=lambda: True) == 1
    assert 'Cancelado' in capsys.readouterr().out
