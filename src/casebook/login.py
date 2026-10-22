"""Login en Chrome: valida la sesión del servicio y la guarda en el keyring."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from . import credentials, systems
from .config import Config
from .systems import System

# Cookies que manda el navegador en una request a Dashboards con sesión iniciada.
OS_EXPECTED_COOKIES = ("ACCESS-TOKEN", "REFRESH-TOKEN", "security_authentication")


@dataclass
class Validation:
    ok: bool
    detail: str
    retry: bool = True  # False: el problema no es la cookie (túnel, red), pegar otra no sirve
    info: tuple[str, ...] = ()  # líneas extra para mostrar al guardar (cuenta, presupuesto...)
    cookie: str | None = field(default=None, repr=False)  # actualización del servidor, nunca se imprime


# --- validación ----------------------------------------------------------------------------------------------


def validate_os(cfg: Config, env: str, cookie: str) -> Validation:
    target = cfg.opensearch[env]
    missing = [name for name in OS_EXPECTED_COOKIES if name not in credentials.parse_cookie(cookie)]
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"https://127.0.0.1:{target.tunnel_port}/_dashboards/api/v1/configuration/account",
                headers={"host": target.domain, "cookie": cookie},
                extensions={"sni_hostname": target.domain},
            )
    except httpx.HTTPError as exc:
        return Validation(False, f"no pude probarla: el túnel no respondió ({exc})", retry=False)
    if resp.status_code == 200:
        return Validation(True, "Dashboards la acepta", cookie=credentials.merge_cookie(cookie, dict(resp.cookies)))
    hint = f" Faltan: {', '.join(missing)}." if missing else ""
    return Validation(
        False,
        f"Dashboards rechazó la sesión (HTTP {resp.status_code}).{hint} Completa el login en el navegador.",
    )


# --- pasos ---------------------------------------------------------------------------------------------------


def _current_session(system: System, check: Callable[[str], Validation], replace: bool | None = None) -> bool:
    """Si ya hay una cookie guardada que funciona, lo dice y pregunta si reemplazarla (o usa `replace` sin
    preguntar, para el instalador gráfico). False = salir sin cambios."""
    saved = credentials.get_cookie(system)
    if not saved:
        print("   No tienes una sesión guardada todavía.")
        return True
    result = check(saved)
    if result.ok:
        if result.cookie and result.cookie != saved:
            credentials.set_cookie(system, result.cookie)
        print(f"   Ya tienes una sesión válida guardada. {'. '.join(result.info) or result.detail}.")
        if replace is not None:
            if not replace:
                print("   Listo, la dejé como estaba.")
            return replace
        try:
            input("   Enter para reemplazarla de todas formas, Ctrl+C para dejarla como está → ")
        except (KeyboardInterrupt, EOFError):
            print("\n   Listo, la dejé como estaba.")
            return False
        return True
    print(f"   La sesión guardada ya no sirve: {result.detail}.")
    return True


@dataclass
class BrowserTarget:
    url: str  # la página donde el usuario inicia sesión
    cookie_url: str  # solo las cookies que el navegador mandaría a esta URL
    check: Callable[[str], Validation]
    # Dominios padre donde viven las cookies de la sesión (por ejemplo "ejemplo.com" para una cookie de
    # .ejemplo.com). Chrome solo deja leer una cookie con permiso sobre su dominio: la extensión lo pide.
    cookie_domains: tuple[str, ...] = ()


def browser_target(cfg: Config, system: System) -> BrowserTarget:
    """Dónde iniciar sesión y cómo validar la cookie de cada sistema; lo usan el login y el native host. Lo define
    el proveedor del sistema (los de auth="browser")."""
    provider = systems.provider(cfg, system)
    target = provider.login_target() if provider else None
    if target is None:
        raise ValueError(f"{system.id} no inicia sesión con el navegador")
    return target


def login_browser(cfg: Config, system: System, timeout_s: int = 300, mode: str = "auto",
                  replace: bool | None = None, cancel: Callable[[], bool] | None = None) -> int:
    """Login con captura automática de sesión. Corre solo desde la terminal del usuario, nunca como tool MCP.

    mode: "extension" abre la página en tu Chrome y espera a que la extensión entregue la sesión; "window" abre
    una ventana de Chrome aparte con Playwright; "auto" usa la extensión si ya se conectó alguna vez.
    cancel: si devuelve True, se deja de esperar (el instalador gráfico lo usa para su botón «Cancelar»).
    """
    from . import native_host

    if not system.browser:
        ids = ", ".join(s.id for s in systems.browser_systems(cfg))
        print(f"El login con el navegador solo sirve para {ids}.")
        return 1
    problem = systems.provider(cfg, system).prepare_login()
    if problem:
        print(problem)
        return 1
    login_target = browser_target(cfg, system)
    url, cookie_url, check = login_target.url, login_target.cookie_url, login_target.check

    if not _current_session(system, check, replace):
        return 0
    if mode == "extension" or (mode == "auto" and native_host.connected()):
        return _login_with_extension(system, url, timeout_s, cancel)
    from .browser_login import BrowserLoginError, capture_session

    print("Se abrirá Chrome con una sesión separada de tu perfil habitual.")
    print("Completa el login y MFA hasta ver la aplicación. No hace falta copiar cookies ni pulsar Enter.")
    print("La ventana se cerrará al guardar la sesión en el keyring. Ctrl+C cancela.")
    saved = False
    connection_failed = False

    def accept(cookie: str) -> bool:
        nonlocal saved, connection_failed
        result = check(cookie)
        if not result.ok:
            if not result.retry:
                connection_failed = True
                print("La validación no pudo conectar al servicio; revisa la red o el túnel y vuelve a intentar.")
                return True  # terminar la espera sin guardar
            return False
        credentials.set_cookie(system, result.cookie or cookie)
        saved = True
        print("✓ Sesión validada y guardada en el keyring.")
        for line in result.info:
            print(f"  {line}.")
        return True

    try:
        capture_session(url, cookie_url, accept, timeout_s=timeout_s, cancel=cancel)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado.")
    except BrowserLoginError as exc:
        print(str(exc))
    if saved:
        return 0
    if not connection_failed:
        print("No se completó el login: se cerró la ventana, venció el tiempo o se canceló.")
    print("No se reemplazó la sesión guardada. Vuelve a ejecutar el comando para reintentar.")
    return 1


def _open_in_chrome(url: str, profile: str | None = None) -> bool:
    """Abre una pestaña en el Chrome habitual (si ya está abierto, en esa ventana), en el perfil de la extensión."""
    import shutil
    import subprocess
    import webbrowser

    args = [f"--profile-directory={profile}", url] if profile else [url]
    if sys.platform == "darwin":
        commands = [["open", "-na", "Google Chrome", "--args", *args]]
    else:
        commands = [[name, *args] for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
                    if shutil.which(name)]
    for command in commands:
        try:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            continue
    return webbrowser.open(url)


def _login_with_extension(system: System, url: str, timeout_s: int, cancel: Callable[[], bool] | None = None) -> int:
    from . import native_host

    started = time.time()
    if _open_in_chrome(url, native_host.login_profile()):
        print(f"Abrí {url} en tu Chrome.")
    else:
        print(f"Abre en tu Chrome: {url}")
    print("Si ya tienes la sesión iniciada, la extensión la entrega sola; si no, inicia sesión ahí como siempre.")
    print("Ctrl+C cancela.")
    last_detail = None
    try:
        while time.time() - started < timeout_s:
            if cancel and cancel():
                raise KeyboardInterrupt
            entry = native_host.last_result(system.id)
            if entry and entry.get("at", 0) >= started:
                if entry.get("ok"):
                    print("✓ La extensión entregó la sesión: validada y guardada en el keyring.")
                    for line in entry.get("info") or ():
                        print(f"  {line}.")
                    return 0
                if entry.get("detail") != last_detail:
                    last_detail = entry.get("detail")
                    print(f"  Todavía no: {last_detail}.")
            time.sleep(1)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado.")
        print("No se reemplazó la sesión guardada.")
        return 1
    print("No llegó una sesión válida desde la extensión. Revisa que esté cargada (`casebook doctor`) o")
    print(f"usa una ventana aparte: casebook login {system.id} --ventana-separada")
    return 1
