"""Тесты window_tracker: SYSTEM_EXCLUDES, get_running_games фильтрация.

Цель — поймать регрессии:
- В список «игр» попадают системные процессы (Discord, Steam, Xbox Game Bar).
- Браузеры (chrome.exe, firefox.exe) засоряют меню привязки.
- ScreenTale сам себя видит в списке (бесконечный цикл привязки).
"""
import pytest

from backend.window_tracker import SYSTEM_EXCLUDES


def test_system_excludes_includes_xbox_game_bar_new_names():
    """Xbox Game Bar в Win10/11 называется Microsoft.XboxGamingOverlay.exe,
    а не gamebar.exe (старое имя). Должны быть оба — для совместимости.
    """
    assert "microsoft.xboxgamingoverlay.exe" in SYSTEM_EXCLUDES, \
        "SYSTEM_EXCLUDES должен включать Microsoft.XboxGamingOverlay.exe (новое имя Game Bar)"
    assert "microsoft.xboxgamebar.exe" in SYSTEM_EXCLUDES, \
        "SYSTEM_EXCLUDES должен включать Microsoft.XboxGameBar.exe (альтернативное имя)"


def test_system_excludes_includes_browsers():
    """Браузеры обычно не нужны как «игры» — засоряют меню привязки."""
    for browser in ("chrome.exe", "firefox.exe", "msedge.exe"):
        assert browser in SYSTEM_EXCLUDES, \
            f"SYSTEM_EXCLUDES должен включать {browser} — иначе попадёт в список игр"


def test_system_excludes_includes_messengers():
    """Мессенджеры (Discord, Telegram, Slack) не должны быть в списке игр."""
    for messenger in ("discord.exe", "telegram.exe", "slack.exe"):
        assert messenger in SYSTEM_EXCLUDES, \
            f"SYSTEM_EXCLUDES должен включать {messenger} — мессенджеры не игры"


def test_system_excludes_includes_game_launchers():
    """Steam-клиент (не сама игра) — должен быть в exclude, т.к. это лаунчер.
    Сама игра (например, eldenring.exe) — НЕ в exclude, это и есть игра.
    """
    assert "steam.exe" in SYSTEM_EXCLUDES, \
        "steam.exe (клиент Steam) должен быть в exclude — это лаунчер, не игра"
    assert "steamwebhelper.exe" in SYSTEM_EXCLUDES


def test_system_excludes_includes_terminals():
    """Терминалы не должны попадать в список «игр».

    Имена хранятся в lowercase — get_running_games использует exe.lower() для
    сравнения. Реальное имя процесса на Win11 — WindowsTerminal.exe, но в
    SYSTEM_EXCLUDES оно лежит как windowsterminal.exe.
    """
    for terminal in ("cmd.exe", "powershell.exe", "conhost.exe",
                     "windowsterminal.exe"):
        assert terminal in SYSTEM_EXCLUDES, \
            f"SYSTEM_EXCLUDES должен включать {terminal} — терминалы не игры"


def test_system_excludes_screentale_self():
    """ScreenTale не должен видеть сам себя в списке игр."""
    assert "screentale.exe" in SYSTEM_EXCLUDES


def test_system_excludes_python():
    """python.exe (dev-режим) не должен попадать в список игр."""
    assert "python.exe" in SYSTEM_EXCLUDES
    assert "py.exe" in SYSTEM_EXCLUDES


def test_system_excludes_all_lowercase():
    """Все имена в SYSTEM_EXCLUDES — lowercase.

    get_running_games использует exe.lower() in SYSTEM_EXCLUDES — если
    имена будут mixed-case, сравнение не сработает.
    """
    for name in SYSTEM_EXCLUDES:
        assert name == name.lower(), \
            f"SYSTEM_EXCLUDES имя должно быть lowercase: {name!r}"


def test_system_excludes_count_grew():
    """После расширения v0.8.1 — минимум 25 имён (раньше было ~15).

    Грубая проверка, что список реально расширили, а не сузили.
    """
    assert len(SYSTEM_EXCLUDES) >= 25, \
        f"SYSTEM_EXCLUDES должен содержать >=25 имён, сейчас {len(SYSTEM_EXCLUDES)}"
