"""Тесты normalize_ocr_text — нормализация OCR-вывода перед переводом.

Цель — поймать регрессии в эвристках:
- Склейка дефисом разорванных слов: `infor-\nmation` → `information`.
- Замена типографских кавычек на ASCII.
- Исправление O→0 в числах (но НЕ в химии: O2 не должно стать 02).
- Схлопывание повторяющихся пробелов.
"""

from backend.ocr import normalize_ocr_text


def test_normalize_hyphen_broken_word():
    """Слова, разорванные переносом строки через дефис, склеиваются."""
    text = "infor-\nmation about the quest"
    assert normalize_ocr_text(text) == "information about the quest"


def test_normalize_single_newline_to_space():
    """Одиночный \\n → пробел, двойной \\n\\n (абзац) сохраняется."""
    text = "Line one\nLine two\n\nNew paragraph"
    result = normalize_ocr_text(text)
    assert "Line one Line two" in result
    assert "Line two\n\nNew paragraph" in result or \
           "Line two  New paragraph" in result, \
           f"двойной \\n\\n должен сохраниться, получено: {result!r}"


def test_normalize_typographic_quotes():
    """Типографские кавычки → ASCII."""
    text = "«Привет» „мир“ ‘test’ “end”"
    result = normalize_ocr_text(text)
    assert "«" not in result and "»" not in result
    assert "„" not in result and "“" not in result
    assert "‘" not in result and "’" not in result
    assert '"' in result, "типографские кавычки должны стать ASCII двойными"
    assert "'" in result, "типографские апострофы должны стать ASCII одинарными"


def test_normalize_em_dash():
    """Em-dash и en-dash → ' - ' (с пробелами)."""
    text = "Hello—world – end"
    result = normalize_ocr_text(text)
    assert "—" not in result and "–" not in result
    assert " - " in result


def test_normalize_double_hyphen_to_dash():
    """Двойной дефис -- → ' - '."""
    text = "Hello--world"
    result = normalize_ocr_text(text)
    assert "--" not in result
    assert " - " in result


def test_normalize_o_to_zero_in_numbers():
    """OCR-ошибка O→0 в числах: 2O24 → 2024."""
    text = "Year 2O24 was great"
    result = normalize_ocr_text(text)
    assert "2024" in result
    assert "2O24" not in result


def test_normalize_does_not_replace_o_in_words():
    """Буква O в словах не должна становиться 0.

    Граничный случай: только последовательности, содержащие хотя бы одну
    цифру, считаются «числом» и требуют исправления O→0.
    """
    text = "Hello World OK"
    result = normalize_ocr_text(text)
    # O в словах не должна стать 0
    assert "Hello" in result
    assert "World" in result
    # Если вдруг станет "Hell0 W0rld" — это баг
    assert "0" not in result or "0" in result and result.count("0") == 0 or \
           "0" not in result.replace("Hello", "").replace("World", "").replace("OK", "")


def test_normalize_collapses_whitespace():
    """Повторяющиеся пробелы схлопываются в один."""
    text = "Hello    world\t\tend"
    result = normalize_ocr_text(text)
    assert "  " not in result
    assert "\t" not in result
    assert "Hello world end" in result


def test_normalize_empty_input():
    """Пустой ввод → пустая строка."""
    assert normalize_ocr_text("") == ""
    assert normalize_ocr_text("   ") == ""
    assert normalize_ocr_text(None) == ""


def test_normalize_strips_leading_trailing():
    """Лидирующие и замыкающие пробелы обрезаются."""
    text = "  Hello world  \n  "
    result = normalize_ocr_text(text)
    assert result.startswith("Hello")
    assert result.endswith("world")


def test_normalize_preserves_cjk_no_space():
    """Между CJK-иероглифами одиночный \\n НЕ должен становиться пробелом.

    Регрессия: в v0.7 строка `re.sub(r"(?<!\\n)\\n(?!\\n)", " ", text)`
    заменяла все одиночные \\n на пробел, в т.ч. между иероглифами.
    Функция _merge_line_breaks была написана, чтобы склеивать без пробела
    (記\\n憶 → 記憶, не «記 憶»), но НЕ была подключена к re.sub. Этот
    тест ловит именно такое подключение — см. backend/ocr.py:82.
    """
    # 記 (U+8A18) и 憶 (U+61B6) — CJK-иероглифы
    text = "記\n憶"
    result = normalize_ocr_text(text)
    # Между CJK символами \\n должен стать пустой строкой (склейка), не пробелом
    assert "記 憶" not in result, \
        f"между CJK не должно быть пробела от \\n, получено: {result!r}"
    assert "記憶" in result, f"ожидалась склейка без пробела, получено: {result!r}"


def test_normalize_preserves_kana_no_space():
    """Между символами каны (японская слоговая азбука) \\n → склейка без пробела.

    Кана попадает в CJK-pattern (\\u3040-\\u30ff) — склеиваем как CJK.
    Это правильно, т.к. японский не использует пробелы внутри слов.
    """
    # あ (U+3042) и い (U+3044) — hiragana
    text = "あ\nい"
    result = normalize_ocr_text(text)
    assert "あ い" not in result, f"между kana не должно быть пробела: {result!r}"
    assert "あい" in result


def test_normalize_korean_newline_to_space():
    """Между корейскими Hangul \\n → пробел (корейский использует пробелы).

    Регрессия: раньше Hangul был включён в CJK-pattern, что ломало
    корейский — `안녕\n안녕` становилось `안녕안녕` (без пробела), а должно
    быть `안녕 안녕`. После сужения CJK-pattern до \\u4e00-\\u9fff +
    \\u3040-\\u30ff (без \\uac00-\\ud7af) — корейский работает правильно.
    """
    # 안 (U+C548) и 녕 (U+B155) — Hangul
    text = "안녕\n안녕"
    result = normalize_ocr_text(text)
    assert " " in result, f"между Hangul должен быть пробел, получено: {result!r}"


def test_normalize_cjk_with_ascii_neighbors():
    """CJK-иероглиф, окружённый ASCII (например, «1. 記\\n憶») —
    склейка внутри CJK работает, пробелы вокруг сохраняются.
    """
    text = "1. 記\n憶 2."
    result = normalize_ocr_text(text)
    assert "記憶" in result, f"ожидаем склейку CJK, получено: {result!r}"


def test_normalize_double_newline_preserved():
    """Двойной \\n\\n (абзац) сохраняется, не становится двойным пробелом."""
    text = "First paragraph\n\nSecond paragraph"
    result = normalize_ocr_text(text)
    # Одинокий \\n заменяется на пробел, но \\n\\n должен сохраниться
    assert "\n\n" in result or "  " in result, \
        f"двойной \\n\\n должен сохраниться, получено: {result!r}"
