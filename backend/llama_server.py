"""Локальный LLM-перевод через llama-server.exe и каталог GGUF-моделей.

Ожидаемая раскладка:
    <корень проекта>/llama/llama-server.exe   (+ все DLL из релиза llama.cpp)
    <корень проекта>/data/models/*.gguf
"""
import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal

from backend.config import get_data_dir
from backend.languages import build_llm_system_prompt
from backend.runtime_manager import get_server_exe

# ============================================================
# Каталог проверенных моделей (Direct HuggingFace GGUF Links)
# ============================================================
GGUF_CATALOG = [
    {
        "id": "hy-mt2-1.8b",
        "filename": "hy-mt2-1.8b-q4_k_m.gguf",
        "title": "Hy-MT2 1.8B (Tencent)",
        "badge": "⚡ Слабый ПК / Ноутбук",
        "approx_size": "~1.2 ГБ",
        "url": "https://huggingface.co/mradermacher/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B.Q4_K_M.gguf",
        "desc": "Специализированная легкая модель машинного перевода. Отлично работает на чистом CPU и видеокартах с 2 ГБ памяти.",
    },
    {
        "id": "qwen2.5-7b",
        "filename": "qwen2.5-7b-instruct-q4_k_m.gguf",
        "title": "Qwen 2.5 7B (Instruct)",
        "badge": "👑 Максимальное качество",
        "approx_size": "~4.5 ГБ",
        "url": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf",
        "desc": "Высочайшее качество перевода, понимание сленга, ругательств и сложного лора. Рекомендуется 6–8 ГБ VRAM (RTX 2060/3060+).",
    },
    {
        "id": "sakura-4b",
        "filename": "sakura-4b-q4_k_m.gguf",
        "title": "Sakura 4B (Galgame / VN)",
        "badge": "🌸 Японские новеллы / Манга",
        "approx_size": "~2.5 ГБ",
        "url": "https://huggingface.co/sakuraumi/Sakura-4B-Galgame-GGUF/resolve/main/sakura-4b-q4_k_m.gguf",
        "desc": "Специализированный ИИ для перевода японских визуальных новелл и манги. Идеальная передача аниме-суффиксов и идиом.",
    },
]

SYSTEM_PROMPT = (
    "You are a professional game localization translator. "
    "Translate the given text into natural, fluent Russian. "
    "Fix minor OCR glitches silently. Keep the character's tone and emotions. "
    "Output ONLY the translation, without notes or quotes."
)

_CREATE_NO_WINDOW = 0x08000000
_job = None  # Job Object с KILL_ON_JOB_CLOSE: сервер не переживёт приложение

def get_models_dir() -> str:
    d = os.path.join(get_data_dir(), "models")
    os.makedirs(d, exist_ok=True)
    return d


def get_installed_models() -> list[dict]:
    """Возвращает список всех .gguf файлов, физически лежащих в data/models."""
    d = get_models_dir()
    models = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".gguf"):
                full_path = os.path.join(d, f)
                try:
                    size = os.path.getsize(full_path)
                    models.append({
                        "filename": f,
                        "path": full_path,
                        "size_bytes": size,
                        "size_str": f"{size / (1024 * 1024 * 1024):.1f} ГБ" if size >= 1024**3 else f"{int(size / (1024 * 1024))} МБ",
                    })
                except OSError:
                    pass
    return models


def resolve_model_path(preferred_filename: str = "") -> str | None:
    """Возвращает путь к указанной модели или к первой найденной."""
    d = get_models_dir()
    if preferred_filename:
        target = os.path.join(d, preferred_filename)
        if os.path.isfile(target):
            return target

    installed = get_installed_models()
    if installed:
        return installed[0]["path"]
    return None


def is_available(preferred_filename: str = "") -> tuple[bool, int]:
    """(готов ли к запуску, размер модели в байтах)."""
    model_path = resolve_model_path(preferred_filename)
    if model_path and os.path.isfile(get_server_exe()):
        return True, os.path.getsize(model_path)
    return False, 0


def delete_gguf_model(filename: str) -> bool:
    """Удаляет файл модели из data/models."""
    path = os.path.join(get_models_dir(), filename)
    if os.path.isfile(path):
        try:
            os.remove(path)
            return True
        except OSError as e:
            print(f"[llama] ошибка удаления {filename}: {e}")
    return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _assign_to_kill_job(proc) -> None:
    """Windows: привязать процесс к Job Object, чтобы он умер вместе с приложением."""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        if _job is None:
            _job = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(_job, int(proc._handle))
    except Exception as e:
        print(f"[llama] job object не создан: {e}")


# ============================================================
# Фоновый загрузчик одного .gguf файла с HuggingFace
# ============================================================
class GgufDownloadWorker(QObject):
    progress = Signal(int, str)  # percent (-1 = indeterminate), status_text
    finished = Signal(str)       # filename
    failed = Signal(str, str)    # filename, error

    def __init__(self, item: dict):
        super().__init__()
        self.item = item
        self._stop_flag = False

    def start_download(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def cancel(self):
        self._stop_flag = True

    def _run(self):
        url = self.item.get("url", "")
        filename = self.item.get("filename", "model.gguf")
        target_path = os.path.join(get_models_dir(), filename)
        part_path = target_path + ".part"

        if not url:
            self.failed.emit(filename, "Не указан URL модели")
            return

        try:
            self.progress.emit(0, f"Подключение: {self.item.get('title', filename)}…")
            req = urllib.request.Request(url, headers={"User-Agent": "ScreenTale-Downloader"})

            with urllib.request.urlopen(req, timeout=30) as resp, open(part_path, "wb") as out:
                total_size = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                block_size = 1024 * 256  # 256 KB
                last_time = time.monotonic()
                last_bytes = 0
                smoothed_speed = 0.0

                while not self._stop_flag:
                    chunk = resp.read(block_size)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)

                    now = time.monotonic()
                    dt = now - last_time
                    if dt >= 0.25:
                        inst_speed = (downloaded - last_bytes) / dt
                        smoothed_speed = 0.7 * smoothed_speed + 0.3 * inst_speed if smoothed_speed > 0 else inst_speed
                        last_time = now
                        last_bytes = downloaded

                        speed_mb = smoothed_speed / (1024 * 1024)
                        cur_gb = downloaded / (1024 * 1024 * 1024)
                        tot_gb = total_size / (1024 * 1024 * 1024)

                        if total_size > 0:
                            pct = min(int(downloaded * 100 / total_size), 99)
                            self.progress.emit(pct, f"Скачивание {self.item['title']}: {cur_gb:.1f} / {tot_gb:.1f} ГБ ({pct}%) · {speed_mb:.1f} МБ/с")
                        else:
                            self.progress.emit(-1, f"Скачивание {self.item['title']}: {cur_gb:.1f} ГБ · {speed_mb:.1f} МБ/с")

            if self._stop_flag:
                if os.path.exists(part_path):
                    os.remove(part_path)
                return

            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(part_path, target_path)

            self.progress.emit(100, f"Готово: {self.item['title']}")
            self.finished.emit(filename)

        except Exception as e:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            self.failed.emit(filename, str(e))


# ============================================================
# Серверный транслятор через subprocess
# ============================================================
class LlamaServerTranslator:
    def __init__(self, use_gpu: bool = True, n_ctx: int = 2048, model_filename: str = "", n_threads: int = 0):
        self._use_gpu = bool(use_gpu)
        self._n_ctx = n_ctx
        self._model_filename = model_filename
        self._n_threads = int(n_threads)
        self._proc = None
        self._port = None
        self._log = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        atexit.register(self.close)

    def translate(self, text: str, src_lang: str = "en", dst_lang: str = "ru") -> str:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("llama-server не запущен")
            
        system_prompt = build_llm_system_prompt(src_lang, dst_lang)
        
        body = json.dumps({
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text}
            ],
            "temperature": 0.2,
            "max_tokens": 512,
            "stream": False,
        }).encode("utf-8")
        
        req = urllib.request.Request(
            self._url("/v1/chat/completions"), data=body,
            headers={"Content-Type": "application/json"})
        with self._opener.open(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()

    def _build_cmd(self, model: str) -> list:
        # Авто-расчет: половина логических ядер, если стоит 0
        effective_threads = self._n_threads if self._n_threads > 0 else max(1, (os.cpu_count() // 2) - 1)

        return [get_server_exe(), "-m", model,
                "-ngl", "99" if self._use_gpu else "0",
                "-c", str(self._n_ctx), "-np", "1",
                "-t", str(effective_threads),
                "--host", "127.0.0.1", "--port", str(self._port)]

    # backend/llama_server.py -> класс LlamaServerTranslator

    def start(self, timeout: float = 180.0) -> None:
        model = resolve_model_path(self._model_filename)
        if not model or not os.path.isfile(get_server_exe()):
            raise RuntimeError(
                "Не найден llama\\llama-server.exe или выбранная .gguf-модель")

        t_start = time.perf_counter()  # <-- 1. Засекаем старт

        self._port = _free_port()
        log_path = os.path.join(get_data_dir(), "llama_server.log")
        self._log = open(log_path, "w", encoding="utf-8", errors="replace")
        flags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
        cmd = self._build_cmd(model)
        self._log.write("CMD: " + " ".join(cmd) + "\n")
        self._log.flush()
        self._proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(get_server_exe()),
            stdout=self._log, stderr=subprocess.STDOUT, creationflags=flags)
        _assign_to_kill_job(self._proc)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server завершился (код {self._proc.returncode}). "
                    f"Смотри {log_path}")
            try:
                with self._opener.open(self._url("/health"), timeout=2) as r:
                    if r.status == 200:
                        # <-- 2. Считаем время, когда сервер полностью готов и ответил 200 OK
                        elapsed = time.perf_counter() - t_start
                        model_name = os.path.basename(model)
                        print(f"[llama] Модель '{model_name}' загружена в VRAM и готова к работе за {elapsed:.2f} сек!")
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.4)
        self.close()
        raise RuntimeError("llama-server не успел запуститься за отведённое время")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def to_device(self, device: str) -> None:
        return

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._port}{path}"

