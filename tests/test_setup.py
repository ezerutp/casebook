"""Asistente de instalación: los pasos sin interfaz, el de terminal sin TTY y el gráfico offscreen."""
import json
import os
from pathlib import Path

import pytest

from casebook import native_host
from casebook.setup import __main__ as entry
from casebook.setup import steps

REPO = Path(steps.__file__).resolve().parents[3]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "casebook").write_text("#!/bin/sh\n")
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setattr(steps.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(steps.shutil, "which", lambda name: None)  # sin claude, codex ni opencode reales
    return steps.Context(repo=REPO, dev=False, venv=venv, home=home)


def test_unknown_agent_is_rejected():
    with pytest.raises(SystemExit):
        entry.context(entry.parse_args(["--agents", "claude,vim"]))


def test_link_cli_creates_link_and_respects_foreign_files(ctx):
    items = steps.link_cli(ctx)
    assert ctx.cli_link.resolve() == ctx.cli_target.resolve() and items[0].status == steps.OK
    ctx.cli_link.unlink()
    ctx.cli_link.write_text("otro programa")
    assert steps.link_cli(ctx)[0].status == steps.WARN
    assert ctx.cli_link.read_text() == "otro programa"


def test_dry_run_changes_nothing(ctx):
    ctx.dry_run = True
    steps.link_cli(ctx)
    results = steps.register_agents(ctx, ["claude", "codex", "opencode"])
    assert not ctx.cli_link.exists() and not (ctx.home / ".claude").exists() and not (ctx.home / ".agents").exists()
    assert not (Path(os.environ["XDG_CONFIG_HOME"]) / "opencode").exists()
    assert set(results) == {"claude", "codex", "opencode"}


PACK_SKILLS = REPO / "tests" / "fixtures" / "pack-acme" / "skills"


@pytest.mark.parametrize("name", sorted(d.name for d in PACK_SKILLS.iterdir()))
def test_skills_copy_and_link_like_the_old_installer(ctx, name):
    assert name in steps.skill_names()  # las del pack activo
    results = steps.register_agents(ctx, ["claude", "codex", "opencode"])
    claude_skill = ctx.home / ".claude" / "skills" / name
    assert (claude_skill / "SKILL.md").is_file() and not claude_skill.is_symlink()  # copia en modo local
    codex_skill = ctx.home / ".agents" / "skills" / name
    assert codex_skill.is_symlink() and Path(os.readlink(codex_skill)) == ctx.app_dir / "skills" / name
    opencode_skill = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "skills" / name
    assert Path(os.readlink(opencode_skill)) == ctx.app_dir / "skills" / name
    assert any("No encontré `claude`" in i.text for i in results["claude"])
    ctx.dev = True
    steps.register_agents(ctx, ["claude"])
    # Sin packs/ en el repo, --dev enlaza la copia instalada del pack.
    assert claude_skill.is_symlink() and claude_skill.resolve() == (steps.config.PACKS_DIR / "acme" / "skills" / name).resolve()


def test_every_pack_skill_has_its_name():
    for name in steps.skill_names():
        assert f"name: {name}" in (PACK_SKILLS / name / "SKILL.md").read_text().splitlines()


def test_without_pack_there_are_no_skills(ctx):
    from demo_pack import no_pack

    no_pack()
    assert steps.skill_names() == [] and not any(
        "Skill" in i.text for i in steps.register_agents(ctx, ["claude"])["claude"])


def test_foreign_skill_is_left_alone(ctx):
    foreign = ctx.home / ".claude" / "skills" / "acme-investigation"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("name: otra-cosa\n")
    items = steps.register_agents(ctx, ["claude"])["claude"]
    assert any(i.status == steps.WARN and "no es de esta instalación" in i.text for i in items)
    assert (foreign / "SKILL.md").read_text() == "name: otra-cosa\n"


def test_opencode_entry_is_merged_without_touching_the_rest(ctx):
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"theme": "dark", "mcp": {"otro": {"type": "local"}}}))
    steps.register_agents(ctx, ["opencode"])
    data = json.loads(config.read_text())
    assert data["theme"] == "dark" and "otro" in data["mcp"]
    assert data["mcp"][steps.MCP_NAME]["command"] == [str(ctx.cli_target), "mcp"]
    config.write_text("{ // comentario\n}")
    items = steps.register_agents(ctx, ["opencode"])["opencode"]
    assert items[0].status == steps.WARN and config.read_text() == "{ // comentario\n}"


def test_extension_profiles_read_chrome_preferences(tmp_path):
    chrome = tmp_path / "config" / "google-chrome"
    for directory, name in (("Default", "Personal"), ("Profile 2", "acme.com"), ("Profile 3", "Viejo")):
        (chrome / directory).mkdir(parents=True)
    (chrome / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"name": "Personal"}, "Profile 2": {"name": "acme.com"}, "Profile 3": {"name": "Viejo"}}}}))
    expected = native_host.extension_dir()
    settings = lambda path, **extra: {"extensions": {"settings": {native_host.EXTENSION_ID: {"path": str(path), **extra}}}}
    (chrome / "Profile 2" / "Secure Preferences").write_text(json.dumps(settings(expected)))
    other = tmp_path / "otra-carpeta"
    other.mkdir()
    (chrome / "Profile 3" / "Preferences").write_text(json.dumps(settings(other)))
    states = {p["name"]: p["state"] for p in steps.extension_profiles()}
    assert states == {"Personal": "missing", "acme.com": "loaded", "Viejo": "other_folder"}
    # La copia de la instalación local se borra al pasar a --dev: Chrome sigue apuntando a ella.
    (chrome / "Profile 3" / "Preferences").write_text(json.dumps(settings(tmp_path / "copia-borrada")))
    assert {p["name"]: p["state"] for p in steps.extension_profiles()}["Viejo"] == "folder_gone"
    (chrome / "Profile 2" / "Secure Preferences").write_text(json.dumps(settings(expected, disable_reasons=[1])))
    assert {p["name"]: p["state"] for p in steps.extension_profiles()}["acme.com"] == "disabled"


def _fast_checks(monkeypatch):
    monkeypatch.setattr(steps, "prereqs", lambda: [steps.Item(steps.OK, "todo bien")])
    monkeypatch.setattr(steps, "slack_checks", lambda: [steps.Item(steps.INFO, "Slack de escritorio no está instalado")])
    monkeypatch.setattr(steps, "sessions", lambda: [
        {"system": s.id, "label": s.label, "description": s.description, "status": "missing",
         "detail": "sin sesión guardada"}
        for s in steps.login_systems()])


def test_console_wizard_runs_without_a_terminal(ctx, monkeypatch):
    from rich.console import Console

    from casebook.setup import console as console_ui

    _fast_checks(monkeypatch)
    ctx.dry_run = True
    out = Console(record=True, width=100)
    assert console_ui.run(ctx, out) == 0
    text = out.export_text()
    assert "Paso 8 de 8 · Listo" in text and "casebook login os" in text
    assert "Paso 6 de 8 · Slack (opcional)" in text and "Pack Acme 1.0.0 activo" in text
    assert "1 pendiente" not in text  # lo que falta de Slack (opcional) no queda como pendiente


def test_ui_choice_without_terminal_is_console(monkeypatch):
    monkeypatch.setattr(entry, "interactive", lambda: False)
    assert entry.choose_ui(entry.parse_args([]), None) == "console"
    assert entry.choose_ui(entry.parse_args(["--gui"]), None) == "gui"


def test_gui_walks_every_page_offscreen(ctx, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from casebook.setup import gui

    _fast_checks(monkeypatch)
    ctx.dry_run = True
    app = QApplication.instance() or QApplication([])
    window = gui.InstallerWindow(ctx, gui.DARK)
    for _ in range(len(window.pages) - 1):
        window.next()
        gui.QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()
    assert window.current().title == "Todo listo"
    assert "pendiente" in window.status.text() or "completada" in window.status.text()
    window.close()


def test_gui_status_is_replaced_when_a_task_finishes(ctx, monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    import time

    from PySide6.QtWidgets import QApplication

    from casebook.setup import gui

    def slow_register(ctx_, selected):
        time.sleep(0.3)
        return {"claude": [steps.Item(steps.OK, "Servidor MCP registrado")]}

    monkeypatch.setattr(steps, "register_agents", slow_register)
    app = QApplication.instance() or QApplication([])
    window = gui.InstallerWindow(ctx, gui.DARK)
    window.next()  # configuración (el pack)
    window.next()
    window.current().checks["claude"].setChecked(True)
    window.current().primary()
    assert window.busy and window.spinner.timer.isActive() and "Registrando" in window.status.text()
    first = window.status_icon.pixmap().toImage()
    deadline = time.monotonic() + 5
    while window.busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert not window.busy and not window.spinner.timer.isActive()
    assert window.status.text() == "Registrado en Claude Code."
    assert window.primary.text().strip() == "Continuar"
    assert first != window.status_icon.pixmap().toImage()
    window.close()


def test_one_working_profile_is_enough(monkeypatch):
    profiles = [{"dir": "Default", "name": "Ezer", "state": "other_folder", "path": "/repo", "login_profile": False},
                {"dir": "Profile 2", "name": "acme.com", "state": "missing", "path": None, "login_profile": True}]
    monkeypatch.setattr(native_host, "connected", lambda: False)
    state = steps.extension_state(profiles)
    assert state["ok"] and state["subtitle"] == "cargada en Ezer"
    assert not steps.extension_state([{**profiles[1]}])["ok"]
    monkeypatch.setattr(native_host, "connected", lambda: True)
    assert steps.extension_state([{**profiles[1]}]) ["subtitle"] == "conectada"
    gone = {**profiles[0], "state": "folder_gone", "path": "/copia-borrada"}
    assert not steps.extension_state([gone, profiles[1]])["ok"]


def test_slack_checks_report_what_is_missing(monkeypatch):
    runs = []

    def fake_run(*command):
        runs.append(command)
        if command[:2] == ("pgrep", "-x"):
            return steps.subprocess.CompletedProcess(command, 0, "123\n", "")
        return steps.subprocess.CompletedProcess(command, 0, "claude.ai Slack:\n  Status: ! Needs authentication\n", "")

    monkeypatch.setattr(steps, "_run", fake_run)
    monkeypatch.setattr(steps, "_slack_desktop", lambda: "/usr/bin/slack")
    monkeypatch.setattr(steps.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(steps.sys, "platform", "linux")
    items = steps.slack_checks()
    assert items[0].status == steps.OK and "abierto" in items[0].text
    assert items[1].status == steps.INFO and "/mcp" in items[1].detail
    assert ("claude", "mcp", "get", "claude.ai Slack") in runs
    assert all(i.status in (steps.OK, steps.INFO) for i in items)  # opcional: nunca WARN ni FAIL


def test_slack_checks_without_slack_or_claude(monkeypatch):
    monkeypatch.setattr(steps, "_slack_desktop", lambda: None)
    monkeypatch.setattr(steps.shutil, "which", lambda name: None)
    items = steps.slack_checks()
    assert "no está instalado" in items[0].text and steps.SLACK_DOWNLOAD in items[0].detail
    assert "No encontré `claude`" in items[1].text


def test_slack_checks_connected(monkeypatch):
    monkeypatch.setattr(steps, "_slack_desktop", lambda: "/usr/bin/slack")
    monkeypatch.setattr(steps, "_slack_running", lambda: True)
    monkeypatch.setattr(steps.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(steps.sys, "platform", "linux")
    monkeypatch.setattr(steps, "_run", lambda *c: steps.subprocess.CompletedProcess(c, 0, "  Status: ✔ Connected\n", ""))
    assert [i.status for i in steps.slack_checks()[:2]] == [steps.OK, steps.OK]


def test_agent_version_reads_the_number(monkeypatch):
    monkeypatch.setattr(steps.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(steps.subprocess, "run",
                        lambda *a, **k: steps.subprocess.CompletedProcess(a, 0, "2.1.306 (Claude Code)\n", ""))
    assert steps.agent_version("claude") == "2.1.306"
    monkeypatch.setattr(steps.shutil, "which", lambda name: None)
    assert steps.agent_version("claude") is None


def test_ignored_warnings_are_remembered(ctx):
    assert steps.ignored() == set()
    steps.set_ignored("aliases")
    steps.set_ignored("notify-send")
    steps.set_ignored("notify-send", False)
    assert steps.ignored() == {"aliases"}


def _gui(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from casebook.setup import gui

    return QApplication.instance() or QApplication([]), gui


def test_gui_plan_skips_the_steps_turned_off(ctx, monkeypatch):
    app, gui = _gui(monkeypatch)
    _fast_checks(monkeypatch)
    ctx.dry_run = True
    window = gui.InstallerWindow(ctx, gui.LIGHT)
    welcome = window.current()
    for key in ("mcp", "extension", "slack", "login"):
        welcome.toggles[key].setChecked(False)
    welcome.toggles["cli"].click()  # el comando no se puede apagar
    assert welcome.toggles["cli"].isChecked()
    welcome.primary()
    assert window.current().title == "La configuración de tu equipo"
    window.next()
    assert window.current().title == "Revisemos tu máquina"
    gui.QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    window.next()
    assert window.current().title == "Todo listo"
    assert [row.skipped for row in window.step_rows] == [False, False, True, False, True, True, True, False]
    window.previous()
    assert window.current().title == "Revisemos tu máquina"
    window.close()


def test_gui_ignored_warning_is_not_pending(ctx, monkeypatch):
    app, gui = _gui(monkeypatch)
    items = [steps.Item(steps.WARN, "no encontré los alias db-login en tu shell (revisa el setup)", group="Sistema",
                        key="aliases"),
             steps.Item(steps.FAIL, "no hay credenciales de db-login: corre `db-login` una vez", group="Credenciales",
                        key="db-credentials", fix="db-login")]
    monkeypatch.setattr(steps, "prereqs", lambda: items)
    window = gui.InstallerWindow(ctx, gui.LIGHT)
    page = window.pages[3]
    window.show_page(3)
    gui.QThreadPool.globalInstance().waitForDone(5000)
    app.processEvents()
    assert [i.key for i in page.summary()] == ["aliases", "db-credentials"]
    page._ignore("aliases", True)
    assert [i.key for i in page.summary()] == ["db-credentials"] and steps.ignored() == {"aliases"}
    assert page.summary_title.text() == "Falta una cosa"
    window.close()


def test_gui_mode_does_not_change_in_preview(ctx, monkeypatch):
    app, gui = _gui(monkeypatch)
    _fast_checks(monkeypatch)
    ctx.dry_run = True
    window = gui.InstallerWindow(ctx, gui.LIGHT)
    welcome = window.current()
    welcome.mode.select(1)
    welcome._mode_changed(1)
    assert welcome.mode.buttons[0].isChecked() and not welcome.confirm.isVisible()
    assert "vista previa" in window.status.text()
    window.close()


def test_rows_never_open_stray_windows(monkeypatch):
    # Un texto que se hace visible antes de tener padre se abre como ventana aparte (y en GNOME, cursor de
    # «abriendo app» por cada una).
    app, gui = _gui(monkeypatch)
    from casebook.setup import theme, widgets

    from PySide6.QtCore import QEvent, QObject

    shown = []

    class Spy(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Show and obj.isWidgetType() and obj.isWindow():
                shown.append(obj)
            return False

    spy = Spy()
    app.installEventFilter(spy)
    try:
        widgets.Row(theme.LIGHT, "Título", "texto", widgets.StatusDot(theme.LIGHT, "ok"), [widgets.Toggle(theme.LIGHT)],
                    widgets.Chip(theme.LIGHT, "Opcional", "neutral", "check"))
        widgets.Row(theme.LIGHT, "Sin texto", "")
    finally:
        app.removeEventFilter(spy)
    assert shown == []


def test_without_browser_sources_the_wizard_skips_extension_and_logins(ctx, monkeypatch):
    from rich.console import Console

    from casebook.setup import console as console_ui
    from demo_pack import no_pack

    no_pack()
    _fast_checks(monkeypatch)
    monkeypatch.setattr(steps, "extension_install", lambda c: pytest.fail("sin fuentes de navegador no hace falta"))
    ctx.dry_run = True
    out = Console(record=True, width=100)
    assert console_ui.run(ctx, out) == 0
    text = out.export_text()
    assert "Sin pack activo" in text and text.count("no hace falta") == 2
