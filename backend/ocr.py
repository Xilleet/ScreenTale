"""OCR-воркер в отдельном QThread.

Поддерживает стратегии распознавания (Windows OCR / EasyOCR)
и выполняет захват и обработку экрана без блокировки интерфейса.
"""
import queue
import re
import time

from PIL import Image, ImageGrab
from PySide6.QtCore import QPoint, QThread, Signal
from PySide6.QtWidgets import QApplication

from backend.languages import get_win_ocr_tag
from backend.logging_setup import vlog
from backend.ocr_engines import (
    BaseOcrEngine,
    EasyOcrEngine,
    RapidOcrEngine,
    WindowsOcrEngine,
)


def get_screen_scale(bbox: tuple | None = None) -> float:
    """Возвращает точный масштаб экрана (devicePixelRatio) для монитора, на котором находится рамка.

    Решает проблему мультимониторов с разным DPI (например, 4K 150% + 1080p 100%).
    """
    try:
        app = QApplication.instance()
        if not app:
            return 1.0

        if bbox is not None and len(bbox) == 4:
            left, top, right, bottom = bbox
            # Берем центр выделенной области
            cx = int((left + right) / 2)
            cy = int((top + bottom) / 2)
            screen = app.screenAt(QPoint(cx, cy))
            if screen is not None:
                return float(screen.devicePixelRatio())

        # Fallback: берем экран, на котором сейчас фокус или основной монитор
        primary = app.primaryScreen()
        if primary is not None:
            return float(primary.devicePixelRatio())
    except Exception:
        pass

    return 1.0

def normalize_ocr_text(text: str) -> str:
    r"""Комплексная очистка и нормализация OCR-текста перед переводом.

    - Склейка разорванных дефисом слов на стыке строк (infor-\nmation -> information).
    - Замена одиночных \n на пробелы (сохраняет контекст для Opus-MT).
    - Нормализация кавычек, апострофов и тире к стандартным ASCII-символам.
    - Исправление типовой ошибки OCR O->0 в числах.
    """
    def _merge_line_breaks(match):
        # Если вокруг переноса CJK-иероглифы (диапазон \u4e00-\u9fff) или кана
        # (\u3040-\u30ff) — склеиваем без пробела (японский/китайский не
        # используют пробелы внутри слов). Hangul (корейский, \uac00-\ud7af)
        # в pattern НЕ включаем — корейскому пробел между словами нужен.
        cjk_pattern = r"[\u4e00-\u9fff\u3040-\u30ff]"
        before = match.string[match.start() - 1] if match.start() > 0 else ""
        after = match.string[match.end()] if match.end() < len(match.string) else ""
        if re.match(cjk_pattern, before) and re.match(cjk_pattern, after):
            return ""
        return " "

    if not text or not text.strip():
        return ""

    # 1. Склейка слов, разорванных переносом строки: "trans-\nlation" -> "translation"
    text = re.sub(r"(\w+)-\s*\n\s*(\w+)", r"\1\2", text)

    # 2. Одиночные \r\n или \n заменяем на пробел, сохраняя двойные \n\n (абзацы).
    #    Используем _merge_line_breaks, чтобы CJK-иероглифы склеивались без
    #    пробела (記\n憶 → 記憶, не «記 憶»). Регрессия: см. tests/test_ocr.py.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<!\n)\n(?!\n)", _merge_line_breaks, text)

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

def _preprocess_for_ocr(img: Image.Image) -> tuple[Image.Image, float]:
    """Адаптивное увеличение картинки фильтром Ланцоша для четкости мелких шрифтов.
    Возвращает (обработанная_картинка, коэффициент_апскейла).
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        return img, 1.0

    # 1. Защита от минимального размера Windows OCR (WinRT требует минимум 40x40 px)
    if w < 40 or h < 40:
        scale_min = max(40 / w, 40 / h) * 1.2
        new_size = (int(w * scale_min), int(h * scale_min))
        return img.resize(new_size, Image.Resampling.LANCZOS), scale_min

    # 2. Адаптивный апскейл для мелких шрифтов
    if h <= 120:
        scale = 2.0
    elif h <= 250:
        scale = 1.5
    else:
        return img, 1.0

    new_size = (int(w * scale), int(h * scale))
    return img.resize(new_size, Image.Resampling.LANCZOS), scale


class OcrWorker(QThread):
    # статус для StatusPill: (state, text); state: ok/busy/off/error
    state_changed = Signal(str, str)
    # NOTE: cuda_status был объявлен, но ни разу не эмитился и ни разу не подключался.
    # Если понадобится отдельный сигнал о доступности CUDA — вернуть вместе с
    # реальным emit в _do_set_gpu и подключением в AppController.
    # результат чтения: (bbox_выделения, распознанный_текст, список_блоков_с_координатами, контекст)
    read_result = Signal(tuple, str, list, str)
    # итог переключения GPU: (success, фактически_работает_на_gpu, сообщение)
    gpu_result = Signal(bool, bool, str)
    # событие смены активного движка
    engine_changed = Signal(str)

    def __init__(
        self,
        use_gpu: bool = False,
        preferred_engine: str = "windows",
        preferred_direction: str = "horizontal",
        src_lang: str = "en",
    ):
        super().__init__()
        self._requested_gpu = bool(use_gpu)
        self._preferred_engine = preferred_engine
        self._preferred_direction = preferred_direction
        self._src_lang = src_lang
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

    def request_language(self, src_lang: str) -> None:
        """Сменить язык распознавания OCR на лету."""
        self._tasks.put(("set_language", str(src_lang)))

    def stop(self) -> None:
        """Остановить поток воркера."""
        self._stop_flag = True
        self._tasks.put(("stop",))
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    # ---------- внутренности (поток воркера) ----------
    def run(self) -> None:
        # Выбираем и загружаем начальный движок (без тяжелого фонового импорта torch)
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
            elif kind == "set_direction":
                self._do_set_direction(task[1])
            elif kind == "set_language":
                self._do_set_language(task[1])

        if self._engine is not None:
            self._engine.unload()   

    def _do_set_language(self, src_lang: str) -> None:
        # Любая ошибка внутри (нет пакета Windows OCR, EasyOCR не принимает BCP-47,
        # RapidOCR не установлен) раньше убивала QThread молча — UI зависал на
        # «OCR-модель ещё не готова». Ловим и сообщаем через state_changed("error").
        try:
            self._src_lang = src_lang
            win_tag = get_win_ocr_tag(src_lang)

            # Если юзер предпочитает Windows OCR — пытаемся переключить на него.
            # Если пакета нет — откатываемся на RapidOCR (с уведомлением).
            if self._preferred_engine == "windows":
                win_engine = WindowsOcrEngine(default_lang=win_tag)
                res = win_engine.check_language_support(win_tag)
                ok = res[0] if (isinstance(res, tuple) and len(res) == 2) else bool(res)

                if ok:
                    # Язык поддерживается Windows — переключаемся на нативный OCR
                    if self._engine is not None:
                        self._engine.unload()
                    win_engine.load(lang=win_tag)
                    self._engine = win_engine
                    self._active_engine_name = "windows"
                    self.state_changed.emit("ok", f"Windows OCR: активен ({win_tag})")
                    self.engine_changed.emit("windows")
                    print(f"[ocr] Язык Windows OCR переключен на: {win_tag}")
                    return

                # Пакета нет в Windows — откат на RapidOCR.
                # ВАЖНО: если мы уже на RapidOCR — не делаем лишний reinit
                # (RapidOcrEngine.load() игнорирует параметр lang, ре-инит бесполезен).
                # Регрессия: см. tests/test_ocr_worker.py::test_do_set_language_no_dead_branch.
                print(f"[ocr] В Windows нет пакета OCR [{win_tag}] -> авто-переход на RapidOCR!")
                if self._active_engine_name != "rapidocr":
                    rapid_engine = RapidOcrEngine(direction=self._preferred_direction)
                    if rapid_engine.is_available():
                        rapid_engine.load()
                        if self._engine is not None:
                            self._engine.unload()
                        self._engine = rapid_engine
                        self._active_engine_name = "rapidocr"
                        self.state_changed.emit("ok", "RapidOCR: активен (авто-переход)")
                        self.engine_changed.emit("rapidocr")
                    else:
                        self.state_changed.emit(
                            "error", "RapidOCR недоступен — невозможно переключиться"
                        )
                    return
                # Уже на RapidOCR — язык не влияет, ничего не делаем.
                return

            # Если preferred_engine != "windows" — просто передаём lang в активный движок.
            if self._engine is not None and hasattr(self._engine, "load"):
                # EasyOCR принимает ISO 639-1 ("ja", "en", "ch_sim", "ko"), а не BCP-47.
                # Раньше сюда передавался win_tag ("ja-JP", "zh-CN") — Reader(...) падал.
                # Для движков, у которых engine_id == "easyocr", отправляем bare src_lang.
                engine_id = getattr(self._engine, "engine_id", "")
                lang_for_engine = src_lang if engine_id == "easyocr" else win_tag
                self._engine.load(lang=lang_for_engine)
        except Exception as e:
            print(f"[ocr] _do_set_language failed: {e}")
            self.state_changed.emit("error", f"Сбой смены языка OCR: {e}")

    def _do_set_direction(self, direction: str) -> None:
        self._preferred_direction = direction
        if hasattr(self._engine, "set_direction"):
            self._engine.set_direction(direction)
        print(f"[ocr] Направление текста переключено на: {direction}")

    def _init_engine(self, engine_name: str, use_gpu: bool) -> None:
        """Инициализация стратегии распознавания."""
        # Синхронизируем предпочтение: ранее request_engine() менял активный
        # движок через _init_engine, но _preferred_engine оставался устаревшим
        # (по умолчанию "windows"). В результате каждая смена языка перепроверяла
        # Windows OCR даже если пользователь явно выбрал RapidOCR/EasyOCR.
        self._preferred_engine = engine_name
        self.state_changed.emit("busy", f"Загрузка {engine_name} OCR…")
        if self._engine is not None:
            self._engine.unload()

        # 1. Нативный Windows OCR
        if engine_name == "windows":
            win_tag = get_win_ocr_tag(self._src_lang)
            win_engine = WindowsOcrEngine(default_lang=win_tag)
            if win_engine.is_available():
                res = win_engine.check_language_support(win_tag)
                ok = res[0] if (isinstance(res, tuple) and len(res) == 2) else bool(res)
                if ok:
                    win_engine.load(lang=win_tag)
                    self._engine = win_engine
                    self._active_engine_name = "windows"
                    self.state_changed.emit("ok", f"Windows OCR: активен ({win_tag})")
                    self.gpu_result.emit(True, False, "Windows OCR работает нативно в ОС")
                    self.engine_changed.emit("windows")
                    print(f"[ocr] Windows OCR успешно инициализирован ({win_tag})")
                    return
                else:
                    print(f"[ocr] Windows OCR: нет пакета [{win_tag}], авто-откат на RapidOCR")
            engine_name = "rapidocr"

        # 2. Легковесный RapidOCR (ONNX / Азия / Tategaki)
        if engine_name == "rapidocr":
            rapid_engine = RapidOcrEngine(direction=self._preferred_direction)
            if rapid_engine.is_available():
                rapid_engine.load()
                self._engine = rapid_engine
                self._active_engine_name = "rapidocr"
                self.state_changed.emit("ok", "RapidOCR: активен (ONNX)")
                self.gpu_result.emit(True, False, "RapidOCR работает на ONNX Runtime")
                self.engine_changed.emit("rapidocr")
                print("[ocr] RapidOCR успешно инициализирован")
                return
            print("[ocr] RapidOCR недоступен, откат на EasyOCR")
            engine_name = "easyocr"

        # 3. Запасной EasyOCR
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
                bbox, "[OCR-модель ещё не готова, подождите несколько секунд]", [], context
            )
            return

        try:
            left, top, right, bottom = bbox
            dpi_scale = get_screen_scale(bbox)
            physical = (
                int(left * dpi_scale),
                int(top * dpi_scale),
                int(right * dpi_scale),
                int(bottom * dpi_scale),
            )
            # 1. Захват экрана (all_screens=True для мультимониторов)
            img = ImageGrab.grab(bbox=physical, all_screens=True)

            # 2. Адаптивная подготовка размера кадра
            img, prep_scale = _preprocess_for_ocr(img)

            # 3. Распознавание с замером времени
            t_start = time.perf_counter()
            # Все движки наследуют read_detailed от BaseOcrEngine (конкретный метод,
            # не абстрактный). Раньше тут был hasattr-check с dead-else — убрали.
            raw_text, raw_blocks = self._engine.read_detailed(img)
            latency_ms = (time.perf_counter() - t_start) * 1000

            text = normalize_ocr_text(raw_text)

            # 4. Пересчитываем локальные координаты строк в АБСОЛЮТНЫЕ экранные пиксели монитора
            total_scale = prep_scale * dpi_scale
            screen_blocks = []
            for b in raw_blocks:
                rx, ry, rw, rh = b["rect"]
                abs_rect = (
                    int(left + rx / total_scale),
                    int(top + ry / total_scale),
                    int(rw / total_scale),
                    int(rh / total_scale),
                )
                abs_poly = [
                    [int(left + p[0] / total_scale), int(top + p[1] / total_scale)]
                    for p in b.get("polygon", [])
                ]
                screen_blocks.append({
                    "text": b["text"],
                    "rect": abs_rect,
                    "polygon": abs_poly,
                })

            print(
                f"[ocr:{self._active_engine_name}] {latency_ms:.1f} ms | "
                f"{text[:60]!r} (найдено строк с координатами: {len(screen_blocks)})"
            )
            vlog(
                f"[ocr:{self._active_engine_name}] (full) {latency_ms:.1f} ms | "
                f"{text!r} (ctx={context})"
            )

            result_text = text if text.strip() else "[Текст не найден]"
            self.read_result.emit(bbox, result_text, screen_blocks, context)

        except Exception as e:
            print(f"[ocr] ошибка чтения: {e}")
            self.read_result.emit(bbox, f"[Ошибка OCR: {e}]", [], context)

    def request_direction(self, direction: str) -> None:
        """Сменить направление текста ('horizontal' или 'vertical')."""
        self._tasks.put(("set_direction", str(direction)))