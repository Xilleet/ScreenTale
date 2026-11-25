"""Регрессионные тесты для разделения opacity.

Контекст: в v0.8 разрезали общий ползунок `opacity` на `frame_opacity`
(подложка) и `text_opacity` (текст). Первая реализация использовала
QGraphicsOpacityEffect на QTextEdit — это приводило к багу: текст
«выезжал» за скруглённые углы рамки, потому что effect рендерит виджет в
прямоугольный offscreen-pixmap, не уважающий border-radius родителя.

Тесты ниже гарантируют, что:
1. text_opacity применяется через QTextCharFormat.setForeground(QColor с alpha),
   а НЕ через QGraphicsOpacityEffect.
2. При смене text_opacity «на лету» пере-рендерится вся история чата,
   а не только новые реплики.
3. frame_opacity меняет QSS подложки (rgba alpha), не трогая тень.
4. _build_frame_qss всегда сохраняет border-radius, иначе текст снова
   начнёт «выезжать».
"""
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsOpacityEffect


@pytest.fixture
def trans_win(qapp, settings):
    """TranslateWindow с временной папкой data/."""
    from frontend.translate_window import TranslateWindow
    win = TranslateWindow(settings)
    yield win
    win.deleteLater()
    qapp.processEvents()


def test_text_widget_has_no_graphics_opacity_effect(trans_win):
    """text_widget НЕ должен иметь QGraphicsOpacityEffect.

    Если кто-то вернёт `_text_opacity_effect = QGraphicsOpacityEffect(text_widget)`
    — этот тест провалится, и разработчик увидит ссылку на этот файл с
    объяснением, почему так делать нельзя.
    """
    eff = trans_win.text_widget.graphicsEffect()
    assert not isinstance(eff, QGraphicsOpacityEffect), (
        "QGraphicsOpacityEffect на QTextEdit рендерит виджет в ПРЯМОУГОЛЬНЫЙ "
        "pixmap. Этот pixmap не уважает border-radius:border_frame. Когда "
        "frame_opacity < 1.0, pixmap становится виден — текст «выезжает» за "
        "скруглённые углы. Используйте QColor(r,g,b,alpha) через QTextCharFormat."
    )


def test_border_frame_keeps_drop_shadow(trans_win):
    """Drop shadow на border_frame должен остаться — он даёт мягкую тень окна.

    Раньше shadow конфликтовал с QGraphicsOpacityEffect (последний затирал
    первого, т.к. setGraphicsEffect один на виджет). После перехода на
    QColor alpha для текста — shadow на border_frame снова работает.
    """
    eff = trans_win.border_frame.graphicsEffect()
    assert isinstance(eff, QGraphicsDropShadowEffect), \
        "border_frame должен иметь QGraphicsDropShadowEffect (мягкая тень окна)"


def test_text_opacity_applied_to_foreground_alpha(trans_win, settings):
    """text_opacity применяется к цвету текста через alpha-канал QColor.

    При text_opacity=0.5 — alpha должна быть 127 (±5 на округление).
    При text_opacity=1.0 — alpha=255 (полностью непрозрачный).
    """
    settings.set("text_opacity", 0.5)
    trans_win.show_translation("(21:19:44) Многократный сбор наград")

    # Берём курсор в начало документа и читаем charFormat первого блока
    doc = trans_win.text_widget.document()
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    fmt = cursor.charFormat()
    color = fmt.foreground().color()
    # 0.5 * 255 = 127.5 → 127 (int). Допускаем ±5 на округление float.
    assert 120 <= color.alpha() <= 132, \
        f"alpha должна быть ~127 при text_opacity=0.5, получено {color.alpha()}"


def test_text_opacity_full_alpha_at_1(trans_win, settings):
    """При text_opacity=1.0 alpha текста = 255."""
    settings.set("text_opacity", 1.0)
    trans_win.show_translation("(21:19:44) Тестовая реплика")
    doc = trans_win.text_widget.document()
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    color = cursor.charFormat().foreground().color()
    assert color.alpha() == 255


def test_text_opacity_zero_alpha_at_0(trans_win, settings):
    """При text_opacity=0.0 alpha текста = 0 (полностью прозрачный).

    Граничный случай: пользователь опустил ползунок в ноль.
    Текст становится невидимым — это допустимо (UI не даёт это сделать,
    text_opacity_slider начинается с 20%, но update_text_opacity должна
    корректно обработать и 0.0).
    """
    settings.set("text_opacity", 0.0)
    trans_win.show_translation("(21:19:44) Невидимая реплика")
    doc = trans_win.text_widget.document()
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    color = cursor.charFormat().foreground().color()
    assert color.alpha() == 0


def test_text_opacity_change_rerenders_history(trans_win, settings):
    """Смена text_opacity «на лету» пере-рендерит уже напечатанные реплики.

    Без _reapply_text_opacity — старый текст сохранял бы alpha, с которым
    был напечатан, и пользователь не видел бы реакции на движение ползунка.
    """
    settings.set("text_opacity", 1.0)
    trans_win.show_translation("(21:19:44) Первая реплика")
    trans_win.show_translation("(21:19:45) Вторая реплика")

    # Проверяем, что обе реплики имеют alpha=255
    doc = trans_win.text_widget.document()
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    assert cursor.charFormat().foreground().color().alpha() == 255

    # Меняем text_opacity «на лету»
    settings.set("text_opacity", 0.3)
    # После re-apply первая реплика должна иметь alpha ~76 (0.3*255=76.5)
    cursor = QTextCursor(doc)
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    new_alpha = cursor.charFormat().foreground().color().alpha()
    assert 70 <= new_alpha <= 82, \
        f"alpha должна быть ~76 при text_opacity=0.3, получено {new_alpha}"


def test_history_cached_after_show_translation(trans_win):
    """show_translation складирует (timestamp, body) в _history для re-рендера."""
    trans_win.show_translation("(21:19:44) Тест")
    trans_win.show_translation("(21:19:45) Вторая")
    assert len(trans_win._history) == 2
    assert trans_win._history[0] == ("(21:19:44)", "Тест")
    assert trans_win._history[1] == ("(21:19:45)", "Вторая")


def test_clear_history_clears_cache(trans_win):
    """clear_history чистит и документ, и _history."""
    trans_win.show_translation("(21:19:44) Тест")
    trans_win.clear_history()
    assert trans_win._history == []
    assert trans_win.text_widget.toPlainText().strip() == ""


def test_frame_opacity_qss_preserves_border_radius(trans_win, settings):
    """_build_frame_qss ВСЕГДА содержит border-radius: 14px.

    Без него скругления пропадут — а с ними и маскировка прямоугольного
    text_widget (опять вылезет та же багулина).
    """
    for opacity in [0.0, 0.1, 0.5, 0.78, 1.0]:
        qss = trans_win._build_frame_qss(opacity)
        assert "border-radius: 14px" in qss, \
            f"border-radius потерян при frame_opacity={opacity}: {qss!r}"


def test_frame_opacity_qss_alpha_scales(trans_win):
    """RGBA alpha в QSS масштабируется с frame_opacity.
    """
    # 1.0 → alpha=255
    qss_full = trans_win._build_frame_qss(1.0)
    assert "rgba(22, 22, 26, 255)" in qss_full

    # 0.5 → alpha≈127 (0.5*255=127.5→127)
    qss_half = trans_win._build_frame_qss(0.5)
    assert "rgba(22, 22, 26, 127)" in qss_half or \
           "rgba(22, 22, 26, 128)" in qss_half

    # 0.0 → alpha=0
    qss_zero = trans_win._build_frame_qss(0.0)
    assert "rgba(22, 22, 26, 0)" in qss_zero


def test_frame_opacity_clamps_out_of_range(trans_win):
    """_build_frame_qss должен clamp'ить alpha в 0..1.

    Если случайно передать 1.5 или -0.3 — не должно быть alpha > 255 или < 0.
    """
    qss_high = trans_win._build_frame_qss(1.5)
    assert "rgba(22, 22, 26, 255)" in qss_high, "alpha > 1.0 должно clamp'иться к 255"

    qss_low = trans_win._build_frame_qss(-0.5)
    assert "rgba(22, 22, 26, 0)" in qss_low, "alpha < 0 должно clamp'иться к 0"
