#!/usr/bin/env python
"""Удобный раннер тестов ScreenTale.

Запуск:
    python run_tests.py            # все тесты
    python run_tests.py tests/test_opacity.py    # конкретный файл
    python run_tests.py -k opacity  # по ключевому слову
    python run_tests.py --cov       # с покрытием (нужен pytest-cov)

Тесты запускаются в offscreen-режиме Qt — дисплей не требуется.
CI-friendly: код возврата 0 = все прошли, 1 = есть провалы.

Вывод:
    - Полный отчёт сохраняется в tests/test_report.txt (перезапись каждый прогон).
    - Summary печатается в консоль.
    - При двойном клике на Windows — окно остаётся открытым до нажатия Enter,
      чтобы пользователь успел прочитать результат.
"""
import datetime
import io
import os
import sys

# Offscreen Qt — тесты можно гонять на Linux-сервере без X-сервера.
# Должно стоять ДО импорта PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Добавляем корень проекта в sys.path, чтобы `import backend...` работало
# независимо от того, откуда запущен run_tests.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _capture_stdout():
    """Перехватывает stdout в StringIO, чтобы потом записать в файл и в консоль.

    Возвращает (buffer, restore_func). restore_func() возвращает накопленный текст
    и восстанавливает оригинальный stdout.
    """
    original = sys.stdout
    buffer = io.StringIO()

    class _Tee:
        """Пишет одновременно в оригинальный stdout и в buffer.

        Проксирует нужные атрибуты файла (isatty, encoding, fileno) на
        оригинальный stdout, иначе pytest падает на TerminalReporter.__init__.
        """
        def write(self, data):
            try:
                original.write(data)
            except Exception:
                pass  # консоль могла закрыться
            buffer.write(data)
        def flush(self):
            try:
                original.flush()
            except Exception:
                pass
        def isatty(self):
            try:
                return original.isatty()
            except Exception:
                return False
        @property
        def encoding(self):
            return getattr(original, "encoding", "utf-8") or "utf-8"
        def fileno(self):
            return original.fileno()
        def __getattr__(self, name):
            # Прочие атрибуты (closed, mode, newlines, ...) — на оригинал.
            return getattr(original, name)

    sys.stdout = _Tee()

    def restore():
        sys.stdout = original
        return buffer.getvalue()

    return buffer, restore


def main():
    args = sys.argv[1:] if len(sys.argv) > 1 else ["tests/"]

    # Готовим файл отчёта рядом с тестами.
    tests_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")
    report_path = os.path.join(tests_dir, "test_report.txt")

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 70)
    print(f"  ScreenTale Test Runner — {timestamp}")
    print("=" * 70)
    print(f"  Платформа: {sys.platform} | Python {sys.version.split()[0]}")
    print(f"  Аргументы: {' '.join(args) if args else '(нет — все тесты)'}")
    print(f"  Отчёт: {report_path}")
    print("=" * 70)
    print()

    import pytest

    buffer, restore = _capture_stdout()
    try:
        exit_code = pytest.main(
            ["-v", "--tb=short", "-rN"] + args
        )
    except Exception as e:
        # Если pytest.main упал — нужно вернуть stdout, чтобы увидеть трейсбек
        restore()
        print(f"\n[FATAL] pytest.main упал с исключением: {e}")
        _wait_on_windows_no_args()
        return 1

    captured = restore()

    # Дописываем в отчёт (перезаписываем — без истории, всегда свежий снапшот)
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"ScreenTale Test Report — {timestamp}\n")
            f.write(f"Python: {sys.version.split()[0]} | Платформа: {sys.platform}\n")
            f.write(f"Аргументы: {' '.join(args) if args else '(нет — все тесты)'}\n")
            f.write("=" * 70 + "\n")
            f.write(captured)
        print(f"\n[OK] Отчёт сохранён: {report_path}")
    except OSError as e:
        print(f"\n[WARN] Не удалось сохранить отчёт: {e}")

    print(f"\n[RESULT] Код возврата: {exit_code} "
          f"({'ВСЕ ПРОШЛИ' if exit_code == 0 else 'ЕСТЬ ПРОВАЛЫ'})")

    _wait_on_windows_no_args()
    return exit_code


def _wait_on_windows_no_args():
    """При двойном клике на Windows (без аргументов) — держим окно открытым.

    Условие: запущен без аргументов И stdin не перенаправлен (интерактивный).
    В CI (с аргументами или с перенаправленным stdin) — выходим сразу.
    """
    if sys.argv[1:]:
        return  # есть аргументы — это CLI-режим, не держим окно
    if not sys.stdin.isatty():
        return  # stdin перенаправлен — это CI, не держим
    try:
        input("\n[Enter] — закрыть окно...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


if __name__ == "__main__":
    sys.exit(main())
