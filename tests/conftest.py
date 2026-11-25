"""Общие фикстуры для тестов ScreenTale.

Запуск: см. pytest.ini и run_tests.py в корне проекта.

Главные фикстуры:
    qapp           — сессионная QApplication (headless offscreen)
    temp_data_dir  — временная папка data/, изолированная от боевой
    settings       — SettingsManager с temp_data_dir
"""
import os
import sys

# Offscreen Qt — обязательно ДО импорта PySide6.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Добавляем корень проекта в sys.path, чтобы `import backend...` работало
# независимо от того, откуда запущен pytest.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# Qt-зависимая часть вынесена в отложенный импорт, чтобы pure-python тесты
# (test_ocr, test_translation, test_languages, test_config) могли работатьать
# даже в окружении без libEGL. Qt создаётся только если какой-то тест
# запросил фикстуру qapp.
def _import_qt():
    """Ленивый импорт PySide6 — нужен только Qt-тестам."""
    from PySide6.QtWidgets import QApplication
    return QApplication


@pytest.fixture(scope="session")
def qapp():
    """Сессионная QApplication для всех Qt-тестов.

    Qt требует ровно один QApplication на процесс. Если уже есть instance
    (например, из ранее импортированного модуля) — переиспользуем.
    """
    QApplication = _import_qt()
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    # Чтобы тесты не падали от случайного нажатия Esc в QMenu и т.п.
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def temp_data_dir(tmp_path, monkeypatch):
    """Изолированная папка data/ для теста.

    Без этого SettingsManager пишет в боевую data/config.json пользователя,
    что при прогоне тестов на dev-машине ломает реальные настройки.

    Меняет:
        backend.config.CONFIG_PATH  — на временный
        backend.config.get_data_dir — на временную
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.json"

    # CONFIG_PATH вычисляется при импорте config.py, поэтому патчим сам атрибут.
    import backend.config as cfg
    monkeypatch.setattr(cfg, "CONFIG_PATH", str(config_path), raising=True)
    monkeypatch.setattr(cfg, "get_data_dir", lambda: str(data_dir), raising=True)

    yield data_dir


@pytest.fixture
def settings(temp_data_dir):
    """Готовый SettingsManager с temp_data_dir."""
    from backend.config import SettingsManager
    sm = SettingsManager()
    yield sm
    # force_save не нужен — temp_data_dir всё равно умрёт с tmp_path.
