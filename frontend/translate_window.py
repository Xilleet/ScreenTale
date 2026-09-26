"""Окно перевода и независимый парящий мини-тулбар управления."""
import sys
from ctypes import wintypes

from PySide6.QtCore import QPoint, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QTextCursor
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
TOOLBAR_GAP = 5  # аккуратный зазор между тулбаром и окном перевода
MIN_W = 100
MIN_H = 60
MAX_HISTORY_LINES = 60
HISTORY_TRIM_INTERVAL_MS = 150_000
NOTICE_TOAST_MS = 2000
STATUS_AUTO_RESET_OK_MS = 1500
STATUS_AUTO_RESET_ERR_MS = 3000


# ============================================================
# Отдельная кнопка-ручка перетаскивания (Grip Button)
# ============================================================
class _GripButton(QPushButton):
    def __init__(self, toolbar):
        super().__init__("⋮⋮")
        self.toolbar = toolbar
        self.setObjectName("ToolbarGripBtn")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Перетащить тулбар (клик — открепить / прикрепить)")
        self._drag_start = None
        self._offset = None
        self._is_dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.globalPosition().toPoint()
            self._offset = event.globalPosition().toPoint() - self.toolbar.pos()
            self._is_dragging = False
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start and (event.buttons() & Qt.MouseButton.LeftButton):
            delta = (event.globalPosition().toPoint() - self._drag_start).manhattanLength()
            if delta > 3:
                self._is_dragging = True
                self.toolbar.undock()
                raw_pos = event.globalPosition().toPoint() - self._offset
                # ЖЕЛЕЗНЫЙ ОГРАНИЧИТЕЛЬ: не выпускаем за пределы экрана/мониторов
                safe_pos = self.toolbar.clamp_to_screen(raw_pos)
                self.toolbar.move(safe_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._is_dragging:
                # Одиночный клик: переключаем dock / undock
                self.toolbar.toggle_dock()
            else:
                # Завершение перетаскивания: умный 4-сторонний магнит
                self.toolbar.check_magnetic_dock()
            self._drag_start = None
            self._offset = None
            self._is_dragging = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)


# ============================================================
# Автономный парящий мини-тулбар (Floating Mini-HUD)
# ============================================================
class FloatingToolbar(QFrame):
    retry_clicked = Signal()
    pause_clicked = Signal()
    stop_clicked = Signal()
    clear_clicked = Signal()
    ghost_clicked = Signal()
    dock_changed = Signal(bool)

    def __init__(self, target_window=None):
        super().__init__(None)
        self.target_window = target_window
        self._is_docked = True
        self._dock_anchor = "top_right"  # top_right | top_left | bottom_right | bottom_left

        self.setObjectName("FloatingToolbar")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # ОБЯЗАТЕЛЬНО: заставляет Qt гарантированно рисовать тёмную подложку:
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self.setStyleSheet("""
            QFrame#FloatingToolbar {
                background-color: #1e1b18;
                border: 1px solid rgba(255, 255, 255, 0.22);
                border-radius: 9px;
            }
            QPushButton#ToolbarBtn {
                background: transparent;
                border: none;
                border-radius: 5px;
                color: #9c9388;
                font-size: 14px;
                min-width: 28px;
                max-width: 28px;
                min-height: 26px;
                max-height: 26px;
                padding: 0;
            }
            QPushButton#ToolbarBtn:hover {
                background: rgba(224, 142, 69, 0.30);
                color: #f2ede4;
            }
            QPushButton#ToolbarBtn[active="true"] {
                background: #e08e45;
                color: #1a1816;
            }
            QPushButton#ToolbarGripBtn {
                background: rgba(255, 255, 255, 0.08);
                border: 1px solid rgba(255, 255, 255, 0.15);
                border-radius: 5px;
                color: #b5aba0;
                font-size: 14px;
                font-weight: bold;
                min-width: 28px;
                max-width: 28px;
                min-height: 26px;
                max-height: 26px;
                padding: 0;
            }
            QPushButton#ToolbarGripBtn:hover {
                background: rgba(224, 142, 69, 0.40);
                border-color: #e08e45;
                color: #f2ede4;
            }
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(4)

        self.btn_retry = QPushButton("↻")
        self.btn_retry.setObjectName("ToolbarBtn")
        self.btn_retry.setToolTip("Повторить распознавание (Alt+R)")
        self.btn_retry.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_retry.clicked.connect(self.retry_clicked.emit)

        self.btn_pause = QPushButton("⏸")
        self.btn_pause.setObjectName("ToolbarBtn")
        self.btn_pause.setToolTip("Пауза авто-перевода (Alt+P)")
        self.btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pause.clicked.connect(self.pause_clicked.emit)

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

        self.btn_grip = _GripButton(self)

        lay.addWidget(self.btn_retry)
        lay.addWidget(self.btn_pause)
        lay.addWidget(self.btn_stop)
        lay.addWidget(self.btn_clear)
        lay.addWidget(self.btn_ghost)
        lay.addWidget(self.btn_grip)

    def clamp_to_screen(self, pos: QPoint) -> QPoint:
        """Не даёт тулбару улететь за границы монитора(-ов), поддерживая multi-monitor setup."""
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if not screen:
            return pos

        # virtualGeometry покрывает все 1, 2 или 3 подключенных монитора:
        v_geo = screen.virtualGeometry()
        w = self.width()
        h = self.height()

        # Ограничиваем X и Y так, чтобы вся плашка оставалась видна:
        clamped_x = max(v_geo.left(), min(pos.x(), v_geo.right() - w + 1))
        clamped_y = max(v_geo.top(), min(pos.y(), v_geo.bottom() - h + 1))

        return QPoint(clamped_x, clamped_y)

    def set_pause_active(self, paused: bool):
        self.btn_pause.setText("▶" if paused else "⏸")
        self.btn_pause.setToolTip("Продолжить авто-перевод (Alt+P)" if paused else "Пауза авто-перевода (Alt+P)")
        self.btn_pause.setProperty("active", bool(paused))
        self.btn_pause.style().unpolish(self.btn_pause)
        self.btn_pause.style().polish(self.btn_pause)

    def set_ghost_active(self, active: bool):
        self.btn_ghost.setProperty("active", bool(active))
        self.btn_ghost.style().unpolish(self.btn_ghost)
        self.btn_ghost.style().polish(self.btn_ghost)

    # ---------- ФИЗИКА КОЛЛИЗИЙ И МАГНИТНОГО ДОКИНГА ----------
    def resolve_collision(self, raw_pos: QPoint) -> QPoint:
        """Не даёт тулбару заехать внутрь карточки с текстом (выталкивает наружу)."""
        if not self.target_window or not self.target_window.isVisible():
            return raw_pos

        card_rect = self.target_window.get_card_screen_rect()
        tb_rect = QRect(raw_pos, self.size())

        if tb_rect.intersects(card_rect):
            # Толкаем к ближайшей внешней грани карточки:
            shift_left = card_rect.left() - self.width() - TOOLBAR_GAP
            shift_right = card_rect.right() + TOOLBAR_GAP
            shift_top = card_rect.top() - self.height() - TOOLBAR_GAP
            shift_bottom = card_rect.bottom() + TOOLBAR_GAP

            cands = [
                (shift_left, raw_pos.y(), abs(raw_pos.x() - shift_left)),
                (shift_right, raw_pos.y(), abs(raw_pos.x() - shift_right)),
                (raw_pos.x(), shift_top, abs(raw_pos.y() - shift_top)),
                (raw_pos.x(), shift_bottom, abs(raw_pos.y() - shift_bottom)),
            ]
            best_x, best_y, _ = min(cands, key=lambda c: c[2])
            return QPoint(best_x, best_y)

        return raw_pos

    def _get_dock_anchor_pos(self, anchor: str) -> QPoint:
        if not self.target_window:
            return self.pos()

        card = self.target_window.get_card_screen_rect()
        self.adjustSize()
        w = self.width()
        h = self.height()

        if anchor == "top_left":
            raw = QPoint(card.left(), card.top() - h - TOOLBAR_GAP)
        elif anchor == "bottom_left":
            raw = QPoint(card.left(), card.bottom() + TOOLBAR_GAP)
        elif anchor == "bottom_right":
            raw = QPoint(card.right() - w + 1, card.bottom() + TOOLBAR_GAP)
        else:  # top_right
            raw = QPoint(card.right() - w + 1, card.top() - h - TOOLBAR_GAP)

        # Если окно перевода у самого края монитора — тулбар останется в поле зрения:
        return self.clamp_to_screen(raw)

    def align_to_window(self, parent_geo=None):
        if self._is_docked and self.target_window:
            target_pos = self._get_dock_anchor_pos(self._dock_anchor)
            self.move(target_pos)
            self.raise_()

    def undock(self):
        if self._is_docked:
            self._is_docked = False
            self.dock_changed.emit(False)

    def toggle_dock(self):
        if self._is_docked:
            self._is_docked = False
            self.move(self.x() - 15, max(10, self.y() - 20))
            self.dock_changed.emit(False)
        else:
            self.dock_to_window(self._dock_anchor)

    def dock_to_window(self, anchor: str = "top_right"):
        if self.target_window:
            self._is_docked = True
            self._dock_anchor = anchor
            self.align_to_window()
            self.dock_changed.emit(True)

    def check_magnetic_dock(self):
        """Проверяет примагничивание к любому из 4 внешних углов окна перевода."""
        if not self.target_window or not self.target_window.isVisible():
            return

        anchors = ["top_right", "bottom_right", "top_left", "bottom_left"]
        best_anchor = None
        min_dist = 999999

        for a in anchors:
            pos = self._get_dock_anchor_pos(a)
            dist = (self.pos() - pos).manhattanLength()
            if dist < min_dist:
                min_dist = dist
                best_anchor = a

        # Если тулбар отпустили ближе 38 px к любому из 4 углов — магнитим!
        if min_dist < 38 and best_anchor:
            self.dock_to_window(best_anchor)


# ============================================================
# Виджеты изменения размера и текста
# ============================================================
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
                max(MIN_H, self._start_geo.height() + delta.y()),
            )
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
        self.setCursor(Qt.CursorShape.ArrowCursor)
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
        self.setStyleSheet(
            "QLabel { background: rgba(26, 24, 22, 230); color: #f2ede4; "
            "border: 1px solid rgba(255, 255, 255, 0.16); border-radius: 7px; "
            "padding: 3px 10px; font-size: 12px; }"
        )
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
            max_w = parent.width() - 2 * SHADOW_MARGIN - 20
            if self.width() > max_w:
                self.setFixedWidth(max_w)
                self.setWordWrap(True)
                self.adjustSize()
            self.move((parent.width() - self.width()) // 2, SHADOW_MARGIN + 6)
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


# ============================================================
# Главное окно перевода
# ============================================================
class TranslateWindow(QWidget):
    retry_requested = Signal()
    pause_requested = Signal()
    stop_requested = Signal()
    clear_requested = Signal()

    def __init__(self, settings):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(MIN_W, MIN_H)
        self.setCursor(Qt.CursorShape.ArrowCursor)

        self._move_pos = None
        self.force_hidden = False
        self._forbidden_rect = None
        self._ghost_mode = False

        # Окно перевода чисто содержит только карточку со стеклом:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.border_frame = QFrame()
        self.border_frame.setObjectName("GlassFrame")
        self.border_frame.setMinimumSize(0, 0)
        self.border_frame.setStyleSheet("""
            QFrame#GlassFrame {
                background-color: rgba(22, 22, 26, 0.78);
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

        # Создаём независимый парящий тулбар
        self.toolbar = FloatingToolbar(target_window=self)
        self.toolbar.retry_clicked.connect(self.retry_requested.emit)
        self.toolbar.pause_clicked.connect(self.pause_requested.emit)
        self.toolbar.stop_clicked.connect(self.stop_requested.emit)
        self.toolbar.clear_clicked.connect(self.clear_requested.emit)
        self.toolbar.ghost_clicked.connect(self.toggle_ghost_mode)

        # Настройки размера и шрифта
        settings.changed.connect(self._on_setting_changed)
        self.update_font_size(int(settings.get("font_size", 14)))
        self.update_opacity(float(settings.get("opacity", 0.95)))

        self.resize(420, 120)
        self._reposition_overlays()

    def clamp_to_screen(self, pos: QPoint) -> QPoint:
        """Удерживает окно перевода в видимой зоне 1, 2 или 3 мониторов."""
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if not screen:
            return pos

        v_geo = screen.virtualGeometry()
        w = self.width()
        h = self.height()

        # Не даём окну скрыться за краями мониторов:
        clamped_x = max(v_geo.left(), min(pos.x(), v_geo.right() - w + SHADOW_MARGIN))
        clamped_y = max(v_geo.top(), min(pos.y(), v_geo.bottom() - h + SHADOW_MARGIN))

        return QPoint(clamped_x, clamped_y)

    def get_card_screen_rect(self) -> QRect:
        """Возвращает точные глобальные экранные координаты карточки с текстом."""
        top_left = self.border_frame.mapToGlobal(QPoint(0, 0))
        return QRect(top_left, self.border_frame.size())

    # ---------- синхронизация положения тулбара и оверлеев ----------
    def _reposition_overlays(self):
        offset = SHADOW_MARGIN + 3
        self.grip.move(
            self.width() - self.grip.width() - offset,
            self.height() - self.grip.height() - offset,
        )
        self.grip.raise_()

        pill_h = self.status_pill.sizeHint().height()
        self.status_pill.move(
            SHADOW_MARGIN + 6,
            self.height() - pill_h - SHADOW_MARGIN - 4,
        )
        self.status_pill.raise_()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "toolbar"):
            self.toolbar.align_to_window()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_overlays()
        if hasattr(self, "toolbar"):
            self.toolbar.align_to_window()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "toolbar"):
            self.toolbar.show()
            self.toolbar.align_to_window()
            self.toolbar.raise_()

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

    # ---------- показ / скрытие / пауза ----------
    def hide_manual(self):
        self.force_hidden = True
        self.hide()
        self.toolbar.hide()

    def toggle_visible(self):
        if self.isVisible():
            self.hide_manual()
        else:
            self.force_hidden = False
            self.show()
            self.toolbar.show()
            self.toolbar.align_to_window()
            self.toolbar.raise_()
            self.raise_()
            self.activateWindow()

    def set_paused_mode(self, paused: bool):
        """Режим паузы: окно текста скрывается, а тулбар остаётся."""
        self.toolbar.set_pause_active(paused)
        if paused:
            self.hide()
            self.toolbar.show()
            self.toolbar.raise_()
        else:
            self.show()
            self.toolbar.show()
            self.toolbar.raise_()
            if self.toolbar._is_docked:
                self.toolbar.align_to_window()
            else:
                # Если во время паузы тулбар затащили на место окна —
                # при пробуждении он автоматически "ОТСКАКИВАЕТ" к ближайшей внешней грани!
                safe_pos = self.toolbar.resolve_collision(self.toolbar.pos())
                self.toolbar.move(safe_pos)

    # ---------- сквозной клик (Ghost mode) ----------
    def toggle_ghost_mode(self) -> bool:
        self._ghost_mode = not self._ghost_mode
        self.toolbar.set_ghost_active(self._ghost_mode)
        status_txt = "ВКЛ (клики сквозь окно)" if self._ghost_mode else "ВЫКЛ"
        self.show_translation(f"[Сквозной клик: {status_txt}]")
        return self._ghost_mode

    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG" and getattr(self, "_ghost_mode", False) and sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                return True, -1  # HTTRANSPARENT: клики проходят сквозь окно с текстом
        return super().nativeEvent(eventType, message)

    # ---------- drag ----------
    def set_forbidden_rect(self, rect):
        self._forbidden_rect = rect

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

            # СТРАХОВКА: держим окно перевода внутри монитора(-ов)
            safe_pos = self.clamp_to_screen(new_pos)
            self.move(safe_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._move_pos = None

    # ---------- контент ----------
    def show_translation(self, text, x=None, y=None):
        if text.startswith("["):
            self._notice_toast.show_notice(text)
            if x is not None and y is not None:
                raw_pos = QPoint(x + 20 - SHADOW_MARGIN, y + 20 - SHADOW_MARGIN)
                self.move(self.clamp_to_screen(raw_pos))
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