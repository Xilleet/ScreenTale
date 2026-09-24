"""Движки перевода и менеджер локальных моделей (QThread)."""
import gc
import os
import queue
import re
import shutil
import threading
import time

from PySide6.QtCore import QThread, Signal
from tqdm import tqdm as tqdm_base

from backend.config import get_data_dir

ENGINE_GOOGLE = "google"
ENGINE_MYMEMORY = "mymemory"
ENGINE_OPUS = "opus"
ENGINE_NLLB = "nllb"
LOCAL_ENGINES = (ENGINE_OPUS, ENGINE_NLLB)

ENGINE_LABELS = {
    ENGINE_GOOGLE: "Google",
    ENGINE_MYMEMORY: "MyMemory",
    ENGINE_OPUS: "Opus-MT",
    ENGINE_NLLB: "NLLB-200",
}

_MODEL_SPECS = {
    ENGINE_OPUS: {"repo": "Helsinki-NLP/opus-mt-en-ru", "approx_size": "~300 МБ"},
    ENGINE_NLLB: {"repo": "facebook/nllb-200-distilled-600M", "approx_size": "~2.5 ГБ"},
}

_DEVNULL = open(os.devnull, "w")


# ============================================================
# Утилиты работы с кэшем и размерами
# ============================================================
def get_folder_size(path: str) -> int:
    """Быстрый подсчет размера папки в байтах через os.scandir."""
    total = 0
    if not os.path.exists(path):
        return 0
    try:
        for entry in os.scandir(path):
            if entry.is_file(follow_symlinks=False):
                total += entry.stat().st_size
            elif entry.is_dir(follow_symlinks=False):
                total += get_folder_size(entry.path)
    except (PermissionError, FileNotFoundError, OSError):
        pass
    return total


def format_size(bytes_val: int) -> str:
    """Форматирование байтов в читаемый вид (ГБ / МБ)."""
    if bytes_val <= 0:
        return "0 МБ"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.1f} ГБ"
    return f"{int(mb)} МБ"

def format_speed(bps: float) -> str:
    """Форматирование скорости загрузки (МБ/с или КБ/с)."""
    if bps <= 0:
        return "0 КБ/с"
    mbps = bps / (1024 * 1024)
    if mbps >= 1.0:
        return f"{mbps:.1f} МБ/с"
    kbps = bps / 1024
    return f"{int(kbps)} КБ/с"

def get_model_cache_dir(engine_id: str) -> str | None:
    """Возвращает путь к папке кэша конкретной модели в hf_cache."""
    spec = _MODEL_SPECS.get(engine_id)
    if not spec:
        return None
    repo_id = spec["repo"]
    folder_name = "models--" + repo_id.replace("/", "--")
    base_hf = os.path.join(get_data_dir(), "hf_cache")
    path_hub = os.path.join(base_hf, "hub", folder_name)
    path_direct = os.path.join(base_hf, folder_name)
    if os.path.exists(path_hub):
        return path_hub
    if os.path.exists(path_direct):
        return path_direct
    return path_hub


def is_model_cached(engine_id: str) -> tuple[bool, int]:
    """Проверяет, скачана ли модель (весит ли папка > 50 МБ). Возвращает (is_cached, size_bytes)."""
    path = get_model_cache_dir(engine_id)
    if not path or not os.path.exists(path):
        return False, 0
    size = get_folder_size(path)
    return (size > 50 * 1024 * 1024), size


def get_total_cache_size() -> int:
    """Общий размер всей папки hf_cache."""
    base_hf = os.path.join(get_data_dir(), "hf_cache")
    return get_folder_size(base_hf)


def delete_model_cache(engine_id: str) -> bool:
    """Удаляет кэш конкретной выбранной модели."""
    path = get_model_cache_dir(engine_id)
    if path and os.path.exists(path):
        try:
            shutil.rmtree(path)
            return True
        except OSError as e:
            print(f"[warn] не удалось удалить модель {engine_id}: {e}")
    return False


def clear_all_cache() -> bool:
    """Полностью очищает всю папку hf_cache."""
    base_hf = os.path.join(get_data_dir(), "hf_cache")
    if os.path.exists(base_hf):
        try:
            shutil.rmtree(base_hf)
            os.makedirs(base_hf, exist_ok=True)
            return True
        except OSError as e:
            print(f"[warn] не удалось очистить кэш: {e}")
    return False


def get_available_offline_engine(preferred: str = "opus") -> str | None:
    """Умный выбор офлайн-модели: возвращает preferred, если она скачана, или любую доступную."""
    is_pref, _ = is_model_cached(preferred)
    if is_pref:
        return preferred
    for eng in LOCAL_ENGINES:
        is_c, _ = is_model_cached(eng)
        if is_c:
            return eng
    return None

def is_network_error(text: str) -> bool:
    """Сетевые проблемы И троттлинг (429) — всё, что лечится офлайн-моделью."""
    t = (text or "").lower()
    markers = ("connection", "timed out", "timeout", "getaddrinfo", "network",
               "internet", "unreachable", "max retries", "failed to establish",
               "temporary failure", "proxy", "ssl",
               "too many requests", "server error", "429", "rate limit")
    return any(m in t for m in markers)


def translate_online(engine: str, text: str) -> str:
    """Блокирующий онлайн-перевод с одним повтором при сбое."""

    import deep_translator
    last_error = None
    for attempt in range(2):
        try:
            if engine == ENGINE_MYMEMORY:
                # MyMemory не поддерживает auto/двухбуквенные коды — только полные
                return deep_translator.MyMemoryTranslator(
                    source="en-GB", target="ru-RU").translate(text)
            return deep_translator.GoogleTranslator(source="auto", target="ru").translate(text)
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(1.5)   # пауза и повтор — часто достаточно
    raise last_error

def _split_into_sentences(text: str) -> list:
    """Разбивает длинный текст на отдельные предложения по знакам . ! ? …"""
    text = text.strip()
    if not text:
        return []
    # Режем по пробелам ПОСЛЕ знаков завершения предложений
    parts = re.split(r'(?<=[.!?…])\s+', text)
    return [p.strip() for p in parts if p.strip()]

# ============================================================
# Локальные модели-обёртки
# ============================================================
class _OpusTranslator:
    def __init__(self, model, tokenizer, device):
        self.model = model
        self.tokenizer = tokenizer
        self.device = device

    def to_device(self, device):
        self.device = device
        self.model = self.model.to(device)

    def _translate_single(self, text: str) -> str:
        """Перевод одного короткого предложения."""
        inputs = self.tokenizer(text, return_tensors="pt", padding=True,
                                truncation=True, max_length=512)
        inputs = inputs.to(self.device)
        out = self.model.generate(**inputs, max_length=512)
        return self.tokenizer.batch_decode(out, skip_special_tokens=True)[0]

    def translate(self, text: str) -> str:
        """Главный метод: режет длинный текст на предложения и переводит по очереди."""
        sentences = _split_into_sentences(text)
        if not sentences:
            return ""
        if len(sentences) == 1:
            return self._translate_single(sentences[0])
        
        # Переводим каждую фразу отдельно и склеиваем через пробел
        results = [self._translate_single(s) for s in sentences]
        return " ".join(results)


class _NllbTranslator(_OpusTranslator):
    SRC, DST = "eng_Latn", "rus_Cyrl"

    def _translate_single(self, text: str) -> str:
        """Перевод одного предложения для NLLB."""
        inputs = self.tokenizer(text, return_tensors="pt", padding=True,
                                truncation=True, max_length=512)
        inputs = inputs.to(self.device)
        if hasattr(self.tokenizer, "lang_code_to_id"):
            bos = self.tokenizer.lang_code_to_id[self.DST]
        else:
            bos = self.tokenizer.convert_tokens_to_ids(self.DST)
        out = self.model.generate(**inputs, forced_bos_token_id=bos,
                                  max_length=512, no_repeat_ngram_size=3)
        return self.tokenizer.batch_decode(out, skip_special_tokens=True)[0]

def _clean_download_name(name: str) -> str:
    """Очищает технические сообщения Hugging Face (например, 'Fetching 11 files')."""
    name = name.strip().rstrip(":")
    if not name or name.lower().startswith("fetching"):
        return "файлы модели"
    return name

# ============================================================
# Прогресс скачивания: tqdm -> сигнал (агрегат по всем файлам)
# ============================================================
class _DownloadTracker:
    """Суммирует байты по всем файлам репозитория, замеряет скорость и статус выделения места."""

    def __init__(self, on_update):
        self._lock = threading.Lock()
        self._on_update = on_update
        self._done = 0
        self._files = {}   # id(tqdm) -> [имя, total, n]
        self._last_time = time.monotonic()
        self._last_bytes = 0
        self._smoothed_speed = 0.0

    def _report(self):
        now = time.monotonic()
        total = self._done + sum(f[1] for f in self._files.values())
        cur = self._done + sum(f[2] for f in self._files.values())
        
        # Очищаем имя от служебного 'Fetching...'
        raw_name = list(self._files.values())[-1][0] if self._files else ""
        name = _clean_download_name(raw_name)

        # Замер скорости каждые 200 мс со сглаживанием
        dt = now - self._last_time
        if dt >= 0.2:
            delta_b = cur - self._last_bytes
            if delta_b >= 0 and dt > 0:
                inst_speed = delta_b / dt
                if self._smoothed_speed <= 0.0:
                    self._smoothed_speed = inst_speed
                else:
                    self._smoothed_speed = 0.7 * self._smoothed_speed + 0.3 * inst_speed
            self._last_time = now
            self._last_bytes = cur

        speed_str = format_speed(self._smoothed_speed)

        if total <= 0:
            self._on_update(-1, f"Скачивание {name}")
            return

        pct = min(int(cur * 100 / total), 99)

        # Фаза выделения места на диске (первые секунды для больших файлов > 50 МБ)
        if total > 50 * 1024 * 1024 and cur < 2 * 1024 * 1024 and self._smoothed_speed < 300 * 1024:
            self._on_update(0, f"Выделение места на диске ({format_size(total)})…")
            return

        self._on_update(pct, f"Скачивание {name} — ~{pct}% · {speed_str}")

    def add(self, tid, name, total, n):
        with self._lock:
            self._files[tid] = [name, total or 0, n or 0]
            self._report()

    def update(self, tid, n):
        with self._lock:
            if tid in self._files:
                self._files[tid][2] = n or 0
                self._report()

    def close(self, tid):
        with self._lock:
            f = self._files.pop(tid, None)
            if f:
                self._done += f[1] or f[2]
            self._report()


class _ProgressTqdm(tqdm_base):
    """tqdm, репортящий прогресс в трекер. Вывод глушим (у нас stdout = лог-файл)."""
    tracker = None

    def __init__(self, *args, **kwargs):
        kwargs["disable"] = False
        kwargs.setdefault("file", _DEVNULL)
        super().__init__(*args, **kwargs)
        t = type(self).tracker
        if t is not None:
            t.add(id(self), str(self.desc or ""), self.total, self.n)

    def update(self, n=1):
        super().update(n)
        t = type(self).tracker
        if t is not None:
            t.update(id(self), self.n)

    def close(self):
        t = type(self).tracker
        if t is not None:
            t.close(id(self))
        super().close()

# ============================================================
# ModelManager: владеет локальными моделями (один поток)
# ============================================================
class ModelManager(QThread):
    progress_started = Signal()
    progress = Signal(int, str)          # percent (-1 = indeterminate), label
    ready = Signal(str)                  # engine_id
    failed = Signal(str, str)            # engine_id, error
    translation_result = Signal(int, str)  # seq, text

    def __init__(self, initial_gpu: bool = False):
        super().__init__()
        self._use_gpu = bool(initial_gpu)
        self._tasks = queue.Queue()
        self._stop_flag = False
        self._current_id = None
        self._translator = None
        self._torch = None

    # ---- публичный API (потокобезопасно) ----
    def load(self, engine_id, use_gpu=None):
        self._tasks.put(("load", engine_id, use_gpu))

    def unload(self):
        self._tasks.put(("unload",))

    def translate(self, text, seq):
        self._tasks.put(("translate", text, seq))

    def set_device(self, use_gpu):
        self._tasks.put(("set_device", bool(use_gpu)))

    def stop(self):
        self._stop_flag = True
        self._tasks.put(("stop",))
        if not self.wait(3000):
            self.terminate()
            self.wait(1000)

    # ---- внутренности (поток воркера) ----
    def run(self):
        try:
            import torch
            self._torch = torch
        except Exception:
            self._torch = None

        while not self._stop_flag:
            try:
                task = self._tasks.get(timeout=0.3)
            except queue.Empty:
                continue
            kind = task[0]
            if kind == "stop":
                break
            if kind == "load":
                self._load(task[1], task[2])
            elif kind == "unload":
                self._unload()
            elif kind == "translate":
                self._translate(task[1], task[2])
            elif kind == "set_device":
                self._apply_device(task[1])

    def _drop_model(self):
        self._translator = None
        self._current_id = None
        gc.collect()
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()

    def _load(self, engine_id, use_gpu_override):
        use_gpu = self._use_gpu if use_gpu_override is None else bool(use_gpu_override)
        if self._current_id == engine_id and self._translator is not None:
            self.ready.emit(engine_id)
            return
        spec = _MODEL_SPECS.get(engine_id)
        if spec is None:
            self.failed.emit(engine_id, "Неизвестный идентификатор модели")
            return

        self.progress_started.emit()
        try:
            # Фаза 1: скачивание с процентами (из кэша — мгновенно)
            from huggingface_hub import snapshot_download
            tracker = _DownloadTracker(lambda pct, label: self.progress.emit(pct, label))
            _ProgressTqdm.tracker = tracker
            path = snapshot_download(repo_id=spec["repo"], tqdm_class=_ProgressTqdm)
            _ProgressTqdm.tracker = None

            # Фаза 2: веса из кэша в память (без сети)
            self.progress.emit(-1, "Загрузка весов в память…")
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            self._drop_model()
            tokenizer = AutoTokenizer.from_pretrained(path)
            model = AutoModelForSeq2SeqLM.from_pretrained(path)

            cuda_ok = self._torch is not None and self._torch.cuda.is_available()
            device = "cuda" if (use_gpu and cuda_ok) else "cpu"
            model = model.to(device)

            if engine_id == ENGINE_NLLB:
                tokenizer.src_lang = _NllbTranslator.SRC
                self._translator = _NllbTranslator(model, tokenizer, device)
            else:
                self._translator = _OpusTranslator(model, tokenizer, device)
            self._current_id = engine_id
            self.ready.emit(engine_id)
        except Exception as e:
            _ProgressTqdm.tracker = None
            self._drop_model()
            self.failed.emit(engine_id, str(e))

    def _unload(self):
        if self._translator is not None:
            self._drop_model()

    def _translate(self, text, seq):
        if self._translator is None:
            self.translation_result.emit(
                seq, "[Локальная модель ещё не готова — попробуйте через несколько секунд]")
            return
        try:
            self.translation_result.emit(seq, self._translator.translate(text))
        except Exception as e:
            self.translation_result.emit(seq, f"[Ошибка локального перевода: {e}]")

    def _apply_device(self, use_gpu):
        self._use_gpu = use_gpu
        if self._translator is None:
            return
        cuda_ok = self._torch is not None and self._torch.cuda.is_available()
        device = "cuda" if (use_gpu and cuda_ok) else "cpu"
        try:
            self._translator.to_device(device)
        except Exception as e:
            self.failed.emit(self._current_id or "", f"Не удалось перенести модель: {e}")