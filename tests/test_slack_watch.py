"""Vigilancia de Slack: qué se analiza, el estado, los análisis y que nunca se permita escribir en Slack."""
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from casebook import config, slack_watch
from casebook.config import Config
from casebook.store import Store
from casebook.viewer.app import create_app
from demo_pack import acme


@pytest.fixture(autouse=True)
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "slack-watch.json"
    monkeypatch.setattr(slack_watch, "STATE_PATH", path)
    monkeypatch.setattr(slack_watch, "WORK_DIR", tmp_path / "work")
    return path


def msg(ts, user="U1", name="Ana", bot=False, text="Hey team, a sale is missing", request=True):
    return {"ts": ts, "user_id": user, "user_name": name, "is_bot": bot, "is_request": request, "preview": text}


def reply(thread, ts, user, name="Otro", request=True):
    return {"thread_ts": thread, "ts": ts, "user_id": user, "user_name": name, "is_request": request,
            "preview": "..."}


def test_new_messages_are_analyzed_and_bots_ignored():
    state = {"last_ts": "100.000001"}
    jobs = slack_watch.plan(state, {"messages": [msg("100.000001"), msg("100.000002"),
                                                 msg("100.000003", bot=True)], "replies": []}, now=0)
    assert [j.ts for j in jobs] == ["100.000002"]  # el ya visto no se repite y el bot no se analiza
    assert state["last_ts"] == "100.000003"
    assert set(state["threads"]) == {"100.000002"}


def test_ts_compare_as_integers_not_floats():
    # Como float, 1792356104.315169 y 1792356104.315170 pueden quedar iguales; como enteros no.
    state = {"last_ts": "1792356104.315169"}
    jobs = slack_watch.plan(state, {"messages": [msg("1792356104.315170")], "replies": []}, now=0)
    assert len(jobs) == 1


def test_follow_up_only_after_someone_else_answered():
    state = {"last_ts": "1.000000"}
    slack_watch.plan(state, {"messages": [msg("2.000000", user="U1")], "replies": []}, now=0)
    # El autor agrega algo antes de que nadie responda: no es una repregunta.
    assert slack_watch.plan(state, {"messages": [], "replies": [reply("2.000000", "3.000000", "U1")]}, now=0) == []
    # Responde soporte y después el autor vuelve a preguntar: se analiza esa repregunta.
    jobs = slack_watch.plan(state, {"messages": [], "replies": [reply("2.000000", "4.000000", "U9"),
                                                                 reply("2.000000", "5.000000", "U1", "Ana")]}, now=0)
    assert [(j.ts, j.thread_ts, j.follow_up) for j in jobs] == [("5.000000", "2.000000", True)]
    # La misma respuesta no vuelve a disparar nada en el siguiente poll.
    assert slack_watch.plan(state, {"messages": [], "replies": [reply("2.000000", "5.000000", "U1")]}, now=0) == []


def test_thanks_and_announcements_are_not_analyzed():
    state = {"last_ts": "1.000000"}
    jobs = slack_watch.plan(state, {"messages": [msg("2.000000"), msg("3.000000", request=False)],
                                    "replies": []}, now=0)
    assert [j.ts for j in jobs] == ["2.000000"]
    # Soporte responde y el autor agradece: no es una repregunta, y el hilo sigue en seguimiento.
    assert slack_watch.plan(state, {"messages": [], "replies": [reply("2.000000", "4.000000", "U9"),
                                                                 reply("2.000000", "5.000000", "U1", request=False)]},
                            now=0) == []
    jobs = slack_watch.plan(state, {"messages": [], "replies": [reply("2.000000", "6.000000", "U1")]}, now=0)
    assert [j.ts for j in jobs] == ["6.000000"]  # sigue respondido: una pregunta posterior sí cuenta


def test_old_threads_stop_being_tracked():
    state = {"last_ts": "1.000000"}
    slack_watch.plan(state, {"messages": [msg("2.000000")], "replies": []}, now=0)
    slack_watch.plan(state, {"messages": [], "replies": []}, now=slack_watch.THREAD_WINDOW_S + 1)
    assert state["threads"] == {}


def test_poll_prompt_lists_tracked_threads_and_forbids_writing():
    prompt = slack_watch.poll_prompt("C1", "9.000000", [("2.000000", "5.000000")])
    assert 'oldest="9.000000"' in prompt and "thread_ts=2.000000 visto=5.000000" in prompt
    assert "nunca envíes mensajes" in prompt


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, prompt, model, schema, allowed, timeout, holder=None):
        self.calls.append({"prompt": prompt, "model": model, "allowed": allowed})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def watcher(tmp_path, runner, connector="connected", **cfg_values):
    """connector: lo que respondería `claude mcp get` (nunca se llama al real en los tests)."""
    cfg = acme()
    for key, value in {"slack_watch": True, "slack_channel_id": "C1", **cfg_values}.items():
        setattr(cfg, key, value)
    store = Store(tmp_path / "s.db")
    check = connector if callable(connector) else (lambda: connector)
    return slack_watch.SlackWatcher(store, cfg, runner=runner, listen=False, connector_check=check), store


def wait_until(predicate, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_first_poll_starts_from_now_without_reading_history(tmp_path):
    runner = FakeRunner()
    w, _ = watcher(tmp_path, runner)
    assert w.poll_once() == [] and runner.calls == []
    state = slack_watch.read_state()
    assert state["channel_id"] == "C1" and float(state["last_ts"]) > 0


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(slack_watch, "TICK_S", 0.02)
    monkeypatch.setattr(slack_watch, "DEBOUNCE_S", 0.05)


def test_turning_on_reads_once_and_then_only_on_notifications(tmp_path, fast):
    import time

    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    calls = []
    w.poll_once = lambda: calls.append(1) or []
    w.start()
    try:
        time.sleep(0.1)
        assert calls == []  # apagada: no lee nada
        w.cfg.slack_watch = True
        assert wait_until(lambda: calls == [1])  # al prender, una lectura para fijar desde dónde vigilar
        time.sleep(0.2)
        assert calls == [1]  # sin notificaciones ni respaldo no vuelve a leer (no gasta tokens)
        w.notified()
        w.notified()  # varias notificaciones seguidas van en una sola lectura
        assert wait_until(lambda: calls == [1, 1])
        time.sleep(0.2)
        assert calls == [1, 1]
    finally:
        w.stop()


def test_backup_poll_reads_without_notifications(tmp_path, fast, monkeypatch):
    w, _ = watcher(tmp_path, FakeRunner(), slack_backup_poll_minutes=1)
    monkeypatch.setattr(w.cfg, "slack_backup_poll_minutes", 0.004)  # ~0.25 s
    calls = []
    w.poll_once = lambda: calls.append(1) or []
    w.start()
    try:
        assert wait_until(lambda: len(calls) >= 3)
    finally:
        w.stop()


@pytest.mark.parametrize("app,summary,body,hints,expected", [
    ("Slack", "New message in #soporte", "Ana: help", {}, True),
    ("slack", "Ana", "in #soporte: a sale is missing", {}, True),
    ("Chromium", "Ana (#soporte)", "", {"desktop-entry": ("s", "slack")}, True),
    ("Slack", "New message in #general", "Ana: hola", {}, False),
    ("Thunderbird", "soporte digest", "", {}, False),
])
def test_slack_notification_matching(app, summary, body, hints, expected):
    assert slack_watch.is_slack_notification(app, summary, body, hints, "soporte") is expected


def test_listener_triggers_on_matching_notify(monkeypatch):
    from types import SimpleNamespace
    import jeepney.io.blocking as blocking

    matched, states = [], []
    listener = slack_watch.NotificationListener(lambda: matched.append(1), lambda: "soporte",
                                                lambda ok, err: states.append((ok, err)))
    messages = [SimpleNamespace(body=("Slack", 0, "", "New message in #general", "x", [], {}, -1)),
                SimpleNamespace(body=("Slack", 0, "", "New message in #soporte", "x", [], {}, -1))]

    class Conn:
        def send_and_get_reply(self, msg, timeout=None):
            return None

        def receive(self, timeout=None):
            if messages:
                return messages.pop(0)
            listener.stop()
            raise TimeoutError

        def close(self):
            pass

    monkeypatch.setattr(blocking, "open_dbus_connection", lambda bus: Conn())
    listener.run()
    assert matched == [1] and states == [(True, None)]


def test_listener_reports_when_there_is_no_bus(monkeypatch):
    import jeepney.io.blocking as blocking

    states = []
    listener = slack_watch.NotificationListener(lambda: None, lambda: "x", lambda ok, err: states.append((ok, err)))

    def fail(bus):
        listener.stop()
        raise FileNotFoundError("no hay bus de sesión")

    monkeypatch.setattr(blocking, "open_dbus_connection", fail)
    listener.run()
    assert states[0][0] is False and "no hay bus de sesión" in states[0][1]


def test_listener_does_not_connect_while_watch_is_off(monkeypatch):
    import threading
    import jeepney.io.blocking as blocking

    opened = []
    monkeypatch.setattr(slack_watch, "TICK_S", 0.01)
    monkeypatch.setattr(blocking, "open_dbus_connection", lambda bus: opened.append(bus))
    listener = slack_watch.NotificationListener(lambda: None, lambda: "x", lambda ok, err: None, lambda: False)
    t = threading.Thread(target=listener.run)
    t.start()
    import time

    time.sleep(0.1)
    listener.stop()
    t.join(1)
    assert opened == [] and not t.is_alive()


def test_notifications_are_ignored_while_watch_is_off(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    w.notified()
    assert w._notified_at is None and slack_watch.read_state() == {}


def test_starting_without_slack_history_does_not_write_state(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    w.start()
    w.stop()
    assert not slack_watch.STATE_PATH.exists()


def test_turning_on_keeps_threads_from_a_manual_analysis(tmp_path):
    slack_watch.write_state({"threads": {"2.000000": {"author": "U1", "seen": "2.000000", "answered": False,
                                                       "at": 9e12}}})
    w, _ = watcher(tmp_path, FakeRunner())
    w.poll_once()
    assert "2.000000" in slack_watch.read_state()["threads"]


def test_viewer_survives_a_watcher_that_fails_to_start(tmp_path, monkeypatch):
    import socket
    import threading

    import httpx
    import uvicorn

    from casebook.viewer.app import ViewerServer

    def broken(*a, **kw):
        raise RuntimeError("falla al crear el vigilante")

    monkeypatch.setattr(slack_watch, "SlackWatcher", broken)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ViewerServer(uvicorn.Config(create_app(Store(tmp_path / "s.db"), acme()), host="127.0.0.1",
                                         port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: server.started, timeout=5)
        assert httpx.get(f"http://127.0.0.1:{port}/api/state").status_code == 200
        assert httpx.get(f"http://127.0.0.1:{port}/api/slack").status_code == 200
        assert server.watcher is None
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_automatic_never_repeats_a_registered_message(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000",
                             "jobs": {"2.000000": {"status": "done", "session_id": 7, "at": 9e12}}})
    # Aunque el cursor vuelva atrás (por ejemplo, otro canal y vuelta), un mensaje registrado no se encola de nuevo.
    w, _ = watcher(tmp_path, FakeRunner({"messages": [msg("2.000000"), msg("3.000000")], "replies": []}))
    assert [j.ts for j in w.poll_once()] == ["3.000000"]
    assert slack_watch.read_state()["jobs"]["3.000000"]["status"] == "queued"


def test_poll_queues_new_messages_and_uses_the_poll_model(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    runner = FakeRunner({"messages": [msg("2.000000")], "replies": []})
    w, _ = watcher(tmp_path, runner)
    jobs = w.poll_once()
    assert [j.ts for j in jobs] == ["2.000000"] and w._queue == jobs
    assert runner.calls[0]["model"] == "haiku" and runner.calls[0]["allowed"] == slack_watch.READ_TOOLS
    assert slack_watch.read_state()["last_error"] is None


def test_poll_error_is_shown_and_keeps_the_cursor(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    w, _ = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError("sin conector")))
    assert w.poll_once() == []
    state = slack_watch.read_state()
    assert state["last_error"]["code"] == "other" and state["last_error"]["detail"] == "sin conector"
    assert state["last_ts"] == "1.000000"


ANALYSIS = {"category": "Sales", "hypothesis": "No llegó", "outcome": "ticket", "summary": "Falta una venta",
            "steps": ["Buscar en CloudWatch"], "card": "Missing sale", "suggested_reply": "Hey @Ana, ..."}


def test_analysis_creates_a_session_with_the_result(tmp_path):
    runner = FakeRunner(ANALYSIS)
    w, store = watcher(tmp_path, runner)
    sid = w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "Hey team"))
    session = store.get_session(sid)
    assert session["name"].startswith("Slack · Ana · ")
    events = store.events(after=0, limit=50)
    assert not [e for e in events if e["type"] == "note"]  # no son notas de Claude: van en su propia categoría
    message, analysis = [e["payload"] for e in events if e["type"] == "slack"]
    assert message["kind"] == "message" and message["text"] == "Hey team" and message["author"] == "Ana"
    assert analysis["kind"] == "analysis" and analysis["outcome"] == "ticket"
    assert analysis["outcome_label"] == "Abrir ticket" and analysis["result"] == ANALYSIS
    assert "**Abrir ticket**" in analysis["text"] and "Hey @Ana" in analysis["text"]
    assert runner.calls[0]["model"] == "sonnet" and f"session_id={sid}" in runner.calls[0]["prompt"]
    state = slack_watch.read_state()
    assert state["today"]["count"] == 1 and state["recent"][0]["outcome"] == "ticket"
    assert state["analyzing"] is None


def test_daily_cap_records_the_message_without_analyzing(tmp_path):
    slack_watch.write_state({"today": {"date": slack_watch._today(), "count": 2}})
    runner = FakeRunner()
    w, store = watcher(tmp_path, runner, slack_max_analyses_per_day=2)
    w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "Hey team"))
    assert runner.calls == []
    assert slack_watch.read_state()["recent"][0]["outcome"] == "skipped"


def test_failed_analysis_is_noted(tmp_path):
    w, store = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError("timeout")))
    w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "Hey team"))
    slack = [e["payload"] for e in store.events(after=0, limit=50) if e["type"] == "slack"]
    error = slack[-1]["error"]
    assert slack[-1]["kind"] == "error" and error["title"] == "El análisis no se pudo completar"
    assert error["detail"] == "timeout" and "Analizar el último mensaje" in error["hint"]
    assert slack_watch.read_state()["recent"][0]["outcome"] == "error"


def test_no_write_tool_is_ever_allowed():
    for allowed in (slack_watch.READ_TOOLS, slack_watch.ANALYSIS_TOOLS):
        assert not set(allowed) & set(slack_watch.DENIED_TOOLS)
    assert set(slack_watch.WRITE_TOOLS) <= set(slack_watch.DENIED_TOOLS)
    assert {"Bash", "Write", "Edit", "WebFetch"} <= set(slack_watch.DENIED_TOOLS)
    assert all("send" not in t and "add_" not in t and "update" not in t for t in slack_watch.READ_TOOLS)


def test_every_server_tool_is_explicitly_allowed_or_denied(tmp_path):
    """Una tool nueva del servidor tiene que decidir si el análisis automático la puede usar."""
    import anyio

    from casebook.events import Recorder
    from casebook.server import build

    mcp = build(Recorder(Store(tmp_path / "s.db")), None)
    tools = anyio.run(mcp.list_tools)
    decided = set(slack_watch.ANALYSIS_TOOLS) | set(slack_watch.DENIED_TOOLS)
    assert {slack_watch.OWN + t.name for t in tools} <= decided
    assert slack_watch.OWN + "knowledge_record" in slack_watch.DENIED_TOOLS


def test_run_claude_denies_write_tools_and_reads_structured_output(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        returncode = 0

        def __init__(self, command, **kw):
            seen["command"], seen["cwd"] = command, kw["cwd"]

        def communicate(self, timeout=None):
            return json.dumps({"is_error": False, "structured_output": {"ok": True}}), ""

    monkeypatch.setattr(slack_watch.subprocess, "Popen", Proc)
    assert slack_watch.run_claude("p", "haiku", {}, slack_watch.READ_TOOLS, 10) == {"ok": True}
    command = seen["command"]
    assert command[command.index("--disallowedTools") + 1] == ",".join(slack_watch.DENIED_TOOLS)
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--allowedTools") + 1] == ",".join(slack_watch.READ_TOOLS)
    assert seen["cwd"] == tmp_path / "work"  # fuera del repo


def test_run_claude_reports_errors(monkeypatch):
    class Proc:
        returncode = 1

        def __init__(self, *a, **kw):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("claude", timeout)

        def kill(self):
            pass

    calls = iter([subprocess.TimeoutExpired("claude", 1), ("", "")])

    def communicate(self, timeout=None):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    Proc.communicate = communicate
    monkeypatch.setattr(slack_watch.subprocess, "Popen", Proc)
    with pytest.raises(slack_watch.ClaudeError, match="no terminó"):
        slack_watch.run_claude("p", "haiku", {}, (), 1)


def test_status_endpoint_and_toggle_setting(tmp_path):
    cfg = acme()
    client = TestClient(create_app(Store(tmp_path / "s.db"), cfg))
    body = client.get("/api/slack").json()
    assert body["enabled"] is False and body["channel_id"] == "C0ACME" and body["recent"] == []
    assert body["backup_poll_minutes"] == 0  # por defecto solo notificaciones: vigilar no gasta tokens
    keys = {s["key"] for s in client.get("/api/settings").json()["settings"]}
    assert {"slack.watch", "slack.channel_name", "slack.backup_poll_minutes", "slack.analysis_model"} <= keys


def latest(*messages):
    return {"messages": list(messages), "replies": []}


def run_manual(w):
    assert w.analyze_latest()
    assert wait_until(lambda: not w._manual.locked())  # terminó la búsqueda del último mensaje
    return slack_watch.read_state()["manual"]


def test_manual_analysis_queues_the_latest_human_message(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000", name="Ana"), msg("3.000000", bot=True),
                                               msg("1.000000", name="Leo"))), slack_watch=False)
    manual = run_manual(w)
    assert manual["status"] == "queued" and manual["author"] == "Ana" and manual["ts"] == "2.000000"
    assert [j.ts for j in w._queue] == ["2.000000"]
    assert slack_watch.read_state()["jobs"]["2.000000"]["status"] == "queued"


@pytest.mark.parametrize("status", ["queued", "analyzing"])
def test_manual_analysis_does_not_repeat_one_in_progress(tmp_path, status):
    slack_watch.write_state({"jobs": {"2.000000": {"status": status, "session_id": 7, "at": 9e12}}})
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000"))), slack_watch=False)
    manual = run_manual(w)
    assert manual["status"] == "queued" and manual["ts"] == "2.000000"  # el panel sigue ese análisis
    assert w._queue == [] and slack_watch.read_state()["jobs"]["2.000000"]["status"] == status


def test_manual_analysis_does_not_repeat_one_done(tmp_path):
    slack_watch.write_state({"jobs": {"2.000000": {"status": "done", "session_id": 7, "at": 9e12}}})
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000"))), slack_watch=False)
    manual = run_manual(w)
    assert manual["status"] == "existing" and manual["job_status"] == "done" and manual["session_id"] == 7
    assert w._queue == []


def test_manual_analysis_retries_a_failed_one(tmp_path):
    slack_watch.write_state({"jobs": {"2.000000": {"status": "error", "session_id": 7, "at": 9e12}}})
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000"))), slack_watch=False)
    assert run_manual(w)["status"] == "queued" and [j.ts for j in w._queue] == ["2.000000"]


def test_manual_then_automatic_does_not_analyze_twice(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000")), {"messages": [msg("2.000000")], "replies": []}))
    run_manual(w)
    assert w.poll_once() == []  # el automático ve el mensaje pero ya está registrado
    assert [j.ts for j in w._queue] == ["2.000000"]


def test_finished_analysis_updates_the_registry_and_manual_status(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000")), ANALYSIS), slack_watch=False)
    run_manual(w)
    job = w._queue.pop(0)
    sid = w.analyze(job)
    state = slack_watch.read_state()
    assert state["jobs"]["2.000000"] == {"status": "done", "session_id": sid, "at": state["jobs"]["2.000000"]["at"]}
    assert state["manual"]["status"] == "done" and state["manual"]["session_id"] == sid


def test_restart_does_not_resume_interrupted_analyses(tmp_path):
    slack_watch.write_state({"analyzing": "2.000000", "manual": {"status": "searching", "at": 1},
                             "jobs": {"2.000000": {"status": "analyzing", "session_id": 7, "at": 9e12},
                                      "3.000000": {"status": "done", "session_id": 8, "at": 9e12}}})
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    w.start()
    w.stop()
    state = slack_watch.read_state()
    assert state["jobs"]["2.000000"]["status"] == "error" and state["jobs"]["3.000000"]["status"] == "done"
    assert state["analyzing"] is None and state["manual"] is None


def test_analyze_latest_endpoint(tmp_path):
    app = create_app(Store(tmp_path / "s.db"), acme())
    client = TestClient(app)
    headers = {"x-casebook-viewer": "1"}
    assert client.post("/api/slack/analyze-latest", headers=headers).status_code == 503  # sin vigilante
    assert client.post("/api/slack/analyze-latest").status_code == 403  # sin el header del visor

    class Watcher:
        busy = False

        def analyze_latest(self):
            started, Watcher.busy = not Watcher.busy, True
            return started

    app.state.watcher = Watcher()
    assert client.post("/api/slack/analyze-latest", headers=headers).status_code == 202
    assert client.post("/api/slack/analyze-latest", headers=headers).status_code == 409


def test_slack_events_are_searchable_exported_and_resumed(tmp_path):
    from casebook import export

    w, store = watcher(tmp_path, FakeRunner(ANALYSIS))
    sid = w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "a sale is missing"))
    assert any(r["type"] == "slack" for r in store.search("sale is missing"))
    notes = store.recent_notes(sid)  # lo que recibe Claude con session_resume
    assert [n["kind"] for n in notes] == ["analysis", "message"]
    md = export.render(store, sid)
    assert "**Slack · Mensaje de Ana" in md and "> a sale is missing" in md
    assert "## Conclusión (última nota)" in md and "Hey @Ana" in md  # sin notas, la conclusión es el análisis


# --- robustez (revisión) --------------------------------------------------------------------------------------


def test_clean_drops_malformed_entries():
    polled = {"messages": [msg("2.000000"), {"ts": "p1792"}, {"ts": None}, "basura", {"ts": "3.5", "user_id": None}],
              "replies": [reply("2.000000", "4.000000", "U9"), {"ts": "5.0"}, {"thread_ts": "x", "ts": "6.0"}]}
    out = slack_watch.clean(polled)
    assert [m["ts"] for m in out["messages"]] == ["2.000000", "3.5"]
    assert out["messages"][1]["user_name"] == "alguien" and out["messages"][1]["user_id"] == ""
    assert [r["ts"] for r in out["replies"]] == ["4.000000"]
    assert slack_watch.clean(None) == {"messages": [], "replies": []}


def test_poll_with_garbage_from_the_model_does_not_break(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    w, _ = watcher(tmp_path, FakeRunner({"messages": [{"ts": "no-es-un-ts"}, msg("2.000000")], "replies": None}))
    assert [j.ts for j in w.poll_once()] == ["2.000000"]


def test_poll_loop_survives_unexpected_errors(tmp_path, fast):
    w, _ = watcher(tmp_path, FakeRunner())
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("algo raro")

    w.poll_once = boom
    w.start()
    try:
        assert wait_until(lambda: calls)
        assert wait_until(lambda: "algo raro" in str(slack_watch.read_state().get("last_error")))
        w.notified()
        assert wait_until(lambda: len(calls) == 2)  # el thread sigue vivo
    finally:
        w.stop()


def test_analysis_loop_survives_unexpected_errors(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    done = []

    def analyze(job):
        if job.ts == "2.000000":
            raise RuntimeError("falla rara")
        done.append(job.ts)

    w.analyze = analyze
    w.start()
    try:
        w._enqueue([slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "x"),
                    slack_watch.Job("3.000000", "3.000000", "U1", "Ana", "y")])
        assert wait_until(lambda: done == ["3.000000"])  # la cola sigue después del error
        assert slack_watch.read_state()["jobs"]["2.000000"]["status"] == "error"
    finally:
        w.stop()


def test_manual_never_stays_stuck_searching(tmp_path, monkeypatch):
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000"))), slack_watch=False)
    monkeypatch.setattr(slack_watch, "_claim", lambda *a, **kw: 1 / 0)
    manual = run_manual(w)
    assert manual["status"] == "error" and "ZeroDivisionError" in manual["error"]["detail"]
    assert not w._manual.locked()


def test_busy_channel_is_read_without_waiting_for_silence(tmp_path, fast, monkeypatch):
    import time

    monkeypatch.setattr(slack_watch, "MAX_DEBOUNCE_S", 0.2)
    w, _ = watcher(tmp_path, FakeRunner())
    calls = []
    w.poll_once = lambda: calls.append(1) or []
    w.start()
    try:
        assert wait_until(lambda: calls == [1])  # la lectura inicial
        end = time.monotonic() + 0.6
        while time.monotonic() < end:  # notificaciones seguidas, sin pausa de DEBOUNCE_S
            w.notified()
            time.sleep(0.01)
        assert len(calls) >= 2
    finally:
        w.stop()


def test_stopping_kills_claude_and_what_it_launched():
    import os
    import subprocess
    import time

    # Como `claude -p` y su servidor MCP: un proceso en su propio grupo con un hijo.
    proc = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!; wait"], stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    child = int(proc.stdout.readline())
    slack_watch.kill_group(proc)
    proc.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("el hijo sigue vivo")


def test_analysis_cut_by_closing_the_viewer_says_interrupted(tmp_path):
    w, store = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError("salió con -9")))
    w._stop.set()
    w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "x"))
    slack = [e["payload"] for e in store.events(after=0, limit=50) if e["type"] == "slack"]
    assert slack[-1]["error"]["code"] == "interrupted" and "interrumpió al cerrar el visor" in slack[-1]["text"]


# --- errores claros, hilos en la misma sesión y adjuntos ---------------------------------------------------------

# Textos reales que escribió el modelo cuando no pudo leer Slack.
CONNECTOR_TEXT = ("No se pudo leer Slack: El conector de Slack (claude.ai Slack) no está disponible en esta sesión: "
                  "la herramienta mcp__claude_ai_Slack__slack_read_channel no aparece tras tres búsquedas con "
                  "ToolSearch. Los listados de la sesión marcan el servidor como pendiente de autorización (OAuth).")
CHANNEL_TEXT = ("No se pudo leer Slack: slack_read_channel devolvió channel_not_found para channel_id=C0ACME "
                "(limit=10, response_format=detailed).")


def test_free_text_errors_are_classified():
    assert slack_watch.classify(CONNECTOR_TEXT) == "connector"
    assert slack_watch.classify(CHANNEL_TEXT) == "channel"
    assert slack_watch.classify("algo raro pasó") == "other"


def test_error_info_is_short_with_a_hint_and_the_detail_apart():
    info = slack_watch.error_info("connector", CONNECTOR_TEXT)
    assert info["title"] == "El conector de Slack no está disponible" and "/mcp" in info["hint"]
    assert info["detail"] == CONNECTOR_TEXT.strip() and info["count"] == 1
    again = slack_watch.repeat(info, slack_watch.error_info("connector", "otra vez"))
    assert again["count"] == 2
    assert slack_watch.repeat(again, slack_watch.error_info("channel"))["count"] == 1  # otro error: el contador vuelve


def fake_popen(monkeypatch, payload):
    class Proc:
        returncode = 0
        pid = 0

        def __init__(self, *a, **kw):
            pass

        def communicate(self, timeout=None):
            return json.dumps(payload), ""

    monkeypatch.setattr(slack_watch.subprocess, "Popen", Proc)


def test_run_claude_turns_the_model_error_into_a_code(monkeypatch):
    fake_popen(monkeypatch, {"is_error": False, "structured_output": {
        "messages": [], "replies": [], "error": {"code": "channel", "detail": "channel_not_found"}}})
    with pytest.raises(slack_watch.ClaudeError) as exc:
        slack_watch.run_claude("p", "haiku", {}, (), 10)
    assert exc.value.code == "channel" and str(exc.value) == "channel_not_found"


def test_run_claude_classifies_a_free_text_answer(monkeypatch):
    fake_popen(monkeypatch, {"is_error": False, "result": CONNECTOR_TEXT})
    with pytest.raises(slack_watch.ClaudeError) as exc:
        slack_watch.run_claude("p", "haiku", {}, (), 10)
    assert exc.value.code == "connector"


def test_connector_not_loaded_in_time_is_retried_once(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    runner = FakeRunner(slack_watch.ClaudeError(CONNECTOR_TEXT, "connector"), {"messages": [msg("2.000000")],
                                                                               "replies": []})
    w, _ = watcher(tmp_path, runner, connector="connected")
    assert [j.ts for j in w.poll_once()] == ["2.000000"] and len(runner.calls) == 2


@pytest.mark.parametrize("status,code", [("needs_auth", "connector"), ("missing", "connector_missing")])
def test_unauthorized_connector_is_reported_and_stops_spending_tokens(tmp_path, status, code):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    runner = FakeRunner(slack_watch.ClaudeError(CONNECTOR_TEXT, "other"))
    connector = {"status": status}
    w, _ = watcher(tmp_path, runner, connector=lambda: connector["status"])
    w.poll_once()
    error = slack_watch.read_state()["last_error"]
    assert error["code"] == code and "/mcp" in error["hint"] and len(runner.calls) == 1
    # Mientras siga sin autorizar, leer el canal no llama a Claude: solo se vuelve a revisar el conector.
    w.poll_once()
    assert len(runner.calls) == 1 and slack_watch.read_state()["last_error"]["count"] == 2
    # Autorizado: se vuelve a leer el canal normalmente.
    connector["status"] = "connected"
    runner.results.append({"messages": [msg("2.000000")], "replies": []})
    assert [j.ts for j in w.poll_once()] == ["2.000000"]
    assert slack_watch.read_state()["last_error"] is None


def test_channel_error_from_the_manual_button_is_structured(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError("channel_not_found", "channel")), slack_watch=False)
    manual = run_manual(w)
    assert manual["status"] == "error" and manual["error"]["code"] == "channel"
    assert manual["error"]["title"] == "No encontré el canal de Slack" and "Ajustes" in manual["error"]["hint"]


def test_manual_with_only_bots_says_so(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner(latest(msg("2.000000", bot=True))), slack_watch=False)
    assert run_manual(w)["error"]["code"] == "empty"


def test_listener_error_is_structured(tmp_path):
    w, _ = watcher(tmp_path, FakeRunner())
    w._listener_state(False, "FileNotFoundError: no hay bus")
    error = slack_watch.read_state()["listener_error"]
    assert error["code"] == "listener" and "respaldo" in error["hint"] and "no hay bus" in error["detail"]
    w._listener_state(True, None)
    assert slack_watch.read_state()["listener_error"] is None


def test_analysis_connector_error_also_shows_in_the_panel(tmp_path):
    w, store = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError(CONNECTOR_TEXT, "connector")),
                       connector="needs_auth")
    w.analyze(slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "x"))
    assert slack_watch.read_state()["last_error"]["code"] == "connector"
    error = [e["payload"] for e in store.events(after=0, limit=50) if e["type"] == "slack"][-1]["error"]
    assert error["title"] == "El conector de Slack no está disponible"


def test_follow_up_continues_in_the_session_of_the_main_message(tmp_path):
    first = {**ANALYSIS, "attachments_reviewed": []}
    runner = FakeRunner(first, {**first, "summary": "Con el dato nuevo, sigue siendo un ticket"})
    w, store = watcher(tmp_path, runner)
    main = slack_watch.Job("2.000000", "2.000000", "U1", "Ana", "falta una venta")
    sid = w.analyze(main)
    slack_watch.write_state({**slack_watch.read_state()})  # el registro ya tiene el hilo con su sesión
    follow = slack_watch.Job("5.000000", "2.000000", "U1", "Ana", "¿y ahora?", follow_up=True)
    assert w.analyze(follow) == sid  # misma sesión
    prompt = runner.calls[1]["prompt"]
    assert f"session_read(session_id={sid})" in prompt and "no repitas el análisis completo" in prompt
    kinds = [(e["type"], e["payload"].get("kind")) for e in store.events(session_id=sid, after=0, limit=50)]
    assert kinds.count(("slack", "analysis")) == 2 and ("session.resumed", None) in kinds
    assert len({s["id"] for s in store.list_sessions()}) == 1
    recent = slack_watch.read_state()["recent"]
    assert [r["session_id"] for r in recent] == [sid, sid] and recent[0]["follow_up"] is True


def test_follow_up_without_the_main_session_opens_a_new_one(tmp_path):
    w, store = watcher(tmp_path, FakeRunner({**ANALYSIS, "attachments_reviewed": []}))
    sid = w.analyze(slack_watch.Job("5.000000", "2.000000", "U1", "Ana", "¿y ahora?", follow_up=True))
    assert store.get_session(sid)["name"].startswith("Repregunta · Ana")


def test_attachments_reach_the_analysis_and_missing_ones_are_flagged(tmp_path):
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})
    files = [{"id": "F0C9QQMP8P5", "name": "image.png", "mimetype": "image/png"},
             {"id": "F0CARB6PXG8", "name": "otra.png", "mimetype": "image/png"}]
    polled = {"messages": [{**msg("2.000000"), "files": files + [{"id": "no-es-id"}]}], "replies": []}
    result = {**ANALYSIS, "attachments_reviewed": [{"file_id": "F0C9QQMP8P5", "description": "CRM con 0 leads"}]}
    runner = FakeRunner(polled, result)
    w, store = watcher(tmp_path, runner)
    job = w.poll_once()[0]
    assert [f["id"] for f in job.files] == ["F0C9QQMP8P5", "F0CARB6PXG8"]  # el ID inválido se descarta
    w.analyze(job)
    prompt = runner.calls[1]["prompt"]
    assert "F0C9QQMP8P5 (image/png)" in prompt and "TODOS los archivos adjuntos" in prompt
    events = [e["payload"] for e in store.events(after=0, limit=50) if e["type"] == "slack"]
    assert [f["id"] for f in events[0]["files"]] == ["F0C9QQMP8P5", "F0CARB6PXG8"]
    assert events[1]["result"]["attachments_missing"] == ["F0CARB6PXG8"]
    assert "**Adjuntos revisados:**" in events[1]["text"]
    assert "**Adjuntos sin revisar:** F0CARB6PXG8" in events[1]["text"]


def test_follow_up_replies_carry_their_attachments():
    state = {"last_ts": "1.000000"}
    slack_watch.plan(state, {"messages": [msg("2.000000")], "replies": []}, now=0)
    reply_with_file = {**reply("2.000000", "4.000000", "U1", "Ana"), "files": [{"id": "F0C9QQMP8P5"}]}
    jobs = slack_watch.plan(state, slack_watch.clean({"messages": [], "replies": [
        reply("2.000000", "3.000000", "U9"), reply_with_file]}), now=0)
    assert jobs[0].follow_up and jobs[0].files == [{"id": "F0C9QQMP8P5", "name": "", "mimetype": ""}]


def test_poll_now_endpoint(tmp_path):
    app = create_app(Store(tmp_path / "s.db"), acme())
    client = TestClient(app)
    headers = {"x-casebook-viewer": "1"}
    assert client.post("/api/slack/poll-now", headers=headers).status_code == 503
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    app.state.watcher = w
    assert client.post("/api/slack/poll-now", headers=headers).status_code == 409  # apagada
    w.cfg.slack_watch = True
    assert client.post("/api/slack/poll-now", headers=headers).status_code == 202
    assert w._notified_at is not None


# --- «Revisar respuestas del hilo» desde una sesión --------------------------------------------------------------


def slack_session(store, thread_ts="2.000000", covers=None, channel="C1"):
    """Una sesión como la que deja la vigilancia: el mensaje principal del hilo (sin registro en el estado, como
    las sesiones viejas convertidas)."""
    from casebook.events import Recorder

    rec = Recorder(store)
    session = rec.start_session("Slack · Ana · 10:00")
    rec.slack("message", "falta una venta", author="Ana", user_id="U1", ts=thread_ts, thread_ts=thread_ts,
              follow_up=False, channel_id=channel, files=[], covers=covers or [thread_ts])
    return session["id"]


def run_check(w, sid):
    assert w.check_thread(sid)
    assert wait_until(lambda: sid not in w._checking)
    return slack_watch.read_state()["thread_checks"][str(sid)]


def test_thread_info_knows_how_far_the_thread_was_analyzed(tmp_path):
    from casebook.events import Recorder

    w, store = watcher(tmp_path, FakeRunner())
    sid = slack_session(store, covers=["2.000000"])
    rec = Recorder(store)
    rec.resume_session(sid)
    # Respuestas en el hilo (con un ts posterior a hoy, para distinguirlo de la hora del análisis).
    rec.slack("message", "a: b", author="Leo", user_id="U2", ts="9999999999.000002", thread_ts="2.000000",
              follow_up=True, covers=["9999999999.000002", "9999999999.000001"])
    info = slack_watch.thread_info(store, sid)
    # Sin análisis todavía: lo único visto es el mensaje principal.
    assert info == {"thread_ts": "2.000000", "channel_id": "C1", "user_id": "U1", "author": "Ana", "since": "2.000000"}
    rec.slack("analysis", "x", outcome="ticket", result={})
    assert slack_watch.thread_info(store, sid)["since"] == "9999999999.000002"
    other = Recorder(store).start_session("SUP-1")["id"]
    assert slack_watch.thread_info(store, other) is None


def test_thread_info_counts_the_time_of_the_last_analysis(tmp_path):
    from casebook.events import Recorder

    w, store = watcher(tmp_path, FakeRunner())
    sid = slack_session(store)
    rec = Recorder(store)
    rec.resume_session(sid)
    # Un análisis lee el hilo completo: lo que había cuando corrió ya está visto.
    rec.slack("analysis", "x", outcome="ticket", result={})
    assert float(slack_watch.thread_info(store, sid)["since"]) > 1_700_000_000


def test_new_thread_replies_are_analyzed_together_in_the_same_session(tmp_path):
    replies = {"messages": [], "replies": [
        reply("2.000000", "1.500000", "U9"),  # anterior a lo analizado: se ignora
        {**reply("2.000000", "3.000000", "U9", "Walter"), "preview": "let me check"},
        {**reply("2.000000", "4.000000", "U1", "Ana"), "preview": "¿y esto?", "files": [{"id": "F0C9QQMP8P5"}]},
    ]}
    runner = FakeRunner(replies, {**ANALYSIS, "attachments_reviewed": [{"file_id": "F0C9QQMP8P5", "description": "x"}]})
    w, store = watcher(tmp_path, runner, slack_watch=False)  # funciona con la vigilancia apagada
    sid = slack_session(store)
    check = run_check(w, sid)
    assert check["status"] == "queued" and check["count"] == 2
    assert 'message_ts=2.000000, oldest="2.000000"' in runner.calls[0]["prompt"]
    job = w._queue.pop(0)
    assert (job.ts, job.also, job.session_id, job.since) == ("4.000000", ["3.000000"], sid, "2.000000")
    assert "Walter: let me check" in job.preview and job.files[0]["id"] == "F0C9QQMP8P5"
    state = slack_watch.read_state()
    assert {ts: j["status"] for ts, j in state["jobs"].items()} == {"3.000000": "queued", "4.000000": "queued"}
    assert state["threads"]["2.000000"]["answered"] is True  # Walter respondió: el hilo queda en seguimiento
    assert w.analyze(job) == sid
    assert "2 respuestas nuevas en el hilo 2.000000" in runner.calls[1]["prompt"]
    assert f"session_read(session_id={sid})" in runner.calls[1]["prompt"]
    state = slack_watch.read_state()
    assert {j["status"] for j in state["jobs"].values()} == {"done"}
    assert state["thread_checks"][str(sid)]["status"] == "done"
    message = [e["payload"] for e in store.events(sid, limit=50) if e["payload"].get("kind") == "message"][-1]
    assert message["covers"] == ["4.000000", "3.000000"]
    # La próxima revisión arranca desde el último análisis (que leyó el hilo entero).
    assert float(slack_watch.thread_info(store, sid)["since"]) > 1_700_000_000


def test_thread_without_new_replies_spends_no_analysis(tmp_path):
    runner = FakeRunner({"messages": [], "replies": []})
    w, store = watcher(tmp_path, runner)
    assert run_check(w, slack_session(store))["status"] == "empty"
    assert w._queue == [] and len(runner.calls) == 1


def test_thread_replies_already_analyzed_are_not_repeated(tmp_path):
    slack_watch.write_state({"jobs": {"3.000000": {"status": "done", "session_id": 1, "at": 9e12}}})
    w, store = watcher(tmp_path, FakeRunner({"messages": [], "replies": [reply("2.000000", "3.000000", "U9")]}))
    check = run_check(w, slack_session(store))
    assert check["status"] == "existing" and check["busy"] is False and w._queue == []


def test_thread_check_errors_are_structured(tmp_path):
    w, store = watcher(tmp_path, FakeRunner(slack_watch.ClaudeError("channel_not_found", "channel")))
    check = run_check(w, slack_session(store))
    assert check["status"] == "error" and check["error"]["code"] == "channel"


def test_thread_check_endpoints(tmp_path):
    w, store = watcher(tmp_path, FakeRunner({"messages": [], "replies": []}))
    app = create_app(store, acme())
    client = TestClient(app)
    headers = {"x-casebook-viewer": "1"}
    sid = slack_session(store)
    from casebook.events import Recorder

    other = Recorder(store).start_session("SUP-1")["id"]
    assert client.get(f"/api/slack/thread/{sid}").json() == {"has_thread": True, "check": None}
    assert client.get(f"/api/slack/thread/{other}").json()["has_thread"] is False
    assert client.post(f"/api/slack/thread/{sid}/check", headers=headers).status_code == 503  # sin vigilante
    app.state.watcher = w
    assert client.post(f"/api/slack/thread/{other}/check", headers=headers).status_code == 404
    assert client.post(f"/api/slack/thread/{sid}/check").status_code == 403  # sin el header del visor
    w._checking.add(sid)
    assert client.post(f"/api/slack/thread/{sid}/check", headers=headers).status_code == 409  # ya en curso
    w._checking.clear()
    assert client.post(f"/api/slack/thread/{sid}/check", headers=headers).status_code == 202
    assert wait_until(lambda: sid not in w._checking)
    assert client.get(f"/api/slack/thread/{sid}").json()["check"]["status"] == "empty"


def test_only_thanks_in_the_thread_is_not_analyzed_unless_forced(tmp_path):
    thanks = {"messages": [], "replies": [{**reply("2.000000", "3.000000", "U1", "Marko", request=False),
                                           "preview": "Okay. Thank you"}]}
    runner = FakeRunner(thanks, thanks)
    w, store = watcher(tmp_path, runner)
    sid = slack_session(store)
    check = run_check(w, sid)
    assert check["status"] == "no_requests" and check["authors"] == ["Marko"] and w._queue == []
    assert "3.000000" not in slack_watch.read_state().get("jobs", {})  # no queda registrada: se puede forzar
    assert w.check_thread(sid, force=True)
    assert wait_until(lambda: sid not in w._checking)
    assert [j.ts for j in w._queue] == ["3.000000"]


def test_session_list_says_which_sessions_come_from_slack(tmp_path):
    from casebook.events import Recorder

    store = Store(tmp_path / "s.db")
    sid = slack_session(store)
    rec = Recorder(store)
    card = rec.start_session("SUP-13510", card="SUP-13510")["id"]
    rec.note("conclusión")
    flags = {s["id"]: s["slack"] for s in store.list_sessions()}
    assert flags == {sid: 1, card: 0}


# --- revisión: estados finales, reinicios y datos parciales --------------------------------------------------------


def test_an_error_next_to_good_data_keeps_the_data(monkeypatch):
    fake_popen(monkeypatch, {"is_error": False, "structured_output": {
        "messages": [msg("2.000000")], "replies": [], "error": {"code": "other", "detail": "un hilo no se pudo leer"}}})
    assert slack_watch.run_claude("p", "haiku", {}, (), 10)["messages"][0]["ts"] == "2.000000"


def test_manual_and_thread_check_say_when_the_analysis_failed(tmp_path):
    runner = FakeRunner(latest(msg("2.000000")), slack_watch.ClaudeError("falló", "other"))
    w, store = watcher(tmp_path, runner, slack_watch=False)
    run_manual(w)
    w.analyze(w._queue.pop(0))
    assert slack_watch.read_state()["manual"]["status"] == "failed"  # no "Listo"

    runner = FakeRunner({"messages": [], "replies": [reply("2.000000", "3.000000", "U1")]})
    w2, store2 = watcher(tmp_path / "b", runner)
    sid = slack_session(store2)
    run_check(w2, sid)
    slack_watch.write_state({**slack_watch.read_state(), "today": {"date": slack_watch._today(), "count": 99}})
    w2.cfg.slack_max_analyses_per_day = 1
    w2.analyze(w2._queue.pop(0))
    assert slack_watch.read_state()["thread_checks"][str(sid)]["status"] == "skipped"


def test_a_failed_follow_up_can_be_picked_up_again_by_the_thread_check(tmp_path):
    from casebook.events import Recorder

    w, store = watcher(tmp_path, FakeRunner())
    sid = slack_session(store)
    rec = Recorder(store)
    rec.resume_session(sid)
    rec.slack("message", "¿y esto?", author="Ana", user_id="U1", ts="5.000000", thread_ts="2.000000",
              follow_up=True, covers=["5.000000"])
    rec.slack("error", "falló", error=slack_watch.error_info("other"))
    assert slack_watch.thread_info(store, sid)["since"] == "2.000000"  # la repregunta fallida no cuenta como vista


def test_restart_unsticks_thread_checks(tmp_path):
    slack_watch.write_state({"thread_checks": {"7": {"status": "queued", "ts": "3.0", "at": 1},
                                               "8": {"status": "empty", "at": 1}}})
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    w.start()
    w.stop()
    checks = slack_watch.read_state()["thread_checks"]
    assert checks["7"]["status"] == "error" and checks["7"]["error"]["code"] == "interrupted"
    assert checks["8"]["status"] == "empty"


def test_events_of_type_only_brings_that_type(tmp_path):
    from casebook.events import Recorder

    store = Store(tmp_path / "s.db")
    sid = slack_session(store)
    rec = Recorder(store)
    rec.resume_session(sid)
    rec.note("una nota")
    assert {e["type"] for e in store.events_of_type(sid, "slack")} == {"slack"}


def test_export_titles_grouped_thread_replies(tmp_path):
    from casebook import export
    from casebook.events import Recorder

    store = Store(tmp_path / "s.db")
    sid = slack_session(store)
    rec = Recorder(store)
    rec.resume_session(sid)
    rec.slack("message", "Walter: ok\nAna: ¿y esto?", author="Ana", ts="4.000000", thread_ts="2.000000",
              follow_up=True, covers=["4.000000", "3.000000"])
    assert "**Slack · Respuestas nuevas en el hilo (2) ·" in export.render(store, sid)


def test_enable_and_disable_watch(tmp_path):
    import time

    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    now = time.time()
    w.enable_watch(lookback_minutes=0)
    assert w.cfg.slack_watch is True
    st = slack_watch.read_state()
    assert abs(float(st["last_ts"]) - now) < 5

    w.enable_watch(lookback_minutes=60)
    st = slack_watch.read_state()
    assert abs(float(st["last_ts"]) - (now - 3600)) < 5

    w.disable_watch()
    assert w.cfg.slack_watch is False


def test_turning_on_with_ancient_cursor_clamps_to_lookback(tmp_path, fast):
    import time

    old_ts = time.time() - (14 * 86400)
    slack_watch.write_state({"channel_id": "C1", "last_ts": f"{old_ts:.6f}"})
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False, slack_default_lookback_minutes=0)
    w.poll_once = lambda: []
    w.start()
    try:
        w.cfg.slack_watch = True
        assert wait_until(lambda: float(slack_watch.read_state().get("last_ts", 0)) > time.time() - 10)
    finally:
        w.stop()


def test_watch_enable_and_disable_endpoints(tmp_path):
    import time

    app = create_app(Store(tmp_path / "s.db"), acme())
    client = TestClient(app)
    headers = {"x-casebook-viewer": "1"}
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False)
    app.state.watcher = w

    resp = client.post("/api/slack/watch/enable", json={"lookback_minutes": 60}, headers=headers)
    assert resp.status_code == 200
    assert w.cfg.slack_watch is True
    st = slack_watch.read_state()
    assert abs(float(st["last_ts"]) - (time.time() - 3600)) < 5

    resp_dis = client.post("/api/slack/watch/disable", headers=headers)
    assert resp_dis.status_code == 200
    assert w.cfg.slack_watch is False


def test_watch_turns_off_when_the_program_stops(tmp_path, monkeypatch):
    import socket
    import threading

    import uvicorn

    from casebook.viewer.app import ViewerServer

    monkeypatch.setattr(slack_watch.SlackWatcher, "poll_once", lambda self: [])
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[slack]\nwatch = true\n")
    cfg = config.load(cfg_file)
    assert cfg.slack_watch is True
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ViewerServer(uvicorn.Config(create_app(Store(tmp_path / "s.db"), cfg), host="127.0.0.1",
                                         port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        # Quedó encendida de una sesión anterior que no se cerró bien: arranca apagada.
        assert wait_until(lambda: server.started, timeout=5)
        assert cfg.slack_watch is False
        assert config.load(cfg_file).slack_watch is False
        server.watcher.cfg.slack_watch = True  # se enciende durante la sesión
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    assert cfg.slack_watch is False
    assert config.load(cfg_file).slack_watch is False


def test_settings_switch_goes_through_the_watcher(tmp_path):
    import time

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    cfg = config.load(cfg_file)
    app = create_app(Store(tmp_path / "s.db"), cfg)
    client = TestClient(app)
    headers = {"x-casebook-viewer": "1"}
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=False, slack_default_lookback_minutes=60)
    w.cfg = cfg
    cfg.slack_default_lookback_minutes = 60
    app.state.watcher = w
    slack_watch.write_state({"channel_id": "C1", "last_ts": "1.000000"})

    resp = client.put("/api/settings", json={"values": {"slack.watch": True}}, headers=headers)
    assert resp.status_code == 200
    assert cfg.slack_watch is True and config.load(cfg_file).slack_watch is True
    assert abs(float(slack_watch.read_state()["last_ts"]) - (time.time() - 3600)) < 5  # no retoma el cursor viejo

    resp = client.put("/api/settings", json={"values": {"slack.watch": False}}, headers=headers)
    assert resp.status_code == 200
    assert cfg.slack_watch is False and config.load(cfg_file).slack_watch is False


def test_explicit_cursor_is_not_left_pending_when_already_on(tmp_path, fast):
    w, _ = watcher(tmp_path, FakeRunner(), slack_watch=True)
    w.poll_once = lambda: []
    w.start()
    try:
        assert wait_until(lambda: w._threads and w._explicit_start_ts is None)
        w._explicit_start_ts = 123.0  # enable_watch con la vigilancia ya encendida
        assert wait_until(lambda: w._explicit_start_ts is None)
    finally:
        w.stop()
