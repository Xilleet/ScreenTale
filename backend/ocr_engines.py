"""Стратегии OCR-движков для ScreenTale.

Базовый класс BaseOcrEngine и реализации:
- WindowsOcrEngine (WinRT API, нативный, 10–25 мс, 0 МБ VRAM)
- EasyOcrEngine (EasyOCR / PyTorch, запасной)
"""
import gc
import os
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

# Глобальный мьютекс для безопасной работы с WinRT COM из любых потоков
_winrt_lock = threading.Lock()
_lang_support_cache: dict[str, bool] = {}

class BaseOcrEngine(ABC):
    """Абстрактный интерфейс OCR-движка."""

    engine_id: str = "base"
    display_name: str = "Base OCR"

    @abstractmethod
    def is_available(self) -> bool:
        """Доступен ли движок в текущей системе (установлены ли библиотеки/пакеты)."""
        ...

    @abstractmethod
    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        """Инициализация или загрузка весов/контекста движка."""
        ...

    @abstractmethod
    def read(self, img: Image.Image) -> str:
        """Распознавание текста с PIL Image."""
        ...

    @abstractmethod
    def unload(self) -> None:
        """Освобождение памяти и дескрипторов."""
        ...

    def get_installed_languages(self) -> list[str]:
        """Список доступных языков для этого движка."""
        return ["en"]

    def check_language_support(self, lang: str) -> tuple[bool, str]:
        """Проверить, поддерживается ли язык. Возвращает (ok, сообщение_с_инструкцией)."""
        return True, ""

# =====================================================================
# 1. Windows Native OCR (WinRT API)
# =====================================================================
class WindowsOcrEngine(BaseOcrEngine):
    """Нативный системный OCR Windows 10/11 через Windows.Media.Ocr."""

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
                return [l.language_tag for l in langs]
            except Exception as e:
                vlog(f"[windows_ocr] ошибка получения языков: {e}")
                return []

    def check_language_support(self, lang: str) -> tuple[bool, str]:
        """Проверяет наличие системного языкового пакета OCR с кэшированием."""
        if not HAS_WINRT or WinrtOcrEngine is None or Language is None:
            return True, ""

        tag = lang if "-" in lang else ("ja-JP" if lang == "ja" else "en-US")
        
        # 1. Быстрый ответ из кэша (без дергания WinRT COM)
        if tag in _lang_support_cache:
            if _lang_support_cache[tag]:
                return True, ""
            cmd = f'Add-WindowsCapability -Online -Name "Language.OCR~~~{tag}~0.0.1.0"'
            return False, f"В Windows не установлен пакет OCR для [{tag}].\nКоманда: {cmd}"

        with _winrt_lock:
            try:
                win_lang = Language(tag)
                supported = WinrtOcrEngine.is_language_supported(win_lang)
                _lang_support_cache[tag] = bool(supported)
                if not supported:
                    cmd = f'Add-WindowsCapability -Online -Name "Language.OCR~~~{tag}~0.0.1.0"'
                    msg = (
                        f"В Windows не установлен языковой пакет OCR для [{tag}].\n"
                        "Установите его в: Параметры Windows -> Время и язык -> Язык,\n"
                        f"либо выполните в PowerShell от админа:\n{cmd}"
                    )
                    return False, msg
                return True, ""
            except Exception as e:
                return False, f"Ошибка проверки языка Windows OCR: {e}"

    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        with _winrt_lock:
            self.lang = lang
            ok, msg = self.check_language_support(self.lang)
            if not ok:
                print(f"[windows_ocr] ПРЕДУПРЕЖДЕНИЕ:\n{msg}")
                self._is_ready = False
                return False
            self._is_ready = True
            return True

    def read(self, img: Image.Image) -> str:
        if not self._is_ready:
            return f"[В Windows не установлен пакет OCR для {self.lang}]"
        if not HAS_WINOCR or winocr is None:
            return "[Ошибка: winocr не установлен]"

        # Обязательно под мьютексом для защиты от нативного краша WinRT COM!
        with _winrt_lock:
            lang_tag = self.lang
            try:
                result = winocr.recognize_pil_sync(img, lang=lang_tag)
                text = result.get("text", "") if isinstance(result, dict) else str(result)
                return text.strip()
            except Exception as e:
                vlog(f"[windows_ocr] сбой чтения: {e}")
                # Мягкий перехват вместо падения
                return f"[Ошибка Windows OCR ({lang_tag}): {e}]"

    def unload(self) -> None:
        with _winrt_lock:
            self._is_ready = False


# =====================================================================
# 2. EasyOCR Engine (PyTorch / GPU / CPU)
# =====================================================================
class EasyOcrEngine(BaseOcrEngine):
    """EasyOCR на базе PyTorch (тяжёлый, универсальный запасной движок)."""

    engine_id: str = "easyocr"
    display_name: str = "EasyOCR (PyTorch)"

    def __init__(self):
        self._reader = None
        self._easyocr = None
        self._torch = None
        self._active_gpu = False

    def is_available(self) -> bool:
        try:
            import easyocr  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self, use_gpu: bool = False, lang: str = "en") -> bool:
        with _winrt_lock:
            self.lang = lang
            ok, msg = self.check_language_support(self.lang)
            if not ok:
                print(f"[windows_ocr] ПРЕДУПРЕЖДЕНИЕ:\n{msg}")
                self._is_ready = False
                return False
            self._is_ready = True
            return True
        import easyocr
        import torch
        self._easyocr = easyocr
        self._torch = torch

        cuda_ok = bool(torch.cuda.is_available())
        target_gpu = bool(use_gpu and cuda_ok)

        self.unload()
        mdir = os.path.join(get_data_dir(), "easyocr_models")
        os.makedirs(mdir, exist_ok=True)

        lang_list = [lang] if lang != "en" else ["en"]
        self._reader = self._easyocr.Reader(lang_list, gpu=target_gpu, model_storage_directory=mdir)
        self._active_gpu = target_gpu
        return True

    def read(self, img: Image.Image) -> str:
        if self._reader is None:
            return ""
        results = self._reader.readtext(np.array(img), detail=0, paragraph=True)
        return " ".join(results).strip()

    def unload(self) -> None:
        with _winrt_lock:
            self._is_ready = False
        if self._reader is not None:
            self._reader = None
            gc.collect()
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()

# =====================================================================
# 3. RapidOCR Engine (ONNX Runtime / Азия / Tategaki)
# =====================================================================
class RapidOcrEngine(BaseOcrEngine):
    """Легковесный C++ OCR на ONNX Runtime для стилизованных шрифтов, иероглифов и манги."""

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
            # Инициализация легковесного ONNX-рантайма
            self._engine = RapidOCR()
            return True
        except Exception as e:
            vlog(f"[rapidocr] сбой загрузки: {e}")
            return False

    def set_direction(self, direction: str) -> None:
        """Направление чтения: 'horizontal' (горизонтальное) или 'vertical' (Tategaki)."""
        self.direction = direction

    def read(self, img: Image.Image, direction: str | None = None) -> str:
        if self._engine is None:
            return ""

        dir_mode = direction or self.direction
        img_np = np.array(img.convert("RGB"))

        try:
            result, _ = self._engine(img_np)
            if not result:
                return ""

            # Режим Tategaki: сортировка японских столбцов СПРАВА НАЛЕВО, СВЕРХУ ВНИЗ
            if dir_mode == "vertical":
                return self._sort_tategaki(result)

            # Стандартный горизонтальный режим
            lines = [item[1] for item in result if item and len(item) > 1]
            return " ".join(lines).strip()
        except Exception as e:
            vlog(f"[rapidocr] сбой распознавания: {e}")
            return ""

    def _sort_tategaki(self, result: list) -> str:
        """Геометрическая группировка столбцов для манги: справа налево, сверху вниз."""
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

        # 1. Сортируем блоки справа налево (-cx)
        boxes_with_text.sort(key=lambda b: -b["cx"])

        # 2. Группируем блоки в вертикальные столбцы
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

        # 3. Внутри каждого столбца сортируем сверху вниз (cy)
        sorted_texts = []
        for col in columns:
            col.sort(key=lambda b: b["cy"])
            # В японском/китайском иероглифы внутри столбца склеиваются без пробелов:
            column_text = "".join(item["text"] for item in col)
            sorted_texts.append(column_text)

        return "\n".join(sorted_texts).strip()

    def unload(self) -> None:
        self._engine = None