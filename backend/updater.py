"""Модуль бесшовного автообновления ScreenTale по manifest.json."""
import hashlib
import json
import os
import subprocess
import sys
import threading
import urllib.request

from PySide6.QtCore import QObject, QRunnable, Signal

from backend.config import APP_VERSION, get_app_dir, get_data_dir

MANIFEST_URL = "https://github.com/Xilleet/ScreenTale/releases/latest/download/manifest.json"


def _parse_version(v_str: str) -> tuple:
    """Преобразует '0.4.4', 'v0.5.0' в кортеж чисел (0, 5, 0)."""
    cleaned = str(v_str).strip().lower().lstrip("v").split("-")[0]
    try:
        return tuple(int(x) for x in cleaned.split("."))
    except Exception:
        return (0, 0, 0)


def calculate_sha256(file_path: str) -> str:
    """Вычисляет хэш SHA-256 файла."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class UpdateCheckTask(QRunnable):
    """Фоновая проверка наличия свежего manifest.json на GitHub."""

    def __init__(self, on_update_found):
        super().__init__()
        self._on_update_found = on_update_found

    def run(self):
        try:
            req = urllib.request.Request(
                MANIFEST_URL,
                headers={"User-Agent": "ScreenTale-App"}
            )
            with urllib.request.urlopen(req, timeout=4) as response:
                if response.status != 200:
                    return
                data = json.loads(response.read().decode("utf-8"))

            remote_ver = data.get("version", "")
            if not remote_ver:
                return

            if _parse_version(remote_ver) > _parse_version(APP_VERSION):
                self._on_update_found(data)
        except Exception as e:
            print(f"[updater] проверка обновлений пропущена: {e}")


class UpdateDownloadWorker(QObject):
    """Фоновое скачивание обновления с передачей процентов, проверка SHA-256 и запуск установки."""
    progress = Signal(int, str)  # percent, label
    finished = Signal()
    failed = Signal(str)

    def __init__(self, manifest_data: dict):
        super().__init__()
        self.manifest_data = manifest_data

    def start_download(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        try:
            download_url = self.manifest_data.get("download_url")
            expected_sha = self.manifest_data.get("sha256", "").strip().lower()

            if not download_url:
                self.failed.emit("В манифесте отсутствует ссылка download_url")
                return

            temp_dir = os.path.join(get_data_dir(), "temp")
            os.makedirs(temp_dir, exist_ok=True)
            zip_path = os.path.join(temp_dir, "update.zip")

            self.progress.emit(0, "Подключение к репозиторию…")

            # Скачиваем с отслеживанием прогресса через urllib
            req = urllib.request.Request(
                download_url,
                headers={"User-Agent": "ScreenTale-App"}
            )
            
            with urllib.request.urlopen(req, timeout=60) as response:
                total_size = int(response.headers.get("Content-Length", 0))
                block_size = 65536
                downloaded = 0
                
                with open(zip_path, "wb") as out_f:
                    while True:
                        buffer = response.read(block_size)
                        if not buffer:
                            break
                        downloaded += len(buffer)
                        out_f.write(buffer)
                        
                        if total_size > 0:
                            pct = int(downloaded * 100 / total_size)
                            mb_cur = downloaded / (1024 * 1024)
                            mb_tot = total_size / (1024 * 1024)
                            self.progress.emit(pct, f"Скачивание: {mb_cur:.1f} / {mb_tot:.1f} МБ ({pct}%)")
                        else:
                            mb_cur = downloaded / (1024 * 1024)
                            self.progress.emit(-1, f"Скачивание: {mb_cur:.1f} МБ…")

            self.progress.emit(100, "Проверка контрольной суммы (SHA-256)…")

            # Сверяем SHA-256
            if expected_sha:
                actual_sha = calculate_sha256(zip_path).lower()
                if actual_sha != expected_sha:
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass
                    self.failed.emit("Контрольная сумма SHA-256 не совпала! Файл повреждён.")
                    return

            self.progress.emit(100, "Подготовка к обновлению…")

            # Создаем updater.bat
            app_dir = get_app_dir()
            updater_bat = os.path.join(temp_dir, "updater.bat")
            extracted_dir = os.path.join(temp_dir, "extracted")

            with open(updater_bat, "w", encoding="utf-8") as f:
                f.write(f"""@echo off
chcp 65001 > nul
timeout /t 2 /nobreak > nul
powershell -Command "Expand-Archive -Path '{zip_path}' -DestinationPath '{extracted_dir}' -Force"
if exist "{extracted_dir}\\ScreenTale" (
    xcopy "{extracted_dir}\\ScreenTale\\*" "{app_dir}\\" /s /e /y /q > nul
) else (
    xcopy "{extracted_dir}\\*" "{app_dir}\\" /s /e /y /q > nul
)
rmdir /s /q "{extracted_dir}" > nul 2>&1
del /f /q "{zip_path}" > nul 2>&1
start "" "{os.path.join(app_dir, 'ScreenTale.exe')}"
del /f /q "%~f0" > nul 2>&1
exit
""")

            creation_flag = 0x08000000 if sys.platform == "win32" else 0
            subprocess.Popen(["cmd.exe", "/c", updater_bat], creationflags=creation_flag, close_fds=True)

            self.finished.emit()
        except Exception as e:
            self.failed.emit(str(e))