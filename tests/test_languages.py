"""Тесты реестра языков (backend/languages.py).

Цель — поймать регрессии при добавлении новых языков:
- LANGUAGES содержит 12 «золотых» языков.
- BCP-47 теги для Windows OCR корректны (ja → ja-JP, ru → ru-RU).
- Flores-200 коды для NLLB корректны.
- build_llm_system_prompt содержит инструкции «concise» и сохранение нумерации.
"""

from backend.languages import (
    DEFAULT_DST,
    DEFAULT_SRC,
    LANGUAGES,
    build_llm_system_prompt,
    format_pair_badge,
    format_pair_menu_item,
    get_google_code,
    get_lang_flag,
    get_lang_name,
    get_nllb_code,
    get_win_ocr_tag,
)


def test_languages_count():
    """Должно быть 12 «золотых» языков (Any-to-Any пул)."""
    assert len(LANGUAGES) >= 12, \
        f"ожидалось >=12 языков, получено {len(LANGUAGES)}"


def test_languages_have_required_keys():
    """Каждая запись LANGUAGES содержит все необходимые поля."""
    required = {"name", "name_en", "flag", "win_ocr", "nllb", "google"}
    for code, info in LANGUAGES.items():
        missing = required - set(info.keys())
        assert not missing, f"язык {code} не имеет полей: {missing}"


def test_win_ocr_tags_correct():
    """BCP-47 теги для Windows OCR корректны."""
    assert get_win_ocr_tag("ja") == "ja-JP"
    assert get_win_ocr_tag("ru") == "ru-RU"
    assert get_win_ocr_tag("en") == "en-US"
    assert get_win_ocr_tag("zh") == "zh-CN"
    assert get_win_ocr_tag("ko") == "ko-KR"


def test_win_ocr_tag_unknown_falls_back():
    """Неизвестный код → дефолт en-US."""
    assert get_win_ocr_tag("xx") == "en-US"


def test_nllb_codes_flores200_format():
    """Flores-200 коды для NLLB корректны (формат lang_Script)."""
    assert get_nllb_code("ru") == "rus_Cyrl"
    assert get_nllb_code("en") == "eng_Latn"
    assert get_nllb_code("ja") == "jpn_Jpan"
    assert get_nllb_code("zh") == "zho_Hans"


def test_google_codes():
    """Коды для Google Translate / deep_translator."""
    assert get_google_code("ru") == "ru"
    assert get_google_code("en") == "en"
    assert get_google_code("zh") == "zh-CN"


def test_get_lang_name():
    """Имя языка по коду."""
    assert get_lang_name("ru") == "Русский"
    assert get_lang_name("en") == "Английский"
    assert get_lang_name("ja") == "Японский"


def test_get_lang_flag():
    """Эмодзи-флаг по коду."""
    assert get_lang_flag("ru") == "🇷🇺"
    assert get_lang_flag("ja") == "🇯🇵"
    assert get_lang_flag("unknown") == "🌐"


def test_build_llm_system_prompt_contains_key_instructions():
    """Системный промпт содержит ключевые инструкции для LLM."""
    prompt = build_llm_system_prompt("en", "ru")
    assert "concise" in prompt.lower() or "CONCISE" in prompt
    assert "CONCISE" in prompt, "промпт должен требовать компактный перевод"
    assert "[1]" in prompt or "[2]" in prompt, \
        "промпт должен инструктировать про сохранение нумерации"
    assert "Russian" in prompt, "промпт должен называть целевой язык"


def test_build_llm_system_prompt_dynamic_target():
    """Промпт динамически подставляет целевой язык."""
    prompt_en_to_ja = build_llm_system_prompt("en", "ja")
    assert "Japanese" in prompt_en_to_ja
    assert "English" in prompt_en_to_ja

    prompt_ja_to_ru = build_llm_system_prompt("ja", "ru")
    assert "Russian" in prompt_ja_to_ru
    assert "Japanese" in prompt_ja_to_ru


def test_format_pair_badge():
    """Бейдж языковой пары для тулбара: 🇬🇧 ➔ 🇷🇺"""
    badge = format_pair_badge("en", "ru")
    assert "🇬🇧" in badge
    assert "🇷🇺" in badge
    assert "➔" in badge


def test_format_pair_menu_item():
    """Пункт меню языковой пары: 🇬🇧 EN ➔ 🇷🇺 RU"""
    item = format_pair_menu_item("en", "ru")
    assert "🇬🇧" in item
    assert "EN" in item
    assert "🇷🇺" in item
    assert "RU" in item
    assert "➔" in item


def test_default_src_dst():
    """DEFAULT_SRC = en, DEFAULT_DST = ru — дефолтная пара для переводчика-геймера."""
    assert DEFAULT_SRC == "en"
    assert DEFAULT_DST == "ru"
