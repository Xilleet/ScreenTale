"""Полноэкранный прозрачный сквозной холст (AR-Canvas) для замещения текста."""
import ctypes
import math
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PIL import ImageGrab
from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


@dataclass
class InPlaceBlock:
    rect: QRect
    text: str
    bg_color: QColor
    font_size: int
    angle: float = 0.0

def cluster_lines(blocks: list[dict], max_v_gap_ratio: float = 1.15) -> list[dict]:
    """Усовершенствованная кластеризация для сложных интерфейсов и бабблов.
    Отсеивает одиночный мусор OCR и предотвращает слипание независимых блоков.
    """
    if not blocks:
        return []

    # 1. Отсеиваем очевидный шум OCR (одиночные символы, кроме знаков препинания диалогов)
    valid_blocks = []
    for b in blocks:
        txt = b.get("text", "").strip()
        if not txt:
            continue
        # Пропускаем одиночные буквы-артефакты (i, e, o, \) если это не знаки диалога
        if len(txt) == 1 and txt not in ("!", "?", "—", "-"):
            continue
        valid_blocks.append(b)

    if not valid_blocks:
        return []

    # Сортируем: сверху вниз по Y, затем слева направо по X
    sorted_blocks = sorted(valid_blocks, key=lambda b: (b["rect"][1], b["rect"][0]))
    clusters = []

    for b in sorted_blocks:
        bx, by, bw, bh = b["rect"]
        placed = False

        for c in clusters:
            cx, cy, cw, ch = c["rect"]
            c_bottom = cy + ch
            v_gap = by - c_bottom
            avg_h = (ch + bh) / 2

            # А. Проверяем слова на одной горизонтальной строке
            v_overlap = max(0, min(cy + ch, by + bh) - max(cy, by))
            h_gap = bx - (cx + cw)
            same_line = (v_overlap >= 0.6 * min(ch, bh)) and (-5 <= h_gap <= 2.0 * avg_h)

            # Б. Проверяем следующую строку того же абзаца/баббла
            h_left = max(cx, bx)
            h_right = min(cx + cw, bx + bw)
            h_overlap = max(0, h_right - h_left)
            min_w = min(cw, bw)
            
            horizontal_aligned = (h_overlap / max(min_w, 15)) >= 0.45
            current_cluster_height = (c_bottom - cy) + bh
            not_too_tall = current_cluster_height <= 140

            next_line = (
                -avg_h * 0.2 <= v_gap <= avg_h * max_v_gap_ratio 
                and horizontal_aligned 
                and not_too_tall
            )

            if same_line or next_line:
                c["texts"].append(b["text"])
                c["blocks"].append(b)
                nx = min(cx, bx)
                ny = min(cy, by)
                nw = max(cx + cw, bx + bw) - nx
                nh = max(cy + ch, by + bh) - ny
                c["rect"] = [nx, ny, nw, nh]
                placed = True
                break

        if not placed:
            clusters.append({
                "texts": [b["text"]],
                "blocks": [b],
                "rect": list(b["rect"])
            })

    # Ограничиваем максимальное количество одновременных плашек до разумных 12
    # (отсекая мелкие паразитные кнопки по краям)
    if len(clusters) > 12:
        clusters.sort(key=lambda c: len(" ".join(c["texts"])), reverse=True)
        clusters = clusters[:12]
        # Возвращаем естественный порядок чтения (сверху-вниз)
        clusters.sort(key=lambda c: c["rect"][1])

    return [
        {
            "text": " ".join(c["texts"]),
            "rect": QRect(c["rect"][0], c["rect"][1], c["rect"][2], c["rect"][3]),
            "blocks": c["blocks"],
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
    
    # Сигнал точечного клика с координатами мыши (X, Y)
    point_translate_requested = Signal(QPoint)

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
        self._fade_timer.timeout.connect(self.fade_out_and_clear)

        # закрытие по правой кнопке мыши
        self._mouse_timer = QTimer(self)
        self._mouse_timer.setInterval(25)
        self._mouse_timer.timeout.connect(self._check_mouse_actions)
        self._mouse_timer.start()

        # Окно видно глазам, но для захвата экрана (OCR / ImageGrab) оно невидимо!
        self.set_capture_visibility(False)

    def _check_mouse_actions(self):
        """Слушает клики мыши: 
        - ПКМ (0x02): мгновенно убирает перевод с экрана.
        - СКМ / Колесико (0x04): запрашивает точечный перевод фразы под курсором!
        """
        if sys.platform != "win32":
            return

        user32 = ctypes.windll.user32
        
        # 0x02 = VK_RBUTTON (ПКМ)
        is_r_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        # 0x04 = VK_MBUTTON (Клик на Колесико / СКМ)
        is_m_down = bool(user32.GetAsyncKeyState(0x04) & 0x8000)

        # 1. ПКМ закрывает активный перевод
        if is_r_down and self.active_blocks:
            self.fade_out_and_clear()

        # 2. Клик на СКМ (Колесико) триггерит точечный перевод под курсором
        # Защита от спама (срабатывает ровно один раз на нажатие, пока не отпустишь)
        if is_m_down and not getattr(self, "_was_m_down", False):
            pos = QCursor.pos()
            print(f"[inplace] Сработал СКМ (Колесико) в точке ({pos.x()}, {pos.y()})")
            self.point_translate_requested.emit(pos)

        self._was_m_down = is_m_down

    def display_structured_translation(self, clusters: list[dict], translated_texts: list[str]):
        """Отрисовывает каждый независимый кластер на экране СВОЁЙ собственной плашкой!"""
        if not clusters or not translated_texts:
            return

        new_blocks = []

        for i, c in enumerate(clusters):
            txt = translated_texts[i].strip() if i < len(translated_texts) else ""
            if not txt or txt == "[skip]":
                continue

            target_rect = c["rect"]
            padded_rect = target_rect.adjusted(-6, -4, 6, 4)

            # Вычисляем угол наклона (atan2)
            angle = 0.0
            if c.get("blocks") and "polygon" in c["blocks"][0]:
                poly = c["blocks"][0]["polygon"]
                if len(poly) >= 2:
                    dx = poly[1][0] - poly[0][0]
                    dy = poly[1][1] - poly[0][1]
                    calc_angle = math.degrees(math.atan2(dy, dx))
                    if abs(calc_angle) >= 1.5:
                        angle = calc_angle

            # Замер цвета фона «Хамелеон»
            bg_color = sample_background_color(padded_rect)
            bg_color.setAlpha(245)

            # Автоподбор шрифта
            font_size = find_optimal_font_size(
                txt, padded_rect.width() - 12, padded_rect.height() - 8
            )

            # Адаптивный рост плашки вниз
            metrics = QFontMetrics(QFont("Segoe UI", font_size, QFont.Weight.Bold))
            calc_rect = metrics.boundingRect(
                QRect(0, 0, padded_rect.width() - 12, 0),
                Qt.TextFlag.TextWordWrap,
                txt,
            )
            if calc_rect.height() + 10 > padded_rect.height():
                padded_rect.setHeight(calc_rect.height() + 10)

            new_blocks.append(
                InPlaceBlock(
                    rect=padded_rect,
                    text=txt,
                    bg_color=bg_color,
                    font_size=font_size,
                    angle=angle,
                )
            )

            # Останавливаем затухание, если оно шло в этот момент
            if hasattr(self, "_anim_out") and self._anim_out.state() == QPropertyAnimation.State.Running:
                self._anim_out.stop()
            self._is_fading_out = False

        print(f"[inplace] Отрисовано независимых плашек: {len(new_blocks)}")
        self.active_blocks = new_blocks
        
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self.update()

        self._anim_in = QPropertyAnimation(self, b"windowOpacity")
        self._anim_in.setDuration(180)
        self._anim_in.setStartValue(0.0)
        self._anim_in.setEndValue(1.0)
        self._anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim_in.start()

        self._fade_timer.start(12000)

    def display_translation(self, raw_blocks: list[dict], translated_text: str):
        """Интеллектуальный роутер: делит текст на кластеры и раскладывает по плашкам."""
        clusters = cluster_lines(raw_blocks)
        if not clusters:
            return

        # Если кластер один — отдаем весь перевод ему
        if len(clusters) == 1:
            self.display_structured_translation(clusters, [translated_text])
            return

        # Если строк несколько, пробуем разбить по переносам строк
        lines = [ln.strip() for ln in translated_text.split("\n") if ln.strip()]
        if len(lines) < len(clusters):
            # Добиваем пустыми или оригиналом
            lines += [""] * (len(clusters) - len(lines))

        self.display_structured_translation(clusters, lines)

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
        """Отрисовка плашек перевода на холсте с поддержкой пространственного наклона."""
        if not self.active_blocks:
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        for b in self.active_blocks:
            rect = b.rect

            p.save()  # 1. Сохраняем состояние холста

            # 2. Переносим центр координат в центр нашей плашки
            cx = rect.center().x()
            cy = rect.center().y()
            p.translate(cx, cy)

            # 3. Поворачиваем холст на угол наклона текста в игре
            if abs(b.angle) >= 1.5:
                p.rotate(b.angle)

            # 4. Локальные координаты прямоугольника относительно центра (0, 0)
            w = rect.width()
            h = rect.height()
            local_rect = QRect(-w // 2, -h // 2, w, h)

            # 5. Рисуем матовую подложку «Хамелеон»
            p.setBrush(b.bg_color)
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))
            p.drawRoundedRect(local_rect, 6, 6)

            # 6. Рисуем текст перевода (он автоматически повернётся вместе с холстом!)
            p.setPen(QColor(245, 240, 235))
            p.setFont(QFont("Segoe UI", b.font_size, QFont.Weight.Bold))
            p.drawText(
                local_rect.adjusted(8, 6, -8, -6),
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignCenter,
                b.text,
            )

            p.restore()  # 7. Возвращаем холст в исходное положение

    def nativeEvent(self, eventType, message):
        """Сквозной клик (HTTRANSPARENT)."""
        if eventType == b"windows_generic_MSG" and sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0084:  # WM_NCHITTEST
                return True, -1
        return super().nativeEvent(eventType, message)

    def fade_out_and_clear(self):
        """Плавное растворение плашек перед очисткой (вызывается по ПКМ)."""
        if not self.active_blocks or getattr(self, "_is_fading_out", False):
            return
            
        self._is_fading_out = True
        self._fade_timer.stop()

        self._anim_out = QPropertyAnimation(self, b"windowOpacity")
        self._anim_out.setDuration(120)  # Быстрое и мягкое затухание
        self._anim_out.setStartValue(self.windowOpacity())
        self._anim_out.setEndValue(0.0)
        
        def _on_hidden():
            self.clear()
            self._is_fading_out = False

        self._anim_out.finished.connect(_on_hidden)
        self._anim_out.start()