"""CLI: casebook {mcp, viewer, stop, login, logout, extension, setup, doctor, export, demo}."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import signal
import sys
import time
import webbrowser

from . import config, credentials, doctor, systems

def cmd_mcp(_: argparse.Namespace) -> int:
    from .server import run

    run()
    return 0


def cmd_viewer(args: argparse.Namespace) -> int:
    import uvicorn

    from .store import Store
    from .viewer.app import ViewerServer, create_app

    cfg = config.load()
    url = f"http://{cfg.viewer_host}:{cfg.viewer_port}"
    running = _running_viewer(url)
    if running is True:
        # Todos los visores leen la misma base: no hace falta otro, se abre el que ya está.
        print(f"Ya hay un visor corriendo en {url} (de Claude Code o de otra terminal): ahí están todas las sesiones.")
        print("Si acabas de reinstalar, reinicia el proceso que sirve este visor (el agente o la terminal "
              "con start/viewer) y recarga la pestaña para cargar los cambios.")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if running == UNRESPONSIVE:
        print(f"error: {_unresponsive_hint(cfg.viewer_port)}", file=sys.stderr)
        return 1
    if running is False:
        print(f"error: el puerto {cfg.viewer_port} está ocupado por otro programa. Cambia viewer.port en "
              f"{config.CONFIG_PATH} o libera el puerto.", file=sys.stderr)
        return 1
    print(f"Visor en {url} (Ctrl-C o `casebook stop` para salir). Si Claude Code está abierto, usa este mismo visor.")
    if not args.no_browser:
        webbrowser.open(url)
    server = ViewerServer(uvicorn.Config(create_app(Store(cfg.db_path), cfg), host=cfg.viewer_host,
                                         port=cfg.viewer_port, log_level="warning", timeout_graceful_shutdown=1))
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # uvicorn vuelve a lanzar el Ctrl-C después de apagarse ordenadamente
    if not server.started:
        return 1  # no llegó a arrancar; uvicorn ya mostró por qué (uvicorn.run salía con error igual)
    print("Visor detenido.")
    return 0


STOP_WAIT_S = 10


def cmd_stop(args: argparse.Namespace) -> int:
    """Detiene el visor de start/viewer. El de Claude Code no: se cierra con su sesión y la dejaría sin herramientas."""
    if args.target == "all":
        return _stop_all()
    cfg = config.load()
    url = f"http://{cfg.viewer_host}:{cfg.viewer_port}"
    state = _viewer_state(url)
    if state is None:
        print(f"No hay ningún visor corriendo en {url}.")
        return 0
    if state == UNRESPONSIVE:
        print(f"error: {_unresponsive_hint(cfg.viewer_port)}", file=sys.stderr)
        return 1
    if state is False:
        print(f"error: el puerto {cfg.viewer_port} lo usa otro programa, no un visor; no lo toco.", file=sys.stderr)
        return 1
    pid = state.get("pid")
    if state.get("served_by") == "mcp":
        print(f"El visor en {url} lo sirve Claude Code (casebook mcp, pid {pid}) y se cierra solo al cerrar "
              "esa sesión. No lo detengo: Claude Code se quedaría sin las herramientas de soporte. Para detener "
              "todo igual, usa `casebook stop all`.")
        return 1
    if state.get("served_by") != "viewer" or not isinstance(pid, int):
        print(f"error: el visor en {url} es de una versión anterior y no dice qué proceso lo sirve. Ciérralo con "
              "Ctrl-C en la terminal de start/viewer, o cerrando Claude Code si lo sirve él.", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # terminó entre la consulta y la señal
    except PermissionError:
        print(f"error: no tengo permiso para detener el proceso {pid} (¿es de otro usuario?).", file=sys.stderr)
        return 1
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        state = _viewer_state(url)
        # Detenido si el puerto quedó libre o ya lo sirve otro proceso (Claude Code lo retoma en unos segundos).
        if state != UNRESPONSIVE and (not isinstance(state, dict) or state.get("pid") != pid):
            print(f"Visor detenido (pid {pid}). Si Claude Code está abierto, retoma el visor en unos segundos.")
            return 0
        time.sleep(0.2)
    print(f"error: el visor (pid {pid}) no se detuvo en {STOP_WAIT_S} s; ciérralo con Ctrl-C en su terminal.",
          file=sys.stderr)
    return 1


def _stop_all() -> int:
    """Detiene todos los procesos del paquete de este usuario: visores de start/viewer y servidores MCP."""
    procs = _server_processes()
    if procs is None:
        print("error: no pude listar los procesos (falta `ps`).", file=sys.stderr)
        return 1
    if not procs:
        print("No hay ningún proceso de casebook corriendo.")
        return 0
    for pid, cmd in procs.items():
        try:
            os.kill(pid, signal.SIGTERM)
            # Uno suspendido (Ctrl-Z en su terminal) no atiende el SIGTERM hasta que lo reanudan.
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"error: no tengo permiso para detener el proceso {pid} ({cmd}).", file=sys.stderr)
    # Se espera con ps y no con kill(pid, 0): un proceso que terminó pero su padre no recogió sigue existiendo.
    deadline = time.monotonic() + STOP_WAIT_S
    while (alive := procs.keys() & (_server_processes() or {}).keys()) and time.monotonic() < deadline:
        time.sleep(0.2)
    for pid, cmd in procs.items():
        label = "Claude Code" if cmd == "mcp" else cmd
        print(f"  {'no se detuvo' if pid in alive else 'detenido'}: pid {pid} ({label})")
    if any(cmd == "mcp" for cmd in procs.values()):
        print("Las sesiones de Claude Code abiertas se quedan sin las herramientas de soporte: reconéctalas con /mcp "
              "o reinicia Claude Code.")
    if alive:
        print(f"error: {len(alive)} proceso(s) no se detuvieron en {STOP_WAIT_S} s.", file=sys.stderr)
        return 1
    return 0


# El script instalado (.../bin/casebook mcp) o python -m casebook.cli viewer. Anclado a "/" o al
# inicio para no confundir un `grep casebook mcp`; las rutas pueden tener espacios.
SERVER_CMDLINE = re.compile(
    r"(?:^|/)casebook\s+(mcp|viewer|start)(?:\s|$)|\s-m\s+casebook\.cli\s+(mcp|viewer|start)(?:\s|$)"
)


def _server_processes() -> dict[int, str] | None:
    """pid -> subcomando (mcp, viewer o start) de los procesos casebook de este usuario, salvo este.

    None si no se pudo correr ps.
    """
    import subprocess

    try:
        # -ww: sin recortar las líneas al ancho de la terminal (ps lo hace si COLUMNS está exportada).
        out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "uid=", "-o", "args="],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or int(parts[1]) != os.getuid() or int(parts[0]) == os.getpid():
            continue
        if match := SERVER_CMDLINE.search(parts[2]):
            procs[int(parts[0])] = match.group(1) or match.group(2)
    return procs


# El puerto acepta conexiones pero nadie responde: típicamente un visor suspendido con Ctrl-Z.
UNRESPONSIVE = "unresponsive"


def _unresponsive_hint(port: int) -> str:
    return (f"el puerto {port} lo tiene un proceso que no responde, probablemente una sesión de Claude Code o una "
            "terminal con start/viewer suspendida con Ctrl-Z. Retómala con `fg` en su terminal, o detenlo con "
            "`casebook stop all`.")


def _viewer_state(url: str) -> dict | bool | str | None:
    """El /api/state del visor; False: el puerto lo usa otro programa; None: el puerto está libre;
    UNRESPONSIVE: alguien tiene el puerto pero no contesta."""
    import httpx

    try:
        resp = httpx.get(f"{url}/api/state", timeout=2)
    except httpx.TimeoutException:
        return UNRESPONSIVE
    except httpx.HTTPError:
        return None
    try:
        state = resp.json()
    except ValueError:
        return False
    return state if isinstance(state, dict) and "latest_event_id" in state else False


def _running_viewer(url: str) -> bool | str | None:
    """True: ya hay un visor nuestro; False: el puerto lo usa otro programa; None: el puerto está libre;
    UNRESPONSIVE: alguien tiene el puerto pero no contesta."""
    state = _viewer_state(url)
    return True if isinstance(state, dict) else state


def _browser_timeout(value: str) -> int:
    # Con choices=range(...) argparse lista los 1771 valores en el error.
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"no es un número: {value}") from None
    if not 30 <= seconds <= 1800:
        raise argparse.ArgumentTypeError("debe estar entre 30 y 1800 segundos")
    return seconds


def cmd_login(args: argparse.Namespace) -> int:
    cfg = config.load()
    try:
        system = systems.get(cfg, args.system)
    except KeyError:
        print(f"sistema desconocido: {args.system}. Opciones: {', '.join(systems.ids(cfg))}", file=sys.stderr)
        return 1
    if getattr(args, "browser", False) and not system.browser:
        ids = ", ".join(s.id for s in systems.browser_systems(cfg))
        print(f"--browser solo sirve para {ids}.", file=sys.stderr)
        return 1
    if system.auth == "missing":
        print(f"error: {systems.missing_provider_message(system)}", file=sys.stderr)
        return 1
    if system.kind == "mysql":
        source = cfg.mysql[system.env].credentials
        where = credentials.describe_source(source, system.id)
        if credentials.external_db_login(source):
            print(f"No hace falta: se usan las credenciales de {where}.")
            print(f"Si dejaron de funcionar: {source.hint}.")
            return cmd_doctor(args)
        if source.kind != "keyring":
            print(f"No encontré credenciales en {where}. Lo más simple: {source.hint}.")
        print(f"Si prefieres, ingrésalas aquí y van al keyring del sistema ({system.label}).")
        user = input("usuario: ").strip()
        password = getpass.getpass("contraseña: ")
        try:
            credentials.set_db_login(user, password, system.keyring)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("Guardado. Verificando con `doctor`...\n")
        return cmd_doctor(args)
    provider = systems.provider(cfg, system)
    if system.auth == "none":
        print(f"{system.label} no necesita iniciar sesión.")
        return cmd_doctor(args)
    if system.auth == "external":
        print(f"{system.label}: {provider.login_hint()}.")
        return cmd_doctor(args)
    if system.auth == "secret":
        values = {}
        for f in provider.secret_fields():
            ask = getpass.getpass if f.key in ("password", "api_key") else input
            values[f.key] = ask(f"{f.label}: ").strip()
            if f.required and not values[f.key]:
                print(f"error: {f.label} es obligatorio", file=sys.stderr)
                return 1
        credentials.set_secret(system, values)
        print("Guardado en el keyring. Verificando con `doctor`...\n")
        return cmd_doctor(args)
    from . import login

    mode = "window" if args.ventana_separada else "auto"
    code = login.login_browser(cfg, system, args.browser_timeout, mode=mode)
    if code == 0:
        print("\nVerificando todo con `doctor`...\n")
        cmd_doctor(args)
    return code


def cmd_logout(args: argparse.Namespace) -> int:
    cfg = config.load()
    try:
        system = systems.get(cfg, args.system)
    except KeyError:
        print(f"sistema desconocido: {args.system}. Opciones: {', '.join(systems.ids(cfg))}", file=sys.stderr)
        return 1
    if system.kind == "mysql":
        credentials.delete_db_login(system.keyring)
    else:
        credentials.delete_cookie(system)  # la misma entrada guarda la cookie o el secreto
    print(f"Credenciales de {args.system} eliminadas del keyring.")
    return 0


def _ask_yes(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [s/N] ").strip().lower().startswith(("s", "y"))
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def cmd_pack(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import packs

    try:
        if args.action in ("install", "update"):
            source = args.target
            if args.action == "update":
                current = packs.active()
                if not current:
                    print("No hay pack activo para actualizar.", file=sys.stderr)
                    return 1
                source = packs.origin(current.name)
                if not source or (source.endswith(".zip") and not Path(source).exists()):
                    print(f"No sé de dónde volver a instalar {current.name}: pásame la carpeta, el zip nuevo o la URL "
                          "con `pack install`.", file=sys.stderr)
                    return 1
            if not source:
                print("error: falta de dónde instalar: una carpeta, un .zip o una URL git", file=sys.stderr)
                return 1

            def confirm(summary: str) -> bool:
                print(summary)
                return True if args.yes else _ask_yes("¿Instalar este pack?")

            def confirm_plugin(what: str) -> bool:
                return _ask_yes(f"El pack trae el plugin {what}. Es código y se instala con pip. ¿Instalarlo?")

            pack = packs.install(source, confirm, confirm_plugin, out=print)
            print(f"Pack {pack.display_name} {pack.version} instalado y activo.")
            print("Las skills nuevas se instalan en tus agentes con `casebook setup`. Las fuentes nuevas "
                  "aparecen al reiniciar Claude Code.")
            return 0
        if args.action == "list":
            items = packs.installed()
            if not items:
                print("No hay packs instalados.")
            for p in items:
                mark = "*" if p["active"] else " "
                print(f"{mark} {p['name']} {p['version']}" + (f"  ({p['origin']})" if p["origin"] else ""))
            return 0
        if args.action == "remove":
            if not args.target:
                print("error: falta el nombre del pack", file=sys.stderr)
                return 1
            packs.remove(args.target)
            print(f"Pack {args.target} quitado. Sus skills instaladas en los agentes quedan; quítalas desde el visor.")
            return 0
        if args.action == "init":
            folder = Path(args.target or ".")
            name = args.name or folder.resolve().name.lower()
            path = packs.init(folder, name)
            print(f"Pack de ejemplo en {path}: edítalo y pruébalo con `casebook pack install {folder}`.")
            return 0
        if args.action == "build":
            out = packs.build(Path(args.target or "."), Path(args.output) if args.output else None)
            print(f"Listo: {out}")
            return 0
        if args.action == "export":
            out = Path(args.output or f"{args.name or 'pack'}-{args.version}.zip")
            out = packs.export(out, args.name, args.version)
            print(f"Exportado a {out} (sin credenciales ni ajustes personales del visor).")
            return 0
    except packs.PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    pack = packs.active()
    if not pack:
        print("No hay pack activo: la app no tiene fuentes salvo las de config.toml.")
        print("Instala el de tu equipo con `casebook pack install <carpeta, zip o URL git>`.")
        return 0
    cfg = config.load()
    print(f"Pack activo: {pack.display_name} {pack.version} ({pack.path.parent})")
    print(f"Fuentes: {', '.join(f'{s.id} ({s.provider})' for s in systems.all_systems(cfg)) or 'ninguna'}")
    print(f"Skills: {', '.join(packs.skill_names(pack.path.parent)) or 'ninguna'}")
    return 0


def cmd_source(args: argparse.Namespace) -> int:
    from . import providers, sources

    cfg = config.load()
    try:
        if args.action == "list":
            items = sources.listing(cfg)
            if not items:
                print("No hay fuentes. Instala el pack de tu equipo (`pack install`) o agrega una con `source add`.")
            for s in items:
                print(f"{s['id']:<18} {s['label']:<26} {s['provider']:<18} sesión: {s['auth']:<9} ({s['origin']})")
            return 0
        if args.action == "test":
            if not args.target:
                print("error: falta el id de la fuente (ver `source list`)", file=sys.stderr)
                return 1
            checks = sources.test(cfg, args.target)
            for c in checks:
                print(f"  {'✓' if c.status == 'ok' else ('!' if c.status == 'warn' else '✗')} {c.detail}")
            return 0 if all(c.status != "fail" for c in checks) else 1
        if args.action == "remove":
            if not args.target:
                print("error: falta el id de la fuente (ver `source list`)", file=sys.stderr)
                return 1
            print(f"{args.target}: {sources.remove(cfg, args.target)}.")
            return 0
        # add
        catalog = sources.catalog()
        kind = args.target or _choose("Tipo de fuente", list(catalog), lambda k: catalog[k]["label"])
        if kind not in catalog:
            print(f"error: tipo desconocido {kind!r}; opciones: {', '.join(catalog)}", file=sys.stderr)
            return 1
        names = [p["name"] for p in catalog[kind]["providers"]]
        provider = args.provider or _choose("Proveedor", names, lambda n: next(
            p["label"] for p in catalog[kind]["providers"] if p["name"] == n))
        env = args.env
        if not env:
            taken = ", ".join(sources.taken_envs(cfg, kind))
            env = input("Nombre del entorno (por ejemplo prod, qa o staging)"
                        + (f"; ya hay {taken}" if taken else "") + ": ").strip()
        raw = dict(item.split("=", 1) for item in args.set or [])
        cls = providers.get(sources.PROVIDER_KIND[kind], provider)
        if cls is None:
            print(f"error: no hay un proveedor {provider!r} para {kind}; opciones: {', '.join(names)}", file=sys.stderr)
            return 1
        if not args.set and sys.stdin.isatty():
            for f in (*cls.fields, *sources.META_FIELDS):
                hint = f" ({'/'.join(f.choices)})" if f.choices else (f" [{f.default}]" if f.default not in (None, "", []) else "")
                need = " *" if f.required else ""
                help_ = f"  — {f.help}" if f.help else ""
                answer = input(f"{f.label}{need}{hint}{help_}: ").strip()
                if answer:
                    raw[f.key] = answer
        system = sources.add(kind, env, provider, raw)
        print(f"Guardada en {config.CONFIG_PATH} como {system.id} ({system.label}).")
        if system.auth in ("browser", "secret"):
            print(f"Inicia sesión con `casebook login {system.id}`.")
        elif system.auth == "external":
            print(f"Sesión: {systems.provider(config.load(), system).login_hint()}.")
        print("Pruébala con `casebook source test " + system.id + "`; aparece en Claude al reiniciarlo.")
        return 0
    except (config.SettingError, KeyError) as exc:
        message = f"no hay una fuente {exc.args[0]!r} (ver `source list`)" if isinstance(exc, KeyError) else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1


def _choose(what: str, options: list[str], label) -> str:
    if len(options) == 1:
        return options[0]
    if not sys.stdin.isatty():
        raise config.SettingError(f"falta {what.lower()}: {', '.join(options)}")
    for i, option in enumerate(options, 1):
        print(f"  {i}. {label(option)} ({option})")
    answer = input(f"{what} (1-{len(options)}): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        raise config.SettingError(f"{what.lower()}: elige un número de la lista")
    return options[int(answer) - 1]


def cmd_extension(args: argparse.Namespace) -> int:
    from . import native_host

    if args.action == "uninstall":
        removed = native_host.uninstall()
        print("Native host quitado." if removed else "El native host no estaba registrado.")
        print("Quita también la extensión en chrome://extensions.")
        return 0
    written = native_host.install()
    print("Native host registrado en:")
    for path in written:
        print(f"  {path}")
    if native_host.connected():
        print("\nLa extensión ya está conectada. Si se actualizó, recárgala en chrome://extensions (⟳) en cada perfil.")
    else:
        print("\nFalta cargar la extensión en cada perfil de Chrome donde inicias sesión (una sola vez por perfil):")
        print("  1. En chrome://extensions activa «Modo de desarrollador» (arriba a la derecha).")
        folder = native_host.extension_dir()
        print(f"  2. «Cargar descomprimida» y elige {folder or 'la carpeta de la extensión'}")
        if folder is None:
            print("     (no encontré el repo: clónalo o corre ./install.sh desde él)")
        print("Desde entonces, al iniciar sesión en las páginas de tus fuentes en ese perfil la sesión se guarda sola, y")
        ids = "|".join(s.id for s in systems.browser_systems(config.load()))
        print(f"`casebook login {ids}` abre la página en tu Chrome en vez de una ventana aparte.")
    profiles = native_host.chrome_profiles()
    if args.no_abrir or not profiles or not sys.stdin.isatty():
        return 0
    print("\nPerfiles de Chrome:")
    for i, (directory, name) in enumerate(profiles, 1):
        print(f"  {i}. {name} ({directory})")
    try:
        answer = input("¿En cuáles abro chrome://extensions? (números separados por espacio, Enter = ninguno) → ")
    except (KeyboardInterrupt, EOFError):
        print()
        return 0
    chosen = []
    for token in answer.replace(",", " ").split():
        if not (token.isdigit() and 1 <= int(token) <= len(profiles)):
            print(f"  ignoro {token!r}: no es un número de la lista")
            continue
        directory, name = profiles[int(token) - 1]
        chosen.append(directory)
        if native_host.open_extensions_page(directory):
            print(f"  abrí chrome://extensions en {name}")
        else:
            print(f"  no pude abrir Chrome; en el perfil {name} abre chrome://extensions a mano")
    if not chosen:
        return 0
    native_host.set_profiles(chosen)
    print(f"`login` abrirá las páginas en {dict(profiles)[chosen[0]]}.")
    if native_host.connected():
        return 0
    try:
        input("Cuando la hayas cargado, pulsa Enter (Ctrl+C para seguir sin ella) → ")
    except (KeyboardInterrupt, EOFError):
        print()
        return 0
    # Al instalarse, la extensión pide la lista de servicios al host: eso la marca como conectada.
    deadline = time.monotonic() + 10
    while not native_host.connected() and time.monotonic() < deadline:
        time.sleep(0.5)
    if native_host.connected():
        print("✓ Extensión conectada. Si ya tenías sesión en tus fuentes, la entrega en unos segundos.")
    else:
        print("Todavía no se conectó. Revisa en chrome://extensions que esté activa y sin errores; "
              "`casebook doctor` muestra el estado.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Vuelve a abrir el asistente de instalación con los datos de la última instalación."""
    from . import native_host
    from .setup.__main__ import main as setup_main

    info = native_host._install_info()
    argv = ["--venv", sys.prefix]
    if info.get("source"):
        argv += ["--repo", info["source"]]
    if info.get("mode") == "dev":
        argv.append("--dev")
    argv += [flag for flag, on in (("--gui", args.gui), ("--consola", args.consola), ("--preview", args.preview)) if on]
    return setup_main(argv)


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = config.load()
    only_prereqs = getattr(args, "requisitos", False)
    print("Requisitos")
    checks = doctor.check_prereqs(cfg)
    _print_checks(checks, with_name=False)
    if not only_prereqs:
        print("\nConexiones")
        connections = doctor.run(cfg)
        _print_checks(connections, with_name=True)
        checks += connections
    return 1 if any(c.status == doctor.FAIL for c in checks) else 0


def _print_checks(checks: list, with_name: bool) -> None:
    marks = {doctor.OK: "✓", doctor.WARN: "!", doctor.FAIL: "✗"}
    for c in checks:
        print(f"  {marks[c.status]} {c.name:<28} {c.detail}" if with_name else f"  {marks[c.status]} {c.detail}")


def cmd_export(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import export
    from .store import Store

    store = Store(config.load().db_path)
    sessions = store.list_sessions()
    if args.session is None:
        if not sessions:
            print("no hay sesiones", file=sys.stderr)
            return 1
        session_id = sessions[0]["id"]
    elif args.session.isdigit():
        session_id = int(args.session)
    else:
        match = [s for s in sessions if args.session in (s["name"], s["card"])]
        if not match:
            print(f"no encontré una sesión llamada {args.session!r}", file=sys.stderr)
            return 1
        session_id = match[0]["id"]  # la más reciente con ese nombre
    try:
        if args.output == "-":
            sys.stdout.write(export.render(store, session_id, args.rows))
        else:
            path = export.write(store, session_id, Path(args.output).expanduser() if args.output else None, args.rows)
            print(f"Exportada a {path}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Escribe una sesión de ejemplo, de a poco, para probar el visor en vivo sin backends."""
    from .events import Recorder
    from .store import Store

    recorder = Recorder(Store(config.load().db_path))
    pause = 0 if args.fast else 1.2
    recorder.start_session("demo", card="DEMO-0000")
    time.sleep(pause)
    recorder.note("Empiezo revisando los índices de usuarios y después los logs de integraciones.")
    time.sleep(pause)
    with recorder.query("os_indices", {"pattern": "users-*"}, "GET _cat/indices/users-*") as q:
        time.sleep(pause)
        q.finish(
            ["index", "docs.count", "store.size"],
            [[f"users-{i}_v-2", 1_200_000 + i * 73_411, f"{3 + i / 10:.1f}gb"] for i in range(20)],
        )
    time.sleep(pause)
    with recorder.query(
        "cw_query",
        {"log_group": "/app/integrations", "start": "2026-10-14T10:00:00Z", "end": "2026-10-14T11:00:00Z"},
        "fields @timestamp, @message\n| filter @message like /gclid/\n| sort @timestamp desc\n| limit 5",
    ) as q:
        for i in range(3):
            time.sleep(pause)
            q.progress(f"RUNNING · {(i + 1) * 120} MB escaneados")
        q.finish(
            ["@timestamp", "@message"],
            [[f"2026-10-14 10:5{i}:00.000", f"upload ok gclid=Cj0K…{i} accessToken=[REDACTED]"] for i in range(5)],
            {"cost_usd": 0.0021},
        )
    time.sleep(pause)
    try:
        with recorder.query("db_select", {"sql": "SELECT 1"}, "SELECT id FROM user WHERE email = ? LIMIT 10"):
            time.sleep(pause)
            raise RuntimeError("ejemplo de error: túnel caído")
    except RuntimeError:
        pass
    recorder.draft("DEMO-0000", "Revisamos el upload del click: la app lo envió bien y Google lo rechazó por gclid vencido.")
    recorder.note("Conclusión de ejemplo: el upload salió bien; el problema está del lado de Google.")
    print(f"Sesión demo {recorder.session_id} escrita.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="casebook")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("mcp", help="servidor MCP por stdio (lo levanta Claude Code)").set_defaults(func=cmd_mcp)

    p = sub.add_parser(
        "viewer", aliases=["start"], help="abre el visor para ver las sesiones (viejas o en vivo); si ya hay uno, lo abre"
    )
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_viewer)

    p = sub.add_parser(
        "stop", help="detiene el visor abierto con start/viewer (el de Claude Code se cierra con su sesión); "
                     "stop all detiene todos, también los de Claude Code"
    )
    p.add_argument("target", nargs="?", choices=["all"],
                   help="all: todos los visores y servidores MCP de este usuario, vengan de donde vengan")
    p.set_defaults(func=cmd_stop)

    for name, func, help_ in (
        ("login", cmd_login, "asistente para guardar la sesión de un sistema (los de `doctor`: os, cw, db...)"),
        ("logout", cmd_logout, "borra las credenciales guardadas"),
    ):
        p = sub.add_parser(name, help=help_)
        # Sin choices: los sistemas salen de config.toml y se validan al correr el comando.
        p.add_argument("system", help="id del sistema (por ejemplo os, cw, db o db-qa; ver `source list`)")
        if name == "login":
            p.add_argument("--browser", action="store_true", help=argparse.SUPPRESS)
            p.add_argument("--browser-timeout", type=_browser_timeout, metavar="SEGUNDOS", default=300,
                           help="tiempo para completar login/MFA (30–1800; por defecto 300)")
            p.add_argument("--ventana-separada", action="store_true",
                           help="login en una ventana de Chrome aparte (Playwright) aunque la extensión esté conectada")
        p.set_defaults(func=func)

    p = sub.add_parser("pack", help="el pack del equipo (fuentes, Slack, textos, skills): ver, instalar, armar",
                       description="show: el activo · list: los instalados · install <carpeta|zip|URL git> · "
                                   "update: reinstala el activo desde donde vino · remove <nombre> · "
                                   "init <carpeta>: uno de ejemplo · build <carpeta>: el zip para compartir · "
                                   "export: tu configuración actual como pack")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "list", "install", "update", "remove", "init", "build", "export"])
    p.add_argument("target", nargs="?", help="la carpeta, el zip, la URL o el nombre, según la acción")
    p.add_argument("-y", "--yes", action="store_true", help="install/update: sin preguntar (los plugins no se instalan)")
    p.add_argument("-o", "--output", help="build/export: el zip de salida")
    p.add_argument("--name", help="init/export: el nombre del pack")
    p.add_argument("--version", default="0.1.0", help="export: la versión del pack")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("source", help="las fuentes (OpenSearch, CloudWatch, MySQL): ver, agregar, probar, quitar",
                       description="list · add [opensearch|cloudwatch|mysql] (pregunta los campos del proveedor) · "
                                   "test <id> · remove <id>. Las fuentes que agregas van a tu config.toml.")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "test", "remove"])
    p.add_argument("target", nargs="?", help="add: el tipo; test/remove: el id de la fuente")
    p.add_argument("--provider", help="add: el proveedor (por ejemplo direct, aws, mysql-ssh)")
    p.add_argument("--env", help="add: el nombre del entorno (prod, qa, staging...); con uno que ya existe, lo reemplaza")
    p.add_argument("--set", action="append", metavar="CLAVE=VALOR", help="add: una opción, sin preguntar (se repite)")
    p.set_defaults(func=cmd_source)

    p = sub.add_parser("extension", help="registra la extensión de Chrome que entrega las sesiones sola")
    p.add_argument("action", nargs="?", choices=["install", "uninstall"], default="install")
    p.add_argument("--no-abrir", action="store_true", help="no ofrecer abrir chrome://extensions en tus perfiles")
    p.set_defaults(func=cmd_extension)

    p = sub.add_parser("setup", help="vuelve a abrir el asistente de instalación (gráfico o en la terminal)")
    ui = p.add_mutually_exclusive_group()
    ui.add_argument("--gui", action="store_true", help="el asistente gráfico")
    ui.add_argument("--consola", action="store_true", help="el asistente en la terminal")
    p.add_argument("--preview", action="store_true", help="sin cambiar nada")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="revisa requisitos (ssh, hosts, credenciales, alias) y conexiones; repara túneles")
    p.add_argument("--requisitos", action="store_true", help="solo los requisitos de la máquina, sin probar sesiones")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("export", help="exporta una sesión a Markdown")
    p.add_argument("session", nargs="?", help="id, nombre o card (por defecto la más reciente)")
    p.add_argument("-o", "--output", help="archivo de salida, o - para stdout (por defecto ~/.local/share/casebook/exports/)")
    p.add_argument("--rows", type=int, default=20, help="filas por tabla (por defecto 20)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("demo", help="escribe una sesión de ejemplo para probar el visor")
    p.add_argument("--fast", action="store_true")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
