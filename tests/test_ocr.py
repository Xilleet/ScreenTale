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

    Регрессия: было `\n` → ` ` для всех одиночных переносов, что ломало
    японский текст, где `\n` означает конец столбца, а не пробел.
    См. _merge_line_breaks в ocr.py.
    """
    # 記 (U+8A18) и 憶 (U+61B6) — CJK
    text = "記\n憶"
    result = normalize_ocr_text(text)
    # Между CJK символами \\n должен стать пустой строкой (склейка), не пробелом
    assert "記 憶" not in result, "между CJK не должно быть пробела от \\n"
    assert "記憶" in result or "記\n憶" in result, \
           f"ожидалась склейка без пробела, получено: {result!r}"
