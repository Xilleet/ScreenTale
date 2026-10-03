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


def cluster_lines(blocks: list[dict], max_v_gap_ratio: float = 2.0) -> list[dict]:
    """Объединяет все прочитанные строки одного абзаца/диалога в единый монолитный блок.
    Гарантирует, что подложка накроет весь исходный текст целиком без дыр снизу.
    """
    if not blocks:
        return []

    # 1. Если пришла всего одна строка — просто оборачиваем её в QRect
    if len(blocks) == 1:
        b = blocks[0]
        x, y, w, h = b["rect"]
        return [{"text": b["text"], "rect": QRect(x, y, w, h)}]

    # 2. Сортируем строки по вертикали (сверху вниз)
    sorted_blocks = sorted(blocks, key=lambda b: b["rect"][1])

    # 3. Вычисляем общий охватывающий прямоугольник (Bounding Box Envelope)
    # для всех строк, чтобы плашка гарантированно перекрыла весь оригинальный абзац
    all_x = [b["rect"][0] for b in sorted_blocks]
    all_y = [b["rect"][1] for b in sorted_blocks]
    all_right = [b["rect"][0] + b["rect"][2] for b in sorted_blocks]
    all_bottom = [b["rect"][1] + b["rect"][3] for b in sorted_blocks]

    min_x = min(all_x)
    min_y = min(all_y)
    total_w = max(all_right) - min_x
    total_h = max(all_bottom) - min_y

    # Склеиваем весь текст в одну связную фразу для отображения
    combined_text = " ".join(b["text"].strip() for b in sorted_blocks if b["text"].strip())

    return [
        {
            "text": combined_text,
            "rect": QRect(int(min_x), int(min_y), int(total_w), int(total_h)),
        }
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
        # if sys.platform == "win32":
        #    try:
        #        # 0x00000011 исключает окно из любого захвата экрана Windows
        #        ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), 0x00000011)
        #    except Exception:
        #        pass
        self.set_capture_visibility(True)

        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.virtualGeometry())

        self.active_blocks: list[InPlaceBlock] = []

        # Таймер автоматического скрытия перевода через 10 секунд бездействия
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self.clear)

# Окно видно глазам, но для захвата экрана (OCR / ImageGrab) оно невидимо!
        self.set_capture_visibility(False)

    def display_translation(self, raw_blocks: list[dict], translated_text: str):
        """Принимает сырые строки OCR и отображает матовую плашку ровно поверх оригинала."""
        if not raw_blocks or not translated_text.strip():
            return

        # 1. Получаем единый блок, накрывающий весь исходный абзац целиком
        clusters = cluster_lines(raw_blocks)
        if not clusters:
            return

        target_rect = clusters[0]["rect"]

        # Добавляем щедрый отступ (8px по бокам, 6px сверху/снизу) для идеального перекрытия
        padded_rect = target_rect.adjusted(-8, -6, 8, 6)

        # 2. Замеряем цвет фона под текстом (Хамелеон)
        bg_color = sample_background_color(padded_rect)
        # Делаем плотную непрозрачность 245/255, чтобы буквы под ней не просвечивали
        bg_color.setAlpha(245)

        # 3. Подбираем идеальный кегль шрифта под размер рамки
        font_size = find_optimal_font_size(
            translated_text, padded_rect.width() - 16, padded_rect.height() - 10
        )

        # 4. Если русский перевод длиннее оригинала — мягко расширяем плашку вниз
        metrics = QFontMetrics(QFont("Segoe UI", font_size, QFont.Weight.Bold))
        calc_rect = metrics.boundingRect(
            QRect(0, 0, padded_rect.width() - 16, 0),
            Qt.TextFlag.TextWordWrap,
            translated_text
        )
        
        needed_height = calc_rect.height() + 16
        if needed_height > padded_rect.height():
            padded_rect.setHeight(needed_height)

        print(
            f"[inplace] Отрисовка на экране: оригинал {target_rect} -> плашка {padded_rect}, "
            f"шрифт={font_size}px, цвет={bg_color.name()}"
        )

        self.active_blocks = [
            InPlaceBlock(
                rect=padded_rect,
                text=translated_text,
                bg_color=bg_color,
                font_size=font_size,
            )
        ]

        self.show()
        self.raise_()
        self.update()
        self._fade_timer.start(12000)  # Держим перевод 12 секунд

    def clear(self):
        """Очистить холст."""
        self.active_blocks.clear()
        self.update()

    def set_capture_visibility(self, visible_to_capture: bool):
        """Переключает видимость для сторонних программ (ShareX, OBS) на лету со строгой типизацией x64."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            # Строгая типизация для 64-битной Windows (защита от тихого сбоя HWND)
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