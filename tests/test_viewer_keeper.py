import os
import signal
import socket
import subprocess
import sys
import time

import anyio
import httpx

from casebook import cli
from casebook.config import Config
from casebook.server import ViewerKeeper
from casebook.store import Store
from demo_pack import acme


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(predicate, timeout=5.0):
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.05)


def test_keeper_waits_while_port_is_taken_and_takes_it_when_freed(tmp_path):
    cfg = acme()
    cfg.viewer_port = free_port()
    keeper = ViewerKeeper(Store(tmp_path / "t.db"), cfg, recheck_s=0.1)
    other = socket.socket()  # "otro visor" ocupando el puerto
    other.bind(("127.0.0.1", cfg.viewer_port))
    other.listen()

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(keeper.run)
            await anyio.sleep(0.4)
            assert not keeper.serving  # el puerto es de otro: no lo pelea
            other.close()  # se cierra el otro visor
            await wait_for(lambda: keeper.serving)
            async with httpx.AsyncClient() as client:
                state = (await client.get(f"http://127.0.0.1:{cfg.viewer_port}/api/state")).json()
            assert "latest_event_id" in state
            assert state["served_by"] == "mcp" and state["pid"] == os.getpid()
            keeper.stop()

    anyio.run(go)
    assert keeper.server is None


def test_keeper_stops_cleanly_without_ever_serving(tmp_path):
    cfg = acme()
    cfg.viewer_port = free_port()
    keeper = ViewerKeeper(Store(tmp_path / "t.db"), cfg, recheck_s=10)
    with socket.socket() as other:
        other.bind(("127.0.0.1", cfg.viewer_port))
        other.listen()

        async def go():
            async with anyio.create_task_group() as tg:
                tg.start_soon(keeper.run)
                await anyio.sleep(0.2)
                with anyio.fail_after(2):  # stop() corta la espera de 10 s
                    keeper.stop()

        anyio.run(go)


def test_viewer_command_reuses_running_viewer(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(cli, "_running_viewer", lambda url: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    assert cli.main(["start"]) == 0  # alias de viewer
    assert opened == ["http://127.0.0.1:8765"]
    assert "Ya hay un visor corriendo" in capsys.readouterr().out


def test_viewer_command_port_taken_by_other_program(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_running_viewer", lambda url: False)
    assert cli.main(["viewer", "--no-browser"]) == 1
    assert "ocupado por otro programa" in capsys.readouterr().err


def test_running_viewer_detection(tmp_path):
    from casebook.viewer.app import create_app
    import threading, time, uvicorn

    port = free_port()
    assert cli._running_viewer(f"http://127.0.0.1:{port}") is None  # libre
    server = uvicorn.Server(uvicorn.Config(create_app(Store(tmp_path / "t.db"), acme()), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    try:
        assert cli._running_viewer(f"http://127.0.0.1:{port}") is True
    finally:
        server.should_exit = True
        t.join(timeout=5)


def stop_with(monkeypatch, states):
    """Corre `stop` con una secuencia de respuestas de /api/state; devuelve las señales enviadas."""
    sent = []
    states = iter(states)
    monkeypatch.setattr(cli, "_viewer_state", lambda url: next(states))
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return sent


def test_stop_without_viewer(monkeypatch, capsys):
    sent = stop_with(monkeypatch, [None])
    assert cli.main(["stop"]) == 0
    assert sent == [] and "No hay ningún visor" in capsys.readouterr().out


def test_stop_does_not_touch_claude_code_viewer(monkeypatch, capsys):
    sent = stop_with(monkeypatch, [{"latest_event_id": 1, "served_by": "mcp", "pid": 4321}])
    assert cli.main(["stop"]) == 1
    assert sent == [] and "lo sirve Claude Code" in capsys.readouterr().out


def test_stop_does_not_touch_old_viewer_or_other_program(monkeypatch, capsys):
    sent = stop_with(monkeypatch, [{"latest_event_id": 1}, False])
    assert cli.main(["stop"]) == 1
    assert cli.main(["stop"]) == 1
    assert sent == []
    err = capsys.readouterr().err
    assert "versión anterior" in err and "otro programa" in err


def test_stop_reports_unresponsive_viewer(monkeypatch, capsys):
    sent = stop_with(monkeypatch, [cli.UNRESPONSIVE])
    assert cli.main(["stop"]) == 1
    assert sent == [] and "Ctrl-Z" in capsys.readouterr().err


def test_running_viewer_detects_suspended_process():
    # Un socket que escucha pero nunca contesta, como el de un proceso suspendido.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        assert cli._running_viewer(f"http://127.0.0.1:{sock.getsockname()[1]}") == cli.UNRESPONSIVE


def test_stop_signals_start_viewer_and_waits(monkeypatch, capsys):
    viewer = {"latest_event_id": 1, "served_by": "viewer", "pid": 4321}
    # Sigue sirviendo un momento y después Claude Code toma el puerto: cuenta como detenido.
    sent = stop_with(monkeypatch, [viewer, viewer, {"latest_event_id": 1, "served_by": "mcp", "pid": 99}])
    assert cli.main(["stop"]) == 0
    assert sent == [(4321, cli.signal.SIGTERM)]
    assert "Visor detenido (pid 4321)" in capsys.readouterr().out


def start_viewer(tmp_path):
    """Un `start` real con su propio puerto y base; devuelve (proceso, url, env)."""
    port = free_port()
    (tmp_path / "config" / "casebook").mkdir(parents=True)
    (tmp_path / "config" / "casebook" / "config.toml").write_text(f"[viewer]\nport = {port}\n")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}
    proc = subprocess.Popen([sys.executable, "-m", "casebook.cli", "start", "--no-browser"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while cli._running_viewer(url) is not True:
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.1)
    return proc, url, env


def test_stop_ends_real_start_process(tmp_path, monkeypatch):
    proc, url, _ = start_viewer(tmp_path)
    try:
        with httpx.stream("GET", f"{url}/api/stream"):  # una pestaña conectada no frena el cierre
            cfg = acme()
            cfg.viewer_port = int(url.rsplit(":", 1)[1])
            monkeypatch.setattr(cli.config, "load", lambda: cfg)
            assert cli.main(["stop"]) == 0
        _, err = proc.communicate(timeout=5)
        assert cli._running_viewer(url) is None
        assert "Traceback" not in err and "Cancel" not in err
    finally:
        proc.kill()


def test_ctrl_c_closes_start_quietly_with_open_tabs(tmp_path):
    proc, url, _ = start_viewer(tmp_path)
    try:
        with httpx.stream("GET", f"{url}/api/stream"), httpx.stream("GET", f"{url}/api/stream"):
            time.sleep(0.3)
            started = time.monotonic()
            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=5)
        assert time.monotonic() - started < 1  # los SSE terminan solos: no se espera el timeout_graceful_shutdown
        assert proc.returncode == 0 and "Visor detenido." in out
        assert err == ""
    finally:
        proc.kill()


def test_server_processes_matches_only_our_servers(monkeypatch):
    uid, me = os.getuid(), os.getpid()
    ps = "\n".join([
        f"101 {uid} /home/u/.local/share/casebook/venv/bin/python /home/u/.local/bin/casebook mcp",
        f"102 {uid} /home/u/support app/.venv/bin/python /home/u/support app/.venv/bin/casebook start --no-browser",
        f"103 {uid} /usr/bin/python3 -m casebook.cli viewer",
        f"104 {uid + 1} /home/otro/.local/bin/casebook mcp",  # otro usuario
        f"105 {uid} /home/u/.local/bin/casebook doctor",
        f"106 {uid} grep casebook mcp",
        f"107 {uid} [dev-support-hyr] <defunct>",
        f"{me} {uid} /home/u/.local/bin/casebook mcp",  # este proceso
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout=ps + "\n"))
    assert cli._server_processes() == {101: "mcp", 102: "start", 103: "viewer"}


def test_stop_all_signals_every_server_and_waits(monkeypatch, capsys):
    sent = []
    scans = iter([{101: "mcp", 102: "start"}, {101: "mcp"}, {}])
    monkeypatch.setattr(cli, "_server_processes", lambda: next(scans))
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["stop", "all"]) == 0
    assert sent == [(101, cli.signal.SIGTERM), (101, cli.signal.SIGCONT), (102, cli.signal.SIGTERM), (102, cli.signal.SIGCONT)]
    out = capsys.readouterr().out
    assert "detenido: pid 101 (Claude Code)" in out and "detenido: pid 102 (start)" in out and "/mcp" in out


def test_stop_all_without_processes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_server_processes", lambda: {})
    assert cli.main(["stop", "all"]) == 0
    assert "No hay ningún proceso" in capsys.readouterr().out


def test_stop_all_ends_real_start_and_mcp(tmp_path, monkeypatch):
    port = free_port()
    (tmp_path / "config" / "casebook").mkdir(parents=True)
    (tmp_path / "config" / "casebook" / "config.toml").write_text(f"[viewer]\nport = {port}\n")
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_DATA_HOME": str(tmp_path / "data")}
    run = [sys.executable, "-m", "casebook.cli"]
    procs = [subprocess.Popen(run + ["start", "--no-browser"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)]
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while cli._running_viewer(url) is not True:
            assert procs[0].poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        # Un servidor MCP esperando el puerto, como el de una sesión de Claude Code (stdin abierto).
        procs.append(subprocess.Popen(run + ["mcp"], env=env, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        ours = {p.pid for p in procs}
        real = cli._server_processes
        # Solo los procesos de este test: los servidores MCP reales de la máquina no se tocan.
        monkeypatch.setattr(cli, "_server_processes", lambda: {pid: c for pid, c in real().items() if pid in ours})
        deadline = time.monotonic() + 15
        while len(cli._server_processes()) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert cli.main(["stop", "all"]) == 0
        for p in procs:
            p.wait(timeout=5)
        assert cli._running_viewer(url) is None  # nadie retomó el puerto
    finally:
        for p in procs:
            p.kill()


def test_viewer_command_fails_when_server_does_not_start(monkeypatch):
    from casebook.viewer.app import ViewerServer

    monkeypatch.setattr(cli, "_running_viewer", lambda url: None)
    monkeypatch.setattr(ViewerServer, "run", lambda self, sockets=None: None)  # nunca llega a started
    assert cli.main(["viewer", "--no-browser"]) == 1


def test_keeper_serves_streams_again_after_a_previous_shutdown(tmp_path):
    cfg = acme()
    cfg.viewer_port = free_port()
    keeper = ViewerKeeper(Store(tmp_path / "t.db"), cfg, recheck_s=0.1)
    keeper.app.state.stopping = True  # como si un servidor anterior de esta app se hubiera apagado

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(keeper.run)
            await wait_for(lambda: keeper.serving)
            assert keeper.app.state.stopping is False
            keeper.stop()

    anyio.run(go)
    assert keeper.app.state.stopping is True
