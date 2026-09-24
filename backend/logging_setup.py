"""Единый лог: файл app.log рядом с приложением + дубль в консоль.

Перехватывает stdout/stderr (принты, warning'и библиотек) и faulthandler
(нативные краши) — всё в одном файле для отправки пользователем.
"""
import faulthandler
import os
import sys
import time

LOG_NAME = "app.log"
_log_file = None   # держим ссылку, чтобы файл не закрылся сборщиком

# Подробный лог (verbose) — полный текст OCR/переводов без обрезки.
# Выставляется в True из AppController после загрузки settings (main.py).
# В обычном режиме — False, vlog() ничего не пишет.
VERBOSE = False


def set_verbose(enabled: bool):
    """Включить/выключить verbose-логирование (вызывается из AppController)."""
    global VERBOSE
    VERBOSE = bool(enabled)
    print(f"[log] verbose-режим: {'ВКЛ — полный лог' if VERBOSE else 'выкл — короткий лог'}")


def vlog(*args, **kwargs):
    """Лог только в verbose-режиме. Каждая строка помечается [verbose].

    Используется для полного текста OCR/переводов — короткий print() рядом
    оставляет обрезанную версию для обычного режима.
    """
    if not VERBOSE:
        return
    # Добавляем префикс [verbose] к первому аргументу, если это строка
    if args and isinstance(args[0], str):
        args = ("[verbose] " + args[0],) + args[1:]
    print(*args, **kwargs)


def setup_logging(app_dir: str):
    global _log_file

    # 1. Сначала сдвигаем старые файлы логов
    _rotate_logs(app_dir, max_backups=2)

    # 2. Открываем новый свежий app.log для текущего запуска программы
    path = os.path.join(app_dir, LOG_NAME)
    _log_file = open(path, "w", encoding="utf-8", buffering=1)  # line-buffered

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams

        def write(self, data):
            for s in self._streams:
                try:
                    s.write(data)
                except Exception:
                    pass
            return len(data)

        def flush(self):
            for s in self._streams:
                try:
                    s.flush()
                except Exception:
                    pass

    # Подмена stdout/stderr — ОДИН раз, здесь. `or _log_file` подставляет
    # файл, если реального потока нет (windowed-сборка без консоли).
    def _wrap(stream):
        targets = []
        if stream is not None:
            targets.append(stream)
        targets.append(_log_file)
        return _Tee(*targets)

    sys.stdout = _wrap(sys.stdout)
    sys.stderr = _wrap(sys.stderr)

    try:
        faulthandler.enable(file=_log_file)
    except Exception:
        faulthandler.enable()

    print(f"=== ScreenTale, запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===")

def _rotate_logs(app_dir: str, max_backups: int = 2):
    """Сдвигает старые логи: app.log -> app_1.log -> app_2.log (сохраняя расширение .log)."""
    base_name, ext = os.path.splitext(LOG_NAME)  # 'app' и '.log'

    for i in range(max_backups, 0, -1):
        src_name = f"{base_name}_{i-1}{ext}" if i > 1 else LOG_NAME
        src = os.path.join(app_dir, src_name)
        dst = os.path.join(app_dir, f"{base_name}_{i}{ext}")

        if os.path.exists(src):
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
            except OSError:
                pass