"""Instalador gráfico (PySide6): los mismos pasos que el de terminal, en una ventana nativa.

Las operaciones lentas (agentes, requisitos, sesiones, login) corren en un QThreadPool y avisan por señales;
la ventana nunca se bloquea. La extensión se detecta sola: cada 2 s se leen las Preferences de cada perfil.
En la bienvenida se elige qué configurar: los pasos apagados se saltan.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config, native_host, systems
from . import steps
from .icons import app_icon, app_pixmap, line_icon, line_pixmap
from .theme import DARK, FONTS_DIR, LIGHT, Palette, stylesheet
from .widgets import (
    Card,
    Chip,
    CopyField,
    ElidingLabel,
    ProgressRing,
    Row,
    RowList,
    Segmented,
    SideStep,
    Spinner,
    StatusDot,
    StepBars,
    Tile,
    TipOverlay,
    Toggle,
    button,
    clear,
    heading,
    hbox,
    icon_label,
    label,
    repolish,
    set_prop,
    tip,
    vbox,
)

CONTENT_WIDTH = 880  # ancho máximo de cada página (márgenes incluidos)
SIDEBAR_WIDTH = 248
WINDOW_SIZE = (1160, 800)
MIN_SIZE = (1040, 680)
AVATAR_COLORS = ("#2f6b4f", "#7a4bb0", "#3b6fb6", "#b45309", "#be185d", "#0f766e")
STATUS_KIND = {steps.OK: "ok", steps.WARN: "warn", steps.FAIL: "fail", steps.INFO: "info"}
MODE_OPENED = "Instalador gráfico abierto"  # lo imprime setup/__main__.py al abrir la ventana nueva


def short_path(path: str | Path) -> str:
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text.startswith(home) else text


def tidy(text: str) -> str:
    return text.replace(str(Path.home()), "~")


def capitalize(text: str) -> str:
    first = text.split(" ", 1)[0]
    if "-" in first or "_" in first:  # un comando (notify-send, xdg-open): va tal cual
        return text
    return text[:1].upper() + text[1:]


def split_text(text: str) -> tuple[str, str]:
    """Un texto de doctor («no encontré X en tu shell (el paquete…)») en título corto y explicación."""
    text = tidy(text)
    for sep in (" → ", " (", ": "):
        i = text.find(sep)
        if i > 6:
            rest = text[i + len(sep):]
            if sep == " (" and rest.endswith(")"):
                rest = rest[:-1]
            return capitalize(text[:i]), capitalize(rest)
    return capitalize(text), ""


# --- tareas en segundo plano ---------------------------------------------------------------------------------


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    line = Signal(str)


class _Stream(io.TextIOBase):
    """stdout de un login: cada línea va a la ventana."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit, self._buffer = emit, ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._emit(line)
        return len(text)


class Task(QRunnable):
    def __init__(self, fn: Callable[[], object], capture: bool = False) -> None:
        super().__init__()
        self.fn, self.capture, self.signals = fn, capture, _Signals()

    def run(self) -> None:
        try:
            if self.capture:
                with contextlib.redirect_stdout(_Stream(self.signals.line.emit)):
                    result = self.fn()
            else:
                result = self.fn()
        except Exception as exc:  # se muestra en la ventana; nunca trae secretos (los pasos no los manejan)
            self._emit(self.signals.failed, f"{type(exc).__name__}: {exc}")
        else:
            self._emit(self.signals.done, result)

    @staticmethod
    def _emit(signal, value: object) -> None:
        with contextlib.suppress(RuntimeError):  # la ventana se cerró mientras la tarea corría
            signal.emit(value)


# --- páginas -------------------------------------------------------------------------------------------------


class Page(QWidget):
    title = ""
    lead = ""
    step_title = ""
    step_subtitle = ""
    step_icon = "info"
    plan_key = ""  # la fila del plan de la bienvenida que prende o apaga este paso
    skippable = False

    def __init__(self, window: "InstallerWindow") -> None:
        super().__init__()
        self.window_ = window
        self.ctx = window.ctx
        self.palette_ = window.palette_
        self.entered = False
        self.warn = False
        self.enabled = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = self.scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("PageHolder")
        scroll.setWidget(holder)
        outer.addWidget(scroll)
        # Ancho fijo de contenido, centrado: en una ventana grande no se estira ni se descuadra.
        centered = QHBoxLayout(holder)
        centered.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        content.setMaximumWidth(CONTENT_WIDTH)
        centered.addStretch(1)
        centered.addWidget(content, 100)
        centered.addStretch(1)
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(48, 34, 48, 24)
        self.body.setSpacing(12)
        self.kicker_row = QHBoxLayout()
        self.kicker_row.setSpacing(12)
        self.kicker_label = label("", "Kicker")
        self.kicker_row.addWidget(self.kicker_label)
        self.body.addLayout(self.kicker_row)
        self.build_header()
        self.build()
        self.body.addStretch(1)

    def build_header(self) -> None:
        self.body.addWidget(label(self.title, "PageTitle"))
        if self.lead:
            self.body.addWidget(label(self.lead, "PageLead", wrap=True))
        self.body.addSpacing(8)

    def set_step(self, number: int, total: int) -> None:
        self.kicker_label.setText(f"Paso {number} de {total}")
        self.kicker_row.addWidget(StepBars(self.palette_, number, total))
        self.kicker_row.addStretch(1)

    def build(self) -> None: ...

    def should_skip(self) -> bool:
        """Se salta sin preguntar (por ejemplo, la extensión si ninguna fuente inicia sesión con el navegador)."""
        return False

    def enter(self) -> None:
        """Al mostrarse; `entered` distingue la primera vez."""

    def primary_text(self) -> str:
        return "Continuar"

    def primary_icon(self) -> str | None:
        return "arrow-right"

    def primary(self) -> None:
        self.window_.next()

    def extra_button(self) -> tuple[str, str, Callable[[], None]] | None:
        """Un botón secundario en el pie, a la izquierda del primario: (texto, ícono, acción)."""
        return None

    def summary(self) -> list[steps.Item]:
        return []

    def say(self, text: str, kind: str | None) -> None:
        """El mensaje del pie, si esta página es la que se ve (una revisión puede terminar después de cambiar)."""
        if self.window_.current() is self:
            self.window_.set_status(text, kind)

    def run(self, fn: Callable[[], object], done: Callable[[object], None], busy: str | None,
            capture: Callable[[str], None] | None = None) -> None:
        """Corre `fn` en el pool. Con `busy`, bloquea los botones y muestra el spinner mientras tanto."""
        window = self.window_
        if busy:
            window.set_busy(busy)
        task = Task(fn, capture=capture is not None)
        task.setAutoDelete(False)
        # Las señales viven en el hilo de la ventana: se entregan ahí. La ventana guarda la tarea hasta que
        # termina; sin esa referencia, Python puede liberar las señales antes de que lleguen.
        window.tasks.add(task)

        def finished(result: object) -> None:
            window.tasks.discard(task)
            if busy:
                window.set_busy(None)
            done(result)

        def failed(error: str) -> None:
            window.tasks.discard(task)
            if busy:
                window.set_busy(None)
            self.say(error, "fail")

        task.signals.done.connect(finished, Qt.ConnectionType.QueuedConnection)
        task.signals.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        if capture:
            task.signals.line.connect(capture, Qt.ConnectionType.QueuedConnection)
        window.pool.start(task)

    # Pequeños ayudantes de diseño.
    def card(self, margins=(0, 4, 0, 4)) -> Card:
        return Card(self.palette_, margins)

    def chip(self, text: str = "", tone: str = "neutral", icon: str | None = None) -> Chip:
        return Chip(self.palette_, text, tone, icon)

    def tile(self, icon: str, color: str | None = None) -> Tile:
        return Tile(icon, self.palette_, color=color)

    def note(self, icon: str, text: str) -> QHBoxLayout:
        return hbox(icon_label(icon, self.palette_.faint, 15), label(text, "RowText"), None, spacing=8)


class WelcomePage(Page):
    title = "Dejemos listo casebook"
    lead = ("Configura el comando, tus agentes y la extensión de Chrome en unos dos minutos. Nada de esto escribe "
            "en producción: solo se instala en tu máquina.")
    step_title, step_subtitle, step_icon = "Bienvenida", "modo y plan", "laptop"

    PLAN = (
        ("cli", "terminal", "Comando en tu PATH", "", None),
        ("mcp", "cpu", "Servidor MCP y skills", "en los agentes que elijas en el paso siguiente", None),
        ("extension", "chrome", "Extensión de Chrome", "guarda las sesiones de tus fuentes al iniciar sesión", "Opcional"),
        ("slack", "message", "Vigilar el canal de soporte", "analiza los mensajes nuevos desde el visor", "Opcional"),
        ("login", "key", "Iniciar sesión al terminar", "OpenSearch y CloudWatch, en tu Chrome", None),
    )

    def build(self) -> None:
        p = self.palette_
        help_icon = icon_label("help", p.faint, 15)
        tip(help_icon, "Instalación local: una copia propia; el repo se puede mover o borrar. Modo desarrollo: "
                       "editable desde el repo; tus cambios aplican sin reinstalar.", "Dos formas de instalarlo")
        self.body.addLayout(hbox(label("Cómo instalarlo", "Heading"), help_icon, None, spacing=6))
        self.mode = Segmented(p, [("package", "Instalación local"), ("code", "Modo desarrollo")], 1 if self.ctx.dev else 0)
        tip(self.mode.buttons[0], "Una copia propia en ~/.local/share/casebook: el repo se puede mover o "
                                  "borrar. Para actualizar: git pull && ./install.sh", "Instalación local")
        tip(self.mode.buttons[1], "Tus cambios en el repo aplican sin reinstalar: solo reconecta el MCP. Usa el "
                                  ".venv del repo.", "Editable desde este repo")
        self.mode.changed.connect(self._mode_changed)
        self.body.addLayout(hbox(self.mode, None))

        self.confirm = QFrame()
        self.confirm.setObjectName("ConfirmBox")
        c = QHBoxLayout(self.confirm)
        c.setContentsMargins(16, 12, 12, 12)
        c.setSpacing(12)
        c.addWidget(icon_label("refresh", p.muted, 16))
        self.confirm_text = label("", "RowText", wrap=True)
        c.addWidget(self.confirm_text, 1)
        self.confirm_cancel = button("Cancelar", "ghost")
        self.confirm_cancel.clicked.connect(self._mode_cancel)
        self.confirm_go = button("Cambiar y reinstalar", "primary", "refresh", p)
        self.confirm_go.clicked.connect(self._mode_switch)
        c.addWidget(self.confirm_cancel)
        c.addWidget(self.confirm_go)
        self.confirm.hide()
        self.body.addWidget(self.confirm)

        self.body.addSpacing(10)
        self.body.addLayout(heading("Qué se va a configurar", "Puedes cambiarlo después con casebook setup"))
        card = self.card()
        self.rows = RowList()
        self.toggles: dict[str, Toggle] = {}
        self.slack_initial = False
        with contextlib.suppress(Exception):  # un config.toml roto no frena el asistente
            self.slack_initial = steps.slack_watch()
        initial = {"cli": True, "mcp": not (self.ctx.skip_mcp and self.ctx.skip_skill), "extension": True,
                   "slack": self.slack_initial, "login": not self.ctx.no_login}
        for key, icon, title, text, chip in self.PLAN:
            toggle = Toggle(p, initial[key], locked=key == "cli")
            if key == "cli":
                tip(toggle, "El comando siempre se instala: lo usan los agentes y el visor.")
            self.toggles[key] = toggle
            row = Row(p, title, text or short_path(self.ctx.cli_link), self.tile(icon), [toggle],
                      self.chip(chip) if chip else None, mono=key == "cli")
            self.rows.add(row)
            if key == "cli":
                self.cli_row = row
        card.box.addWidget(self.rows)
        self.body.addWidget(card)

        self.problems_card = self.card()
        self.problems = RowList(inset=48)
        self.problems_card.box.addWidget(self.problems)
        self.problems_card.hide()
        self.body.addWidget(self.problems_card)
        self.items: list[steps.Item] = []
        self.process: subprocess.Popen | None = None

    def enter(self) -> None:
        if self.entered:
            return
        self.items = steps.environment(self.ctx) + steps.link_cli(self.ctx)
        problems = [i for i in self.items if i.status in (steps.WARN, steps.FAIL)]
        self.problems.clear()
        for item in problems:
            row = Row(self.palette_, *split_text(item.text), StatusDot(self.palette_, STATUS_KIND[item.status]))
            command = item.detail.split(": ", 1)[1] if "export " in item.detail else ""
            if command:  # «agrégalo a tu shell: export PATH=…»: el comando, para copiarlo
                row.extra.addWidget(label("Agrégalo a tu shell:", "Strong"))
                row.extra.addWidget(CopyField(self.palette_, command))
            elif item.detail:
                row.extra.addWidget(label(item.detail, "RowText", wrap=True))
            self.problems.add(row)
        self.problems_card.setVisible(bool(problems))
        self.warn = bool(problems)
        self.window_.set_subtitle(self, self.ctx.mode_label)
        self.window_.set_mode_label()

    def primary_text(self) -> str:
        return "Comenzar"

    def primary(self) -> None:
        plan = {key: toggle.isChecked() for key, toggle in self.toggles.items()}
        for page in self.window_.pages:
            if page.plan_key:
                page.enabled = plan[page.plan_key]
        if plan["slack"] != self.slack_initial and not self.ctx.dry_run:
            try:
                steps.set_slack_watch(plan["slack"])
                self.slack_initial = plan["slack"]
            except (OSError, ValueError) as exc:
                self.say(f"No pude guardar el ajuste de Slack: {exc}", "fail")
                return
        self.window_.refresh_sidebar()
        self.window_.next()

    def summary(self) -> list[steps.Item]:
        return [i for i in self.items if i.status in (steps.WARN, steps.FAIL)]

    # --- cambio de modo ---------------------------------------------------------------------------------------

    def _script(self) -> Path | None:
        script = self.ctx.repo / "install.sh" if self.ctx.repo else None
        return script if script and script.is_file() else None

    def _mode_changed(self, index: int) -> None:
        wants_dev = index == 1
        if wants_dev == self.ctx.dev:
            self.confirm.hide()
            return
        target = "modo desarrollo" if wants_dev else "instalación local"
        if self.ctx.dry_run:
            self.mode.select(1 if self.ctx.dev else 0)
            self.say("En la vista previa no se cambia de modo.", "info")
            return
        if not self._script():
            self.mode.select(1 if self.ctx.dev else 0)
            self.say("No encontré install.sh en el repo: cambia de modo desde una copia del repo con "
                                    "./install.sh --dev o --local.", "warn")
            return
        flag = "--dev" if wants_dev else "--local"
        self.confirm_text.setText(f"Pasar a {target} vuelve a correr ./install.sh {flag}: reinstala el paquete y "
                                  "abre de nuevo este asistente. Tus sesiones y credenciales no se tocan.")
        self.confirm.show()

    def _mode_cancel(self) -> None:
        self.mode.select(1 if self.ctx.dev else 0)
        self.confirm.hide()

    def _mode_switch(self) -> None:
        script = self._script()
        wants_dev = not self.ctx.dev
        args = ["bash", str(script), "--dev" if wants_dev else "--local", "--gui"]
        if self.ctx.no_login:
            args.append("--no-login")
        log_path = config.DATA_DIR / "setup-cambio-de-modo.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "wb")  # noqa: SIM115 - queda abierto mientras corre install.sh
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True, cwd=str(script.parent))
        log.close()
        self.confirm_go.setEnabled(False)
        self.confirm_cancel.setEnabled(False)
        self.mode.setEnabled(False)
        target = "modo desarrollo" if wants_dev else "instalación local"
        self.window_.set_busy(f"Reinstalando en {target}… (puede tardar un minuto)")
        self.log_path = log_path
        self.watch = QTimer(self)
        self.watch.setInterval(400)
        self.watch.timeout.connect(self._mode_progress)
        self.watch.start()

    def _mode_progress(self) -> None:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            text = ""
        if MODE_OPENED in text:
            self.watch.stop()
            self.window_.close()
            return
        lines = [line.strip(" ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✓●") for line in text.replace("\r", "\n").splitlines() if line.strip()]
        lines = [line for line in lines if line and not line.startswith("\x1b")]
        if self.process and self.process.poll() is not None:
            self.watch.stop()
            self.window_.set_busy(None)
            self.mode.setEnabled(True)
            self.confirm_go.setEnabled(True)
            self.confirm_cancel.setEnabled(True)
            last = lines[-1] if lines else "sin salida"
            self.say(f"El cambio de modo no terminó: {last}. Detalle en {short_path(self.log_path)}", "fail")
        elif lines:
            self.window_.status.setText(f"Reinstalando… {lines[-1][:90]}")


class PackPage(Page):
    title = "La configuración de tu equipo"
    lead = ("Un pack trae las fuentes (OpenSearch, CloudWatch, MySQL), el canal de Slack y las skills del equipo. Sin "
            "pack la app queda sin fuentes: las puedes agregar después en el visor (Ajustes → Fuentes).")
    step_title, step_subtitle, step_icon = "Configuración", "pack del equipo", "package"

    def build(self) -> None:
        p = self.palette_
        self.current_card = self.card()
        self.list = RowList(inset=50)
        self.current_card.box.addWidget(self.list)
        self.body.addWidget(self.current_card)
        folder = button("Instalar desde una carpeta", "", "folder", p, small=True)
        archive = button("Instalar un .zip", "", "package", p, small=True)
        folder.clicked.connect(lambda: self._pick(folder=True))
        archive.clicked.connect(lambda: self._pick(folder=False))
        # Mientras se revisa un pack, la vista previa ocupa el lugar del estado actual y de los botones.
        self.pickers = QWidget()
        v = vbox(spacing=12)
        v.addLayout(hbox(folder, archive, None))
        v.addLayout(self.note("info", "Desde una URL git: casebook pack install <url>. Fuentes a mano: "
                                      "casebook source add, o Ajustes → Fuentes en el visor."))
        self.pickers.setLayout(v)
        self.body.addWidget(self.pickers)
        self.preview = QWidget()
        self.preview_box = vbox(spacing=12)
        self.preview.setLayout(self.preview_box)
        self.preview.hide()
        self.body.addWidget(self.preview)
        self.pending: str | None = None
        self.plugin_toggles: dict[str, Toggle] = {}

    def enter(self) -> None:
        self._show()

    def _show(self) -> None:
        from .. import packs, systems

        p = self.palette_
        self.list.clear()
        pack = packs.active()
        cfg = config.load()
        if pack:
            self.list.add(Row(p, f"Pack {pack.display_name} {pack.version}", str(pack.path.parent), self.tile("package"),
                              [self.chip("Activo", "ok", "check")]))
        else:
            self.list.add(Row(p, "Sin pack", "instala el de tu equipo o sigue sin fuentes", self.tile("package", p.muted),
                              [self.chip("Opcional", "neutral")]))
        for s in systems.all_systems(cfg):
            self.list.add(Row(p, s.label, f"{s.id} · {s.provider}", self.tile("database" if s.kind == "mysql" else "search"),
                              []))
        self.window_.set_subtitle(self, pack.display_name if pack else "sin pack")
        self.window_.refresh_sidebar()

    def _pick(self, folder: bool) -> None:
        if folder:
            path = QFileDialog.getExistingDirectory(self, "La carpeta del pack (con pack.toml)")
        else:
            path, _ = QFileDialog.getOpenFileName(self, "El zip del pack", "", "Packs (*.zip)")
        if path:
            self._install(path)

    def _install(self, path: str) -> None:
        from .. import packs

        try:
            bundle = packs.open_bundle(path)
            try:
                info = packs.describe(bundle)
            finally:
                bundle.close()
        except packs.PackError as exc:
            self.say(str(exc), "fail")
            return
        self._preview(path, info)

    def _preview(self, path: str, info: dict) -> None:
        """Lo que trae el pack, en filas como el resto del asistente; se aplica con «Instalar pack»."""
        p = self.palette_
        clear(self.preview_box)
        self.pending = path
        self.plugin_toggles = {}
        installed = info["installed_version"]
        if info["older"]:
            chip = self.chip("Más viejo", "warn", "alert")
        elif installed:
            chip = self.chip("Actualización", "brand")
        else:
            chip = self.chip("Nuevo", "ok")
        self.preview_box.addLayout(heading("Lo que trae el pack", f"instalado: {installed}" if installed else
                                           "revísalo antes de instalarlo"))
        card = self.card()
        rows = RowList(inset=50)
        rows.add(Row(p, f"{info['display_name']} {info['version']}", short_path(path), self.tile("package"), [], chip,
                     mono=True))
        ids = [s.split(" (")[0] for s in info["sources"]]
        row = rows.add(Row(p, self._count(len(ids), "fuente", "fuentes"), ", ".join(ids) or "el pack no trae fuentes",
                           self.tile("database")))
        if info["sources"]:
            tip(row.text, "\n".join(info["sources"]), "Fuente (proveedor)")
        rows.add(Row(p, self._count(len(info["skills"]), "skill", "skills"), ", ".join(info["skills"]) or "ninguna",
                     self.tile("cpu")))
        if info.get("analysis_tools"):
            rows.add(self._expandable(Row(p, self._count(len(info["analysis_tools"]), "tool para el análisis de Slack",
                                                         "tools para el análisis de Slack"),
                                          "el análisis automático puede usarlas, solo en lectura", self.tile("lock")),
                                      info["analysis_tools"], mono=True))
        if info["knowledge"]:
            rows.add(Row(p, self._count(info["knowledge"], "entrada de conocimiento", "entradas de conocimiento"),
                         "quedan pendientes de revisar y no pisan las existentes", self.tile("search")))
        if info["domains"]:
            rows.add(self._expandable(Row(p, self._count(len(info["domains"]), "dominio", "dominios"),
                                          "la extensión de Chrome te pedirá permiso para leer sus sesiones",
                                          self.tile("chrome")), info["domains"], mono=True))
        for plugin in info["plugins"]:
            provides = ", ".join(plugin.get("provides", []))
            if plugin["missing"]:
                where = "viene en el zip" if plugin.get("path") else f"pip install {plugin.get('pip', '')}".strip()
                toggle = Toggle(p, True)
                tip(toggle, "Es código: se instala con pip en el entorno de casebook. Sin él, las fuentes que "
                            "lo usan no funcionan.", "Plugin del pack")
                self.plugin_toggles[plugin["name"]] = toggle
                right, text = [toggle], f"aporta {provides} · {where}"
            else:
                right, text = [self.chip("Instalado", "ok", "check")], f"aporta {provides}"
            rows.add(Row(p, plugin["name"], text, self.tile("code"), right, mono=False))
        if info["changes"]:
            rows.add(self._expandable(Row(p, self._count(len(info["changes"]), "cambio", "cambios"),
                                          "respecto al pack instalado", self.tile("refresh")), info["changes"]))
        card.box.addWidget(rows)
        self.preview_box.addWidget(card)
        if info["older"]:
            self.preview_box.addLayout(self.note("alert", f"Es más viejo que el instalado ({installed}): "
                                                          "instalarlo vuelve atrás."))
        self.pending_label = "Actualizar pack" if installed else "Instalar pack"
        self.current_card.hide()
        self.pickers.hide()
        self.preview.show()
        self.scroll.verticalScrollBar().setValue(0)
        self.window_.refresh_buttons()

    # Con un pack en revisión, el pie lo instala o lo descarta en vez de avanzar.
    def primary_text(self) -> str:
        return self.pending_label if self.pending else "Continuar"

    def primary_icon(self) -> str | None:
        return "check" if self.pending else "arrow-right"

    def primary(self) -> None:
        if self.pending:
            self._apply()
        else:
            self.window_.next()

    def extra_button(self) -> tuple[str, str, Callable[[], None]] | None:
        return ("Cancelar", "x", self._cancel) if self.pending else None

    @staticmethod
    def _count(n: int, one: str, many: str) -> str:
        return f"{n} {one if n == 1 else many}"

    def _expandable(self, row: Row, items: list[str], mono: bool = False) -> Row:
        """Una fila con chevron que abre la lista completa (dominios, cambios) sin cargar la vista."""
        chevron = QPushButton()
        chevron.setObjectName("Chevron")
        chevron.setCursor(Qt.CursorShape.PointingHandCursor)
        chevron.setIcon(line_icon("chevron-right", self.palette_.muted, 16))
        row.right.addWidget(chevron)
        box = QFrame()
        box.setObjectName("Detail")
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(4)
        for item in items:
            v.addWidget(ElidingLabel("Mono", item) if mono else label(item, "RowText", wrap=True))
        box.hide()
        row.extra.addWidget(box)

        def toggle() -> None:
            box.setVisible(not box.isVisible())
            chevron.setIcon(line_icon("chevron-down" if box.isVisible() else "chevron-right", self.palette_.muted, 16))

        chevron.clicked.connect(toggle)
        return row

    def _cancel(self) -> None:
        self.pending = None
        self.preview.hide()
        self.pickers.show()
        self.current_card.show()
        self.window_.refresh_buttons()

    def _apply(self) -> None:
        from .. import packs

        path = self.pending
        approved = {name for name, toggle in self.plugin_toggles.items() if toggle.isChecked()}
        self._cancel()
        if not path:
            return
        if self.ctx.dry_run:
            self.say("Vista previa: no se instaló nada.", "info")
            return
        self.run(lambda: packs.install(path, confirm=lambda text: True,
                                       confirm_plugin=lambda what: what.split(" ")[0] in approved),
                 self._installed, "Instalando el pack…")

    def _installed(self, pack) -> None:
        self.say(f"Pack {pack.display_name} {pack.version} instalado y activo.", "ok")
        self._show()

    def summary(self) -> list[steps.Item]:
        from .. import packs, systems

        if packs.active() or systems.all_systems(config.load()):
            return []
        return [steps.Item(steps.WARN, "Sin fuentes configuradas",
                           "instala el pack del equipo (pack install) o agrégalas en Ajustes → Fuentes")]


class AgentsPage(Page):
    title = "¿En qué agentes lo registro?"
    lead = "Se registra el servidor MCP y las skills de investigación y triage. Marcamos los que encontramos."
    step_title, step_subtitle, step_icon = "Agentes", "MCP y skills", "cpu"
    plan_key = "mcp"

    def build(self) -> None:
        p = self.palette_
        self.checks: dict[str, Toggle] = {}
        self.agent_rows: dict[str, dict] = {}
        card = self.card()
        rows = RowList(inset=16)
        defaults = steps.default_agents(self.ctx)
        for agent in steps.agents_available():
            logo = label(agent["label"][:1], "AgentLogo")
            logo.setFixedSize(36, 36)
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo.setProperty("installed", "true" if agent["installed"] else "false")
            chip = self.chip("…" if agent["installed"] else "No instalado")
            toggle = Toggle(p, agent["id"] in defaults)
            toggle.toggled.connect(lambda _on: self.window_.refresh_buttons())
            if not agent["installed"]:
                tip(toggle, "La configuración queda lista para cuando lo instales.", f"{agent['label']} no está instalado")
            chevron = QPushButton()
            chevron.setObjectName("Chevron")
            chevron.setCursor(Qt.CursorShape.PointingHandCursor)
            chevron.setIcon(line_icon("chevron-right", p.muted, 16))
            text = "MCP + 2 skills" + (" · scope user" if agent["id"] == "claude" else "")
            row = Row(p, agent["label"], text, logo, [chevron, toggle], chip)
            if not agent["installed"]:
                set_prop(row.title, "muted", True)
            detail = self._detail(agent["id"])
            detail.hide()
            row.extra.addWidget(detail)
            chevron.clicked.connect(lambda _=False, a=agent["id"]: self._expand(a))
            rows.add(row)
            self.checks[agent["id"]] = toggle
            self.agent_rows[agent["id"]] = {"row": row, "chip": chip, "detail": detail, "chevron": chevron,
                                            "installed": agent["installed"]}
        card.box.addWidget(rows)
        self.body.addWidget(card)
        self.body.addLayout(self.note("info", "Si ya estaba registrado con otra ruta, se reemplaza y te avisamos para "
                                              "reconectar con /mcp."))
        self.done_results: dict[str, list[steps.Item]] | None = None

    def _detail(self, agent: str) -> QFrame:
        p = self.palette_
        box = QFrame()
        box.setObjectName("Detail")
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(6)
        v.addWidget(label("Comando", "DetailLabel"))
        command = ElidingLabel("Mono", f"{short_path(self.ctx.cli_target)} mcp")
        v.addWidget(command)
        v.addSpacing(4)
        v.addWidget(label("Skills", "DetailLabel"))
        v.addLayout(hbox(*[Chip(p, name, "neutral", "check") for name in steps.skill_names()], None))
        results = RowList(inset=34)
        results.hide()
        v.addWidget(results)
        box.results = results
        return box

    def _expand(self, agent: str, open_: bool | None = None) -> None:
        w = self.agent_rows[agent]
        show = (not w["detail"].isVisible()) if open_ is None else open_
        w["detail"].setVisible(show)
        w["chevron"].setIcon(line_icon("chevron-down" if show else "chevron-right", self.palette_.muted, 16))

    def enter(self) -> None:
        if self.entered:
            return
        installed = [a for a, w in self.agent_rows.items() if w["installed"]]
        self.run(lambda: {a: steps.agent_version(a) for a in installed}, self._versions, None)

    def _versions(self, versions: dict[str, str | None]) -> None:
        for agent, version in versions.items():
            self.agent_rows[agent]["chip"].set(f"v{version}" if version else "instalado")

    def selected(self) -> list[str]:
        return [a for a, toggle in self.checks.items() if toggle.isChecked()]

    def primary_text(self) -> str:
        if self.done_results is not None:
            return "Continuar"
        n = len(self.selected())
        return "Continuar sin agentes" if not n else f"Registrar en {n} agente{'s' if n > 1 else ''}"

    def primary(self) -> None:
        if self.done_results is not None:
            self.window_.next()
            return
        selected = self.selected()
        if not selected or (self.ctx.skip_mcp and self.ctx.skip_skill):
            self.done_results = {}
            self.window_.set_subtitle(self, "ninguno")
            self.window_.next()
            return
        self.run(lambda: steps.register_agents(self.ctx, selected), self._show, "Registrando en los agentes…")

    def _show(self, results: dict[str, list[steps.Item]]) -> None:
        self.done_results = results
        labels = dict(steps.AGENTS)
        for agent, items in results.items():
            w = self.agent_rows[agent]
            results_list: RowList = w["detail"].results
            results_list.clear()
            for item in items:
                row = Row(self.palette_, item.text, item.detail, StatusDot(self.palette_, STATUS_KIND[item.status]),
                          height=44)
                row.layout().setContentsMargins(0, 6, 0, 6)
                results_list.add(row)
            results_list.show()
            problems = any(i.status != steps.OK for i in items)
            w["chip"].set("con avisos" if problems else "registrado", "warn" if problems else "ok",
                          "alert" if problems else "check")
            self._expand(agent, problems)
        for toggle in self.checks.values():
            toggle.setEnabled(False)
        self.warn = any(i.status != steps.OK for items in results.values() for i in items)
        self.window_.set_subtitle(self, ", ".join(labels[a] for a in results) or "ninguno")
        names = ", ".join(labels[a] for a in results)
        if self.warn:
            self.say(f"Registrado en {names}, con avisos: revísalos arriba.", "warn")
        else:
            self.say(f"Registrado en {names}.", "ok")
        self.window_.refresh_buttons()

    def summary(self) -> list[steps.Item]:
        return [i for items in (self.done_results or {}).values() for i in items if i.status in (steps.WARN, steps.FAIL)]


class PrereqsPage(Page):
    title = "Revisemos tu máquina"
    lead = "Solo se revisa; nada se cambia. Lo que falte te dice cómo arreglarlo."
    step_title, step_subtitle, step_icon = "Requisitos", "ssh, VPN, keyring", "shield"

    def build(self) -> None:
        p = self.palette_
        summary = Card(p, (16, 14, 16, 14))
        self.ring = ProgressRing(p)
        self.summary_title = label("Revisando…", "SummaryTitle")
        self.summary_text = label("ssh y alias tardan unos segundos", "RowText", wrap=True)
        again = button("Revisar de nuevo", "", "refresh", p)
        again.clicked.connect(self._check)
        line = hbox(self.ring, 6, spacing=12)
        line.addLayout(vbox(self.summary_title, self.summary_text), 1)
        line.addWidget(again)
        summary.box.addLayout(line)
        self.body.addWidget(summary)
        self.body.addSpacing(6)
        # Una sola columna: en dos, los avisos largos quedan angostos y Qt recorta los textos que se ajustan.
        self.groups = vbox(spacing=16)
        self.body.addLayout(self.groups)
        self.items: list[steps.Item] = []

    def enter(self) -> None:
        if not self.entered:
            self._check()

    def _check(self) -> None:
        self.run(steps.prereqs, self._show, "Revisando requisitos… (ssh y alias tardan unos segundos)")

    def _active(self) -> list[steps.Item]:
        ignored = steps.ignored()
        return [i for i in self.items if i.status in (steps.WARN, steps.FAIL)
                and not (i.status == steps.WARN and i.key and i.key in ignored)]

    def _show(self, items: list[steps.Item]) -> None:
        self.items = items
        self._render()

    def _render(self) -> None:
        items = self.items
        ignored = steps.ignored()
        active = self._active()
        clear(self.groups)
        for g in steps.PREREQ_GROUPS:
            group_items = [i for i in items if (i.group or "Sistema") == g]
            if not group_items:
                continue
            card = self.card()
            rows = RowList(inset=50)
            for item, hover in self._grouped(group_items):
                rows.add(self._row(item, ignored, hover))
            card.box.addWidget(rows)
            self.groups.addLayout(vbox(label(g.upper(), "SectionTitle"), card, spacing=8))
        total, n = len(items), len(active)
        self.ring.set(total - n, total)
        self.summary_title.setText("Todo en orden" if not n else "Falta una cosa" if n == 1 else f"Faltan {n} cosas")
        self.summary_text.setText(split_text(active[0].text)[0] + (f" y {n - 1} más." if n > 1 else ".") if active
                                  else "Puedes seguir.")
        self.warn = bool(active)
        self.window_.set_subtitle(self, f"{total - n} de {total} en orden")
        self.window_.refresh_sidebar()
        if self.warn:
            self.say(f"{n} aviso{'s' if n > 1 else ''}: puedes seguir y resolverlo después.", "warn")
        else:
            self.say("Todos los requisitos en orden.", "ok")

    def _grouped(self, items: list[steps.Item]) -> list[tuple[steps.Item, str]]:
        """(item, tooltip). Los hosts de ssh en orden van en una sola fila; el detalle de cada uno, en el tooltip."""
        hosts = [i for i in items if i.key.startswith("ssh-host:")]
        if len(hosts) < 2 or any(i.status != steps.OK for i in hosts):
            return [(i, "") for i in items]
        names = ", ".join(i.key.split(":", 1)[1] for i in hosts)
        merged = steps.Item(steps.OK, f"{len(hosts)} hosts de ssh en ~/.ssh/config", names, group=hosts[0].group,
                            key="ssh-hosts")
        hover = "\n".join(i.text.replace("host ", "", 1).replace(" en ~/.ssh/config", "") for i in hosts)
        first = items.index(hosts[0])
        rest = [(i, "") for i in items if i not in hosts]
        return rest[:first] + [(merged, hover)] + rest[first:]

    def _row(self, item: steps.Item, ignored: set[str], hover: str = "") -> Row:
        p = self.palette_
        title, text = split_text(item.text)
        is_ignored = item.status == steps.WARN and item.key in ignored
        problem = item.status in (steps.WARN, steps.FAIL) and not is_ignored
        chip = None
        if problem:
            chip = self.chip("Revisar" if item.status == steps.WARN else "Falta",
                             "warn" if item.status == steps.WARN else "fail")
        elif is_ignored:
            chip = self.chip("Ignorado")
        dot = StatusDot(p, "info" if is_ignored else STATUS_KIND.get(item.status, "info"))
        row = Row(p, title, item.detail if item.key == "ssh-hosts" else ("" if problem else text), dot, chip=chip,
                  height=58, chip_right=True)
        if hover:
            tip(row, hover, "Hosts de ssh")
        if problem:
            set_prop(row, "tone", "warn")
            set_prop(row, "expanded", True)
            if text:
                row.extra.addWidget(label(text, "RowText", wrap=True))
            if item.fix:
                row.extra.addWidget(label("Para arreglarlo, corre:", "Strong"))
                field = CopyField(p, item.fix)
                field.copied.connect(lambda value: self.say(f"Copiado: {value}", "ok"))
                row.extra.addWidget(field)
            if item.status == steps.WARN and item.key:
                ignore = button("Ignorar", "subtle", small=True)
                ignore.clicked.connect(lambda _=False, k=item.key: self._ignore(k, True))
                row.extra.addLayout(hbox(ignore, None))
        elif is_ignored:
            undo = button("Dejar de ignorar", "ghost", small=True)
            undo.clicked.connect(lambda _=False, k=item.key: self._ignore(k, False))
            row.right.addWidget(undo)
        return row

    def _ignore(self, key: str, on: bool) -> None:
        if not self.ctx.dry_run:
            steps.set_ignored(key, on)
        self._render()

    def summary(self) -> list[steps.Item]:
        return self._active()


class ExtensionPage(Page):
    title = "Conecta tu Chrome"
    lead = ("La extensión guarda las sesiones de tus fuentes cuando inicias sesión. Se carga una vez por "
            "perfil; aquí la detectamos sola.")
    step_title, step_subtitle, step_icon = "Extensión de Chrome", "opcional", "chrome"
    plan_key = "extension"

    def should_skip(self) -> bool:
        return not steps.login_systems()  # ninguna fuente inicia sesión con el navegador
    skippable = True

    def build(self) -> None:
        p = self.palette_
        self.install_dot = StatusDot(p, "pending")
        self.install_text = label("Preparando la extensión…", "RowText")
        self.install_line = hbox(self.install_dot, self.install_text, None, spacing=8)
        self.body.addLayout(self.install_line)
        self.body.addSpacing(4)
        self.body.addLayout(heading("Tus perfiles de Chrome", "los logins se abren en el marcado"))
        card = self.card()
        self.profile_rows = RowList(inset=62)
        card.box.addWidget(self.profile_rows)
        self.body.addWidget(card)
        self.cards: dict[str, dict] = {}
        self.login_group = QButtonGroup(self)
        self.login_group.setExclusive(True)

        self.body.addSpacing(10)
        self.body.addLayout(heading("Cómo cargarla", "toma un minuto"))
        guide = QHBoxLayout()
        guide.setSpacing(12)
        for n, (title, text, mock) in enumerate((
            ("Activa el modo de desarrollador", "arriba a la derecha de chrome://extensions", self._mock_toggle()),
            ("Pulsa «Cargar descomprimida»", "no «Empaquetar extensión»", self._mock_button()),
            ("Pega la ruta y acepta", "en el selector de carpetas", self._mock_keys()),
        ), 1):
            step = Card(p, (16, 14, 16, 14), spacing=10)
            badge = label(str(n), "StepNumber")
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step.box.addLayout(hbox(badge, vbox(label(title, "Strong", wrap=True), label(text, "RowText", wrap=True)),
                                    spacing=10))
            step.box.addWidget(mock)
            guide.addWidget(step, 1)
        self.body.addLayout(guide)
        self.path = CopyField(p, "", "folder")
        self.path.copied.connect(lambda _v: self.say(
            "Ruta copiada. En el selector de carpetas de Chrome: Ctrl+L, Ctrl+V y Enter.", "ok"))
        self.body.addWidget(self.path)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._poll)
        self.items: list[steps.Item] = []

    def _mock(self) -> tuple[QFrame, QHBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Illustration")
        frame.setFixedHeight(40)
        h = QHBoxLayout(frame)
        h.setContentsMargins(10, 0, 10, 0)
        h.setSpacing(6)
        return frame, h

    def _mock_toggle(self) -> QFrame:
        frame, h = self._mock()
        h.addWidget(label("Modo de desarrollador", "RowText"))
        h.addStretch(1)
        toggle = Toggle(self.palette_, True)
        toggle.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h.addWidget(toggle)
        return frame

    def _mock_button(self) -> QFrame:
        frame, h = self._mock()
        h.addWidget(label("Cargar descomprimida", "MockButton"))
        h.addStretch(1)
        return frame

    def _mock_keys(self) -> QFrame:
        frame, h = self._mock()
        for key in ("Ctrl+L", "Ctrl+V", "Enter"):
            h.addWidget(label(key, "KeyCap"))
        h.addStretch(1)
        return frame

    def enter(self) -> None:
        if not self.entered:
            self.items = steps.extension_install(self.ctx)
            bad = [i for i in self.items if i.status != steps.OK]
            self.install_dot.set("fail" if bad else "ok")
            self.install_text.setText(" · ".join(i.text for i in self.items))
            tip(self.install_text, "\n".join(f"{i.text}: {i.detail}" for i in self.items if i.detail))
            self.path.set_value(str(native_host.extension_dir() or "—"))
        self._poll()
        self.timer.start()

    def leave(self) -> None:
        self.timer.stop()
        state = steps.extension_state()
        self.warn = not state["ok"]
        self.window_.set_subtitle(self, state["subtitle"])

    def _recommended(self, profiles: list[dict]) -> str | None:
        team = steps.team_name()
        for p in profiles:
            if team and team in p["name"].lower():
                return p["dir"]
        return profiles[0]["dir"] if profiles else None

    def _poll(self) -> None:
        profiles = steps.extension_profiles()
        state = steps.extension_state(profiles)
        working = [p["dir"] for p in profiles if p["state"] in steps.WORKING]
        # Con la extensión ya cargada no se insiste con otro perfil; los logins van a uno donde está.
        recommended = None if working else self._recommended(profiles)
        login_profile = next((p["dir"] for p in profiles if p["login_profile"]), None)
        if (working and login_profile not in working) or (not login_profile and recommended):
            steps.remember_login_profile(working[0] if working else recommended)
            profiles = steps.extension_profiles()
        for i, profile in enumerate(profiles):
            if profile["dir"] not in self.cards:
                self._add_row(i, profile)
            self._update_row(profile, profile["dir"] == recommended)
        loaded = state["loaded"]
        self.warn = not state["ok"]
        self.window_.set_subtitle(self, state["subtitle"])
        self.window_.refresh_sidebar()
        gone = [p["name"] for p in profiles if p["state"] == "folder_gone"]
        if state["ok"] and state["connected"]:
            self.say("Extensión conectada. Puedes seguir.", "ok")
        elif gone:
            self.say(f"En {', '.join(gone)} hay que volver a cargarla: su carpeta ya no existe.", "warn")
        elif loaded:
            self.say("Cargada; esperando su primer contacto con el native host…", "warn")
        elif profiles:
            self.say("Esperando a que la cargues en un perfil… se detecta sola.", "warn")
        else:
            self.say("No encontré perfiles de Google Chrome en esta máquina.", "fail")

    def _add_row(self, index: int, profile: dict) -> None:
        p = self.palette_
        avatar = label(profile["name"][:1].upper(), "Avatar")
        avatar.setFixedSize(32, 32)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"background: {AVATAR_COLORS[index % len(AVATAR_COLORS)]};")
        chip = self.chip()
        radio = QRadioButton("Logins aquí")
        radio.setCursor(Qt.CursorShape.PointingHandCursor)
        radio.toggled.connect(lambda on, d=profile["dir"]: on and steps.remember_login_profile(d))
        self.login_group.addButton(radio)
        open_button = button("Cargar", "", "external", p, small=True)
        open_button.setFixedWidth(150)
        open_button.clicked.connect(lambda _=False, d=profile["dir"]: self._open(d))
        row = Row(p, profile["name"], profile["dir"], avatar, [radio, open_button], chip)
        self.profile_rows.add(row)
        self.cards[profile["dir"]] = {"row": row, "chip": chip, "open": open_button, "radio": radio}

    def _update_row(self, profile: dict, recommended: bool) -> None:
        p = self.palette_
        w = self.cards[profile["dir"]]
        text, tone, icon = {
            "loaded": ("Cargada", "ok", "check"),
            "other_folder": ("Cargada · desde otra carpeta", "ok", "check"),
            "disabled": ("Desactivada", "warn", "alert"),
            "folder_gone": ("Su carpeta ya no existe", "warn", "alert"),
            "missing": ("Falta cargarla" + (" · recomendado" if recommended else ""), "neutral", None),
        }[profile["state"]]
        w["chip"].set(text, tone, icon)
        tip_text, tip_title = "", ""
        if profile["state"] == "other_folder":
            tip_title = "Cargada desde otra carpeta"
            tip_text = (f"{profile['path']}. Funciona igual; para que se actualice al reinstalar, quítala y cárgala "
                        "desde la ruta de abajo.")
        elif profile["state"] == "folder_gone":
            tip_title = "Chrome la cargaba desde"
            tip_text = f"{profile['path']}, que ya no existe. Quítala y cárgala desde la ruta de abajo."
        tip(w["chip"], tip_text, tip_title)
        missing = profile["state"] not in steps.WORKING
        primary = missing and (recommended or profile["state"] == "folder_gone")
        w["open"].setObjectName("Primary" if primary else "Small")
        w["open"].setText("Cargar" if missing else "Ver en Chrome")
        w["open"].setIcon(line_icon("external", p.primary_text if primary else p.ink, 16))
        repolish(w["open"])
        if profile["login_profile"] and not w["radio"].isChecked():
            w["radio"].blockSignals(True)
            w["radio"].setChecked(True)
            w["radio"].blockSignals(False)

    def _open(self, directory: str) -> None:
        if self.ctx.dry_run or native_host.open_extensions_page(directory):
            self.say("Abrí chrome://extensions. Activa el modo de desarrollador y carga la carpeta.", "ok")
        else:
            self.say("No pude abrir Chrome; abre chrome://extensions en ese perfil a mano.", "fail")

    def summary(self) -> list[steps.Item]:
        if not steps.extension_state()["ok"]:
            return [steps.Item(steps.WARN, "La extensión de Chrome no está cargada en ningún perfil",
                               "sin ella, el login usa una ventana aparte; `casebook extension` para cargarla")]
        return []


class SlackPage(Page):
    title = "Vigilar el canal de soporte"
    lead = ("Opcional. El visor analiza los mensajes nuevos del canal con la skill de triage y deja la respuesta "
            "lista para revisar. Aquí solo se revisa lo que hace falta.")
    step_title, step_subtitle, step_icon = "Slack", "opcional", "message"
    plan_key = "slack"

    def build(self) -> None:
        p = self.palette_
        card = self.card()
        watch = False
        with contextlib.suppress(Exception):
            watch = steps.slack_watch()
        self.watch = Toggle(p, watch)
        self.watch.toggled.connect(self._watch)
        card.box.addWidget(Row(p, "Vigilar el canal desde el visor",
                               "Se prende y se apaga también en el panel Slack del visor", self.tile("bell"),
                               [self.watch]))
        self.body.addWidget(card)
        self.body.addSpacing(8)
        self.body.addLayout(heading("Código local", "opcional · solo lectura"))
        code = self.card()
        self.code_list = RowList(inset=50)
        code.box.addWidget(self.code_list)
        self.body.addWidget(code)
        self.body.addSpacing(8)
        again = button("Revisar de nuevo", "ghost", "refresh", p, small=True)
        again.clicked.connect(self._check)
        self.body.addLayout(heading("Lo que hace falta", note_widget=again))
        checks = self.card()
        self.list = RowList(inset=50)
        checks.box.addWidget(self.list)
        self.body.addWidget(checks)
        self.body.addLayout(self.note("lock", "Nada se publica en Slack: las respuestas quedan como borrador en el visor."))

    def enter(self) -> None:
        if not self.entered:
            self._check()
            self._code()

    # --- código local

    def _code(self) -> None:
        def look() -> tuple[steps.Item, config.CodeRepo, list[str]]:
            repo = steps.code_repo()
            found = [] if steps.code_paths() else steps.find_code_candidates(repo)
            return steps.code_status(), repo, found

        self.run(look, self._show_code, None)

    def _show_code(self, result: tuple[steps.Item, config.CodeRepo, list[str]]) -> None:
        p = self.palette_
        status, repo, found = result
        self.code_list.clear()
        configured = bool(steps.code_paths())
        right = []
        choose = button("Elegir carpeta…" if not configured else "Cambiar", "", "folder", p, small=True)
        choose.clicked.connect(lambda _=False, r=repo: self._choose_code(r))
        right.append(choose)
        if configured:
            drop = button("Quitar", "ghost", "x", p, small=True)
            drop.clicked.connect(self._clear_code)
            right.append(drop)
        self.code_list.add(Row(p, tidy(status.text), tidy(status.detail), StatusDot(p, STATUS_KIND[status.status]),
                               right, height=58))
        for path in found:
            use = button("Usar esta", "", "check", p, small=True)
            use.clicked.connect(lambda _=False, v=path, r=repo: self._save_code(v, r))
            self.code_list.add(Row(p, f"Encontré {repo.label}", path, self.tile("folder"), [use], mono=True,
                                   height=58))

    def _choose_code(self, repo: config.CodeRepo) -> None:
        path = QFileDialog.getExistingDirectory(self, f"La carpeta de {repo.label}")
        if path:
            self._save_code(path, repo)

    def _save_code(self, path: str, repo: config.CodeRepo) -> None:
        if self.ctx.dry_run:
            self.say("Vista previa: la carpeta no se guarda.", "info")
            return
        try:
            saved = steps.set_code_paths(path)
        except (OSError, ValueError) as exc:  # SettingError es un ValueError: ruta que no sirve
            self.say(f"No se guardó esa carpeta: {exc}", "fail")
            return
        missing = steps.missing_markers(saved or "", repo)
        if missing:
            self.say(f"Guardada, pero no veo {', '.join(missing)} ahí: revisa que sea la carpeta correcta.", "warn")
        else:
            self.say("Listo: el análisis de Slack puede leer ese código (solo lectura).", "ok")
        self._code()

    def _clear_code(self) -> None:
        if self.ctx.dry_run:
            return
        try:
            steps.set_code_paths("")
        except (OSError, ValueError) as exc:
            self.say(f"No pude quitarla: {exc}", "fail")
            return
        self.say("El análisis ya no lee código local.", "info")
        self._code()

    def _watch(self, on: bool) -> None:
        if self.ctx.dry_run:
            return
        try:
            steps.set_slack_watch(on)
        except (OSError, ValueError) as exc:
            self.say(f"No pude guardar el ajuste: {exc}", "fail")
            return
        self.say("Vigilancia prendida: el visor la usa en cuanto esté abierto." if on else
                                "Vigilancia apagada.", "ok" if on else "info")

    def _check(self) -> None:
        self.run(steps.slack_checks, self._show, "Revisando Slack…")

    def _show(self, items: list[steps.Item]) -> None:
        p = self.palette_
        self.list.clear()
        for item in items:
            right = []
            if item.fix:
                copy = button(f"Copiar {item.fix}", "", "copy", p, small=True)
                copy.clicked.connect(lambda _=False, v=item.fix: self._copy(v))
                right.append(copy)
            elif item.link:
                link = button("Abrir", "", "external", p, small=True)
                link.clicked.connect(lambda _=False, u=item.link: QDesktopServices.openUrl(QUrl(u)))
                right.append(link)
            self.list.add(Row(p, tidy(item.text), tidy(item.detail), StatusDot(p, STATUS_KIND[item.status]), right,
                              height=58))
        ok = sum(i.status == steps.OK for i in items)
        self.window_.set_subtitle(self, "listo" if ok == 2 else "opcional")
        self.window_.refresh_sidebar()
        if ok == 2:
            self.say("Slack listo: el visor analiza los mensajes nuevos del canal.", "ok")
        else:
            self.say("Slack es opcional: puedes seguir y resolverlo cuando lo quieras usar.", "info")

    def _copy(self, value: str) -> None:
        QGuiApplication.clipboard().setText(value)
        self.say(f"Copiado: {value}. Pégalo en Claude Code.", "ok")

    # Opcional: lo que falta no va a los pendientes del final (summary vacío).


class SessionsPage(Page):
    title = "Inicia sesión una vez"
    lead = ("Validamos la sesión guardada de cada servicio. Si falta o venció, se abre en tu Chrome; Claude nunca "
            "ve las cookies.")
    step_title, step_subtitle, step_icon = "Inicio de sesión", "OpenSearch, CloudWatch", "key"
    plan_key = "login"
    skippable = True

    def should_skip(self) -> bool:
        return not steps.login_systems()

    def build(self) -> None:
        p = self.palette_
        self.rows: dict[str, dict] = {}
        # Las filas se arman al entrar al paso, no aquí: todas las páginas se construyen al abrir la ventana, y el
        # pack se puede instalar o cambiar después, en el paso de configuración (con otros servicios).
        self.row_list = RowList()
        self.row_key: tuple | None = None
        card = self.card()
        card.box.addWidget(self.row_list)
        self.body.addWidget(card)

        self.detail_toggle = button("Ver detalle", "subtle", "chevron-right", p, small=True)
        self.detail_toggle.clicked.connect(self._toggle_detail)
        self.last_line = label("Aquí aparece lo que hace cada login.", "RowText")
        self.last_time = label("", "RowMono")
        self.body.addLayout(hbox(self.detail_toggle, self.last_line, None, self.last_time, spacing=10))
        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        self.log.hide()
        self.body.addWidget(self.log)
        self.status: list[dict] = []
        self.logging_in: str | None = None
        self.cancelled = False

    def enter(self) -> None:
        if self.logging_in:
            return
        cfg = config.load()
        key = (tuple(s.id for s in systems.browser_systems(cfg)), tuple(cfg.mysql))
        if key != self.row_key:  # primera vez, o el pack cambió desde la última
            self._build_rows(cfg)
            self.row_key = key
            self._refresh()
        elif not self.entered:
            self._refresh()

    def _build_rows(self, cfg: config.Config) -> None:
        p = self.palette_
        for w in self.rows.values():
            w["spinner"].stop()
        self.row_list.clear()
        self.rows = {}
        for s in systems.browser_systems(cfg):
            system, name, description = s.id, s.label, s.description
            chip = self.chip("Revisando…")
            waiting_icon = QLabel()
            waiting_icon.setFixedSize(16, 16)
            spinner = Spinner(waiting_icon, p.ink, 16)
            waiting_text = label("Esperando tu Chrome…", "RowText")
            waiting = QWidget()
            waiting.setLayout(hbox(waiting_icon, waiting_text, spacing=8))
            waiting.hide()
            action = button("Iniciar sesión", "", "log-in", p, small=True)
            action.setFixedWidth(150)
            action.setEnabled(False)
            action.clicked.connect(lambda _=False, s=system: self._action(s))
            row = Row(p, name, description, self.tile("activity" if s.kind == "cloudwatch" else "search"),
                      [chip, waiting, action])
            self.row_list.add(row)
            self.rows[system] = {"chip": chip, "waiting": waiting, "spinner": spinner, "waiting_text": waiting_text,
                                 "button": action}
        for env, src in cfg.mysql.items():
            name = config.env_name("mysql", env, src)
            title = "MySQL (réplica)" if name == config.DEFAULT else f"MySQL {name}"
            self.row_list.add(Row(p, src.label or title, f"credenciales: {src.credentials.hint}",
                                  self.tile("database", p.muted), [self.chip("Automático", "neutral", "lock")]))

    def _toggle_detail(self) -> None:
        show = not self.log.isVisible()
        self.log.setVisible(show)
        self.detail_toggle.setText("Ocultar detalle" if show else "Ver detalle")
        self.detail_toggle.setIcon(line_icon("chevron-down" if show else "chevron-right", self.palette_.ink, 16))

    def _append(self, line: str) -> None:
        self.log.appendPlainText(line)
        if line.strip():
            self.last_line.setText(line.strip()[:90])
            self.last_time.setText(time.strftime("%H:%M:%S"))

    def _refresh(self) -> None:
        self.run(steps.sessions, self._show, "Validando las sesiones guardadas…")

    def _show(self, sessions: list[dict]) -> None:
        p = self.palette_
        self.status = sessions
        for s in sessions:
            w = self.rows.get(s["system"])
            if w is None:  # una fuente que apareció después de armar las filas: se agrega al volver a entrar
                continue
            text, tone, icon = {
                "ok": ("Sesión válida", "ok", "check"),
                "missing": ("Sin sesión", "neutral", None),
                "expired": ("Venció", "fail", "x"),
                "unknown": ("No se pudo validar", "warn", "alert"),
            }[s["status"]]
            w["chip"].set(text, tone, icon)
            tip(w["chip"], ". ".join(x for x in (s["detail"], s.get("extra")) if x))
            primary = s["status"] != "ok"
            w["button"].setText("Renovar" if not primary else "Iniciar sesión")
            w["button"].setObjectName("Primary" if primary else "Small")
            w["button"].setIcon(line_icon("log-in" if primary else "refresh", p.primary_text if primary else p.ink, 16))
            w["button"].setEnabled(not self.ctx.dry_run and not self.logging_in)
            repolish(w["button"])
        ok = sum(s["status"] == "ok" for s in sessions)
        self.warn = ok < len(sessions)
        self.window_.set_subtitle(self, f"{ok} de {len(sessions)} activas")
        self.window_.refresh_sidebar()
        if not self.logging_in and not self.window_.status.text():
            missing = len(sessions) - ok
            self.say("Todas las sesiones activas." if not self.warn else
                                    f"{missing} servicio{'s' if missing > 1 else ''} sin sesión: puedes iniciar sesión "
                                    "después.", "ok" if not self.warn else "info")

    def _action(self, system: str) -> None:
        if self.logging_in == system:
            self.cancelled = True
            self.rows[system]["waiting_text"].setText("Cancelando…")
            return
        self._login(system)

    def _login(self, system: str) -> None:
        p = self.palette_
        self.logging_in, self.cancelled = system, False
        for s, w in self.rows.items():
            w["button"].setEnabled(s == system)
        w = self.rows[system]
        w["chip"].hide()
        mode = "extension" if native_host.connected() else "window"
        w["waiting_text"].setText("Esperando tu Chrome…" if mode == "extension" else "Esperando la ventana de Chrome…")
        w["waiting"].show()
        w["spinner"].start()
        w["button"].setText("Cancelar")
        w["button"].setObjectName("Small")
        w["button"].setIcon(line_icon("x", p.ink, 16))
        repolish(w["button"])
        self._append(f"$ {steps.login_command(system)}")
        self.say("Inicia sesión en Chrome; esta pantalla lo detecta sola (hasta 5 min).", "info")
        self.run(lambda: steps.login(system, 300, mode=mode, replace=True, cancel=lambda: self.cancelled),
                 lambda code: self._logged(system, code), None, capture=self._append)

    def _logged(self, system: str, code: object) -> None:
        w = self.rows[system]
        w["spinner"].stop()
        w["waiting"].hide()
        w["chip"].show()
        self.logging_in = None
        self._append("")
        if self.cancelled:
            self.say("Login cancelado; la sesión guardada no cambió.", "info")
        else:
            self.say("Sesión guardada." if code == 0 else "El login no terminó; puedes reintentar.",
                                    "ok" if code == 0 else "warn")
        self._refresh()

    def summary(self) -> list[steps.Item]:
        return [steps.Item(steps.WARN, f"{s['label']}: sin sesión activa", f"corre `{steps.login_command(s['system'])}`",
                           fix=steps.login_command(s["system"]))
                for s in self.status if s["status"] != "ok"]


class DonePage(Page):
    title = "Todo listo"
    step_title, step_subtitle, step_icon = "Listo", "siguientes pasos", "flag"

    NEXT = (
        ("refresh", "Reconecta tu agente", "En Claude Code: /mcp → reconectar", "/mcp", "terminal"),
        ("search", "Pide tu primera investigación", "El visor se abre solo con cada consulta", f"investiga {steps.card_example()}",
         "message"),
        ("activity", "Revisa el estado cuando quieras", "Túneles, sesiones y credenciales", "casebook doctor",
         "terminal"),
        ("laptop", "Tus investigaciones, en un lugar", "El visor con todas las sesiones", "casebook start",
         "terminal"),
    )

    def build_header(self) -> None:
        p = self.palette_
        badge = QLabel()
        badge.setFixedSize(56, 56)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(f"background: {p.success_soft}; border-radius: 28px;")
        inner = QLabel(badge)
        inner.setGeometry(10, 10, 36, 36)
        inner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.setStyleSheet(f"background: {p.success}; border-radius: 18px;")
        inner.setPixmap(line_pixmap("check", "#ffffff", 20, 3))
        self.hero_lead = label("", "PageLead", wrap=True)
        heading_box = vbox(label(self.title, "HeroTitle"), self.hero_lead, spacing=4)
        row = hbox(badge, spacing=16)
        row.addLayout(heading_box, 1)
        self.body.addLayout(row)
        self.body.addSpacing(8)

    def set_step(self, number: int, total: int) -> None:
        self.kicker_label.hide()

    def build(self) -> None:
        p = self.palette_
        self.banner = QFrame()
        self.banner.setObjectName("Banner")
        b = QHBoxLayout(self.banner)
        b.setContentsMargins(16, 12, 12, 12)
        b.setSpacing(12)
        self.banner_dot = StatusDot(p, "ok")
        self.banner_title = label("", "Strong")
        self.banner_text = label("", "RowText", wrap=True)
        self.banner_button = button("Ver cómo", "", None, p, small=True)
        self.banner_button.clicked.connect(self._toggle_pending)
        b.addWidget(self.banner_dot, 0, Qt.AlignmentFlag.AlignTop)
        b.addLayout(vbox(self.banner_title, self.banner_text), 1)
        b.addWidget(self.banner_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addWidget(self.banner)
        self.pending_card = self.card()
        self.pending = RowList(inset=50)
        self.pending_card.box.addWidget(self.pending)
        self.pending_card.hide()
        self.body.addWidget(self.pending_card)

        self.body.addSpacing(8)
        self.body.addWidget(label("Siguientes pasos", "Heading"))
        grid = QGridLayout()
        grid.setSpacing(16)
        for i, (icon, title, text, command, field_icon) in enumerate(self.NEXT):
            card = Card(p, (16, 16, 16, 16), spacing=14)
            card.box.addLayout(hbox(self.tile(icon), vbox(label(title, "RowTitle"), label(text, "RowText")), None,
                                    spacing=12))
            field = CopyField(p, command, field_icon)
            field.copied.connect(lambda value: self.say(f"Copiado: {value}", "ok"))
            card.box.addWidget(field)
            grid.addWidget(card, i // 2, i % 2)
        self.body.addLayout(grid)

    def enter(self) -> None:
        p = self.palette_
        self.hero_lead.setText(f"casebook quedó instalado en {self.ctx.mode_label}. Esto es lo que sigue.")
        pending = [i for page in self.window_.pages[:-1] if page.enabled for i in page.summary()]
        self.pending.clear()
        for item in pending:
            title, text = split_text(item.text)
            row = Row(p, title, text or item.detail, StatusDot(p, STATUS_KIND.get(item.status, "info")), height=56)
            if item.fix:
                row.extra.addWidget(CopyField(p, item.fix))
            self.pending.add(row)
        tone = "warn" if pending else "ok"
        set_prop(self.banner, "tone", tone)
        self.banner_dot.set(tone)
        n = len(pending)
        self.banner_title.setText(f"{n} cosa{'s' if n > 1 else ''} para después" if pending else "Nada pendiente")
        self.banner_text.setText(" · ".join(split_text(i.text)[0] for i in pending[:3]) if pending
                                 else "Todo quedó en orden.")
        self.banner_button.setVisible(bool(pending))
        self.say("Instalación completada." if not pending else f"{n} pendiente{'s' if n > 1 else ''} "
                                "arriba.", "ok" if not pending else "warn")

    def _toggle_pending(self) -> None:
        show = not self.pending_card.isVisible()
        self.pending_card.setVisible(show)
        self.banner_button.setText("Ocultar" if show else "Ver cómo")

    def primary_text(self) -> str:
        return "Cerrar"

    def primary_icon(self) -> str | None:
        return None

    def primary(self) -> None:
        self.window_.close()

    def extra_button(self) -> tuple[str, str, Callable[[], None]] | None:
        return "Abrir el visor", "external", self._open_viewer

    def _open_viewer(self) -> None:
        if self.ctx.dry_run:
            self.say("En la vista previa no se abre el visor.", "info")
            return
        target = self.ctx.cli_target
        if not target.exists():
            self.say(f"No encontré {short_path(target)}.", "fail")
            return
        subprocess.Popen([str(target), "start"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        self.say("Abriendo el visor en tu navegador…", "ok")


# --- ventana -------------------------------------------------------------------------------------------------


class InstallerWindow(QMainWindow):
    def __init__(self, ctx: steps.Context, palette: Palette) -> None:
        super().__init__()
        self.ctx, self.palette_ = ctx, palette
        self.pool = QThreadPool.globalInstance()
        self.tasks: set[Task] = set()
        self.busy = False
        self.setWindowTitle("casebook · Instalador")
        self.setWindowIcon(app_icon())
        self.setMinimumSize(*MIN_SIZE)
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        width, height = WINDOW_SIZE
        if available:  # en pantallas chicas, lo que quepa (sin bajar del mínimo)
            width = max(MIN_SIZE[0], min(width, available.width() - 40))
            height = max(MIN_SIZE[1], min(height, available.height() - 40))
        self.resize(width, height)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(12, 22, 12, 16)
        side.setSpacing(0)
        logo = QLabel()
        logo.setFixedSize(32, 32)
        logo.setPixmap(app_pixmap(32, palette.logo))
        from importlib.metadata import version

        names = vbox(label("casebook", "BrandName"),
                     label(f"v{version('casebook')} · vista previa" if ctx.dry_run else
                           f"Instalador · v{version('casebook')}", "BrandSubtitle"), spacing=1)
        side.addLayout(hbox(logo, names, spacing=10, margins=(12, 0, 0, 0)))
        side.addSpacing(28)
        side.addLayout(hbox(label("PASOS", "SideSection"), margins=(12, 0, 0, 0)))
        side.addSpacing(10)

        self.stack = QStackedWidget()
        self.pages: list[Page] = [WelcomePage(self), PackPage(self), AgentsPage(self), PrereqsPage(self), ExtensionPage(self),
                                  SlackPage(self), SessionsPage(self), DonePage(self)]
        self.step_rows: list[SideStep] = []
        for n, page in enumerate(self.pages, 1):
            page.set_step(n, len(self.pages))
            row = SideStep(palette, page.step_icon, page.step_title, page.step_subtitle, n == 1, n == len(self.pages))
            self.step_rows.append(row)
            side.addWidget(row)
            self.stack.addWidget(page)
        side.addStretch(1)
        self.mode_box = QFrame()
        self.mode_box.setObjectName("ModeBox")
        self.mode_box.setFixedHeight(44)
        self.mode_icon = icon_label("code" if ctx.dev else "package", palette.muted, 16)
        self.mode_text = label("", "ModeText")
        info = icon_label("info", palette.faint, 16)
        self.mode_info = info
        self.mode_box.setLayout(hbox(self.mode_icon, self.mode_text, None, info, spacing=10, margins=(12, 0, 12, 0)))
        side.addWidget(self.mode_box)
        h.addWidget(sidebar)

        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self.stack, 1)
        bar = QFrame()
        bar.setObjectName("BottomBar")
        bar.setFixedHeight(72)
        b = QHBoxLayout(bar)
        b.setContentsMargins(48, 0, 48, 0)
        b.setSpacing(10)
        self.status_icon = StatusDot(palette, "pending")
        self.spinner = Spinner(self.status_icon, palette.ink, 18)
        b.addWidget(self.status_icon)
        self.status = label("", "StatusText", wrap=True)
        b.addWidget(self.status, 1)
        self.back = button("Atrás", "ghost", "arrow-left", palette)
        self.back.clicked.connect(self.previous)
        self.skip = button("Omitir", "ghost")
        self.skip.clicked.connect(self.next)
        self.extra = button("", "")
        self.extra.clicked.connect(lambda: self._extra_action and self._extra_action())
        self._extra_action: Callable[[], None] | None = None
        self.primary = button("", "primary")
        self.primary.setMinimumWidth(130)
        self.primary.setIconSize(QSize(16, 16))
        self.primary.clicked.connect(lambda: self.current().primary())
        for widget in (self.back, self.skip, self.extra, self.primary):
            b.addWidget(widget)
        main.addWidget(bar)
        h.addLayout(main, 1)
        self.tips = TipOverlay(root, palette)

        self.index = 0
        self.set_mode_label()
        self.show_page(0)

    def set_mode_label(self) -> None:
        self.mode_text.setText(self.ctx.mode_label[:1].upper() + self.ctx.mode_label[1:])
        tip(self.mode_info, short_path(self.ctx.venv), "El entorno de esta instalación")

    def current(self) -> Page:
        return self.pages[self.index]

    def show_page(self, index: int) -> None:
        old = self.current()
        if hasattr(old, "leave") and old.entered:
            old.leave()
        self.index = index
        page = self.current()
        self.stack.setCurrentWidget(page)
        self.set_status("", None)
        page.enter()
        page.entered = True
        self.refresh_sidebar()
        self.refresh_buttons()

    def next(self) -> None:
        for i in range(self.index + 1, len(self.pages)):
            if self.pages[i].enabled and not self.pages[i].should_skip():
                self.show_page(i)
                return

    def previous(self) -> None:
        for i in range(self.index - 1, -1, -1):
            if self.pages[i].enabled and not self.pages[i].should_skip():
                self.show_page(i)
                return

    def set_subtitle(self, page: Page, text: str) -> None:
        row = self.step_rows[self.pages.index(page)]
        row.subtitle = text
        row.update()

    def refresh_sidebar(self) -> None:
        above = False
        for i, (row, page) in enumerate(zip(self.step_rows, self.pages)):
            done = page.entered and i != self.index and page.enabled
            row.set_state(current=i == self.index, done=done, warn=page.warn,
                          skipped=not page.enabled or page.should_skip(),
                          above_done=above)
            above = done

    def refresh_buttons(self) -> None:
        page = self.current()
        p = self.palette_
        icon = page.primary_icon()
        self.primary.setText(page.primary_text() + (" " if icon else ""))
        self.primary.setIcon(line_icon(icon, p.primary_text, 16) if icon else QIcon())
        self.primary.setLayoutDirection(Qt.LayoutDirection.RightToLeft)  # la flecha, a la derecha del texto
        self.primary.setEnabled(not self.busy)
        first = next(i for i, pg in enumerate(self.pages) if pg.enabled)
        self.back.setVisible(self.index > first and self.index < len(self.pages) - 1)
        self.back.setEnabled(not self.busy)
        self.skip.setVisible(page.skippable)
        self.skip.setEnabled(not self.busy)
        extra = page.extra_button()
        self.extra.setVisible(bool(extra))
        if extra:
            text, icon_name, action = extra
            self.extra.setText(text)
            self.extra.setIcon(line_icon(icon_name, p.ink, 16))
            self._extra_action = action
        self.extra.setEnabled(not self.busy)

    def set_busy(self, text: str | None) -> None:
        """Con texto: bloquea los botones y muestra el spinner. None: los libera y borra el «Registrando…»; la
        página pone después su propio resultado."""
        was_busy, self.busy = self.busy, text is not None
        if text:
            self.set_status(text, "busy")
        elif was_busy:
            self.set_status("", None)
        self.refresh_buttons()

    def set_status(self, text: str, kind: str | None) -> None:
        self.status.setText(text)
        self.spinner.stop()
        if kind is None or not text:
            self.status_icon.clear()
            return
        if kind == "busy":
            self.spinner.start()
            return
        self.status_icon.set(kind if kind in ("ok", "warn", "fail") else "info")

    def closeEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        self.pool.clear()
        super().closeEvent(event)


def system_palette(app: QApplication) -> Palette:
    try:
        return DARK if app.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT
    except AttributeError:  # Qt < 6.5
        return LIGHT


def load_fonts() -> None:
    """Inter y Source Code Pro vienen con el paquete: así se ve igual en cualquier máquina."""
    for path in sorted(FONTS_DIR.glob("*.[ot]tf")):
        QFontDatabase.addApplicationFont(str(path))


def quiet_qt() -> None:
    """Qt en Wayland avisa por cada campo de texto (qt.qpa.wayland.textinput) y no aporta nada: solo quedan
    los errores críticos. Se respeta un QT_LOGGING_RULES propio, para depurar."""
    import os

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    if os.environ.get("QT_LOGGING_RULES"):
        return

    def handler(kind, _context, message) -> None:
        if kind in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
            print(message, file=sys.stderr)

    os.environ["QT_LOGGING_RULES"] = "qt.qpa.*=false"
    qInstallMessageHandler(handler)


def run(ctx: steps.Context, screenshot: str | None = None, page: int = 0) -> int:
    quiet_qt()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("casebook")
    app.setWindowIcon(app_icon())
    load_fonts()
    app.setFont(QFont("Inter", 10))
    palette = system_palette(app)
    app.setStyleSheet(stylesheet(palette))
    window = InstallerWindow(ctx, palette)
    window.show()
    if screenshot:  # para docs y pruebas: abre en una página y guarda la captura
        for _ in range(page):
            window.next()
        QTimer.singleShot(2500, lambda: (window.grab().save(screenshot), app.quit()))
    return app.exec()
