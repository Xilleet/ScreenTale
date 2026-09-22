"""Оверлей выделения области экрана (перед OCR)."""
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

MIN_SELECTION = 20  # минимальный размер области, px


class ScreenSelector(QWidget):
    """Полупрозрачный оверлей на весь виртуальный рабочий стол.
    callback(bbox) — при успешном выделении, on_cancel() — при отмене (Esc/слишком мало)."""

    def __init__(self, callback, on_cancel=None):
        super().__init__()
        self.callback = callback
        self.on_cancel = on_cancel
        self._start = None
        self._current = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus) 

        screen = QApplication.primaryScreen()
        self.setGeometry(screen.virtualGeometry())
        self.show()
        self.activateWindow()
        self.setFocus()   

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # 1. Затемняем весь экран
        p.fillRect(self.rect(), QColor(0, 0, 0, 80))

        if self._start and self._current:
            rect = QRect(self._start, self._current).normalized()
            if rect.width() > 0 and rect.height() > 0:
                # 2. Вырезаем прозрачное окно к рабочему столу (Snipping Tool style)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                p.fillRect(rect, Qt.GlobalColor.transparent)

                # 3. Рисуем контрастную двойную рамку (тёмная 3px + белая 1px)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                p.setBrush(Qt.BrushStyle.NoBrush)

                # Тёмная внешняя обводка (чтобы рамку было видно на светлых фонах)
                p.setPen(QPen(QColor(0, 0, 0, 160), 3))
                p.drawRect(rect)

                # Белая внутренняя линия
                p.setPen(QPen(QColor(255, 255, 255, 240), 1))
                p.drawRect(rect)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._start = event.position().toPoint()
            self._current = self._start
            self.update()

    def mouseMoveEvent(self, event):
        if self._start:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._start:
            return
        p1 = self.mapToGlobal(self._start)
        p2 = self.mapToGlobal(event.position().toPoint())
        left, top = min(p1.x(), p2.x()), min(p1.y(), p2.y())
        right, bottom = max(p1.x(), p2.x()), max(p1.y(), p2.y())
        self.close()
        if (right - left) > MIN_SELECTION and (bottom - top) > MIN_SELECTION:
            self.callback((left, top, right, bottom))
        elif self.on_cancel:
            self.on_cancel()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            if self.on_cancel:
                self.on_cancel()