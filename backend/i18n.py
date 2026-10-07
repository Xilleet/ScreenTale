"""Модуль мультиязычности интерфейса (i18n) для ScreenTale."""
import json
import os
import sys

from backend.config import get_app_dir
from backend.logging_setup import vlog

# Язык интерфейса по умолчанию
_current_lang = "en"
_translations: dict[str, str] = {}
_fallback_translations: dict[str, str] = {}


def get_locales_dir() -> str:
    """Возвращает путь к папке locales/ рядом с программой или из _internal."""
    d = os.path.join(get_app_dir(), "locales")
    # Если в корне папки нет или она пустая, проверяем распакованный PyInstaller (_MEIPASS)
    if (not os.path.isdir(d) or not os.listdir(d)) and hasattr(sys, "_MEIPASS"):
        d_internal = os.path.join(sys._MEIPASS, "locales")
        if os.path.isdir(d_internal):
            return d_internal

    os.makedirs(d, exist_ok=True)
    return d

def detect_system_ui_lang() -> str:
    """Определяет язык интерфейса Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            # GetUserDefaultUILanguage возвращает первичный язык (Primary Language ID)
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
            # 0x19 = Русский, 0x22 = Украинский, 0x23 = Белорусский
            if lang_id in (0x19, 0x22, 0x23):
                return "ru"
        except Exception:
            pass
    return "en"


def get_available_ui_languages() -> list[tuple[str, str]]:
    """Сканирует папку locales/ и возвращает список доступных языков: [('en', 'English'), ...]."""
    locales_dir = get_locales_dir()
    langs = [("en", "English 🇬🇧"), ("ru", "Русский 🇷🇺")]
    existing_codes = {"en", "ru"}

    if os.path.isdir(locales_dir):
        for f in sorted(os.listdir(locales_dir)):
            if f.endswith(".json"):
                code = f[:-5].lower()
                if code not in existing_codes:
                    # Пытаемся прочитать красивое название языка из JSON
                    try:
                        with open(os.path.join(locales_dir, f), "r", encoding="utf-8") as jf:
                            data = json.load(jf)
                            name = data.get("_language_name", code.upper())
                            langs.append((code, name))
                            existing_codes.add(code)
                    except Exception:
                        langs.append((code, code.upper()))
                        existing_codes.add(code)

    return langs


def load_locale(lang_code: str):
    """Загружает выбранный языковой файл."""
    global _current_lang, _translations, _fallback_translations
    _current_lang = lang_code
    locales_dir = get_locales_dir()

    # 1. Загружаем английский как гарантированную подушку безопасности (fallback)
    en_file = os.path.join(locales_dir, "en.json")
    if os.path.isfile(en_file):
        try:
            with open(en_file, "r", encoding="utf-8") as f:
                _fallback_translations = json.load(f)
        except Exception:
            _fallback_translations = {}

    # 2. Загружаем целевой язык
    target_file = os.path.join(locales_dir, f"{lang_code}.json")
    if os.path.isfile(target_file):
        try:
            with open(target_file, "r", encoding="utf-8") as jf:
                _translations = json.load(jf)
            print(f"[i18n] Загружен язык интерфейса: {lang_code} (строк: {len(_translations)})")
            vlog(f"[i18n] Загружен язык интерфейса: {lang_code} ({len(_translations)} строк)")
            return
        except Exception as e:
            vlog(f"[i18n] Ошибка чтения {target_file}: {e}")

    _translations = _fallback_translations


def t(key: str, default: str = "") -> str:
    """Главная функция перевода интерфейса. Возвращает строку по ключу."""
    # 1. Ищем в текущем языке
    if key in _translations:
        return _translations[key]
    # 2. Ищем в английском fallback
    if key in _fallback_translations:
        return _fallback_translations[key]
    # 3. Отдаем дефолт или сам ключ
    return default or key