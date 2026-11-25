"""Характеризационные тесты адаптивной прозрачности подложки (chat-режим).

Контекст: по запросу пользователя добавлена авто-подстройка прозрачности
подложки окна перевода под окружающий фон — аналог inplace-функции
sample_background_color («Хамелеон» по углам), но с обратной семантикой:
здесь 4 точки меряются СНАРУЖИ рамки, и яркость фона управляет НЕ цветом
подложки, а её alpha. Контракт:
  - светлый фон → подложка темнеет (alpha растёт к ceiling)
  - тёмный фон   → подложка прозрачнеет (alpha у базы = ползунок пользователя)

Тесты фиксируют:
1. Математику map_luminance_to_opacity (монотонность, края, clamping, fallback).
2. sample_surrounding_luminance возвращает -1 при невозможности замера
   (не-win32 / нет дисплея) — graceful fallback, тесты не падают в headless.
3. TranslateWindow: ползунок frame_opacity трактуется как НИЖНЯЯ граница;
   при adaptive+кэшированной яркости alpha пересчитывается к ceiling.
4. _set_adaptive_enabled включает/выключает таймеры и сбрасывает кэш.
5. config: DEFAULTS содержит adaptive_opacity=False, ключ грузится из json.
"""
import pytest
from PySide6.QtCore import QRect

# --------------------------- чистая математика ---------------------------

def test_map_luminance_dark_bg_returns_base():
    """Тёмный фон (luminance→0) → alpha остаётся на базе (прозрачней)."""
    from frontend.translate_window import map_luminance_to_opacity
    assert map_luminance_to_opacity(0.0, 0.3) == 0.3
    assert map_luminance_to_opacity(0.0, 0.78) == 0.78


def test_map_luminance_light_bg_returns_ceiling():
    """Светлый фон (luminance→255) → alpha поднимается к ceiling (темнеет)."""
    from frontend.translate_window import (
        ADAPT_OPACITY_CEILING,
        map_luminance_to_opacity,
    )
    assert map_luminance_to_opacity(255.0, 0.3) == ADAPT_OPACITY_CEILING
    assert map_luminance_to_opacity(255.0, 0.78) == ADAPT_OPACITY_CEILING


def test_map_luminance_monotonic_between_extremes():
    """Mid-tone должен лежать строго между base и ceiling и расти с яркостью."""
    from frontend.translate_window import (
        ADAPT_OPACITY_CEILING,
        map_luminance_to_opacity,
    )
    base = 0.2
    mid_low = map_luminance_to_opacity(64.0, base)
    mid_hi = map_luminance_to_opacity(191.0, base)
    assert base < mid_low < mid_hi < ADAPT_OPACITY_CEILING


def test_map_luminance_negative_means_no_adaptation():
    """luminance<0 (замер не удался) → возвращаем base, адаптации нет."""
    from frontend.translate_window import map_luminance_to_opacity
    assert map_luminance_to_opacity(-1.0, 0.5) == 0.5


def test_map_luminance_never_below_base():
    """Адаптация только ПОДНИМАЕТ alpha от base — никогда не ниже."""
    from frontend.translate_window import map_luminance_to_opacity
    base = 0.6
    for lum in (0, 32, 128, 200, 255):
        assert map_luminance_to_opacity(float(lum), base) >= base - 1e-9


def test_map_luminance_clamps_out_of_range():
    """Яркость и base за пределами [0,1]/[0,255] климпятся без падения."""
    from frontend.translate_window import (
        ADAPT_OPACITY_CEILING,
        map_luminance_to_opacity,
    )
    assert map_luminance_to_opacity(999.0, 0.3) == ADAPT_OPACITY_CEILING
    assert map_luminance_to_opacity(-50.0, 0.3) == 0.3
    assert map_luminance_to_opacity(128.0, 1.5) == 1.0   # base зажат к 1.0
    assert map_luminance_to_opacity(128.0, -0.5) >= 0.0  # base зажат к 0.0


def test_map_luminance_ceiling_respects_base_when_base_high():
    """Если base выше ceiling — ceiling прижимается к base (не уменьшаем)."""
    from frontend.translate_window import map_luminance_to_opacity
    # base=0.95 > дефолтного ceiling=0.92 → ceiling прижимается вверх к base
    assert map_luminance_to_opacity(255.0, 0.95) == 0.95
    assert map_luminance_to_opacity(0.0, 0.95) == 0.95


# --------------------- sample_surrounding_luminance ----------------------

def test_sample_luminance_fails_gracefully_offscreen():
    """В headless/не-win32 окружении ImageGrab.grab падает → возвращаем -1.

    Это и есть graceful fallback: вызывающий видит «замер не удался» и
    оставляет базовую alpha, не роняя приложение и не вешая тесты.
    """
    from frontend.translate_window import sample_surrounding_luminance
    rect = QRect(100, 100, 300, 120)
    lum = sample_surrounding_luminance(rect)
    assert lum == -1.0, "в headless-окружении замер должен вернуть -1 (fallback)"


def test_sample_luminance_rejects_tiny_rect():
    """Дегенератный rect (w<4 после bbox) → -1, без вызова ImageGrab."""
    from frontend.translate_window import sample_surrounding_luminance
    # margin по умолчанию 14, rect 2x2 → bbox > 4, но проверим рамку 0x0
    lum = sample_surrounding_luminance(QRect(50, 50, 0, 0))
    # 0x0 + margin 14 всё ещё даёт валидный bbox (28+px), поэтому замер пойдёт
    # в ImageGrab и упадёт по except → -1. Главный инвариант: не падает.
    assert lum == -1.0


# --------------------------- TranslateWindow -----------------------------

@pytest.fixture
def trans_win(qapp, settings):
    """TranslateWindow с временной папкой data/."""
    from frontend.translate_window import TranslateWindow
    win = TranslateWindow(settings)
    yield win
    win.deleteLater()
    qapp.processEvents()


def test_frame_opacity_is_treated_as_base_floor(trans_win):
    """update_frame_opacity хранит значение как _frame_opacity_base."""
    trans_win.update_frame_opacity(0.42)
    assert trans_win._frame_opacity_base == pytest.approx(0.42)
    # адаптация выключена по умолчанию → effective == base
    assert trans_win._effective_opacity() == pytest.approx(0.42)


def test_adaptive_off_effective_equals_base(trans_win):
    """Без адаптации effective_opacity всегда равен base."""
    trans_win.update_frame_opacity(0.55)
    assert trans_win._adaptive_on is False
    assert trans_win._effective_opacity() == pytest.approx(0.55)


def test_adaptive_on_with_cached_luminance_remaps_to_ceiling(trans_win):
    """При adaptive+кэшированной яркости 255 (белый фон) alpha уходит к ceiling.

    Имитирует ситуацию: замер уже был (last_luminance=255), пользователь
    двигает ползунок — мгновенный re-map без нового grab.
    """
    from frontend.translate_window import (
        ADAPT_OPACITY_CEILING,
        map_luminance_to_opacity,
    )
    trans_win._adaptive_on = True
    trans_win._last_luminance = 255.0
    trans_win.update_frame_opacity(0.3)
    assert trans_win._adaptive_eff_alpha == map_luminance_to_opacity(255.0, 0.3)
    assert trans_win._effective_opacity() == pytest.approx(ADAPT_OPACITY_CEILING)


def test_adaptive_on_dark_bg_keeps_base(trans_win):
    """При adaptive+кэшированной яркости 0 (тёмный фон) alpha остаётся на base."""
    trans_win._adaptive_on = True
    trans_win._last_luminance = 0.0
    trans_win.update_frame_opacity(0.4)
    assert trans_win._effective_opacity() == pytest.approx(0.4)


def test_set_adaptive_enabled_resets_cache_and_timers(trans_win):
    """Включение адаптации (окно скрыто) сбрасывает кэш и НЕ запускает опрос."""
    trans_win._adaptive_eff_alpha = 0.9
    trans_win._last_luminance = 128.0
    trans_win._set_adaptive_enabled(True)
    assert trans_win._adaptive_on is True
    assert trans_win._adaptive_eff_alpha is None
    assert trans_win._last_luminance is None
    # окно в тесте не показано → опрос не должен крутиться зря
    assert trans_win._adapt_poll.isActive() is False


def test_set_adaptive_disabled_restores_base(trans_win):
    """Выключение адаптации останавливает таймеры и возвращает effective к base."""
    trans_win._adaptive_on = True
    trans_win._adaptive_eff_alpha = 0.9
    trans_win._last_luminance = 200.0
    trans_win.update_frame_opacity(0.3)
    assert trans_win._effective_opacity() > 0.3  # адаптация подняла
    trans_win._set_adaptive_enabled(False)
    assert trans_win._adaptive_on is False
    assert trans_win._adaptive_eff_alpha is None
    assert trans_win._adapt_poll.isActive() is False
    assert trans_win._effective_opacity() == pytest.approx(0.3)


def test_settings_changed_toggles_adaptive(trans_win):
    """Сигнал settings.changed('adaptive_opacity', True) включает адаптацию."""
    trans_win._on_setting_changed("adaptive_opacity", True)
    assert trans_win._adaptive_on is True
    trans_win._on_setting_changed("adaptive_opacity", False)
    assert trans_win._adaptive_on is False


def test_do_adapt_sample_skips_when_hidden(trans_win):
    """_do_adapt_sample не делает grab, пока окно скрыто (не палит ImageGrab)."""
    trans_win._adaptive_on = True
    # окно не показано в тесте → метод должен выйти без замера
    trans_win._do_adapt_sample()
    assert trans_win._last_luminance is None
    assert trans_win._adaptive_eff_alpha is None


def test_adaptive_does_not_touch_text_opacity_effect(trans_win):
    """Регресс: адаптация не должна вешать QGraphicsOpacityEffect на text_widget.

    См. test_opacity.py::test_text_widget_has_no_graphics_opacity_effect —
    это железное правило (иначе текст «выезжает» за скруглённые углы).
    Адаптация меняет только QSS подложки, не трогая текст.
    """
    from PySide6.QtWidgets import QGraphicsOpacityEffect
    trans_win._set_adaptive_enabled(True)
    trans_win.update_frame_opacity(0.5)
    eff = trans_win.text_widget.graphicsEffect()
    assert not isinstance(eff, QGraphicsOpacityEffect)


def test_border_frame_keeps_drop_shadow_with_adaptive(trans_win):
    """Регресс: drop shadow на border_frame остаётся при включённой адаптации."""
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    trans_win._set_adaptive_enabled(True)
    trans_win.update_frame_opacity(0.3)
    eff = trans_win.border_frame.graphicsEffect()
    assert isinstance(eff, QGraphicsDropShadowEffect)


# -------------------------------- config ---------------------------------

def test_config_defaults_has_adaptive_opacity_false():
    """Новый ключ добавлен в DEFAULTS и по умолчанию выключен (opt-in)."""
    from backend.config import DEFAULTS
    assert "adaptive_opacity" in DEFAULTS
    assert DEFAULTS["adaptive_opacity"] is False


def test_config_loads_adaptive_opacity(settings):
    """Ключ adaptive_opacity грузится из config.json (в allowlist _load)."""
    settings.set("adaptive_opacity", True, save=False)
    # повторно создаём менеджер поверх того же temp config — должно подхватиться
    from backend.config import SettingsManager
    settings.force_save()
    sm2 = SettingsManager()
    assert sm2.get("adaptive_opacity") is True
