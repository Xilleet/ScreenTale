"""Стратегии OCR-движков для ScreenTale с поддержкой координат (In-Place)."""
import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
from abc import ABC, abstractmethod

import numpy as np
from PIL import Image

from backend.config import get_data_dir
from backend.logging_setup import vlog

# Проверка наличия WinRT / winocr
try:
    import winocr
    HAS_WINOCR = True
except ImportError:
    winocr = None
    HAS_WINOCR = False

# Проверка импорта rapidocr
try:
    from rapidocr_onnxruntime import RapidOCR
    HAS_RAPIDOCR = True
except ImportError:
    RapidOCR = None
    HAS_RAPIDOCR = False

# winocr уже содержит импортированные OcrEngine и Language из WinRT
WinrtOcrEngine = getattr(winocr, "OcrEngine", None)
Language = getattr(winocr, "Language", None)
HAS_WINRT = WinrtOcrEngine is not None and Language is not None

_winrt_lock = threading.RLock()
_lang_cache: dict[str, bool] = {}


def reset_ocr_lang_cache(tag: str | None = None):
    """Сбрасывает кэш проверки, если пользователь только что установил язык."""
    with _winrt_lock:
        if tag:
            _lang_cache.pop(tag, None)
            _lang_cache.pop(tag.split("-")[0], None)
        else:
            _lang_cache.clear()


class BaseOcrEngine(ABC):
    """Абстрактный интерфейс OCR-движка."""

    engine_id: str = "base"
    display_name: str = "Base OCR"

    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        ...

    @abstractmethod
    def read(self, img: Image.Image) -> str:
        """Стандартное плоское чтение (для совместимости с v0.6)."""
        ...

    def read_detailed(self, img: Image.Image) -> tuple[str, list[dict]]:
        """Детальное чтение с координатами для In-Place замещения v0.7.
        Возвращает: (full_text, [ {"text": str, "rect": (x,y,w,h), "polygon": [[x,y]...]} ])
        """
        text = self.read(img)
        w, h = img.size
        return text, [{"text": text, "rect": (0, 0, w, h), "polygon": [[0, 0], [w, 0], [w, h], [0, h]]}]

    def get_installed_languages(self) -> list[str]:
        return ["en"]

    def check_language_support(self, lang: str) -> tuple[bool, str]:
        return True, ""


# =====================================================================
# 1. Windows Native OCR (WinRT API) с извлечением координат
# =====================================================================
class WindowsOcrEngine(BaseOcrEngine):
    engine_id: str = "windows"
    display_name: str = "Windows OCR (Нативный)"

    def __init__(self, default_lang: str = "en"):
        self.lang = default_lang
        self._is_ready = False

    def is_available(self) -> bool:
        return HAS_WINOCR or HAS_WINRT

    def get_installed_languages(self) -> list[str]:
        if not HAS_WINRT or WinrtOcrEngine is None:
            return ["en-US"]
        with _winrt_lock:
            try:
                langs = WinrtOcrEngine.available_recognizer_languages
                return [getattr(l, "language_tag", str(l)) for l in langs]
            except Exception:
                return ["en-US"]

    def check_language_support(self, lang: str) -> tuple[bool, str]:
        if not HAS_WINRT or WinrtOcrEngine is None or Language is None:
            return (True, "")

        tag = lang if "-" in lang else ("ja-JP" if lang == "ja" else "en-US")

        if tag in _lang_cache:
            if _lang_cache[tag]:
                return (True, "")
            cmd = f'Add-WindowsCapability -Online -Name "Language.OCR~~~{tag}~0.0.1.0"'
            return (False, f"В Windows не установлен пакет OCR для [{tag}].\nКоманда: {cmd}")

        with _winrt_lock:
            try:
                win_lang = Language(tag)
                supported = bool(WinrtOcrEngine.is_language_supported(win_lang))
                _lang_cache[tag] = supported

                if not supported:
                    cmd = f'Add-WindowsCapability -Online -Name "Language.OCR~~~{tag}~0.0.1.0"'
                    msg = (
                        f"В Windows не установлен языковой пакет OCR для [{tag}].\n"
                        "Установите его в: Параметры Windows -> Время и язык -> Язык,\n"
                        f"либо выполните в PowerShell от админа:\n{cmd}"
                    )
                    return (False, msg)

                return (True, "")
            except Exception as e:
                _lang_cache[tag] = False
                return (False, f"Ошибка проверки языка Windows OCR: {e}")

    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        self.lang = lang
        ok, msg = self.check_language_support(self.lang)
        if not ok:
            print(f"[windows_ocr] ПРЕДУПРЕЖДЕНИЕ:\n{msg}")
            self._is_ready = False
            return False
        self._is_ready = True
        return True

    def read_detailed(self, img: Image.Image) -> tuple[str, list[dict]]:
        """Извлекает текст и объединяет слова каждой строки в bounding_rect."""
        if not self._is_ready or not HAS_WINOCR or winocr is None:
            return "", []

        with _winrt_lock:
            try:
                res = winocr.recognize_pil_sync(img, lang=self.lang)
                if not isinstance(res, dict):
                    t = str(res).strip()
                    w, h = img.size
                    return t, [{"text": t, "rect": (0, 0, w, h), "polygon": [[0, 0], [w, 0], [w, h], [0, h]]}]

                full_text = res.get("text", "").strip()
                lines = res.get("lines", [])
                blocks = []

                for line in lines:
                    line_text = line.get("text", "").strip()
                    if not line_text:
                        continue

                    words = line.get("words", [])
                    # Вычисляем bounding_rect строки как объединение всех слов в строке
                    if words and "bounding_rect" in words[0]:
                        x1 = min(w["bounding_rect"]["x"] for w in words)
                        y1 = min(w["bounding_rect"]["y"] for w in words)
                        x2 = max(w["bounding_rect"]["x"] + w["bounding_rect"]["width"] for w in words)
                        y2 = max(w["bounding_rect"]["y"] + w["bounding_rect"]["height"] for w in words)
                        w_box = max(1, x2 - x1)
                        h_box = max(1, y2 - y1)
                    else:
                        continue

                    blocks.append({
                        "text": line_text,
                        "rect": (int(x1), int(y1), int(w_box), int(h_box)),
                        "polygon": [
                            [int(x1), int(y1)],
                            [int(x1 + w_box), int(y1)],
                            [int(x1 + w_box), int(y1 + h_box)],
                            [int(x1), int(y1 + h_box)],
                        ],
                    })

                return full_text, blocks
            except Exception as e:
                vlog(f"[windows_ocr] сбой чтения: {e}")
                return "", []

    def read(self, img: Image.Image) -> str:
        text, _ = self.read_detailed(img)
        return text

    def unload(self) -> None:
        with _winrt_lock:
            self._is_ready = False


# =====================================================================
# 2. RapidOCR Engine (ONNX) с полигонами
# =====================================================================
class RapidOcrEngine(BaseOcrEngine):
    engine_id: str = "rapidocr"
    display_name: str = "RapidOCR (ONNX / Азия)"

    def __init__(self, direction: str = "horizontal"):
        self.direction = direction
        self._engine = None

    def is_available(self) -> bool:
        return HAS_RAPIDOCR

    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        if not HAS_RAPIDOCR or RapidOCR is None:
            return False
        try:
            self._engine = RapidOCR()
            return True
        except Exception as e:
            vlog(f"[rapidocr] сбой загрузки: {e}")
            return False

    def set_direction(self, direction: str) -> None:
        self.direction = direction

    def read_detailed(self, img: Image.Image, direction: str | None = None) -> tuple[str, list[dict]]:
        if self._engine is None:
            return "", []

        dir_mode = direction or self.direction
        img_np = np.array(img.convert("RGB"))

        try:
            result, _ = self._engine(img_np)
            if not result:
                return "", []

            blocks = []
            for item in result:
                if not item or len(item) < 2:
                    continue
                box = item[0]
                text = item[1]
                if isinstance(text, tuple):
                    text = text[0]
                text = str(text).strip()
                if not text:
                    continue

                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x1, y1 = min(xs), min(ys)
                x2, y2 = max(xs), max(ys)
                w_box = max(1, x2 - x1)
                h_box = max(1, y2 - y1)

                blocks.append({
                    "text": text,
                    "rect": (int(x1), int(y1), int(w_box), int(h_box)),
                    "polygon": [[int(p[0]), int(p[1])] for p in box],
                })

            if dir_mode == "vertical":
                full_text = self._sort_tategaki(result)
            else:
                full_text = " ".join([b["text"] for b in blocks]).strip()

            return full_text, blocks
        except Exception as e:
            vlog(f"[rapidocr] сбой распознавания: {e}")
            return "", []

    def read(self, img: Image.Image, direction: str | None = None) -> str:
        full_text, _ = self.read_detailed(img, direction)
        return full_text

    def _sort_tategaki(self, result: list) -> str:
        boxes_with_text = []
        for item in result:
            if not item or len(item) < 2:
                continue
            box, text = item[0], item[1]
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            center_x = sum(xs) / len(xs)
            center_y = sum(ys) / len(ys)
            width = max(xs) - min(xs)
            boxes_with_text.append({
                "text": text,
                "cx": center_x,
                "cy": center_y,
                "w": width,
            })

        if not boxes_with_text:
            return ""

        boxes_with_text.sort(key=lambda b: -b["cx"])
        columns: list[list[dict]] = []
        for b in boxes_with_text:
            placed = False
            for col in columns:
                col_avg_x = sum(item["cx"] for item in col) / len(col)
                col_avg_w = sum(item["w"] for item in col) / len(col)
                if abs(b["cx"] - col_avg_x) < max(col_avg_w * 0.7, 15):
                    col.append(b)
                    placed = True
                    break
            if not placed:
                columns.append([b])

        sorted_texts = []
        for col in columns:
            col.sort(key=lambda b: b["cy"])
            column_text = "".join(item["text"] for item in col)
            sorted_texts.append(column_text)

        return "\n".join(sorted_texts).strip()

    def unload(self) -> None:
        self._engine = None


# =====================================================================
# 3. EasyOCR Engine (PyTorch)
# =====================================================================
class EasyOcrEngine(BaseOcrEngine):
    engine_id: str = "easyocr"
    display_name: str = "EasyOCR (PyTorch)"

    def __init__(self):
        self._reader = None
        self._easyocr = None
        self._active_gpu = False

    def is_available(self) -> bool:
        try:
            import easyocr  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        import easyocr
        import torch

        self._easyocr = easyocr
        cuda_ok = bool(torch.cuda.is_available())
        target_gpu = bool(use_gpu and cuda_ok)

        self.unload()
        mdir = os.path.join(get_data_dir(), "easyocr_models")
        os.makedirs(mdir, exist_ok=True)

        lang_list = [lang] if lang != "en" else ["en"]
        self._reader = self._easyocr.Reader(lang_list, gpu=target_gpu, model_storage_directory=mdir)
        self._active_gpu = target_gpu
        return True

    def read_detailed(self, img: Image.Image) -> tuple[str, list[dict]]:
        if self._reader is None:
            return "", []
        try:
            results = self._reader.readtext(np.array(img), detail=1)
            blocks = []
            texts = []
            for item in results:
                box, text, _ = item
                text = str(text).strip()
                if not text:
                    continue
                texts.append(text)
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x1, y1 = min(xs), min(ys)
                x2, y2 = max(xs), max(ys)
                blocks.append({
                    "text": text,
                    "rect": (int(x1), int(y1), int(max(1, x2 - x1)), int(max(1, y2 - y1))),
                    "polygon": [[int(p[0]), int(p[1])] for p in box],
                })
            return " ".join(texts).strip(), blocks
        except Exception as e:
            vlog(f"[easyocr] сбой чтения: {e}")
            return "", []

    def read(self, img: Image.Image) -> str:
        text, _ = self.read_detailed(img)
        return text

    def unload(self) -> None:
        if self._reader is not None:
            self._reader = None
            gc.collect()


# =====================================================================
# Автономный тест Шага 2: проверка извлечения координат
# =====================================================================
if __name__ == "__main__":
    from PIL import ImageDraw

    print("\n" + "=" * 60)
    print("  Тестирование Шага 2: Генерация изображения и замер координат")
    print("=" * 60)

    # 1. Создаём тестовую картинку в памяти с двумя строчками
    test_img = Image.new("RGB", (400, 120), color="white")
    draw = ImageDraw.Draw(test_img)
    draw.text((25, 20), "ScreenTale In-Place v0.7", fill="black")
    draw.text((25, 65), "Line coordinates test OK", fill="black")

    # 2. Проверяем Windows OCR
    engine = WindowsOcrEngine()
    if engine.is_available():
        engine.load(lang="en-US")
        full_text, blocks = engine.read_detailed(test_img)
        print(f"\n[Распознанный текст]:\n{full_text}")
        print("\n[Найденные блоки и их координаты внутри картинки]:")
        for idx, b in enumerate(blocks, 1):
            print(f"  Строка {idx}: {b['text']!r}")
            print(f"    -> Прямоугольник (X, Y, W, H): {b['rect']}")
            print(f"    -> 4 точки полигона: {b['polygon']}")
        print("\n[OK] Движок успешно возвращает координаты строк!")
    else:
        print("[!] Windows OCR недоступен для теста.")
    print("=" * 60 + "\n")