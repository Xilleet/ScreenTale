"""OCR-воркер: EasyOCR в отдельном QThread.

Загрузка модели, перезагрузка при смене GPU и чтение текста выполняются
в одном потоке через очередь задач. UI общается с воркером только сигналами.
"""
import ctypes
import gc
import os
import queue
import re

from PIL import Image
from PySide6.QtCore import QThread, Signal

from backend.config import get_app_dir
from backend.logging_setup import vlog


def get_screen_scale() -> float:
    """Масштаб экрана: логические -> физические пиксели."""
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        return 1.0


def fix_ocr_glitches(text: str) -> str:
    r"""Правка типовой ошибки распознавания: O→0 в числах.

    Предыдущая версия (v0.4.0) портила нормальный текст — правило
    `\BI[согласная]` с re.IGNORECASE превращало i внутри слов в l:
      rapid → rapld, artificial → artlflcial, intelligence → intelllgence,
      changing → changlng, available → avallable, will → wlll и т.д.
    Это ломало перевод: NLLB получал мусор и галлюцинировал.

    Правила для I/l удалены — EasyOCR на en-модели nowadays достаточно
    точен, ручная правка регулярками чаще вредит, чем помогает.

    Оставляем только безопасное правило: O в составе числа → 0.
    Ловит все позиции: O12, 2O2, 12O, O2OO, 2O2O — все превратятся в 012,
    202, 120, 0200, 2020. Слова без цифр (Open, Oranges, Hello) не трогает.
    """
    if not text:
        return text
    # Последовательность из [O или цифра] с хотя бы одной цифрой — это
    # число с возможными OCR-ошибками O вместо 0. Заменяем все O на 0.
    def _replace_o(match):
        return match.group(0).replace("O", "0")
    return re.sub(r"\b[O\d]*\d[O\d]*\b", _replace_o, text)

def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Адаптивное увеличение картинки фильтром Ланцоша для четкости мелких шрифтов."""
    if img.height <= 120:
        scale = 2.5
    elif img.height <= 300:
        scale = 2.0
    elif img.height <= 500:
        scale = 1.5
    else:
        return img

    new_size = (int(img.width * scale), int(img.height * scale))
    return img.resize(new_size, Image.Resampling.LANCZOS)

class OcrWorker(QThread):
    # статус для StatusPill: (state, text); state: ok/busy/off/error
    state_changed = Signal(str, str)
    # доступность CUDA (проверяется в фоновом потоке после импорта torch)
    cuda_status = Signal(bool)
    # результат чтения: (bbox, текст)
    # было:  read_result = Signal(tuple, str)
    read_result = Signal(tuple, str, str)   # bbox, текст, контекст ("single"/"auto")
    # итог переключения GPU: (success, фактически_работает_на_gpu, сообщение)
    gpu_result = Signal(bool, bool, str)

    def __init__(self, use_gpu: bool):
        super().__init__()
        self._requested_gpu = bool(use_gpu)
        self._active_gpu = False
        self._reader = None
        self._easyocr = None
        self._torch = None
        self._tasks = queue.Queue()
        self._stop_flag = False

    # ---------- публичный API (безопасно звать из любого потока) ----------
    def read(self, bbox, context="single"):
        self._tasks.put(("read", bbox, context))

    def request_gpu(self, use_gpu: bool):
        """Переключить OCR на GPU/CPU (перезагрузка модели)."""
        self._tasks.put(("set_gpu", bool(use_gpu)))

    def stop(self):
        """Остановить поток. Блокируется максимум на ~3 c."""
        self._stop_flag = True
        self._tasks.put(("stop",))
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    # ---------- внутренности (выполняются в потоке воркера) ----------
    def run(self):
        # Тяжёлые библиотеки импортируем здесь, чтобы не тормозить старт GUI
        try:
            import easyocr
            import torch
            self._easyocr = easyocr
            self._torch = torch
        except Exception as e:
            self.state_changed.emit("error", f"OCR-библиотеки недоступны: {e}")
            self.cuda_status.emit(False)

        cuda_available = bool(self._torch and self._torch.cuda.is_available())
        self.cuda_status.emit(cuda_available)

        if self._easyocr is not None:
            self._load_model(self._requested_gpu and cuda_available)

        while not self._stop_flag:
            try:
                task = self._tasks.get(timeout=0.3)
            except queue.Empty:
                continue
            kind = task[0]
            if kind == "stop":
                break
            if kind == "read":
                self._do_read(task[1], task[2])
            elif kind == "set_gpu":
                self._do_set_gpu(task[1])

    def _drop_reader(self):
        if self._reader is not None:
            self._reader = None
            gc.collect()
            if self._torch is not None and self._torch.cuda.is_available():
                self._torch.cuda.empty_cache()

    def _load_model(self, use_gpu: bool):
        self.state_changed.emit("busy", "Загрузка OCR-модели…")
        try:
            self._drop_reader()
            # Модели EasyOCR — портативно, рядом с приложением
            # (по умолчанию EasyOCR пишет в C:\Users\<юзер>\.EasyOCR)
            mdir = os.path.join(get_app_dir(), "easyocr_models")
            os.makedirs(mdir, exist_ok=True)
            self._reader = self._easyocr.Reader(["en"], gpu=use_gpu,
                                                model_storage_directory=mdir)
            self._active_gpu = use_gpu
            self.state_changed.emit(
                "ok" if use_gpu else "off",
                "GPU: ускорение активно" if use_gpu else "CPU: стандартный режим")
            self.gpu_result.emit(True, use_gpu, "")
        except Exception as e:
            self.state_changed.emit("error", f"Ошибка загрузки OCR: {e}")
            self.gpu_result.emit(False, False, str(e))

    def _do_set_gpu(self, use_gpu: bool):
        # уже в этом режиме — тихо игнорируем (защита от повторных триггеров)
        if self._reader is not None and use_gpu == self._active_gpu:
            return
        cuda_available = bool(self._torch and self._torch.cuda.is_available())
        if use_gpu and not cuda_available:
            self.gpu_result.emit(False, False, "CUDA недоступна")
            return

        self.state_changed.emit("busy", "Переключение режима OCR…")
        try:
            self._drop_reader()
            self._reader = self._easyocr.Reader(["en"], gpu=use_gpu)
            self._active_gpu = use_gpu
            self.state_changed.emit(
                "ok" if use_gpu else "off",
                "GPU: ускорение активно" if use_gpu else "CPU: стандартный режим")
            self.gpu_result.emit(True, use_gpu, "")
        except Exception as e:
            # откат на CPU, чтобы OCR не умер совсем
            try:
                self._reader = self._easyocr.Reader(["en"], gpu=False)
                self._active_gpu = False
                self.state_changed.emit("off", "CPU: стандартный режим (после ошибки GPU)")
                self.gpu_result.emit(False, False, str(e))
            except Exception as e2:
                self.state_changed.emit("error", f"Ошибка OCR: {e2}")
                self.gpu_result.emit(False, False, str(e2))

    def _do_read(self, bbox, context):
        if self._reader is None:
            self.read_result.emit(bbox, "[OCR-модель ещё не готова, попробуйте через несколько секунд]", context)
            return
        try:
            import numpy as np
            from PIL import ImageGrab

            left, top, right, bottom = bbox
            scale = get_screen_scale()
            physical = (int(left * scale), int(top * scale),
                        int(right * scale), int(bottom * scale))
            
            # 1. Захватываем область
            img = ImageGrab.grab(bbox=physical)
            
            # 2. Апскейлим картинку (для твоих 1307x184 увеличит в 2 раза)
            img = _preprocess_for_ocr(img)

            # 3. Отдаем в EasyOCR уже четкую увеличенную картинку
            results = self._reader.readtext(np.array(img), detail=0, paragraph=True)
            text = fix_ocr_glitches(" ".join(results))
            
            print(f"[ocr] прочитано: {text[:60]!r} (ctx={context})")
            vlog(f"[ocr] прочитано (full): {text!r} (ctx={context})")
            self.read_result.emit(bbox, text if text.strip() else "[Текст не найден]", context)
        except Exception as e:
            self.read_result.emit(bbox, f"[Ошибка OCR: {e}]", context)