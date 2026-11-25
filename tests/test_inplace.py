"""Тесты InPlace-режима: cluster_lines, find_optimal_font_size.

Цель — поймать регрессии в эвристиках кластеризации:
- Близкие строки одного абзаца должны склеиваться в один баббл.
- Дальние строки разных углов экрана — разделяться.
- Фильтр шума не должен отрезать знаки диалога (! ? — -).
- Бинарный поиск шрифта должен давать кегль, реально влезающий в рамку.
"""

import pytest
from PySide6.QtCore import QRect


@pytest.fixture
def inplace_canvas(qapp):
    from frontend.inplace_canvas import InPlaceCanvas
    canvas = InPlaceCanvas()
    yield canvas
    canvas.deleteLater()
    qapp.processEvents()


def test_cluster_near_lines_merge(qapp):
    """Две строки одного абзаца (близко по Y, выровнены по X) → 1 кластер."""
    from frontend.inplace_canvas import cluster_lines
    blocks = [
        {"text": "Line 1", "rect": (100, 200, 300, 25)},
        {"text": "Line 2", "rect": (100, 230, 320, 25)},  # зазор 5px (близко)
    ]
    clusters = cluster_lines(blocks)
    assert len(clusters) == 1, "близкие строки должны склеиться в 1 баббл"
    assert clusters[0]["text"] == "Line 1 Line 2"


def test_cluster_distant_lines_separate(qapp):
    """Две строки из разных углов экрана → 2 кластера."""
    from frontend.inplace_canvas import cluster_lines
    blocks = [
        {"text": "Quest in top-left", "rect": (50, 50, 200, 30)},
        {"text": "Dialogue at bottom", "rect": (1200, 800, 500, 60)},  # далеко
    ]
    clusters = cluster_lines(blocks)
    assert len(clusters) == 2, "дальние строки должны остаться независимыми"


def test_cluster_filters_single_char_noise(qapp):
    """Одиночные символы-артефакты OCR (i, e, o) — отбрасываются как шум."""
    from frontend.inplace_canvas import cluster_lines
    blocks = [
        {"text": "i", "rect": (10, 10, 5, 10)},  # шум
        {"text": "e", "rect": (20, 20, 5, 10)},  # шум
        {"text": "Real text here", "rect": (50, 50, 200, 25)},
    ]
    clusters = cluster_lines(blocks)
    assert len(clusters) == 1
    assert "Real text here" in clusters[0]["text"]


def test_cluster_keeps_dialogue_punctuation(qapp):
    """Знаки диалога (! ? — -) НЕ должны отфильтровываться как шум."""
    from frontend.inplace_canvas import cluster_lines
    blocks = [
        {"text": "!", "rect": (10, 10, 5, 10)},
        {"text": "?", "rect": (20, 10, 5, 10)},
        {"text": "—", "rect": (30, 10, 5, 10)},
        {"text": "-", "rect": (40, 10, 5, 10)},
    ]
    clusters = cluster_lines(blocks)
    # Все 4 знака диалога — на одной строке, должны склеиться в 1 кластер
    assert len(clusters) == 1
    assert "!" in clusters[0]["text"]
    assert "?" in clusters[0]["text"]


def test_cluster_empty_input(qapp):
    """Пустой список блоков → пустой список кластеров."""
    from frontend.inplace_canvas import cluster_lines
    assert cluster_lines([]) == []


def test_cluster_caps_at_12(qapp):
    """Если кластеров > 12, оставляем 12 самых длинных по тексту."""
    from frontend.inplace_canvas import cluster_lines
    # 15 блоков в разных углах (дальние друг от друга)
    blocks = [
        {"text": f"text_{i:02d}", "rect": (i * 100, i * 100, 80, 25)}
        for i in range(15)
    ]
    clusters = cluster_lines(blocks)
    assert len(clusters) <= 12, f"должно быть <=12 кластеров, получено {len(clusters)}"


def test_find_optimal_font_size_fits(qapp):
    """Бинарный поиск подбирает кегль, реально влезающий в рамку."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontMetrics

    from frontend.inplace_canvas import find_optimal_font_size

    text = "Быстрый перевод текста с экрана."
    max_w, max_h = 300, 80
    f_size = find_optimal_font_size(text, max_w, max_h, min_sz=8, max_sz=40)

    # Проверяем, что кегль действительно влезает
    metrics = QFontMetrics(QFont("Segoe UI", f_size, QFont.Weight.Bold))
    rect = metrics.boundingRect(QRect(0, 0, max_w, 0),
                                 Qt.TextFlag.TextWordWrap, text)
    assert rect.height() <= max_h, \
        f"высота {rect.height()} > {max_h} при кегле {f_size}"
    assert rect.width() <= max_w, \
        f"ширина {rect.width()} > {max_w} при кегле {f_size}"


def test_find_optimal_font_size_min_floor(qapp):
    """Если текст не влезает даже при min_sz — возвращаем min_sz.
    """
    from frontend.inplace_canvas import find_optimal_font_size
    # Очень длинный текст в маленькую рамку
    text = "A" * 1000
    f_size = find_optimal_font_size(text, max_w=50, max_h=20, min_sz=9, max_sz=40)
    assert f_size == 9, f"при невозможной посадке должен вернуться min_sz, получено {f_size}"


def test_inplace_block_text_opacity_field(inplace_canvas):
    """InPlaceBlock имеет поле text_opacity с дефолтом 1.0.

    Это добавлено в v0.8 для применения text_opacity в InPlace-режиме.
    Если кто-то удалит поле — paintEvent упадёт с AttributeError.
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor

    from frontend.inplace_canvas import InPlaceBlock

    block = InPlaceBlock(
        rect=QRect(0, 0, 100, 50),
        text="test",
        bg_color=QColor(20, 20, 24, 240),
        font_size=14,
    )
    assert block.text_opacity == 1.0


def test_inplace_block_text_opacity_custom(inplace_canvas):
    """InPlaceBlock принимает произвольный text_opacity."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor

    from frontend.inplace_canvas import InPlaceBlock

    block = InPlaceBlock(
        rect=QRect(0, 0, 100, 50),
        text="test",
        bg_color=QColor(20, 20, 24, 240),
        font_size=14,
        text_opacity=0.4,
    )
    assert block.text_opacity == 0.4
