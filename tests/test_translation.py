"""Тесты parse_structured_translation — разбор ответа LLM по тегам [1] [2] ....

Цель — поймать регрессии при multi-bubble In-Place переводе:
- LLM вернул [1] foo [2] bar — должны получить ["foo", "bar"].
- LLM забыл [2] — должны получить ["foo", "", "bar"] (с пустым вторым).
- LLM просто сделал переносы строк без тегов — фоллбэк по \\n.
- LLM вернул одну строку для нескольких кластеров — отдаём её первому.
"""

from main import is_target_script, parse_structured_translation


def test_parse_numbered_blocks():
    """Корректный ответ [1] ... [2] ... → ["foo", "bar"]."""
    text = "[1] Первый баббл\n[2] Второй баббл"
    result = parse_structured_translation(text, expected_count=2)
    assert result == ["Первый баббл", "Второй баббл"]


def test_parse_missing_block_returns_empty():
    """LLM пропустил [2] → ["foo", "", "bar"] (с пустым на пропущенной позиции)."""
    text = "[1] foo [3] bar"
    result = parse_structured_translation(text, expected_count=3)
    assert result == ["foo", "", "bar"]


def test_parse_fallback_to_newlines():
    """Если тегов нет, но есть переносы строк — разбиваем по \\n."""
    text = "Первая реплика\nВторая реплика\nТретья реплика"
    result = parse_structured_translation(text, expected_count=3)
    assert len(result) == 3
    assert result[0] == "Первая реплика"
    assert result[1] == "Вторая реплика"
    assert result[2] == "Третья реплика"


def test_parse_single_string_for_multiple_clusters():
    """LLM вернул одну строку для N кластеров → отдаём её первому,
    остальные добиваем пустыми.
    """
    text = "Единственная реплика"
    result = parse_structured_translation(text, expected_count=3)
    assert len(result) == 3
    assert result[0] == "Единственная реплика"
    assert result[1] == ""
    assert result[2] == ""


def test_parse_extra_blocks_ignored():
    """LLM вернул больше блоков, чем нужно — берём первые expected_count."""
    text = "[1] foo [2] bar [3] baz [4] extra"
    result = parse_structured_translation(text, expected_count=2)
    assert len(result) == 2
    assert result[0] == "foo"
    assert result[1] == "bar"


def test_parse_empty_input():
    """Пустой ввод → список пустых строк длиной expected_count."""
    result = parse_structured_translation("", expected_count=3)
    assert result == ["", "", ""]


def test_parse_zero_expected():
    """expected_count=0 → пустой список."""
    result = parse_structured_translation("whatever", expected_count=0)
    assert result == []


def test_parse_strips_whitespace_in_blocks():
    """Пробелы вокруг содержимого блоков обрезаются."""
    text = "[1]   пробелы по краям   \n[2] \n  чистый  "
    result = parse_structured_translation(text, expected_count=2)
    assert result[0] == "пробелы по краям"
    assert result[1] == "чистый"


# ============================================================================
# is_target_script — детекция «на экране уже целевой язык»
# ============================================================================

def test_is_target_script_ru_cyrillic():
    """Текст преимущественно кириллица → True для dst=ru."""
    assert is_target_script("Привет, мир!", "ru") is True


def test_is_target_script_ru_latin():
    """Английский текст → False для dst=ru (не перевод уже)."""
    assert is_target_script("Hello world!", "ru") is False


def test_is_target_script_ko_hangul():
    """Корейский текст → True для dst=ko."""
    assert is_target_script("안녕하세요 세계", "ko") is True


def test_is_target_script_japanese_kana_and_kanji():
    """Японский (kana + kanji) → True для dst=ja.

    Регрессия: до v0.8 is_target_script не покрывала ja, защита от
    самоперевода молча отключалась. См. AUDIT.md #6.
    """
    assert is_target_script("こんにちは世界", "ja") is True


def test_is_target_script_chinese():
    """Китайский → True для dst=zh.

    Регрессия: до v0.8 не покрывался zh.
    """
    assert is_target_script("你好世界", "zh") is True


def test_is_target_script_en_latin():
    """Английский (латиница) → True для dst=en.

    Это для направления *→en (например, ja→en — перевод на английский).
    До v0.8 не покрывался.
    """
    assert is_target_script("Hello world", "en") is True


def test_is_target_script_es_with_accents():
    """Испанский с диакритикой → True для dst=es."""
    assert is_target_script("¡Hola, qué tal estás?", "es") is True


def test_is_target_script_fr_with_accents():
    """Французский с диакритикой → True для dst=fr."""
    assert is_target_script("Bonjour à tous, ça va?", "fr") is True


def test_is_target_script_de_with_umlaute():
    """Немецкий с умлаутами → True для dst=de."""
    assert is_target_script("Grüße, liebe Freunde!", "de") is True


def test_is_target_script_pl_with_polish_chars():
    """Польский с диакритикой → True для dst=pl."""
    assert is_target_script("Cześć, jak się masz?", "pl") is True


def test_is_target_script_tr_with_turkish_chars():
    """Турецкий с уникальными буквами → True для dst=tr."""
    assert is_target_script("Merhaba, nasılsın İstanbul?", "tr") is True


def test_is_target_script_unknown_language_returns_false():
    """Неизвестный язык → False (не можем определить, безопасно пропустить)."""
    assert is_target_script("anything", "xx") is False
    assert is_target_script("Привет", "xx") is False


def test_is_target_script_empty_text():
    """Пустой или короткий текст → False."""
    assert is_target_script("", "ru") is False
    assert is_target_script("   ", "ru") is False
    assert is_target_script("ab", "ru") is False  # <3 символов


def test_is_target_script_mixed_threshold():
    """25% порог: 1 кириллический символ в английском тексте → False.

    Защита от ложного срабатывания на один случайно распознанный символ.
    """
    # "Hello мир!" — «мир» это 3 из 9 букв = 33%, но с пробелами и знаками
    # считаем по clean = "Hellомир!" — 3 кириллицы из 9 = 33% > 25% → True
    assert is_target_script("Hello мир!", "ru") is True
    # "Hello world" — 0 кириллицы → False
    assert is_target_script("Hello world", "ru") is False


def test_is_target_script_pt_with_portuguese_chars():
    """Португальский (Бразилия) с диакритикой → True для dst=pt."""
    assert is_target_script("Olá, como você está?", "pt") is True


def test_is_target_script_it_with_italian_chars():
    """Итальянский с диакритикой → True для dst=it."""
    assert is_target_script("Ciao, come va? è tutto ok?", "it") is True
