import math
import time

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from backend.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN
from frontend.theme import PALETTE


# ============================================================
# ToggleSwitch — анимированный тумблер в духе Windows 11
# ============================================================
class ToggleSwitch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"handlePos", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def _get_pos(self):
        return self._pos

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    handlePos = Property(float, _get_pos, _set_pos)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(PALETTE["stroke"]), 1))
        p.setBrush(QColor(PALETTE["accent"]) if self.isChecked() else QColor(PALETTE["bg_hover"]))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 11, 11)
        d = self.height() - 8
        x = 4 + self._pos * (self.width() - d - 8)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, 4, d, d))


# ============================================================
# SegmentedControl — капсула с взаимоисключающими кнопками
# ============================================================
class SegmentedControl(QFrame):
    valueChanged = Signal(str)

    def __init__(self, items, parent=None):
        # items: [(id, title), ...]
        super().__init__(parent)
        self.setObjectName("Segmented")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._buttons = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for ident, title in items:
            b = QPushButton(title)
            b.setObjectName("SegBtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, i=ident: self.valueChanged.emit(i))
            self._group.addButton(b)
            lay.addWidget(b, 1)
            self._buttons[ident] = b
        self._buttons[items[0][0]].setChecked(True)

    def set_value(self, ident):
        b = self._buttons.get(ident)
        if b and not b.isChecked():
            b.setChecked(True)
        # На Windows setChecked(True) на невидимом/неактивном виджете
        # иногда не триггерит repaint вовремя — кнопка остаётся визуально
        # в прежнем состоянии. Принудительно обновляем все кнопки группы,
        # чтобы :checked QSS-правило гарантированно перерисовалось.
        # Это no-op, если виджет уже корректно перерисован.
        for other in self._buttons.values():
            other.update()

    def value(self):
        for ident, b in self._buttons.items():
            if b.isChecked():
                return ident
        return None


# ============================================================
# HotkeyRecorder — кнопка-записыватель сочетания
# ============================================================
_MOD_QT_KEYS = {
    Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt,
    Qt.Key.Key_Meta, Qt.Key.Key_AltGr, Qt.Key.Key_CapsLock,
}

_VK_NAMES = {0xC0: "`", 0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]",
             0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/", 0xDC: "\\"}


def _vk_name(vk: int) -> str:
    """Имя клавиши по VK-коду — независимо от раскладки."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return _VK_NAMES.get(vk, f"Key 0x{vk:02X}")


def _mods_label(mods: int) -> str:
    parts = []
    if mods & MOD_CONTROL: parts.append("Ctrl")
    if mods & MOD_ALT: parts.append("Alt")
    if mods & MOD_SHIFT: parts.append("Shift")
    if mods & MOD_WIN: parts.append("Win")
    return "+".join(parts)


def _qt_mods_to_native(qt_mods) -> int:
    n = 0
    if qt_mods & Qt.KeyboardModifier.ControlModifier: n |= MOD_CONTROL
    if qt_mods & Qt.KeyboardModifier.AltModifier: n |= MOD_ALT
    if qt_mods & Qt.KeyboardModifier.ShiftModifier: n |= MOD_SHIFT
    if qt_mods & Qt.KeyboardModifier.MetaModifier: n |= MOD_WIN
    return n


class HotkeyRecorder(QPushButton):
    """Клик — режим записи; Esc — отмена; фокус потерян — отмена.
    sequenceCaptured(label, native_mods, native_vk) — label для показа, vk для RegisterHotKey."""

    sequenceCaptured = Signal(str, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Recorder")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._listening = False
        self._display = ""
        self._err_timer = QTimer(self)
        self._err_timer.setSingleShot(True)
        self._err_timer.timeout.connect(self._restore_text)
        self.clicked.connect(self._start_listening)

    def set_display(self, label):
        self._display = label
        if not self._listening:
            self._set_text(label)

    def _set_text(self, text):
        self.setProperty("error", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(text)

    def show_error(self, message, ms=2200):
        self.setProperty("error", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(message)
        self._err_timer.start(ms)

    def _restore_text(self):
        self._set_text(self._display)

    def _start_listening(self):
        if self._listening:
            return
        self._listening = True
        self.setChecked(True)
        self.setText("Нажмите сочетание…")
        self.setFocus(Qt.FocusReason.PopupFocusReason)

    def _stop_listening(self, cancel=False):
        self._listening = False
        self.setChecked(False)
        self._set_text(self._display)
        if cancel:
            self.clearFocus()

    def keyPressEvent(self, e):
        if not self._listening:
            super().keyPressEvent(e)
            return
        e.accept()
        if e.isAutoRepeat():
            return
        key = e.key()
        mods = e.modifiers()
        if key == Qt.Key.Key_Escape:
            self._stop_listening(cancel=True)
            return
        if key in _MOD_QT_KEYS or key == Qt.Key.Key_unknown:
            label = _mods_label(_qt_mods_to_native(mods))
            self.setText(f"{label} + …" if label else "Нажмите сочетание…")
            return
        vk = int(e.nativeVirtualKey())
        nmods = _qt_mods_to_native(mods)
        has_real_mod = bool(nmods & (MOD_CONTROL | MOD_ALT | MOD_WIN))
        single_ok = 0x70 <= vk <= 0x87 or vk == 0xC0  # F1..F24 или `
        if not vk or (not has_real_mod and not single_ok):
            self.show_error("Добавьте Ctrl или Alt")
            return
        name = _vk_name(vk)
        label = f"{_mods_label(nmods)} + {name}" if _mods_label(nmods) else name
        self._stop_listening()
        self.set_display(label)
        self.sequenceCaptured.emit(label, nmods, vk)

    def focusOutEvent(self, e):
        if self._listening:
            self._stop_listening(cancel=True)
        super().focusOutEvent(e)


# ============================================================
# StatusPill — цветной кружок-статус + текст
# ============================================================
class StatusPill(QWidget):

    def _color_for(self, state):
        if state == "ok":    return QColor(PALETTE["green"])
        if state == "busy":  return QColor(PALETTE["yellow"])
        if state == "error": return QColor(PALETTE["red"])
        return QColor(PALETTE["text_dim"])
        
    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "off"
        self._text = ""
        self._pulse = 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def set_state(self, state, text):
        self._state = state
        self._text = text
        if state == "busy":
            self._timer.start()
        else:
            self._timer.stop()
            self._pulse = 1.0
        self.updateGeometry()
        self.update()

    def _tick(self):
        self._pulse = 0.5 + 0.5 * abs(math.sin(time.time() * 4.0))
        self.update()

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._text) + 26, 22)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self._color_for(self._state)
        if self._state == "busy":
            c.setAlphaF(0.35 + 0.65 * self._pulse)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(3, 7, 8, 8)
        p.setPen(QColor(PALETTE["text_dim"]))
        p.drawText(QRect(18, 0, self.width() - 18, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)


# ============================================================
# BusyBar — полоса прогресса: indeterminate или 0..100
# ============================================================
class BusyBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self._value = -1
        self._pos = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._advance)

    def start_indeterminate(self):
        self._value = -1
        self._timer.start()
        self.show()

    def set_value(self, percent):
        self._timer.stop()
        self._value = max(0, min(100, int(percent)))
        if self._value >= 100:
            self._timer.stop()
        self.update()

    def _advance(self):
        self._pos = (self._pos + 0.012) % 1.0
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(PALETTE["bg_hover"]))
        p.drawRoundedRect(self.rect(), 2, 2)
        p.setBrush(QColor(PALETTE["accent"]))
        w = self.width()
        if self._value < 0:
            bar = w * 0.28
            x = -bar + self._pos * (w + 2 * bar)
            p.drawRoundedRect(QRectF(x, 0, bar, self.height()), 2, 2)
        else:
            p.drawRoundedRect(QRectF(0, 0, w * self._value / 100.0, self.height()), 2, 2)


# ============================================================
# Toast — всплывающее уведомление внутри окна
# ============================================================
class Toast(QLabel):
    def __init__(self, parent, left_offset=0):
        super().__init__(parent)
        self.setObjectName("Toast")
        self._left_offset = int(left_offset)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.finished.connect(self._on_anim_done)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

    def show_toast(self, text, ms=1800):
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._hide_timer.start(ms)

    def reposition(self):
        parent = self.parentWidget()
        if parent:
            avail_w = parent.width() - self._left_offset
            x = self._left_offset + (avail_w - self.width()) // 2
            
            # Базовый отступ снизу
            y = parent.height() - self.height() - 22
            
            # Если в SettingsWindow открыт баннер обновления — поднимаем тост выше баннера
            if hasattr(parent, "update_banner") and parent.update_banner.isVisible():
                banner_h = parent.update_banner.height()
                y = parent.height() - self.height() - banner_h - 32
                
            self.move(x, y)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_done(self):
        if self._effect.opacity() < 0.01:
            self.hide()