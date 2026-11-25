"""Тесты SettingsManager: загрузка, сохранение, debounce, миграции.

Цель — поймать регрессии:
- Добавили новый ключ в DEFAULTS, забыли добавить в _load → старый config.json
  не подхватит значение.
- Меняем имя ключа без миграции → пользователь теряет настройки.
- set() эмитит changed даже при том же значении (или наоборот — не эмитит).
"""

import json

from backend.config import DEFAULTS, SettingsManager


def test_defaults_contain_opacity_keys():
    """После рефакторинга opacity→frame_opacity/text_outline удалили.
    Эти ключи обязаны быть в DEFAULTS, иначе SettingsManager.get() будет
    всегда возвращать None и ползунки в SettingsWindow не подхватят значения.
    """
    assert "frame_opacity" in DEFAULTS, "DEFAULTS должен содержать frame_opacity"
    assert "text_opacity" in DEFAULTS, "DEFAULTS должен содержать text_opacity"
    assert "text_outline" not in DEFAULTS, "text_outline удалён в v0.8 — не должен остаться"
    assert "opacity" not in DEFAULTS, "старый ключ opacity переименован в frame_opacity"


def test_defaults_ranges_sane():
    """opacity-ключи — float в диапазоне 0..1. Иначе ползунки будут читать кривое.
    """
    assert 0.0 <= DEFAULTS["frame_opacity"] <= 1.0
    assert 0.0 <= DEFAULTS["text_opacity"] <= 1.0


def test_settings_set_get_roundtrip(settings):
    """set() сохраняет, get() возвращает то же значение."""
    settings.set("frame_opacity", 0.42)
    assert abs(settings.get("frame_opacity") - 0.42) < 0.001


def test_settings_set_emits_changed(settings):
    """set() эмитит changed с правильным (key, value)."""
    seen = []
    settings.changed.connect(lambda k, v: seen.append((k, v)))
    settings.set("frame_opacity", 0.55)
    assert ("frame_opacity", 0.55) in seen


def test_settings_set_same_value_no_emit(settings):
    """set() с тем же значением — no-op, changed НЕ эмитится.

    Это документированное поведение SettingsManager — см. audit #2.6.
    Если когда-нибудь поменяем на «всегда эмитить» — этот тест провалится,
    и нужно будет проверить, что все подписчики это переварят.
    """
    settings.set("frame_opacity", 0.5)
    seen = []
    settings.changed.connect(lambda k, v: seen.append((k, v)))
    settings.set("frame_opacity", 0.5)  # то же значение
    assert seen == [], f"не должно быть эмиссии, получено: {seen}"


def test_settings_save_persists_to_disk(settings, temp_data_dir):
    """force_save() пишет на диск синхронно."""
    settings.set("frame_opacity", 0.33)
    settings.force_save()
    cfg_path = temp_data_dir / "config.json"
    assert cfg_path.exists()
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    assert abs(data["frame_opacity"] - 0.33) < 0.001


def test_settings_load_legacy_opacity_migrates(temp_data_dir):
    """Старый config.json (v0.7) с ключом `opacity` без `frame_opacity`
    должен автоматически мигрировать: frame_opacity = opacity.

    Это критично для апгрейда пользователей с v0.7 на v0.8 — иначе
    у них сбросится прозрачность в дефолт.
    """
    cfg_path = temp_data_dir / "config.json"
    # Старый формат v0.7
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "opacity": 0.65,
            "theme": "dark",
            "font_size": 14,
        }, f)

    sm = SettingsManager()
    assert abs(sm.get("frame_opacity") - 0.65) < 0.001, \
        "старый `opacity` должен мигрировать в `frame_opacity`"
    assert sm.get("opacity") is None, \
        "старый `opacity` ключ не должен остаться в _values"
    assert sm.get("text_opacity") == 1.0, \
        "text_opacity должен быть дефолтным, т.к. в старом config его не было"


def test_settings_load_with_existing_frame_opacity(temp_data_dir):
    """Если в config.json есть и `opacity`, и `frame_opacity` — frame_opacity побеждает
    (миграция не перезаписывает уже установленное значение).
    """
    cfg_path = temp_data_dir / "config.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "opacity": 0.99,        # старый
            "frame_opacity": 0.50,  # новый — должен победить
        }, f)

    sm = SettingsManager()
    assert abs(sm.get("frame_opacity") - 0.50) < 0.001


def test_settings_does_not_load_unknown_keys(temp_data_dir):
    """Ключи, не перечисленные в _load, игнорируются.

    Иначе при добавлении ключа в DEFAULTS можно случайно загрузить мусор
    из старого config.json (например, experimental флаги, которые мы убрали).
    """
    cfg_path = temp_data_dir / "config.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({
            "frame_opacity": 0.5,
            "definitely_unknown_key_2024": "should_be_ignored",
        }, f)

    sm = SettingsManager()
    assert sm.get("definitely_unknown_key_2024") is None


def test_settings_reset_defaults_clears_custom_value(settings):
    """reset_defaults возвращает значение из DEFAULTS и эмитит changed.

    Это важно для UI: кнопка «Вернуть стандартные» в Hotkeys должна обновить
    рекордеры, а не оставить их с прежним значением.
    """
    settings.set("frame_opacity", 0.42)
    seen = []
    settings.changed.connect(lambda k, v: seen.append((k, v)))
    settings.reset_defaults(["frame_opacity"])
    assert settings.get("frame_opacity") == DEFAULTS["frame_opacity"]
    assert ("frame_opacity", DEFAULTS["frame_opacity"]) in seen


def test_settings_first_run_flag(temp_data_dir):
    """is_first_run = True, если config.json не существует. Иначе False."""
    sm = SettingsManager()
    assert sm.is_first_run is True

    # Создаём config.json
    sm.set("frame_opacity", 0.5)
    sm.force_save()

    sm2 = SettingsManager()
    assert sm2.is_first_run is False


def test_settings_save_does_not_persist_immediately(temp_data_dir):
    """save() взводит debounced-таймер 500 мс, на диск НЕ пишет сразу.

    Это документированное поведение. Если когда-нибудь
    поменяем на синхронную запись — этот тест провалится, и нужно проверить,
    что подписчики SettingsManager.changed это переварят (частые записи на
    диск при движении ползунков opacity).
    """
    sm = SettingsManager()
    sm.set("frame_opacity", 0.42)
    sm.save()
    # Файл НЕ существует — save() только взвёл таймер
    assert not (temp_data_dir / "config.json").exists(), \
        "save() должна быть debounced — немедленной записи на диск быть не должно"


def test_settings_force_save_persists_immediately(temp_data_dir):
    """force_save() — синхронная запись. Применяется в _handle_exit
    SettingsWindow (см. AUDIT.md #5), иначе настройки теряются при выходе.
    """
    sm = SettingsManager()
    sm.set("frame_opacity", 0.42)
    sm.force_save()
    # Файл существует немедленно
    assert (temp_data_dir / "config.json").exists()


def test_handle_exit_persists_synchronously(temp_data_dir, qapp):
    """SettingsWindow._handle_exit вызывает force_save(), а не save().

    Регрессия: в v0.7 был save() (debounced), приложение закрывалось через
    «Выйти из программы» в сайдбаре — настройки (изменённые в последние
    500 мс) терялись. После перехода на force_save() — пишутся синхронно.
    """
    from backend.config import SettingsManager
    from backend.hotkeys import HotkeyManager
    from frontend.settings_window import SettingsWindow

    sm = SettingsManager()
    sm.set("frame_opacity", 0.42)   # только что изменили
    # Имитируем SettingsManager.changed без полной перезаписи

    hotkeys = HotkeyManager()
    win = SettingsWindow(sm, hotkeys, on_exit=lambda: None, hide_on_close=True)
    qapp.processEvents()

    # Должен был записать сразу (debounced-таймер НЕ успел сработать)
    win._handle_exit()
    qapp.processEvents()

    # Файл должен существовать и содержать 0.42
    cfg_path = temp_data_dir / "config.json"
    assert cfg_path.exists(), "_handle_exit должен был сделать force_save()"
    with open(cfg_path, encoding="utf-8") as f:
        data = __import__("json").load(f)
    assert abs(data["frame_opacity"] - 0.42) < 0.001, \
        "настройки должны сохраниться синхронно при выходе"
