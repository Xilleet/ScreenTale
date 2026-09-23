"""Окно перевода: стеклянная плашка (Aero blur), drag, ресайз, история."""
import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QPoint, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from frontend.widgets import StatusPill

SHADOW_MARGIN = 10
MIN_W = 100
MIN_H = 60
MAX_HISTORY_LINES = 60
HISTORY_TRIM_INTERVAL_MS = 150_000
NOTICE_TOAST_MS = 2000
STATUS_AUTO_RESET_OK_MS = 1500
STATUS_AUTO_RESET_ERR_MS = 3000


# ============================================================
# Плавающий мини-тулбар (Ghost Toolbar)
# ============================================================
class _FloatingToolbar(QFrame):
    retry_clicked = Signal()
    stop_clicked = Signal()
    clear_clicked = Signal()
    ghost_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FloatingToolbar")
        self.setStyleSheet("""
            QFrame#FloatingToolbar {
                background: rgba(26, 24, 22, 230);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 7px;
            }
            QPushButton#ToolbarBtn {
                background: transparent;
                border: none;
                border-radius: 4px;
                color: #9c9388;
                font-size: 13px;
                min-width: 24px;
                max-width: 24px;
                min-height: 22px;
                max-height: 22px;
                padding: 0;
            }
            QPushButton#ToolbarBtn:hover {
                background: rgba(224, 142, 69, 0.25);
                color: #f2ede4;
            }
            QPushButton#ToolbarBtn[active="true"] {
                background: #e08e45;
                color: #1a1816;
            }
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)

        self.btn_retry = QPushButton("↻")
        self.btn_retry.setObjectName("ToolbarBtn")
        self.btn_retry.setToolTip("Повторить распознавание (Alt+R)")
        self.btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_retry.clicked.connect(self.retry_clicked.emit)

        self.btn_stop = QPushButton("■")
        self.btn_stop.setObjectName("ToolbarBtn")
        self.btn_stop.setToolTip("Остановить перевод (Alt+C)")
        self.btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_stop.clicked.connect(self.stop_clicked.emit)

        self.btn_clear = QPushButton("🗑")
        self.btn_clear.setObjectName("ToolbarBtn")
        self.btn_clear.setToolTip("Очистить историю (Alt+X)")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear_clicked.emit)

        self.btn_ghost = QPushButton("👻")
        self.btn_ghost.setObjectName("ToolbarBtn")
        self.btn_ghost.setToolTip("Сквозной клик (Alt+G)")
        self.btn_ghost.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ghost.clicked.connect(self.ghost_clicked.emit)

        lay.addWidget(self.btn_retry)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.btn_clear)
        lay.addWidget(self.btn_ghost)

    def set_ghost_active(self, active: bool):
        self.btn_ghost.setProperty("active", bool(active))
        self.btn_ghost.style().unpolish(self.btn_ghost)
        self.btn_ghost.style().polish(self.btn_ghost)


class ResizeGrip(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._start_global_pos = None
        self._start_geo = None

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 90))
        for c in (15, 18, 21):
            for x in range(3, 16, 3):
                y = c - x
                if 1 <= y <= 15:
                    painter.drawEllipse(x - 1, y - 1, 2, 2)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_global_pos = event.globalPosition().toPoint()
            self._start_geo = self.parent_window.geometry()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.parent_window.hide_manual()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._start_global_pos and (event.buttons() & Qt.MouseButton.LeftButton):
            delta = event.globalPosition().toPoint() - self._start_global_pos
            self.parent_window.resize(
                max(MIN_W, self._start_geo.width() + delta.x()),
                max(MIN_H, self._start_geo.height() + delta.y()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._start_global_pos = None
        self._start_geo = None


class CustomTextEdit(QTextEdit):
    def __init__(self, parent_window):
        super().__init__()
        self.parent_window = parent_window
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setMinimumSize(0, 0)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.parent_window.hide_manual()
            event.accept()
        else:
            self.parent_window.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.parent_window.mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.parent_window.mouseReleaseEvent(event)


class _NoticeToast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Ставим стиль под янтарную тему и аккуратную высоту
        self.setStyleSheet(
            "QLabel { background: rgba(26, 24, 22, 230); color: #f2ede4; "
            "border: 1px solid rgba(255, 255, 255, 0.16); border-radius: 7px; "
            "padding: 3px 10px; font-size: 12px; }")
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

    def show_notice(self, text, ms=NOTICE_TOAST_MS):
        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        if parent:
            # Ограничиваем ширину, чтобы не наезжать на тулбар справа
            max_w = parent.width() - 2 * SHADOW_MARGIN - 130
            if self.width() > max_w:
                self.setFixedWidth(max_w)
                self.setWordWrap(True)
                self.adjustSize()
            # Сажаем тост ровно в верхнюю парящую зону вровень с тулбаром
            self.move((parent.width() - self.width()) // 2, SHADOW_MARGIN)
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._hide_timer.start(ms)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_anim_done(self):
        if self._effect.opacity() < 0.01:
            self.hide()


class TranslateWindow(QWidget):
    retry_requested = Signal()
    stop_requested = Signal()
    clear_requested = Signal()

    def __init__(self, settings):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(MIN_W, MIN_H)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._move_pos = None
        self.force_hidden = False
        self._forbidden_rect = None
        self._ghost_mode = False

        # добавляем 22px сверху под плавающее ушко:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN + 28, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.border_frame = QFrame()
        self.border_frame.setObjectName("GlassFrame")
        self.border_frame.setMinimumSize(0, 0)
        self.border_frame.setStyleSheet("""
            QFrame#GlassFrame {
                background-color: rgba(22, 22, 26, 0.72);
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 14px;
            }
        """)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setColor(QColor(0, 0, 0, 160))
        shadow.setOffset(0, 4)
        self.border_frame.setGraphicsEffect(shadow)
        outer.addWidget(self.border_frame)

        frame_layout = QVBoxLayout(self.border_frame)
        frame_layout.setContentsMargins(16, 14, 16, 14)
        frame_layout.setSpacing(0)

        self.text_widget = CustomTextEdit(self)
        self.text_widget.setStyleSheet("""
            QTextEdit { background-color: transparent; color: #ffffff; border: none; }
            QScrollBar:vertical { border: none; background: transparent; width: 4px; margin: 0px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); min-height: 20px; border-radius: 2px; }
            QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.5); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """)
        frame_layout.addWidget(self.text_widget)

        self.grip = ResizeGrip(self)
        self._notice_toast = _NoticeToast(self)

        self.status_pill = StatusPill(self)
        self.status_pill.set_state("off", "Ожидание")

        self._status_reset_timer = QTimer(self)
        self._status_reset_timer.setSingleShot(True)
        self._status_reset_timer.timeout.connect(self._reset_status_to_off)

        self._trim_timer = QTimer(self)
        self._trim_timer.timeout.connect(self._cleanup_history)
        self._trim_timer.start(HISTORY_TRIM_INTERVAL_MS)

        # Плавающий тулбар
        self.toolbar = _FloatingToolbar(self)
        self.toolbar.retry_clicked.connect(self.retry_requested.emit)
        self.toolbar.stop_clicked.connect(self.stop_requested.emit)
        self.toolbar.clear_clicked.connect(self.clear_requested.emit)
        self.toolbar.ghost_clicked.connect(self.toggle_ghost_mode)

        self._toolbar_effect = QGraphicsOpacityEffect(self.toolbar)
        self._toolbar_effect.setOpacity(0.0)
        self.toolbar.setGraphicsEffect(self._toolbar_effect)

        self._toolbar_anim = QPropertyAnimation(self._toolbar_effect, b"opacity", self)
        self._toolbar_anim.setDuration(150)

        self._toolbar_hide_timer = QTimer(self)
        self._toolbar_hide_timer.setSingleShot(True)
        self._toolbar_hide_timer.setInterval(400)
        self._toolbar_hide_timer.timeout.connect(self._fade_out_toolbar)

        # Настройки размера и шрифта
        settings.changed.connect(self._on_setting_changed)
        self.update_font_size(int(settings.get("font_size", 14)))
        self.update_opacity(float(settings.get("opacity", 0.95)))

        # ВАЖНО: задаем размер в самом конце, когда ВСЕ виджеты уже созданы
        self.resize(420, 120)
        self._reposition_overlays()

    # ---------- позиционирование плавающих элементов ----------
    def _reposition_overlays(self):
        offset = SHADOW_MARGIN + 3
        # 1. Grip в нижнем правом углу
        self.grip.move(self.width() - self.grip.width() - offset,
                       self.height() - self.grip.height() - offset)
        self.grip.raise_()

        # 2. StatusPill в нижнем левом углу
        pill_h = self.status_pill.sizeHint().height()
        self.status_pill.move(SHADOW_MARGIN + 6,
                              self.height() - pill_h - SHADOW_MARGIN - 4)
        self.status_pill.raise_()

        # 3. FloatingToolbar в верхнем правом углу
        self.toolbar.adjustSize()
        tb_w = self.toolbar.width()
        self.toolbar.move(
            self.width() - tb_w - SHADOW_MARGIN - 6,
            SHADOW_MARGIN,
        )
        self.toolbar.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()

    # ---------- настройки ----------
    def _on_setting_changed(self, key, value):
        if key == "font_size":
            self.update_font_size(int(value))
        elif key == "opacity":
            self.update_opacity(float(value))

    def update_font_size(self, size):
        size = int(size)
        self.text_widget.setStyleSheet(
            f"""
            QTextEdit {{
                background-color: transparent;
                color: #ffffff;
                border: none;
                font-size: {size}px;
            }}
            QScrollBar:vertical {{ border: none; background: transparent; width: 4px; margin: 0px; }}
            QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.25); min-height: 20px; border-radius: 2px; }}
            QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.5); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            """
        )
        new_font = QFont("Segoe UI", size)
        self.text_widget.setFont(new_font)
        doc = self.text_widget.document()
        doc.setDefaultFont(new_font)
        cursor = QTextCursor(doc)
        cursor.select(QTextCursor.SelectionType.Document)
        fmt = cursor.charFormat()
        fmt.setFont(new_font)
        cursor.mergeCharFormat(fmt)

    def update_opacity(self, value):
        self.setWindowOpacity(float(value))

    # ---------- показ/скрытие ----------
    def hide_manual(self):
        self.force_hidden = True
        self.hide()

    def toggle_visible(self):
        if self.isVisible():
            self.hide_manual()
        else:
            self.force_hidden = False
            self.show()
            self.raise_()
            self.activateWindow()

    # ---------- сквозной клик (Ghost mode) ----------
    def toggle_ghost_mode(self) -> bool:
        self._ghost_mode = not self._ghost_mode
        self.toolbar.set_ghost_active(self._ghost_mode)

        if self._ghost_mode:
            self._toolbar_hide_timer.stop()
            self._toolbar_anim.stop()
            self._toolbar_effect.setOpacity(1.0)
        else:
            self._toolbar_hide_timer.start(500)

        status_txt = "ВКЛ (клики сквозь окно)" if self._ghost_mode else "ВЫКЛ"
        self.show_translation(f"[Сквозной клик: {status_txt}]")
        return self._ghost_mode

    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG" and getattr(self, "_ghost_mode", False) and sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                x = ctypes.c_short(msg.lParam & 0xFFFF).value
                y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                local_pt = self.mapFromGlobal(QPoint(x, y))
                if self.toolbar.geometry().contains(local_pt):
                    return True, 1  # HTCLIENT (тулбар кликабелен)
                return True, -1     # HTTRANSPARENT (текст прозрачен для мыши)
        return super().nativeEvent(eventType, message)

    # ---------- тулбар при наведении ----------
    def enterEvent(self, event):
        super().enterEvent(event)
        if not self._ghost_mode:
            self._toolbar_hide_timer.stop()
            self._toolbar_anim.stop()
            self._toolbar_anim.setStartValue(self._toolbar_effect.opacity())
            self._toolbar_anim.setEndValue(1.0)
            self._toolbar_anim.start()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        if not self._ghost_mode:
            self._toolbar_hide_timer.start(800)

    def _fade_out_toolbar(self):
        if not self._ghost_mode:
            # Защита: если курсор физически всё ещё находится в пределах окна/кнопок — не гасим!
            if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                return
            self._toolbar_anim.stop()
            self._toolbar_anim.setStartValue(self._toolbar_effect.opacity())
            self._toolbar_anim.setEndValue(0.0)
            self._toolbar_anim.start()

    # ---------- запретная зона для drag'а ----------
    def set_forbidden_rect(self, rect):
        self._forbidden_rect = rect

    # ---------- drag ----------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._move_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self.hide_manual()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._move_pos and (event.buttons() & Qt.MouseButton.LeftButton):
            new_pos = event.globalPosition().toPoint() - self._move_pos
            if self._forbidden_rect is not None:
                new_x, new_y = new_pos.x(), new_pos.y()
                new_rect = QRect(new_x, new_y, self.width(), self.height())
                if new_rect.intersects(self._forbidden_rect):
                    fr = self._forbidden_rect
                    shift_left = fr.left() - self.width() - 1
                    shift_right = fr.right() + 1
                    shift_top = fr.top() - self.height() - 1
                    shift_bottom = fr.bottom() + 1
                    cands = [
                        (shift_left, new_y, abs(new_x - shift_left)),
                        (shift_right, new_y, abs(new_x - shift_right)),
                        (new_x, shift_top, abs(new_y - shift_top)),
                        (new_x, shift_bottom, abs(new_y - shift_bottom)),
                    ]
                    valid_cands = [c for c in cands if not QRect(c[0], c[1], self.width(), self.height()).intersects(self._forbidden_rect)]
                    if valid_cands:
                        best_x, best_y, _ = min(valid_cands, key=lambda c: c[2])
                        new_pos.setX(best_x)
                        new_pos.setY(best_y)
                    else:
                        return
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._move_pos = None

    # ---------- контент ----------
    def show_translation(self, text, x=None, y=None):
        if text.startswith("["):
            self._notice_toast.show_notice(text)
            if x is not None and y is not None:
                self.move(x + 20 - SHADOW_MARGIN, y + 20 - SHADOW_MARGIN)
            if not self.isVisible() and not self.force_hidden:
                self.show()
            return
        cursor = QTextCursor(self.text_widget.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.text_widget.toPlainText().strip():
            cursor.insertText("\n\n---\n\n")
        cursor.insertText(text)
        self.text_widget.setTextCursor(cursor)
        self.text_widget.ensureCursorVisible()

        if x is not None and y is not None:
            self.move(x + 20 - SHADOW_MARGIN, y + 20 - SHADOW_MARGIN)

        if not self.isVisible() and not self.force_hidden:
            self.show()

    def clear_history(self):
        self.text_widget.clear()

    def _cleanup_history(self):
        try:
            doc = self.text_widget.document()
            excess = doc.blockCount() - MAX_HISTORY_LINES
            if excess > 0:
                cursor = QTextCursor(doc)
                cursor.movePosition(QTextCursor.MoveOperation.Start)
                for _ in range(excess):
                    cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                    cursor.removeSelectedText()
                    cursor.deleteChar()
        except Exception as _e:
            print(f"[warn] _cleanup_history: {_e}")

    # ---------- статус ----------
    def set_status(self, state, text):
        self._status_reset_timer.stop()
        self.status_pill.set_state(state, text)
        if state == "ok":
            self._status_reset_timer.start(STATUS_AUTO_RESET_OK_MS)
        elif state == "error":
            self._status_reset_timer.start(STATUS_AUTO_RESET_ERR_MS)

    def _reset_status_to_off(self):
        self.status_pill.set_state("off", "Ожидание")