"""Login interactivo en Chrome separado; solo las cookies del servicio llegan al keyring.

No usa el perfil habitual, no exporta storage_state ni registra credenciales o respuestas del navegador.
"""
from __future__ import annotations

import time
from typing import Callable


class BrowserLoginError(Exception):
    """Error apto para mostrar al usuario, sin detalles del navegador que puedan contener secretos."""


def capture_session(url: str, cookie_url: str, accept: Callable[[str], bool], timeout_s: int = 300,
                    cancel: Callable[[], bool] | None = None) -> bool:
    """Abre Chrome visible y espera una cookie aceptada; el callback valida y guarda sin exponerla al agente.
    Si `cancel` devuelve True, cierra la ventana y devuelve False."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise BrowserLoginError(
            "Falta Playwright. Reinstala con ./install.sh --no-login "
            "(en desarrollo: .venv/bin/python -m pip install -e '.[dev]')."
        ) from None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
            try:
                context = browser.new_context()
                page = context.new_page()
                # La navegación puede seguir a un proveedor de login: no inspeccionamos sus cookies.
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                deadline = time.monotonic() + timeout_s
                last_check = 0.0
                while time.monotonic() < deadline:
                    if not browser.is_connected() or not context.pages or (cancel and cancel()):
                        return False
                    if time.monotonic() - last_check >= 3:
                        cookies = context.cookies([cookie_url])
                        cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                        last_check = time.monotonic()
                        if cookie and accept(cookie):
                            return True
                    # Bombea eventos de Playwright mientras el usuario completa login/MFA.
                    context.pages[-1].wait_for_timeout(500)
                return False
            finally:
                if browser.is_connected():
                    browser.close()
    except Error:
        raise BrowserLoginError(
            "No se pudo completar el login en Chrome. Comprueba que Google Chrome esté instalado, "
            "que haya una sesión gráfica y que la página sea accesible. Vuelve a ejecutar el comando para reintentar."
        ) from None
