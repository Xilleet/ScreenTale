"""Менеджер рантаймов llama.cpp: автоопределение железа, скачивание и Smoke Test."""
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from PySide6.QtCore import QObject, Signal

from backend.config import get_app_dir, get_data_dir

# Фиксированный проверенный тег llama.cpp на GitHub
LLAMA_PINNED_TAG = "b11224"
GITHUB_RELEASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_PINNED_TAG}/"

BACKENDS_CONFIG = {
    "cuda12": {
        "title": "NVIDIA CUDA 12.4 (Максимальная скорость)",
        "tag": LLAMA_PINNED_TAG,
        "files": [
            f"llama-{LLAMA_PINNED_TAG}-bin-win-cuda-12.4-x64.zip",
            "cudart-llama-bin-win-cuda-12.4-x64.zip",
        ],
        "desc": "Рекомендуется для видеокарт NVIDIA GeForce GTX / RTX.",
    },
    "vulkan": {
        "title": "Vulkan (Универсальный GPU)",
        "tag": LLAMA_PINNED_TAG,
        "files": [
            f"llama-{LLAMA_PINNED_TAG}-bin-win-vulkan-x64.zip",
        ],
        "desc": "Для видеокарт AMD Radeon, Intel Arc/Iris и любых современных GPU.",
    },
    "cpu": {
        "title": "CPU AVX2 (Базовый процессорный)",
        "tag": LLAMA_PINNED_TAG,
        "files": [
            f"llama-{LLAMA_PINNED_TAG}-bin-win-cpu-x64.zip",
        ],
        "desc": "Работает на любых современных процессорах без использования видеокарты.",
    },
}

_CREATE_NO_WINDOW = 0x08000000


def get_llama_dir() -> str:
    """Путь к папке llama рядом с main.py."""
    d = os.path.join(get_app_dir(), "llama")
    os.makedirs(d, exist_ok=True)
    return d


def get_server_exe() -> str:
    return os.path.join(get_llama_dir(), "llama-server.exe")


# backend/runtime_manager.py -> функция get_installed_backend

def get_installed_backend() -> str | None:
    """Возвращает ID установленного бэкенда. Умеет определять ручную установку по DLL."""
    llama_dir = get_llama_dir()
    exe = get_server_exe()
    
    if not os.path.isfile(exe):
        return None

    marker = os.path.join(llama_dir, ".backend")
    
    # 1. Если маркер уже есть — читаем его
    if os.path.isfile(marker):
        try:
            with open(marker, "r", encoding="utf-8") as f:
                val = f.read().strip()
                if val:
                    return val
        except OSError:
            pass

    # 2. ФОЛЛБЕК ДЛЯ РУЧНОЙ УСТАНОВКИ: определяем тип по лежащим рядом DLL
    detected = "cpu"
    if os.path.isfile(os.path.join(llama_dir, "ggml-cuda.dll")):
        detected = "cuda12"
    elif os.path.isfile(os.path.join(llama_dir, "ggml-vulkan.dll")):
        detected = "vulkan"

    # Записываем маркер, чтобы в будущем не сканировать заново
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(detected)
    except OSError:
        pass

    return detected


def detect_best_backend() -> str:
    """Определяет оптимальный бэкенд для текущей машины."""
    if sys.platform != "win32":
        return "cpu"

    # 1. Проверяем NVIDIA через системный драйвер NVML
    try:
        import ctypes
        nvml = ctypes.WinDLL("nvml.dll")
        if nvml.nvmlInit_v2() == 0:
            nvml.nvmlShutdown()
            return "cuda12"
    except Exception:
        pass

    # 2. Проверяем поддержку Vulkan (наличие системного vulkan-1.dll)
    try:
        import ctypes
        vulkan = ctypes.WinDLL("vulkan-1.dll")
        if vulkan:
            return "vulkan"
    except Exception:
        pass

    # 3. Базовый fallback — чистый процессор
    return "cpu"


def run_smoke_test() -> tuple[bool, str]:
    """Скрытый запуск llama-server.exe --version для проверки совместимости железа."""
    exe = get_server_exe()
    if not os.path.isfile(exe):
        return False, "Файл llama-server.exe не найден"

    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
            timeout=6,
        )
        if proc.returncode == 0:
            return True, "OK"

        # Расшифровка типичных кодов падений Windows
        rc = proc.returncode
        if rc == 3221225781 or rc == -1073741515:  # 0xC0000135
            return False, "Не найдены необходимые системные DLL (Visual C++ Redistributable)"
        if rc == 3221225501 or rc == -1073741795:  # 0xC000001D
            return False, "Процессор не поддерживает инструкции AVX2 этого бинарника"

        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        return False, f"Код ошибки {rc}: {err[:120]}"

    except subprocess.TimeoutExpired:
        return False, "Тестовый запуск завис по таймауту"
    except Exception as e:
        return False, str(e)
    
def kill_llama_server():
    """Принудительно глушит зависший процесс llama-server.exe БЕЗ удаления файлов."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
            time.sleep(0.3)
        except Exception:
            pass

def clean_llama_dir() -> bool:
    """Убивает зависший сервер и полностью очищает папку llama/ от старых DLL (только для переустановки)."""
    # 1. Принудительно глушим старый процесс, если он почему-то еще жив
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "llama-server.exe", "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
            time.sleep(0.3)
        except Exception:
            pass

    # 2. Удаляем все старые DLL и .exe, чтобы не смешивать бэкенды
    d = get_llama_dir()
    if os.path.isdir(d):
        for item in os.listdir(d):
            path = os.path.join(d, item)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
            except OSError as e:
                print(f"[runtime] не удалось удалить {item}: {e}")
                return False
    return True

    # backend/runtime_manager.py

def is_backend_supported(backend_id: str) -> tuple[bool, str]:
    """Проверяет аппаратную поддержку бэкенда. Возвращает (доступен, причина_если_нет)."""
    if backend_id == "cpu":
        return True, ""

    if backend_id == "cuda12":
        if sys.platform != "win32":
            return False, "Только для Windows"
        try:
            import ctypes
            nvml = ctypes.WinDLL("nvml.dll")
            if nvml.nvmlInit_v2() == 0:
                nvml.nvmlShutdown()
                return True, ""
        except Exception:
            pass
        return False, "Требуется видеокарта NVIDIA"

    if backend_id == "vulkan":
        if sys.platform != "win32":
            return False, "Только для Windows"
        try:
            import ctypes
            vulkan = ctypes.WinDLL("vulkan-1.dll")
            if vulkan:
                return True, ""
        except Exception:
            pass
        return False, "Нет драйвера Vulkan"

    return False, "Неизвестный бэкенд"

# ============================================================
# Фоновый воркер загрузки и распаковки рантайма
# ============================================================
class RuntimeDownloadWorker(QObject):
    progress = Signal(int, str)  # percent, label
    finished = Signal(str)       # backend_id
    failed = Signal(str, str)    # backend_id, error_message

    def __init__(self, backend_id: str):
        super().__init__()
        self.backend_id = backend_id
        self._stop_flag = False

    def start_download(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def cancel(self):
        self._stop_flag = True

    def _run(self):
        config = BACKENDS_CONFIG.get(self.backend_id)
        if not config:
            self.failed.emit(self.backend_id, f"Неизвестный бэкенд: {self.backend_id}")
            return

        # Чистим старый движок перед установкой нового
        self.progress.emit(0, "Подготовка: очистка старых компонентов…")
        if not clean_llama_dir():
            self.failed.emit(self.backend_id, "Не удалось очистить папку llama/ (файлы заняты другим процессом)")
            return

        temp_dir = os.path.join(get_data_dir(), "temp_runtime")
        os.makedirs(temp_dir, exist_ok=True)
        files = config["files"]
        total_files = len(files)

        try:
            for idx, filename in enumerate(files):
                if self._stop_flag:
                    return

                url = f"{GITHUB_RELEASE_URL}{filename}"
                zip_path = os.path.join(temp_dir, filename)

                step_label = f"[{idx + 1}/{total_files}]" if total_files > 1 else ""
                self.progress.emit(0, f"Подключение {step_label}: {filename}…")

                # Скачиваем файл
                req = urllib.request.Request(url, headers={"User-Agent": "ScreenTale-RuntimeManager"})
                with urllib.request.urlopen(req, timeout=30) as resp, open(zip_path, "wb") as out:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    block_size = 1024 * 256  # 256 KB

                    while not self._stop_flag:
                        chunk = resp.read(block_size)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)

                        if total_size > 0:
                            pct = min(int(downloaded * 100 / total_size), 99)
                            mb_cur = downloaded / (1024 * 1024)
                            mb_tot = total_size / (1024 * 1024)
                            self.progress.emit(pct, f"Скачивание {step_label}: {mb_cur:.1f} / {mb_tot:.1f} МБ")

                if self._stop_flag:
                    return

                # Распаковываем поверх в папку llama/
                self.progress.emit(100, f"Распаковка {step_label}…")
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(get_llama_dir())

                # Чистим временный zip
                try:
                    os.remove(zip_path)
                except OSError:
                    pass

            # 4. Проводим скрытый Smoke Test
            self.progress.emit(100, "Проверка совместимости рантайма…")
            ok, msg = run_smoke_test()
            if not ok:
                self.failed.emit(self.backend_id, f"Smoke Test не пройден: {msg}")
                return

            # Записываем маркер успешной установки
            marker = os.path.join(get_llama_dir(), ".backend")
            with open(marker, "w", encoding="utf-8") as f:
                f.write(self.backend_id)

            self.progress.emit(100, f"Движок {config['title']} готов!")
            self.finished.emit(self.backend_id)

        except Exception as e:
            self.failed.emit(self.backend_id, str(e))
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)