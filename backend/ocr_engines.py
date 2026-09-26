"""Стратегии OCR-движков для ScreenTale.

Базовый класс BaseOcrEngine и реализации:
- WindowsOcrEngine (WinRT API, нативный, 10–25 мс, 0 МБ VRAM)
- EasyOcrEngine (EasyOCR / PyTorch, запасной)
"""
import gc
import os
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

# winocr уже содержит импортированные OcrEngine и Language из WinRT
WinrtOcrEngine = getattr(winocr, "OcrEngine", None)
Language = getattr(winocr, "Language", None)
HAS_WINRT = WinrtOcrEngine is not None and Language is not None


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
        """Возвращает список языковых тегов OCR, установленных в Windows (например, ['en-US', 'ja-JP'])."""
        if not HAS_WINRT or WinrtOcrEngine is None:
            return ["en-US"]
        try:
            langs = WinrtOcrEngine.available_recognizer_languages
            return [l.language_tag for l in langs]
        except Exception as e:
            vlog(f"[windows_ocr] ошибка получения языков: {e}")
            return []

    def check_language_support(self, lang: str) -> tuple[bool, str]:
        """Проверяет наличие системного языкового пакета OCR.

        Если язык не установлен, возвращает команду PowerShell для его добавления.
        """
        if not HAS_WINRT or WinrtOcrEngine is None or Language is None:
            return True, ""
        try:
            tag = lang if "-" in lang else ("ja-JP" if lang == "ja" else "en-US")
            win_lang = Language(tag)
            supported = WinrtOcrEngine.is_language_supported(win_lang)
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
        self.lang = lang
        ok, msg = self.check_language_support(self.lang)
        if not ok:
            print(f"[windows_ocr] ПРЕДУПРЕЖДЕНИЕ:\n{msg}")
        self._is_ready = True
        return True

    def read(self, img: Image.Image) -> str:
        if not self._is_ready:
            return ""
        if not HAS_WINOCR or winocr is None:
            return "[Ошибка: winocr не установлен]"

        lang_tag = self.lang
        try:
            result = winocr.recognize_pil_sync(img, lang=lang_tag)
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            return text.strip()
        except Exception as e:
            vlog(f"[windows_ocr] сбой чтения: {e}")
            raise

    def unload(self) -> None:
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
        if self._reader is not None:
            self._reader = None
            gc.collect()
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()