#!/usr/bin/env python
"""Удобный раннер тестов ScreenTale.

Запуск:
    python run_tests.py            # все тесты
    python run_tests.py tests/test_opacity.py    # конкретный файл
    python run_tests.py -k opacity  # по ключевому слову
    python run_tests.py --cov       # с покрытием (нужен pytest-cov)

Тесты запускаются в offscreen-режиме Qt — дисплей не требуется.
CI-friendly: код возврата 0 = все прошли, 1 = есть провалы.
"""
import os
import sys

# Offscreen Qt — тесты можно гонять на Linux-сервере без X-сервера.
# Должно стоять ДО импорта PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

if __name__ == "__main__":
    # Если аргументов нет — запускаем все тесты из tests/
    args = sys.argv[1:] if len(sys.argv) > 1 else ["tests/"]
    sys.exit(pytest.main(["-v", "--tb=short"] + args))
