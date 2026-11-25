"""Тесты updater: динамическое имя процесса в updater.bat, _parse_version.

Цель — поймать регрессии:
- updater.bat ждёт ScreenTale.exe, но в dev это python.exe → архив
  распаковывается поверх работающего приложения.
- _parse_version неправильно парсит «0.7-beta» или «v0.7.1».
- calculate_sha256 возвращает hex-строку правильной длины.
"""
import hashlib
import os
import pytest


def test_parse_version_plain():
    """'0.4.4' → (0, 4, 4)."""
    from backend.updater import _parse_version
    assert _parse_version("0.4.4") == (0, 4, 4)


def test_parse_version_with_v_prefix():
    """'v0.5.0' → (0, 5, 0) — v префикс должен срезаться."""
    from backend.updater import _parse_version
    assert _parse_version("v0.5.0") == (0, 5, 0)


def test_parse_version_with_beta_suffix():
    """'0.7-beta' → (0, 7) — суффикс должен отсекаться."""
    from backend.updater import _parse_version
    assert _parse_version("0.7-beta") == (0, 7)


def test_parse_version_comparison():
    """Сравнение версий через tuple работает корректно."""
    from backend.updater import _parse_version
    assert _parse_version("0.7.1") > _parse_version("0.7.0")
    assert _parse_version("1.0.0") > _parse_version("0.9.9")
    assert _parse_version("0.7") < _parse_version("0.7.1")


def test_parse_version_garbage_returns_zero_tuple():
    """Некорректная строка → (0, 0, 0), не падает."""
    from backend.updater import _parse_version
    assert _parse_version("garbage") == (0, 0, 0)
    assert _parse_version("") == (0, 0, 0)
    assert _parse_version("v") == (0, 0, 0)


def test_calculate_sha256_known_content(tmp_path):
    """calculate_sha256 возвращает правильный хэш для известного содержимого."""
    from backend.updater import calculate_sha256
    # SHA256 пустой строки — известное значение
    test_file = tmp_path / "test.bin"
    test_file.write_bytes(b"")
    assert calculate_sha256(str(test_file)) == hashlib.sha256(b"").hexdigest()

    # SHA256 «hello»
    test_file.write_bytes(b"hello")
    assert calculate_sha256(str(test_file)) == hashlib.sha256(b"hello").hexdigest()


def test_calculate_sha256_returns_hex_64_chars(tmp_path):
    """SHA256 всегда возвращает 64-символьную hex-строку."""
    from backend.updater import calculate_sha256
    test_file = tmp_path / "test.bin"
    test_file.write_bytes(b"some content for sha256")
    result = calculate_sha256(str(test_file))
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_bat_waits_for_actual_process_name_source():
    """В исходнике updater.py имя процесса — динамическое (sys.executable),
    а не захардкоженное «ScreenTale.exe».

    Регрессия: в v0.7 было `tasklist /FI "IMAGENAME eq ScreenTale.exe"`,
    что в dev-режиме (процесс python.exe) мгновенно завершало wait-loop и
    архив распаковывался поверх работающего приложения.
    """
    updater_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "updater.py"
    )
    with open(updater_py_path, encoding="utf-8") as f:
        source = f.read()

    # Должна быть строка с динамическим именем
    assert "os.path.basename(sys.executable)" in source, \
        "updater.py должен использовать os.path.basename(sys.executable) для имени процесса"

    # Не должно быть захардкоженного ScreenTale.exe в tasklist
    assert 'IMAGENAME eq ScreenTale.exe' not in source, \
        "updater.py НЕ должен жёстко ждать ScreenTale.exe — в dev это python.exe"


def test_bat_uses_current_exe_name_in_template():
    """f-string в bat подставляет {current_exe_name}, не константу."""
    updater_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "updater.py"
    )
    with open(updater_py_path, encoding="utf-8") as f:
        source = f.read()

    # Должна быть f-строка с подстановкой {current_exe_name}
    assert "current_exe_name = os.path.basename(sys.executable)" in source
    assert "{current_exe_name}" in source, \
        "updater.bat шаблон должен использовать {current_exe_name} — динамическое имя"


def test_bat_starts_screentale_exe_after_update():
    """После распаковки запускается ScreenTale.exe (не текущий процесс).

    В dev-режиме (python.exe) это запустит боевую сборку, если она есть.
    В prod (ScreenTale.exe) — перезапуск самого себя.
    """
    updater_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "updater.py"
    )
    with open(updater_py_path, encoding="utf-8") as f:
        source = f.read()

    # После обновления запускается ScreenTale.exe
    assert "start \"\" \"{os.path.join(app_dir, 'ScreenTale.exe')}\"" in source or \
           "start \"\" " in source and "ScreenTale.exe" in source
