"""Las piezas del instalador gráfico: toggle, chips, puntos de estado, tooltips con flecha, campos copiables,
el anillo de progreso y los pasos de la barra lateral. Lo que QSS no dibuja se pinta con QPainter."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .icons import line_icon, line_pixmap
from .theme import Palette, status_colors

STATUS_ICON = {"ok": "check", "warn": "alert", "fail": "x", "info": "info"}


def label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def icon_label(name: str, color: str, size: int = 16, stroke: float = 2.0) -> QLabel:
    widget = QLabel()
    widget.setPixmap(line_pixmap(name, color, size, stroke))
    widget.setFixedSize(size, size)
    return widget


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    for child in widget.findChildren(QWidget):
        child.style().unpolish(child)
        child.style().polish(child)


def set_prop(widget: QWidget, key: str, value: object) -> None:
    widget.setProperty(key, value if isinstance(value, str) else ("true" if value else "false"))
    repolish(widget)


def clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear(item.layout())


def hbox(*widgets, spacing: int = 8, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    box = QHBoxLayout()
    box.setSpacing(spacing)
    box.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            box.addStretch(1)
        elif isinstance(w, int):
            box.addSpacing(w)
        elif isinstance(w, QWidget):
            box.addWidget(w)
        else:
            box.addLayout(w)
    return box


def vbox(*widgets, spacing: int = 2, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    box = QVBoxLayout()
    box.setSpacing(spacing)
    box.setContentsMargins(*margins)
    for w in widgets:
        if w is None:
            box.addStretch(1)
        elif isinstance(w, QWidget):
            box.addWidget(w)
        else:
            box.addLayout(w)
    return box


def tip(widget: QWidget, text: str, title: str = "") -> QWidget:
    """Un tooltip con flecha (TipOverlay) en vez del nativo: `title` va en negrita sobre `text`."""
    widget.setProperty("tip", text)
    widget.setProperty("tipTitle", title)
    widget.setToolTip("")
    return widget


def button(text: str, kind: str = "", icon: str | None = None, palette: Palette | None = None,
           icon_right: bool = False, small: bool = False) -> QPushButton:
    # Un espacio separa el texto del ícono (QSS no tiene un margen para eso).
    b = QPushButton((f"{text} " if icon_right else f" {text}") if icon and text else text)
    b.setObjectName({"primary": "Primary", "ghost": "Ghost", "subtle": "Subtle"}.get(kind, "Small" if small else ""))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon and palette:
        color = palette.primary_text if kind == "primary" else palette.muted if kind == "ghost" else palette.ink
        b.setIcon(line_icon(icon, color, 16))
        b.setIconSize(QSize(16, 16))
        if icon_right:
            b.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    return b


def shadow(widget: QWidget, palette: Palette, blur: int = 10, dy: int = 1, alpha: int = 16) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    color = QColor(palette.shadow)
    color.setAlpha(alpha)
    effect.setColor(color)
    widget.setGraphicsEffect(effect)


# --- piezas simples ------------------------------------------------------------------------------------------


class Card(QFrame):
    def __init__(self, palette: Palette, margins=(0, 0, 0, 0), spacing: int = 0, name: str = "Card") -> None:
        super().__init__()
        self.setObjectName(name)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(*margins)
        self.box.setSpacing(spacing)
        if name == "Card":
            shadow(self, palette)


class Divider(QFrame):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("Divider")


class Tile(QLabel):
    """Cuadro con un ícono de línea, a la izquierda de cada fila."""

    def __init__(self, icon: str, palette: Palette, size: int = 36, color: str | None = None) -> None:
        super().__init__()
        self.setObjectName("Tile")
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setPixmap(line_pixmap(icon, color or palette.ink, size // 2))


class Chip(QFrame):
    def __init__(self, palette: Palette, text: str = "", tone: str = "neutral", icon: str | None = None) -> None:
        super().__init__()
        self.setObjectName("Chip")
        self.palette_ = palette
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(22)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 0, 9, 0)
        h.setSpacing(4)
        self.icon = QLabel()
        self.icon.setFixedSize(12, 12)
        h.addWidget(self.icon)
        self.text = label(text, "ChipText")
        h.addWidget(self.text)
        self.set(text, tone, icon)

    def set(self, text: str, tone: str = "neutral", icon: str | None = None) -> None:
        self.text.setText(text)
        self.icon.setVisible(bool(icon))
        if icon:
            self.icon.setPixmap(line_pixmap(icon, status_colors(self.palette_, tone)[0], 12, 2.6))
        set_prop(self, "tone", tone)


class StatusDot(QLabel):
    """Círculo de 20 px con fondo suave y el ícono del estado (ok, warn, fail, info; pending: vacío)."""

    def __init__(self, palette: Palette, status: str = "pending", size: int = 20) -> None:
        super().__init__()
        self.palette_, self.size_ = palette, size
        self.setFixedSize(size, size)
        self.set(status)

    def set(self, status: str) -> None:
        p, size = self.palette_, self.size_
        color, soft = status_colors(p, status) if status != "pending" else (p.faint, p.sunken)
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(soft))
        painter.drawEllipse(QRectF(0, 0, size, size))
        if status in STATUS_ICON:
            inner = round(size * 0.6)
            painter.drawPixmap(QRect((size - inner) // 2, (size - inner) // 2, inner, inner),
                               line_pixmap(STATUS_ICON[status], color, inner, 2.8))
        painter.end()
        self.setPixmap(pixmap)


class Spinner(QObject):
    """Un arco que gira en un QLabel (16 cuadros por vuelta se ven fluidos)."""

    def __init__(self, target: QLabel, color: str, size: int = 18) -> None:
        super().__init__(target)
        self.target, self.size, self.angle = target, size, 0
        self.base = line_pixmap("loader", color, size, 2.4)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self.timer.isActive():
            self.timer.start()
            self._tick()

    def stop(self) -> None:
        self.timer.stop()

    def _tick(self) -> None:
        ratio = self.base.devicePixelRatio()
        frame = QPixmap(self.base.size())
        frame.setDevicePixelRatio(ratio)
        frame.fill(Qt.GlobalColor.transparent)
        painter = QPainter(frame)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        half = self.size / 2
        painter.translate(half, half)
        painter.rotate(self.angle)
        painter.translate(-half, -half)
        painter.drawPixmap(0, 0, self.base)
        painter.end()
        self.target.setPixmap(frame)
        self.angle = (self.angle + 22.5) % 360


class ElidingLabel(QLabel):
    """Recorta el centro de un texto largo (una ruta) en vez de estirar la página; el tooltip lo muestra entero."""

    def __init__(self, name: str, text: str = "") -> None:
        super().__init__()
        self.setObjectName(name)
        self.full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self.full_text = text
        self._elide()

    def resizeEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = max(self.width() - 4, 40)
        elided = QFontMetrics(self.font()).elidedText(self.full_text, Qt.TextElideMode.ElideMiddle, width)
        super().setText(elided)
        tip(self, self.full_text if elided != self.full_text else "")


class CopyField(QFrame):
    """Un comando o una ruta en monoespaciada con su botón de copiar (que confirma con un ✓)."""

    copied = Signal(str)

    def __init__(self, palette: Palette, value: str = "", icon: str = "terminal") -> None:
        super().__init__()
        self.setObjectName("CopyField")
        self.palette_ = palette
        self.setFixedHeight(36)
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 0, 4, 0)
        h.setSpacing(10)
        self.icon = icon_label(icon, palette.muted, 16)
        h.addWidget(self.icon)
        self.value = ElidingLabel("Mono", value)
        h.addWidget(self.value, 1)
        self.copy_button = QPushButton()
        self.copy_button.setObjectName("IconButton")
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.setFixedSize(28, 28)
        self.copy_button.setIcon(line_icon("copy", palette.muted, 14))
        tip(self.copy_button, "Copiar")
        self.copy_button.clicked.connect(self._copy)
        h.addWidget(self.copy_button)

    def set_value(self, value: str) -> None:
        self.value.set_full_text(value)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.value.full_text)
        self.copy_button.setIcon(line_icon("check", self.palette_.success, 14))
        self.copied.emit(self.value.full_text)
        QTimer.singleShot(2000, lambda: self.copy_button.setIcon(line_icon("copy", self.palette_.muted, 14)))


# --- controles pintados --------------------------------------------------------------------------------------


class Toggle(QAbstractButton):
    """Interruptor de 36x20 con la perilla animada. `locked`: siempre encendido, con candado."""

    def __init__(self, palette: Palette, checked: bool = False, locked: bool = False) -> None:
        super().__init__()
        self.palette_, self.locked = palette, locked
        self.setCheckable(True)
        self.setChecked(checked or locked)
        self.setFixedSize(36, 20)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.PointingHandCursor)
        self._pos = 1.0 if self.isChecked() else 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(140)
        self.animation.valueChanged.connect(self._moved)
        self.toggled.connect(self._animate)
        self.lock_pixmap = line_pixmap("lock", palette.toggle_locked, 9, 2.6)

    def nextCheckState(self) -> None:  # noqa: N802 - nombre de Qt
        if not self.locked:
            super().nextCheckState()

    def _animate(self, on: bool) -> None:
        self.animation.stop()
        self.animation.setStartValue(self._pos)
        self.animation.setEndValue(1.0 if on else 0.0)
        self.animation.start()

    def _moved(self, value) -> None:
        self._pos = float(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        p = self.palette_
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        off, on = QColor(p.toggle_off), QColor(p.toggle_locked if self.locked else p.primary)
        if not self.isEnabled() and not self.locked:
            off, on = QColor(p.border), QColor(p.toggle_locked)
        t = self._pos
        track = QColor(round(off.red() + (on.red() - off.red()) * t), round(off.green() + (on.green() - off.green()) * t),
                       round(off.blue() + (on.blue() - off.blue()) * t))
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, 36, 20), 10, 10)
        x = 2 + 16 * t
        shade = QColor(p.shadow)
        shade.setAlpha(50)
        painter.setBrush(shade)
        painter.drawEllipse(QRectF(x, 3, 16, 16))
        k_off, k_on = QColor(p.knob), QColor(p.knob if self.locked else p.knob_on)
        painter.setBrush(QColor(round(k_off.red() + (k_on.red() - k_off.red()) * t),
                                round(k_off.green() + (k_on.green() - k_off.green()) * t),
                                round(k_off.blue() + (k_on.blue() - k_off.blue()) * t)))
        painter.drawEllipse(QRectF(x, 2, 16, 16))
        if self.locked:
            painter.drawPixmap(QRect(round(x) + 4, 5, 9, 9), self.lock_pixmap)
        painter.end()


class Segmented(QFrame):
    """Selector de dos o más opciones (íconos + texto); `changed` da el índice elegido."""

    changed = Signal(int)

    def __init__(self, palette: Palette, options: list[tuple[str, str]], current: int = 0) -> None:
        super().__init__()
        self.setObjectName("Segmented")
        h = QHBoxLayout(self)
        h.setContentsMargins(3, 3, 3, 3)
        h.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: list[QPushButton] = []
        for i, (icon, text) in enumerate(options):
            b = QPushButton(f" {text}")
            b.setObjectName("Segment")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIcon(line_icon(icon, palette.ink, 16))
            b.setIconSize(QSize(16, 16))
            b.setMinimumWidth(200)
            self.group.addButton(b, i)
            self.buttons.append(b)
            h.addWidget(b)
        self.buttons[current].setChecked(True)
        self.group.idClicked.connect(self.changed.emit)

    def select(self, index: int) -> None:
        self.buttons[index].setChecked(True)


class ProgressRing(QWidget):
    def __init__(self, palette: Palette, size: int = 48) -> None:
        super().__init__()
        self.palette_, self.done, self.total = palette, 0, 0
        self.setFixedSize(size, size)

    def set(self, done: int, total: int) -> None:
        self.done, self.total = done, total
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        p = self.palette_
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(4, 4, self.width() - 8, self.height() - 8)
        painter.setPen(QPen(QColor(p.border_soft), 5))
        painter.drawEllipse(rect)
        if self.total:
            frac = self.done / self.total
            pen = QPen(QColor(p.success if frac == 1 else p.success), 5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(rect, 90 * 16, -round(360 * 16 * frac))
        painter.setPen(QColor(p.ink))
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{self.done}/{self.total}" if self.total else "…")
        painter.end()


class StepBars(QWidget):
    """El avance en el encabezado: una barrita por paso."""

    def __init__(self, palette: Palette, step: int, total: int) -> None:
        super().__init__()
        self.palette_, self.step, self.total = palette, step, total
        self.setFixedSize(total * 22, 10)

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(self.total):
            painter.setBrush(QColor(self.palette_.ink if i < self.step else self.palette_.border))
            painter.drawRoundedRect(QRectF(i * 22, 3, 18, 4), 2, 2)
        painter.end()


class SideStep(QWidget):
    """Un paso de la barra lateral, pintado: fondo y barra de marca si es el actual, el círculo (ícono, ✓ o !),
    la línea que lo une con el siguiente y los dos textos."""

    clicked = Signal()
    HEIGHT = 64

    def __init__(self, palette: Palette, icon: str, title: str, subtitle: str, first: bool, last: bool) -> None:
        super().__init__()
        self.palette_, self.icon, self.title, self.subtitle = palette, icon, title, subtitle
        self.first, self.last = first, last
        self.current = self.done = self.warn = self.skipped = self.above_done = False
        self.setFixedHeight(self.HEIGHT)

    def set_state(self, current: bool, done: bool, warn: bool = False, skipped: bool = False,
                  above_done: bool = False) -> None:
        self.current, self.done, self.warn, self.skipped, self.above_done = current, done, warn, skipped, above_done
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        p = self.palette_
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        box = QRectF(0.5, 0.5, w - 1, 52)
        if self.current:
            painter.setPen(QPen(QColor(p.border), 1))
            painter.setBrush(QColor(p.surface))
            painter.drawRoundedRect(box, 10, 10)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.brand))
            painter.drawRoundedRect(QRectF(0, 14, 3, 24), 1.5, 1.5)
        cx, cy = 27, 26
        if not self.first:
            painter.setPen(QPen(QColor(p.success if self.above_done else p.border), 1.5))
            painter.drawLine(QPointF(cx, 0), QPointF(cx, cy - 13))
        if not self.last:
            painter.setPen(QPen(QColor(p.success if self.done and not self.current else p.border), 1.5))
            painter.drawLine(QPointF(cx, cy + 13), QPointF(cx, self.HEIGHT))
        circle = QRectF(cx - 12, cy - 12, 24, 24)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.current:
            painter.setBrush(QColor(p.primary))
            painter.drawEllipse(circle)
            icon = line_pixmap(self.icon, p.primary_text, 14, 2.2)
        elif self.done and self.warn:
            painter.setBrush(QColor(p.warning_soft))
            painter.drawEllipse(circle)
            icon = line_pixmap("alert", p.warning, 12, 2.6)
        elif self.done:
            painter.setBrush(QColor(p.success_soft))
            painter.drawEllipse(circle)
            icon = line_pixmap("check", p.success, 12, 2.8)
        else:
            painter.setPen(QPen(QColor(p.border), 1))
            painter.setBrush(QColor(p.surface))
            painter.drawEllipse(circle.adjusted(0.5, 0.5, -0.5, -0.5))
            icon = line_pixmap(self.icon, p.faint, 13)
        size = icon.width() / icon.devicePixelRatio()
        painter.drawPixmap(QPointF(cx - size / 2, cy - size / 2), icon)

        font = QFont(self.font())
        font.setPixelSize(13)
        font.setWeight(QFont.Weight.DemiBold if self.current else QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(p.ink if (self.current or self.done) and not self.skipped else p.muted))
        painter.drawText(QPointF(50, 23), self.title)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        sub_color = p.faint
        if self.skipped:
            sub_color = p.faint
        elif self.done and self.warn:
            sub_color = p.warning
        elif self.done:
            sub_color = p.success
        painter.setPen(QColor(sub_color))
        text = "omitido" if self.skipped else self.subtitle
        text = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, w - 60)
        painter.drawText(QPointF(50, 40), text)
        painter.end()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        self.clicked.emit()


# --- tooltips ------------------------------------------------------------------------------------------------


class TipOverlay(QWidget):
    """El tooltip: un widget dentro de la ventana (no una ventana aparte), así se ubica igual en Wayland.

    Cualquier widget con la propiedad `tip` (ver tip()) lo muestra al pasar el mouse, debajo si cabe y si no
    arriba, con la flecha apuntando al centro del widget."""

    ARROW = 6
    MAX_WIDTH = 320

    def __init__(self, root: QWidget, palette: Palette) -> None:
        super().__init__(root)
        self.root, self.palette_ = root, palette
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self.box = QVBoxLayout(self)
        self.box.setSpacing(2)
        self.title = QLabel()
        self.text = QLabel()
        for widget, weight, color in ((self.title, 600, palette.tooltip_text), (self.text, 400, palette.tooltip_muted)):
            widget.setWordWrap(True)
            widget.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: {weight}; background: transparent;")
            self.box.addWidget(widget)
        self.arrow_up, self.arrow_x = True, 20
        self.target: QWidget | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self._show)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - nombre de Qt
        kind = event.type()
        if isinstance(obj, QWidget) and kind == QEvent.Type.Enter and obj.property("tip"):
            if obj.window() is self.root.window():
                self.target = obj
                self.timer.start()
        elif kind in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.Hide) and obj is self.target:
            self.timer.stop()
            self.hide()
            self.target = None
        return False

    def _show(self) -> None:
        target = self.target
        if target is None or not target.isVisible() or not target.property("tip"):
            return
        title = target.property("tipTitle") or ""
        self.title.setText(title)
        self.title.setVisible(bool(title))
        self.text.setText(target.property("tip"))
        # Tan ancho como el texto, hasta MAX_WIDTH; más largo se ajusta de línea.
        widest = max(widget.fontMetrics().horizontalAdvance(line) for widget in (self.title, self.text)
                     for line in (widget.text() or " ").splitlines())
        for widget in (self.title, self.text):
            widget.setFixedWidth(min(widest + 2, self.MAX_WIDTH - 24))
        top_left = target.mapTo(self.root, QPoint(0, 0))
        rect = QRect(top_left, target.size())
        self.box.setContentsMargins(12, 8 + self.ARROW, 12, 8)
        self.adjustSize()
        height = self.sizeHint().height()
        self.arrow_up = rect.bottom() + 6 + height < self.root.height() - 8
        if not self.arrow_up:
            self.box.setContentsMargins(12, 8, 12, 8 + self.ARROW)
            self.adjustSize()
        width, height = self.sizeHint().width(), self.sizeHint().height()
        x = max(8, min(rect.center().x() - 24, self.root.width() - width - 8))
        y = rect.bottom() + 4 if self.arrow_up else rect.top() - height - 4
        self.arrow_x = max(14, min(rect.center().x() - x, width - 14))
        self.setGeometry(x, y, width, height)
        self.raise_()
        self.show()

    def paintEvent(self, event) -> None:  # noqa: N802 - nombre de Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        a, w, h = self.ARROW, self.width(), self.height()
        body = QRectF(0, a, w, h - a) if self.arrow_up else QRectF(0, 0, w, h - a)
        path = QPainterPath()
        path.addRoundedRect(body, 8, 8)
        tri = QPainterPath()
        if self.arrow_up:
            tri.moveTo(self.arrow_x - a, a + 0.5)
            tri.lineTo(self.arrow_x, 0)
            tri.lineTo(self.arrow_x + a, a + 0.5)
        else:
            tri.moveTo(self.arrow_x - a, h - a - 0.5)
            tri.lineTo(self.arrow_x, h)
            tri.lineTo(self.arrow_x + a, h - a - 0.5)
        tri.closeSubpath()
        painter.fillPath(path.united(tri), QColor(self.palette_.tooltip))
        painter.end()


# --- filas ---------------------------------------------------------------------------------------------------


class Row(QFrame):
    """Fila de lista: a la izquierda un tile o un punto de estado, título (con chip) y texto; a la derecha los
    controles. `extra` es un bloque que va debajo del texto (el detalle de un aviso, por ejemplo)."""

    def __init__(self, palette: Palette, title: str, text: str = "", lead: QWidget | None = None,
                 right: list[QWidget] | None = None, chip: Chip | None = None, mono: bool = False,
                 height: int = 60, chip_right: bool = False) -> None:
        super().__init__()
        self.setObjectName("Row")
        self.setMinimumHeight(height)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(10)
        h = QHBoxLayout()
        h.setSpacing(14)
        if lead is not None:
            h.addWidget(lead, 0, Qt.AlignmentFlag.AlignTop if isinstance(lead, StatusDot) else Qt.AlignmentFlag.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(3)
        line = QHBoxLayout()
        line.setSpacing(8)
        # Con chip, el título es corto y el chip va pegado a él; sin chip, el título usa todo el ancho y se ajusta.
        # `chip_right`: el chip va con los controles y el título largo se ajusta de línea.
        inline = chip is not None and not chip_right
        self.title = label(title, "RowTitle", wrap=not inline)
        line.addWidget(self.title, 0 if inline else 1)
        self.chip = chip
        if inline:
            line.addWidget(chip)
            line.addStretch(1)
        texts.addLayout(line)
        self.text = label(text, "RowMono" if mono else "RowText", wrap=not mono)
        if not text:
            # Solo ocultar: setVisible(True) en un widget que todavía no tiene padre lo abre como una ventana aparte
            # (en GNOME, cada una enciende el cursor de «abriendo app» unos segundos).
            self.text.hide()
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        texts.addWidget(self.text)
        h.addLayout(texts, 1)
        self.right = QHBoxLayout()
        self.right.setSpacing(10)
        for widget in ([chip] if chip is not None and chip_right else []) + (right or []):
            self.right.addWidget(widget)
        h.addLayout(self.right)
        outer.addLayout(h)
        self.extra = QVBoxLayout()
        self.extra.setSpacing(10)
        self.extra.setContentsMargins((lead.width() + 14) if lead is not None else 0, 0, 0, 4)
        outer.addLayout(self.extra)


class RowList(QWidget):
    """Filas una debajo de otra, con un separador fino entre ellas (con sangría desde el texto)."""

    def __init__(self, inset: int = 66) -> None:
        super().__init__()
        self.inset = inset
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.rows: list[QWidget] = []

    def add(self, row: QWidget) -> QWidget:
        if self.rows:
            divider = Divider()
            holder = QHBoxLayout()
            holder.setContentsMargins(self.inset, 0, 16, 0)
            holder.addWidget(divider)
            self.box.addLayout(holder)
        self.rows.append(row)
        self.box.addWidget(row)
        return row

    def clear(self) -> None:
        clear(self.box)
        self.rows = []


def heading(text: str, note: str = "", note_widget: QWidget | None = None) -> QHBoxLayout:
    """El rótulo de una sección («Qué se va a configurar») con una nota gris a la derecha."""
    h = QHBoxLayout()
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(label(text, "Heading"))
    h.addStretch(1)
    if note:
        h.addWidget(label(note, "HeadingNote"))
    if note_widget:
        h.addWidget(note_widget)
    return h

