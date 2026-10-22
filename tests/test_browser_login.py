"""Prueba el alcance de las cookies y el cierre del navegador sin acceder a credenciales reales."""
from types import SimpleNamespace
import sys

import pytest

from casebook import browser_login


@pytest.fixture
def browser(monkeypatch):
    state = {'closed': False, 'requests': [], 'waits': 0}

    class Error(Exception):
        pass

    def cookies(urls):
        state['requests'].append(urls)
        return [{'name': 'session', 'value': 'private-value'}]

    def wait(ms):
        state['waits'] += 1
        context.pages.clear()

    page = SimpleNamespace(goto=lambda *a, **kw: None, wait_for_timeout=wait)
    context = SimpleNamespace(new_page=lambda: page, cookies=cookies, pages=[page])

    def close():
        state['closed'] = True

    browser = SimpleNamespace(new_context=lambda: context, is_connected=lambda: not state['closed'], close=close)
    state['launch'] = []

    def launch(**kw):
        state['launch'].append(kw)
        return browser

    class Manager:
        def __enter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(launch=launch))

        def __exit__(self, *args):
            pass

    monkeypatch.setitem(sys.modules, 'playwright.sync_api', SimpleNamespace(Error=Error, sync_playwright=Manager))
    return state


def test_capture_scopes_cookie_url_and_closes_on_success(browser):
    seen = []
    assert browser_login.capture_session('https://app.example/', 'https://api.example/me',
                                         lambda cookie: seen.append(cookie) or True)
    assert browser['requests'] == [['https://api.example/me']]
    assert seen == ['session=private-value']
    assert browser['launch'] == [{'channel': 'chrome', 'headless': False}]
    assert browser['closed']


def test_closing_window_stops_without_acceptance(browser):
    assert not browser_login.capture_session('https://app.example/', 'https://api.example/me', lambda c: False)
    assert browser['waits'] == 1 and browser['closed']


def test_cancel_always_closes_browser(browser):
    def cancel(cookie):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        browser_login.capture_session('https://app.example/', 'https://api.example/me', cancel)
    assert browser['closed']
