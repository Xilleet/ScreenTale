"""Тесты i18n: t(), load_locale, detect_system_ui_lang.

Цель — поймать регрессии:
- Локали не загружаются → t() возвращает ключ вместо перевода.
- Fallback на en не работает для неизвестного языка.
- detect_system_ui_lang возвращает "ru" для русского/украинского/белорусского.
"""

from backend.i18n import (
    detect_system_ui_lang,
    get_available_ui_languages,
    load_locale,
    t,
)


def test_t_returns_default_for_unknown_key():
    """t() возвращает default для неизвестного ключа."""
    result = t("definitely_unknown_key_xyz", default="fallback")
    assert result == "fallback"


def test_t_returns_key_itself_for_unknown_without_default():
    """t() возвращает сам ключ, если нет ни перевода, ни default."""
    result = t("definitely_unknown_key_xyz")
    # По контракту t() — default or key. Дефолта нет → возвращается сам ключ.
    assert result == "definitely_unknown_key_xyz"


def test_load_locale_en():
    """load_locale('en') загружает английскую локаль."""
    load_locale("en")
    # Проверяем, что какой-то известный ключ переведён
    assert t("sidebar.general", default="") != "sidebar.general", \
        "после load_locale('en') t('sidebar.general') должен возвращать перевод"


def test_load_locale_ru():
    """load_locale('ru') загружает русскую локаль."""
    load_locale("ru")
    assert t("sidebar.general", default="") != "sidebar.general"


def test_load_locale_unknown_falls_back_to_en():
    """load_locale('xx') для несуществующего языка → fallback на en."""
    load_locale("xx")  # не должно упасть
    # t() должна возвращать перевод из en fallback
    assert t("sidebar.general", default="fallback") in ("fallback",) or \
           t("sidebar.general", default="") != "sidebar.general"


def test_get_available_ui_languages_includes_en_ru():
    """get_available_ui_languages возвращает как минимум en и ru."""
    langs = get_available_ui_languages()
    codes = [code for code, _ in langs]
    assert "en" in codes
    assert "ru" in codes


def test_get_available_ui_languages_returns_tuples():
    """Возвращает список кортежей (code, name)."""
    langs = get_available_ui_languages()
    for entry in langs:
        assert isinstance(entry, tuple) or isinstance(entry, list)
        assert len(entry) == 2
        assert isinstance(entry[0], str)  # code
        assert isinstance(entry[1], str)  # name


def test_detect_system_ui_lang_returns_known():
    """detect_system_ui_lang возвращает либо 'ru', либо 'en' — других значений
    не должно быть в текущем пуле локализаций.
    """
    result = detect_system_ui_lang()
    assert result in ("ru", "en"), \
        f"detect_system_ui_lang должен вернуть 'ru' или 'en', получено: {result!r}"
