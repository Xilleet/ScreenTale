"""Тесты parse_structured_translation — разбор ответа LLM по тегам [1] [2] ....

Цель — поймать регрессии при multi-bubble In-Place переводе:
- LLM вернул [1] foo [2] bar — должны получить ["foo", "bar"].
- LLM забыл [2] — должны получить ["foo", "", "bar"] (с пустым вторым).
- LLM просто сделал переносы строк без тегов — фоллбэк по \\n.
- LLM вернул одну строку для нескольких кластеров — отдаём её первому.
"""

from main import parse_structured_translation


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
