"""Окно перевода: стеклянная плашка (Aero blur), drag, ресайз, история."""
import ctypes

from PySide6.QtCore import QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QLabel,
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
NOTICE_TOAST_MS = 2000        # короткая вспышка, чтобы привлечь внимание
STATUS_AUTO_RESET_OK_MS = 1500   # «Готово» → «Ожидание» через 1.5 сек
STATUS_AUTO_RESET_ERR_MS = 3000  # «Ошибка» → «Ожидание» через 3 сек


def apply_blur_effect(hwnd):
    """Нативное системное размытие фона Windows под окном (Win 10/11)."""
    try:
        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_int), ("AnimationId", ctypes.c_int)]

        class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p),
                        ("SizeOfData", ctypes.c_size_t)]

        accent = ACCENT_POLICY()
        accent.AccentState = 4          # ACCENT_ENABLE_BLURBEHIND, было accent.AccentState = 3 
        accent.AccentFlags = 2
        accent.GradientColor = 0x01000000

        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = 19             # WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(accent)
        ctypes.windll.user32.SetWindowCompositionAttribute(int(hwnd), ctypes.byref(data))
    except Exception:
        pass


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
    """Тонкий toast в верхней части окна перевода для служебных сообщений.

    Автоматически гаснет через NOTICE_TOAST_MS (4 сек) с fade-анимацией.
    Не блокирует QTextEdit — поверх, в углу. Поднимается raise_().
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "QLabel { background: rgba(33, 36, 41, 220); color: #e8ecf1; "
            "border: 1px solid rgba(255,255,255,0.18); border-radius: 6px; "
            "padding: 6px 12px; font-size: 12px; }")
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
        # Ширина не больше 90% ширины родителя
        parent = self.parentWidget()
        if parent:
            max_w = parent.width() - 2 * SHADOW_MARGIN - 24
            if self.width() > max_w:
                self.setFixedWidth(max_w)
                self.setWordWrap(True)
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


class TranslateWindow(QWidget):
    def __init__(self, settings):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(MIN_W, MIN_H)
        self.resize(420, 120)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

        self._move_pos = None
        self.force_hidden = False
        # Запретная зона для drag'а — bbox OCR в авто-режиме.
        # Устанавливается из AppController через set_forbidden_rect().
        # При drag'е окно "прилипает" к границе снаружи, не заходя внутрь.
        self._forbidden_rect = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
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
        self.grip.raise_()

        # Toast для служебных сообщений (в верхней части окна)
        self._notice_toast = _NoticeToast(self)

        # StatusPill в нижнем левом углу: ожидание / перевод / готово / ошибка
        self.status_pill = StatusPill(self)
        self.status_pill.set_state("off", "Ожидание")

        # Таймер авто-сброса статуса (single-shot): «Готово» → «Ожидание»
        # через 1.5 сек, «Ошибка» → «Ожидание» через 3 сек
        self._status_reset_timer = QTimer(self)
        self._status_reset_timer.setSingleShot(True)
        self._status_reset_timer.timeout.connect(self._reset_status_to_off)

        #apply_blur_effect(self.winId()) - закомментировано потому что нормально не блюрилось, оставлено на всякий

        # Тримминг истории — само окно, контроллеру делать нечего
        self._trim_timer = QTimer(self)
        self._trim_timer.timeout.connect(self._cleanup_history)
        self._trim_timer.start(HISTORY_TRIM_INTERVAL_MS)

        # Подписка на настройки вместо колбэков контроллера
        settings.changed.connect(self._on_setting_changed)
        self.update_font_size(int(settings.get("font_size", 14)))
        self.update_opacity(float(settings.get("opacity", 0.95)))

    # ---------- настройки ----------
    def _on_setting_changed(self, key, value):
        if key == "font_size":
            self.update_font_size(int(value))
        elif key == "opacity":
            self.update_opacity(float(value))

    def update_font_size(self, size):
        # Глобальный QSS (theme.py: QWidget { font-size: 13px }) на Windows
        # может перекрывать setFont() на QTextEdit. Задаём font-size через QSS
        # прямо на text_widget — это имеет приоритет над глобальным правилом.
        # Сохраняем все остальные стили (фон, скроллбар) из исходного блока.
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
        # setFont меняет шрифт только для НОВОГО текста. Существующий текст
        # сохраняет старый charFormat. Чтобы слайдер сразу менял размер
        # всего содержимого — перерисовываем весь документ через mergeCharFormat.
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

    # ---------- запретная зона для drag'а ----------
    def set_forbidden_rect(self, rect):
        """Установить запретную зону (bbox OCR) или None для снятия.

        При drag'е окно не сможет зайти в эту зону — прилипнет к её
        ближайшей границе снаружи.
        """
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
            # Если есть запретная зона (bbox OCR) — не давать окну зайти в неё.
            # "Прилипание" к ближайшей границе снаружи (вариант C):
            # если новая позиция пересекает bbox, корректируем её, чтобы
            # окно остановилось у границы bbox.
            if self._forbidden_rect is not None:
                new_x, new_y = new_pos.x(), new_pos.y()
                new_rect = QRect(new_x, new_y, self.width(), self.height())
                if new_rect.intersects(self._forbidden_rect):
                    fr = self._forbidden_rect
                    # Корректируем по минимальному смещению — прилипаем к
                    # ближайшей границе bbox снаружи.
                    # Если окно пересекает bbox и по X, и по Y — выбираем
                    # ось, по которой меньше "проталкивать".
                    # Подсчёт 4 вариантов сдвига:
                    shift_left = fr.left() - self.width() - 1   # окно левее bbox
                    shift_right = fr.right() + 1                # окно правее bbox
                    shift_top = fr.top() - self.height() - 1    # окно выше bbox
                    shift_bottom = fr.bottom() + 1             # окно ниже bbox
                    # 4 кандидата: (x, y, расстояние)
                    cands = [
                        (shift_left, new_y, abs(new_x - shift_left)),
                        (shift_right, new_y, abs(new_x - shift_right)),
                        (new_x, shift_top, abs(new_y - shift_top)),
                        (new_x, shift_bottom, abs(new_y - shift_bottom)),
                    ]
                    # Фильтруем кандидатов: только те, кто не
                    # пересекает bbox даже с учётом касания (поэтому сдвиг с зазором).
                    valid_cands = []
                    for cx, cy, dist in cands:
                        test_rect = QRect(cx, cy, self.width(), self.height())
                        if not test_rect.intersects(self._forbidden_rect):
                            valid_cands.append((cx, cy, dist))
                    if valid_cands:
                        # Есть кандидат, полностью вне bbox — прилипаем
                        best_x, best_y, _ = min(valid_cands, key=lambda c: c[2])
                        new_pos.setX(best_x)
                        new_pos.setY(best_y)
                    else:
                        # Окно больше bbox — ни один кандидат не
                        # помещается вне bbox. Не двигаем окно —
                        # остаёмся на старой позиции. Юзер может
                        # уменьшить окно (ResizeGrip) или область OCR.
                        print("[auto] WARN: окно больше bbox — не движу")
                        return  # не двигать
            self.move(new_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._move_pos = None

    # ---------- контент ----------
    def show_translation(self, text, x=None, y=None):
        # Служебные сообщения (начинаются с «[») — только в toast,
        # не добавляем в QTextEdit. Переводы с меткой времени
        # используют круглые скобки ((HH:MM:SS) текст),
        # поэтому startswith("[") не переводы не поймает.
        if text.startswith("["):
            self._notice_toast.show_notice(text)
            if x is not None and y is not None:
                self.move(x + 20 - SHADOW_MARGIN, y + 20 - SHADOW_MARGIN)
            if not self.isVisible() and not self.force_hidden:
                self.show()
            return
        # Обычный перевод — добавляем в QTextEdit
        cursor = QTextCursor(self.text_widget.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self.text_widget.toPlainText().strip():
            cursor.insertText("\n\n---\n\n")
        cursor.insertText(text)
        self.text_widget.setTextCursor(cursor)
        self.text_widget.ensureCursorVisible()

        # Если переданы координаты — всегда перемещать окно в эту точку.
        if x is not None and y is not None:
            self.move(x + 20 - SHADOW_MARGIN, y + 20 - SHADOW_MARGIN)

        if not self.isVisible() and not self.force_hidden:
            self.show()

    def clear_history(self):
        """Очистить весь накопленный текст в окне перевода."""
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
            # Повреждение document — редкий кейс (одновременный insertText
            # и trim, или слишком много символов). Без лога окно тихо
            # раздувается до лимита и в итоге крашит QTextEdit.
            print(f"[warn] _cleanup_history: {_e}")

    # ---------- статус ----------
    def set_status(self, state, text):
        """Установить состояние StatusPill (вызывается из AppController).

        state: "off" (ожидание), "busy" (перевод), "ok" (готово),
              "error" (ошибка)
        "ok" и "error" автоматически сбрасываются в "off" через
        STATUS_AUTO_RESET_OK_MS / STATUS_AUTO_RESET_ERR_MS.
        """
        self._status_reset_timer.stop()
        self.status_pill.set_state(state, text)
        if state == "ok":
            self._status_reset_timer.start(STATUS_AUTO_RESET_OK_MS)
        elif state == "error":
            self._status_reset_timer.start(STATUS_AUTO_RESET_ERR_MS)

    def _reset_status_to_off(self):
        """Таймер авто-сброса: «Готово»/«Ошибка» → «Ожидание»."""
        self.status_pill.set_state("off", "Ожидание")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        offset = SHADOW_MARGIN + 3
        self.grip.move(self.width() - self.grip.width() - offset,
                       self.height() - self.grip.height() - offset)
        self.grip.raise_()
        # StatusPill — в нижнем левом углу, не перекрывает ResizeGrip
        pill_h = self.status_pill.sizeHint().height()
        self.status_pill.move(SHADOW_MARGIN + 3,
                              self.height() - pill_h - SHADOW_MARGIN - 3)
        self.status_pill.raise_()
