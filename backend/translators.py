"""Движки перевода и менеджер локальных моделей (QThread).

Онлайн-движки (Google, MyMemory) выполняются в QThreadPool.
Локальные модели (Opus, NLLB) живут в потоке ModelManager: загрузка
со скачиванием (честный процент), перевод, перенос между CPU/GPU.
"""
import gc
import os
import queue
import re
import threading

from PySide6.QtCore import QThread, Signal
from tqdm import tqdm as tqdm_base

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
    ENGINE_OPUS: {"repo": "Helsinki-NLP/opus-mt-en-ru"},
    ENGINE_NLLB: {"repo": "facebook/nllb-200-distilled-600M"},
}

_DEVNULL = open(os.devnull, "w")


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
    import time

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


# ============================================================
# Прогресс скачивания: tqdm -> сигнал (агрегат по всем файлам)
# ============================================================
class _DownloadTracker:
    """Суммирует байты по всем файлам репозитория: большие веса + мелочь = один процент."""

    def __init__(self, on_update):
        self._lock = threading.Lock()
        self._on_update = on_update
        self._done = 0
        self._files = {}   # id(tqdm) -> [имя, total, n]

    def _report(self):
        total = self._done + sum(f[1] for f in self._files.values())
        cur = self._done + sum(f[2] for f in self._files.values())
        name = ""
        if self._files:
            name = list(self._files.values())[-1][0]
        # Честный режим: без известных размеров — indeterminate, не «вечные 99%»
        if total <= 0:
            self._on_update(-1, f"Скачивание: {name}")
            return
        # если большинство файлов без total — тоже честнее indeterminate
        known = sum(1 for f in self._files.values() if f[1] > 0)
        if known < 2 and self._files:
            self._on_update(-1, f"Скачивание: {name}")
            return
        pct = min(int(cur * 100 / total), 99)
        self._on_update(pct, f"Скачивание: {name} — ~{pct}%")

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