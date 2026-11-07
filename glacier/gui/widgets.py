"""Reusable widgets: flow layout, game cards, banners."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap,
                         QRadialGradient)
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QProgressBar, QPushButton,
                             QSizePolicy, QVBoxLayout, QWidget)

from .. import desktop, paths
from ..library import GameState
from . import theme

_PIXMAPS: dict[tuple[str, int, float], QPixmap] = {}


def icon(name: str, fallback: str = "") -> QIcon:
    ic = QIcon.fromTheme(name)
    if ic.isNull() and fallback:
        ic = QIcon.fromTheme(fallback)
    return ic


def app_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 22, 24, 32, 48, 64, 96, 128, 256, 512):
        f = paths.ASSETS_DIR / f"glacier-{s}.png"
        if f.exists():
            ic.addFile(str(f), QSize(s, s))
    return ic


def game_pixmap(st: GameState, size: int) -> QPixmap:
    png = desktop.icon_png(desktop.icon_name(st.uid))
    key = (str(png) if png else f"ph:{st.uid}", size, png.stat().st_mtime if png else 0.0)
    pm = _PIXMAPS.get(key)
    if pm is None:
        if png:
            pm = QPixmap(str(png)).scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
        else:
            pm = placeholder_pixmap(st.game.initials, st.game.color, size)
        _PIXMAPS[key] = pm
    return pm


def placeholder_pixmap(text: str, color: str, size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QColor(color)
    g = QLinearGradient(0, 0, 0, size)
    g.setColorAt(0, base.lighter(140))
    g.setColorAt(1, base.darker(220))
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.2, size * 0.2)
    p.fillPath(path, g)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * (0.42 if len(text) <= 2 else 0.3 if len(text) == 3 else 0.24)))
    p.setFont(f)
    p.setPen(QColor(255, 255, 255, 235))
    p.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


# Text for icon-only buttons when no icon theme provides the icon (desktops without one).
_GLYPHS = {"view-refresh": "↻", "window-close": "✕"}


def button(text: str, kind: str = "", icon_name: str = "", tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if icon_name:
        ic = icon(icon_name)
        b.setIcon(ic)
        if ic.isNull() and not text:
            b.setText(_GLYPHS.get(icon_name, "…"))
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def label(text: str = "", obj: str = "", muted: bool = False, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if obj:
        lab.setObjectName(obj)
    if muted:
        lab.setProperty("muted", True)
    if wrap:
        lab.setWordWrap(True)
    return lab


class FlowLayout(QLayout):
    """Wrap children left-to-right, like a grid that adapts to width."""

    def __init__(self, parent=None, spacing: int = 16):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() and not it.widget().isVisible() and not test:
                continue
            if it.widget() and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line_h > 0:
                x = r.x()
                y += line_h + self._spacing
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class ArtPanel(QWidget):
    """Top part of a game card: colour wash with the game's icon."""

    def __init__(self, st: GameState, height: int = 132):
        super().__init__()
        self.st = st
        self.setFixedHeight(height)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_state(self, st: GameState):
        self.st = st
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        path = QPainterPath()
        path.addRoundedRect(0, 0, r.width(), r.height() + 12, 11, 11)
        p.setClipPath(path)
        base = QColor(self.st.game.color)
        g = QLinearGradient(0, 0, r.width(), r.height())
        g.setColorAt(0, base.darker(170))
        g.setColorAt(1, QColor(theme.SURFACE))
        p.fillRect(r, g)
        glow = QRadialGradient(r.center().x(), r.center().y(), r.width() * 0.45)
        c = base.lighter(130)
        c.setAlpha(110 if self.st.installed else 50)
        glow.setColorAt(0, c)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.fillRect(r, glow)
        size = 84
        pm = game_pixmap(self.st, size)
        if not self.st.installed:
            p.setOpacity(0.55)
        p.drawPixmap(int((r.width() - size) / 2), int((r.height() - size) / 2), pm)
        p.setOpacity(1)
        if self.st.running:
            p.setBrush(QColor(theme.OK))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(r.width() - 22, 12, 10, 10)
        p.end()


class GameCard(QFrame):
    clicked = pyqtSignal(str)
    action = pyqtSignal(str, str)      # uid, action

    W, H = 196, 262

    def __init__(self, st: GameState):
        super().__init__()
        self.setObjectName("Card")
        self.setFixedSize(self.W, self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.st = st
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 12)
        lay.setSpacing(6)
        self.art = ArtPanel(st)
        lay.addWidget(self.art)
        inner = QVBoxLayout()
        inner.setContentsMargins(12, 2, 12, 0)
        inner.setSpacing(4)
        self.name = QLabel()
        self.name.setWordWrap(True)
        f = self.name.font()
        f.setBold(True)
        f.setPointSizeF(10.5)
        self.name.setFont(f)
        self.name.setFixedHeight(40)
        self.name.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.btn = QPushButton()
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._on_button)
        inner.addWidget(self.name)
        inner.addWidget(self.status)
        inner.addWidget(self.progress)
        inner.addStretch(1)
        inner.addWidget(self.btn)
        lay.addLayout(inner)
        self.set_state(st)

    def set_selected(self, sel: bool):
        self.setProperty("selected", sel)
        repolish(self)

    def set_state(self, st: GameState):
        self.st = st
        self.art.set_state(st)
        self.name.setText(st.game.name)
        text = st.status_text
        if st.installed and st.version and text == "Ready":
            text = f"v{st.version}"
        color = theme.STATUS_COLORS.get(st.status_text, theme.ACCENT)
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; background: transparent;")
        if st.progress is not None:
            self.progress.setVisible(True)
            self.progress.setValue(int(st.progress * 1000))
        else:
            self.progress.setVisible(False)
        if st.running:
            kind, txt = "running", "Running"
        elif st.progress is not None:
            kind, txt = "", "In Battle.net"
        elif st.needs_locate:
            kind, txt = "", "Add to Battle.net"
        elif st.installed:
            kind, txt = "primary", "Play"
        else:
            kind, txt = "", "Install"
        self.btn.setText(txt)
        self.btn.setProperty("kind", kind)
        repolish(self.btn)

    def _on_button(self):
        st = self.st
        if st.running:
            self.action.emit(st.uid, "stop")
        elif st.progress is not None:
            self.action.emit(st.uid, "open")
        elif st.installed:
            self.action.emit(st.uid, "play")
        else:
            self.action.emit(st.uid, "install")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.st.uid)
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self.st.installed and not self.st.running:
            self.action.emit(self.st.uid, "play")


class Banner(QFrame):
    """Wide notice with optional progress and action button."""

    def __init__(self):
        super().__init__()
        self.setObjectName("Banner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(16)
        self.icon = QLabel()
        self.icon.setFixedSize(40, 40)
        lay.addWidget(self.icon)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.title = QLabel()
        f = self.title.font()
        f.setBold(True)
        f.setPointSizeF(11)
        self.title.setFont(f)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setProperty("muted", True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)   # commands to copy
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        col.addWidget(self.title)
        col.addWidget(self.text)
        col.addWidget(self.progress)
        lay.addLayout(col, 1)
        self.btn = QPushButton()
        self.btn.setProperty("kind", "primary")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.btn)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def show_message(self, title: str, text: str, button_text: str = "", fraction: float | None = None,
                     pixmap: QPixmap | None = None):
        self.title.setText(title)
        self.text.setText(text)
        self.text.setVisible(bool(text))
        self.btn.setText(button_text)
        self.btn.setVisible(bool(button_text))
        if fraction is None:
            self.progress.setVisible(False)
        else:
            self.progress.setVisible(True)
            if fraction < 0:
                self.progress.setRange(0, 0)
            else:
                self.progress.setRange(0, 1000)
                self.progress.setValue(int(fraction * 1000))
        if pixmap is not None:
            self.icon.setPixmap(pixmap.scaled(40, 40, Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))
        self.icon.setVisible(pixmap is not None)
        self.setVisible(True)


def pixmap_from_file(path: Path | None, size: int) -> QPixmap | None:
    if not path or not Path(path).exists():
        return None
    return QPixmap(str(path)).scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
