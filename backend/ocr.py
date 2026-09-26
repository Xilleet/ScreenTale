"""OCR-воркер в отдельном QThread.

Поддерживает стратегии распознавания (Windows OCR / EasyOCR)
и выполняет захват и обработку экрана без блокировки интерфейса.
"""
import ctypes
import queue
import re
import time

from PIL import Image, ImageGrab
from PySide6.QtCore import QThread, Signal

from backend.logging_setup import vlog
from backend.ocr_engines import BaseOcrEngine, EasyOcrEngine, WindowsOcrEngine


def get_screen_scale() -> float:
    """Масштаб экрана: логические -> физические пиксели."""
    try:
        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
    except Exception:
        return 1.0

def normalize_ocr_text(text: str) -> str:
    r"""Комплексная очистка и нормализация OCR-текста перед переводом.

    - Склейка разорванных дефисом слов на стыке строк (infor-\nmation -> information).
    - Замена одиночных \n на пробелы (сохраняет контекст для Opus-MT).
    - Нормализация кавычек, апострофов и тире к стандартным ASCII-символам.
    - Исправление типовой ошибки OCR O->0 в числах.
    """
    if not text or not text.strip():
        return ""

    # 1. Склейка слов, разорванных переносом строки: "trans-\nlation" -> "translation"
    text = re.sub(r"(\w+)-\s*\n\s*(\w+)", r"\1\2", text)

    # 2. Одиночные \r\n или \n заменяем на пробел, сохраняя двойные \n\n (абзацы)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)

    # 3. Нормализация кавычек и апострофов к стандарту
    quote_map = {
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        "’": "'", "‘": "'", "`": "'", "‚": "'", "‛": "'",
    }
    for bad, good in quote_map.items():
        text = text.replace(bad, good)

    # 4. Нормализация тире и дублирующихся дефисов
    text = re.sub(r"[—–]", " - ", text)
    text = re.sub(r"-{2,}", " - ", text)

    # 5. Правка типовой ошибки OCR: буква O вместо 0 в числах (2O24 -> 2024)
    def _replace_o(match):
        return match.group(0).replace("O", "0")

    text = re.sub(r"\b[O\d]*\d[O\d]*\b", _replace_o, text)

    # 6. Схлопывание лишних пробелов внутри строк
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """Адаптивное увеличение картинки фильтром Ланцоша для четкости мелких шрифтов.

    Также защищает от ограничения WinRT API (минимальный размер кадра 40x40).
    """
    w, h = img.size
    # 1. Защита от минимального размера Windows OCR (WinRT требует минимум 40x40 px)
    if w < 40 or h < 40:
        scale_min = max(40 / max(w, 1), 40 / max(h, 1)) * 1.2
        new_size = (int(w * scale_min), int(h * scale_min))
        return img.resize(new_size, Image.Resampling.LANCZOS)

    # 2. Адаптивный апскейл для мелких шрифтов
    if h <= 120:
        scale = 2.0
    elif h <= 250:
        scale = 1.5
    else:
        return img

    new_size = (int(w * scale), int(h * scale))
    return img.resize(new_size, Image.Resampling.LANCZOS)


class OcrWorker(QThread):
    # статус для StatusPill: (state, text); state: ok/busy/off/error
    state_changed = Signal(str, str)
    # доступность CUDA
    cuda_status = Signal(bool)
    # результат чтения: (bbox, текст, контекст)
    read_result = Signal(tuple, str, str)
    # итог переключения GPU: (success, фактически_работает_на_gpu, сообщение)
    gpu_result = Signal(bool, bool, str)
    # событие смены активного движка
    engine_changed = Signal(str)

    def __init__(self, use_gpu: bool = False, preferred_engine: str = "windows"):
        super().__init__()
        self._requested_gpu = bool(use_gpu)
        self._preferred_engine = preferred_engine
        self._active_engine_name = "windows"
        self._engine: BaseOcrEngine | None = None
        self._tasks: queue.Queue = queue.Queue()
        self._stop_flag = False

    # ---------- публичный API (потокобезопасно) ----------
    def read(self, bbox: tuple, context: str = "single") -> None:
        self._tasks.put(("read", bbox, context))

    def request_gpu(self, use_gpu: bool) -> None:
        """Переключить режим GPU (актуально для EasyOCR)."""
        self._tasks.put(("set_gpu", bool(use_gpu)))

    def request_engine(self, engine_name: str) -> None:
        """Сменить OCR-движок на лету ('windows' или 'easyocr')."""
        self._tasks.put(("set_engine", str(engine_name)))

    def stop(self) -> None:
        """Остановить поток воркера."""
        self._stop_flag = True
        self._tasks.put(("stop",))
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    # ---------- внутренности (поток воркера) ----------
    def run(self) -> None:
        # Проверяем доступность CUDA для настроек
        cuda_available = False
        try:
            import torch

            cuda_available = bool(torch.cuda.is_available())
        except Exception:
            pass
        self.cuda_status.emit(cuda_available)

        # Выбираем и загружаем начальный движок
        self._init_engine(self._preferred_engine, self._requested_gpu)

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
            elif kind == "set_engine":
                self._init_engine(task[1], self._requested_gpu)

        if self._engine is not None:
            self._engine.unload()

    def _init_engine(self, engine_name: str, use_gpu: bool) -> None:
        """Инициализация стратегии распознавания."""
        self.state_changed.emit("busy", f"Загрузка {engine_name} OCR…")
        if self._engine is not None:
            self._engine.unload()

        # 1. Пробуем нативный Windows OCR
        if engine_name == "windows":
            win_engine = WindowsOcrEngine(default_lang="en")
            if win_engine.is_available():
                win_engine.load()
                self._engine = win_engine
                self._active_engine_name = "windows"
                self.state_changed.emit("ok", "Windows OCR: активен")
                self.gpu_result.emit(True, False, "Windows OCR работает нативно в ОС")
                self.engine_changed.emit("windows")
                print("[ocr] Windows OCR успешно инициализирован")
                return
            print("[ocr] Windows OCR недоступен, откат на EasyOCR")
            engine_name = "easyocr"

        # 2. Запасной EasyOCR
        easy_engine = EasyOcrEngine()
        if easy_engine.is_available():
            try:
                easy_engine.load(use_gpu=use_gpu, lang="en")
                self._engine = easy_engine
                self._active_engine_name = "easyocr"
                self.state_changed.emit(
                    "ok" if use_gpu else "off",
                    "EasyOCR: GPU ускорение" if use_gpu else "EasyOCR: CPU режим",
                )
                self.gpu_result.emit(True, use_gpu, "")
                self.engine_changed.emit("easyocr")
                print(f"[ocr] EasyOCR загружен (gpu={use_gpu})")
                return
            except Exception as e:
                self.state_changed.emit("error", f"Ошибка EasyOCR: {e}")
                self.gpu_result.emit(False, False, str(e))
                return

        self.state_changed.emit("error", "Нет доступных OCR-движков")

    def _do_set_gpu(self, use_gpu: bool) -> None:
        self._requested_gpu = use_gpu
        if self._active_engine_name == "easyocr" and self._engine is not None:
            self._init_engine("easyocr", use_gpu)
        else:
            # Для Windows OCR GPU не требуется
            self.gpu_result.emit(True, False, "Windows OCR использует нативные API Windows")

    def _do_read(self, bbox: tuple, context: str) -> None:
        if self._engine is None:
            self.read_result.emit(
                bbox, "[OCR-модель ещё не готова, подождите несколько секунд]", context
            )
            return

        try:
            left, top, right, bottom = bbox
            scale = get_screen_scale()
            physical = (
                int(left * scale),
                int(top * scale),
                int(right * scale),
                int(bottom * scale),
            )

            # 1. Захват экрана
            img = ImageGrab.grab(bbox=physical)

            # 2. Адаптивная подготовка размера кадра
            img = _preprocess_for_ocr(img)

            # 3. Замер реальной скорости распознавания
            t_start = time.perf_counter()
            raw_text = self._engine.read(img)
            latency_ms = (time.perf_counter() - t_start) * 1000

            text = normalize_ocr_text(raw_text)

            print(
                f"[ocr:{self._active_engine_name}] {latency_ms:.1f} ms | "
                f"{text[:60]!r} (ctx={context})"
            )
            vlog(
                f"[ocr:{self._active_engine_name}] (full) {latency_ms:.1f} ms | "
                f"{text!r} (ctx={context})"
            )

            result_text = text if text.strip() else "[Текст не найден]"
            self.read_result.emit(bbox, result_text, context)

        except Exception as e:
            print(f"[ocr] ошибка чтения: {e}")
            self.read_result.emit(bbox, f"[Ошибка OCR: {e}]", context)