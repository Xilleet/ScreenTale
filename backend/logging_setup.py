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
    path = os.path.join(app_dir, LOG_NAME)

    _log_file = open(path, "w", encoding="utf-8", buffering=1)  # line-buffered

    class _Tee:
        """Пишет во все потоки сразу; ошибки отдельных потоков глотает
        (в windowed-режиме реальной консоли нет — None просто пропустится)."""

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
        """stdout + файл; если stdout нет (windowed) — только файл, без дублей."""
        targets = []
        if stream is not None:
            targets.append(stream)
        targets.append(_log_file)
        return _Tee(*targets)

    sys.stdout = _wrap(sys.stdout)
    sys.stderr = _wrap(sys.stderr)

    # Нативные краши (segfault и пр.) — тоже в app.log
    try:
        faulthandler.enable(file=_log_file)
    except Exception:
        faulthandler.enable()

    print(f"=== ScreenTale, запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===")