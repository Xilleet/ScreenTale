"""Полноэкранный прозрачный сквозной холст (AR-Canvas) для замещения текста."""
import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PIL import ImageGrab
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


@dataclass
class InPlaceBlock:
    rect: QRect
    text: str
    bg_color: QColor
    font_size: int


def cluster_lines(blocks: list[dict], max_v_gap_ratio: float = 1.8) -> list[dict]:
    """Группирует близкие строки в отдельные смысловые бабблы по пространственной близости.
    Текст в левом углу и текст в правом углу сформируют РАЗНЫЕ независимые плашки!
    """
    if not blocks:
        return []

    sorted_blocks = sorted(blocks, key=lambda b: b["rect"][1])
    clusters = []

    for b in sorted_blocks:
        bx, by, bw, bh = b["rect"]
        placed = False
        for c in clusters:
            cx, cy, cw, ch = c["rect"]
            c_bottom = cy + ch
            v_gap = by - c_bottom
            avg_h = (ch + bh) / 2
            h_overlap = max(0, min(cx + cw, bx + bw) - max(cx, bx))
            min_w = min(cw, bw)

            # Если строка лежит строго под бабблом и перекрывается по ширине
            if -avg_h * 0.5 <= v_gap <= avg_h * max_v_gap_ratio and (h_overlap / max(min_w, 1)) > 0.2:
                c["texts"].append(b["text"])
                nx = min(cx, bx)
                ny = min(cy, by)
                nw = max(cx + cw, bx + bw) - nx
                nh = max(cy + ch, by + bh) - ny
                c["rect"] = [nx, ny, nw, nh]
                placed = True
                break
        if not placed:
            clusters.append({"texts": [b["text"]], "rect": list(b["rect"])})

    return [
        {
            "text": " ".join(c["texts"]),
            "rect": QRect(c["rect"][0], c["rect"][1], c["rect"][2], c["rect"][3]),
        }
        for c in clusters
    ]


def sample_background_color(rect: QRect) -> QColor:
    """Замеряет цвет фона по 4 углам с отступом 3px (Хамелеон)."""
    try:
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if w < 10 or h < 10:
            return QColor(20, 20, 24, 235)

        crop = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        cw, ch = crop.size

        # 4 точки с отступом в 3 пикселя внутрь
        c_pts = [
            (min(3, cw - 1), min(3, ch - 1)),
            (max(0, cw - 4), min(3, ch - 1)),
            (min(3, cw - 1), max(0, ch - 4)),
            (max(0, cw - 4), max(0, ch - 4)),
        ]
        r = sum(crop.getpixel(p)[0] for p in c_pts) // 4
        g = sum(crop.getpixel(p)[1] for p in c_pts) // 4
        b = sum(crop.getpixel(p)[2] for p in c_pts) // 4
        return QColor(r, g, b, 240)  # Матовая непрозрачность 240/255
    except Exception:
        return QColor(20, 20, 24, 240)


def find_optimal_font_size(text: str, max_w: int, max_h: int, min_sz: int = 9, max_sz: int = 40) -> int:
    """Бинарный поиск идеального кегля шрифта под рамку за O(log N)."""
    best = min_sz
    low, high = min_sz, max_sz

    while low <= high:
        mid = (low + high) // 2
        metrics = QFontMetrics(QFont("Segoe UI", mid, QFont.Weight.Bold))
        # Считаем высоту с учетом переноса слов
        rect = metrics.boundingRect(QRect(0, 0, max_w, 0), Qt.TextFlag.TextWordWrap, text)

        if rect.height() <= max_h and rect.width() <= max_w:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    return best


class InPlaceCanvas(QWidget):
    """Невидимый полноэкранный слой поверх рабочего стола и игр."""

    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.virtualGeometry())

        self.active_blocks: list[InPlaceBlock] = []

        # Таймер автоматического скрытия перевода через 10 секунд бездействия
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self.clear)

        # закрытие по правой кнопке мыши
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(25)
        self._mouse_timer.timeout.connect(self._check_mouse_dismiss)
        self._mouse_timer.start()

        # Окно видно глазам, но для захвата экрана (OCR / ImageGrab) оно невидимо!
        self.set_capture_visibility(False)

    def _check_mouse_dismiss(self):
        """ПКМ (правая кнопка мыши) мгновенно убирает перевод с экрана."""
        if sys.platform != "win32":
            return
        # 0x02 = VK_RBUTTON
        is_r_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000)
        if is_r_down and self.active_blocks:
            self.clear()

    def display_translation(self, raw_blocks: list[dict], translated_text: str):
        """Отрисовывает перевод: каждый независимый баббл в своём углу экрана!"""
        if not raw_blocks or not translated_text.strip():
            return

        clusters = cluster_lines(raw_blocks)
        if not clusters:
            return

        new_blocks = []
        # Если блок один — накрываем его целиком переводом
        if len(clusters) == 1:
            padded = clusters[0]["rect"].adjusted(-8, -6, 8, 6)
            bg = sample_background_color(padded)
            bg.setAlpha(245)
            f_sz = find_optimal_font_size(translated_text, padded.width() - 16, padded.height() - 10)
            metrics = QFontMetrics(QFont("Segoe UI", f_sz, QFont.Weight.Bold))
            calc = metrics.boundingRect(QRect(0, 0, padded.width() - 16, 0), Qt.TextFlag.TextWordWrap, translated_text)
            if calc.height() + 16 > padded.height():
                padded.setHeight(calc.height() + 16)
            new_blocks.append(InPlaceBlock(rect=padded, text=translated_text, bg_color=bg, font_size=f_sz))
        else:
            # Если несколько бабблов (например, квест слева и диалог справа)
            lines = [ln.strip() for ln in translated_text.split("\n") if ln.strip()]
            for i, c in enumerate(clusters):
                txt = lines[i] if i < len(lines) else (translated_text if i == 0 else "")
                if not txt:
                    continue
                padded = c["rect"].adjusted(-6, -4, 6, 4)
                bg = sample_background_color(padded)
                bg.setAlpha(245)
                f_sz = find_optimal_font_size(txt, padded.width() - 12, padded.height() - 8)
                new_blocks.append(InPlaceBlock(rect=padded, text=txt, bg_color=bg, font_size=f_sz))

        self.active_blocks = new_blocks
        self.show()
        self.raise_()
        self.update()
        self._fade_timer.start(12000)

    def clear(self):
        """Очистить холст."""
        self.active_blocks.clear()
        self.update()

    def set_capture_visibility(self, visible_to_capture: bool):
        """Переключает видимость для сторонних программ (ShareX, OBS) на лету со строгой типизацией x64."""
        
        # Защита от холостых и повторных вызовов
        if getattr(self, "_current_capture_visibility", None) == visible_to_capture:
            return  # Состояние не изменилось — выходим без спама!
        self._current_capture_visibility = visible_to_capture

        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

            hwnd = int(self.winId())
            # 0x00000000 = WDA_NONE (видно ShareX и OBS)
            # 0x00000011 = WDA_EXCLUDEFROMCAPTURE (скрыто от скриншотов)
            affinity = 0x00000000 if visible_to_capture else 0x00000011
            ok = user32.SetWindowDisplayAffinity(hwnd, affinity)
            
            status_text = "ВЫКЛЮЧЕНА (ShareX ВИДИТ плашку)" if visible_to_capture else "ВКЛЮЧЕНА (Плашка СКРЫТА от ShareX/OBS)"
            print(f"[inplace] Защита от захвата: {status_text} | результат Windows API: {bool(ok)}")
        except Exception as e:
            print(f"[inplace] Ошибка переключения защиты захвата: {e}")

    def paintEvent(self, event):
        if not self.active_blocks:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        for b in self.active_blocks:
            rect = b.rect

            # 1. Плотная матовая плашка в тон фона игры
            p.setBrush(b.bg_color)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))  # Тонкая рамка
            p.drawRoundedRect(rect, 6, 6)

            # 2. Текст перевода (белый с аккуратным центрированием)
            p.setPen(QColor(245, 240, 235))
            p.setFont(QFont("Segoe UI", b.font_size, QFont.Weight.Bold))
            p.drawText(
                rect.adjusted(8, 6, -8, -6),
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter,
                b.text,
            )

    def nativeEvent(self, eventType, message):
        """Сквозной клик (HTTRANSPARENT)."""
        if eventType == b"windows_generic_MSG" and sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                return True, -1
        return super().nativeEvent(eventType, message)