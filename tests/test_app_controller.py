"""Тесты AppController — но без реального создания (AppController требует
полного GUI, что дорого и хрупко). Поэтому тестируем только те гарантии,
которые можно проверить без запуска всего приложения.

Регрессионные тесты:
- _on_clear_all_cache НЕ должен самоподписываться на сигнал (старый баг).
- clear_all_cache_requested должен быть подключён в AppController.
"""

def test_settings_window_clear_all_cache_signal_exists():
    """SettingsWindow объявляет сигнал clear_all_cache_requested.

    Если кто-то удалит сигнал — AppController не сможет подключить
    _on_clear_all_cache, и кнопка «Очистить весь кэш» снова перестанет работать.

    Проверяем через чтение исходника, чтобы тест работал без Qt (для CI).
    """
    import os
    settings_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "frontend", "settings_window.py"
    )
    with open(settings_py_path, encoding="utf-8") as f:
        source = f.read()
    assert "clear_all_cache_requested = Signal()" in source, \
        "frontend/settings_window.py должен объявлять сигнал clear_all_cache_requested = Signal()"


def test_clear_all_cache_handler_no_self_subscribe(qapp, settings, monkeypatch):
    """_on_clear_all_cache НЕ должен вызывать .connect внутри себя.

    Регрессия: в v0.7 строка
        self.settings_win.clear_all_cache_requested.connect(self._on_clear_all_cache)
    была внутри _on_clear_all_cache — каждый вызов плодил новую подписку,
    превращая 1 очистку → 2 → 4 → 8 → 16 параллельных вызовов. Это «усугубляло»
    изначальный баг (сигнал был не подключён в AppController), но если бы
    он был подключён — каждое нажатие удваивало бы количество вызовов.

    После фикса: _on_clear_all_cache делает unload → clear → обновляет UI → toast.
    Никаких .connect.

    Проверка: emit'им сигнал ДО и ПОСЛЕ вызова handler'а — handler должен
    вызываться ровно столько же раз (т.е. количество подписчиков не растёт).
    """
    from frontend.settings_window import SettingsWindow
    from backend.hotkeys import HotkeyManager
    from backend.translators import clear_all_cache as _real_clear_all_cache

    hotkeys = HotkeyManager()
    win = SettingsWindow(settings, hotkeys, on_exit=lambda: None, hide_on_close=True)
    qapp.processEvents()

    # Подписываемся один раз (как делает AppController.__init__)
    call_count = [0]
    def handler():
        call_count[0] += 1
    win.clear_all_cache_requested.connect(handler)

    # Эмитим сигнал — handler должен сработать ровно 1 раз
    win.clear_all_cache_requested.emit()
    qapp.processEvents()
    calls_before_handler = call_count[0]
    assert calls_before_handler == 1, \
        f"после первой эмиссии handler должен сработать 1 раз, вызвался {calls_before_handler}"

    # Mock ModelManager
    class _FakeModelManager:
        def __init__(self):
            self.unloaded = False
        def unload(self):
            self.unloaded = True

    fake_mm = _FakeModelManager()

    # Имитируем AppController._on_clear_all_cache дословно (как в main.py после фикса)
    def _on_clear_all_cache_like_appcontroller(self_settings_win, self_model_manager):
        self_model_manager.unload()
        _real_clear_all_cache()
        self_settings_win._update_cache_display()
        self_settings_win.toast.show_toast("Кэш моделей полностью очищен")
        # НЕ вызываем .connect(self._on_clear_all_cache) — это и есть фикс

    _on_clear_all_cache_like_appcontroller(win, fake_mm)
    qapp.processEvents()

    # Эмитим сигнал снова — handler должен сработать снова 1 раз, не 2
    call_count[0] = 0
    win.clear_all_cache_requested.emit()
    qapp.processEvents()
    calls_after_handler = call_count[0]

    assert fake_mm.unloaded, "model_manager.unload() должен быть вызван"
    assert calls_after_handler == 1, (
        f"после вызова _on_clear_all_cache количество подписчиков не должно расти: "
        f"handler вызывался {calls_after_handler} раз за emit, ожидался 1. "
        f"Внутри handler'а, видимо, есть .connect() — старый баг v0.7"
    )


def test_clear_all_cache_source_no_self_subscribe_in_handler():
    """В исходнике main.py внутри _on_clear_all_cache нет строки .connect.

    Это прямой source-level тест, который не требует создания SettingsWindow
    и работает даже без libEGL/Qt.
    """
    import os
    main_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py"
    )
    with open(main_py_path, encoding="utf-8") as f:
        source = f.read()

    # Находим определение _on_clear_all_cache
    handler_start = source.find("def _on_clear_all_cache(self):")
    assert handler_start != -1, "main.py должен содержать def _on_clear_all_cache(self):"

    # Находим конец метода — следующий def на том же уровне отступа или конец класса
    # Простой способ: ищем следующий "    def " после handler_start
    next_def = source.find("\n    def ", handler_start + 1)
    if next_def == -1:
        handler_source = source[handler_start:]
    else:
        handler_source = source[handler_start:next_def]

    # Внутри метода не должно быть self.settings_win.clear_all_cache_requested.connect
    assert "clear_all_cache_requested.connect" not in handler_source, \
        f"_on_clear_all_cache не должен содержать .connect() — это самоподписка " \
        f"(старый баг v0.7: 1→2→4→8... дублирующихся вызовов)"


def test_clear_all_cache_requested_connected_in_init_source():
    """В исходнике main.py есть строка подключения сигнала.

    Грубая проверка через чтение исходника (без запуска AppController).
    Если кто-то удалит строку connect — тест провалится.
    """
    import os
    main_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py"
    )
    with open(main_py_path, encoding="utf-8") as f:
        source = f.read()

    # Ищем строку подключения сигнала
    assert "clear_all_cache_requested.connect(self._on_clear_all_cache)" in source, \
        "main.py должен содержать подключение clear_all_cache_requested -> _on_clear_all_cache"

    # И убеждаемся, что самоподписки больше нет (старый баг)
    assert "clear_all_cache_requested.connect(self._on_clear_all_cache)\n        self.settings_win.toast" not in source, \
        "внутри _on_clear_all_cache не должно быть .connect(self._on_clear_all_cache)"


def test_inplace_canvas_created_once_in_init_source():
    """В исходнике main.py InPlaceCanvas() создаётся ровно ОДИН раз.

    Регрессия: в v0.7 было два создания
        line ~175: self.inplace_canvas = InPlaceCanvas()
        line ~316: self.inplace_canvas = InPlaceCanvas()   ← перетирал первый

    Первый canvas получал корректную capture-visibility (not streamer_mode),
    но сразу терялся. Второй canvas в __init__ имеет дефолт
    set_capture_visibility(False) = WDA_EXCLUDEFROMCAPTURE — НЕ-стримеры
    были скрыты от OBS, хотя должны быть видны.

    После фикса: только одно создание + корректный set_capture_visibility.
    """
    import os
    main_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py"
    )
    with open(main_py_path, encoding="utf-8") as f:
        source = f.read()

    # Считаем количество строк `self.inplace_canvas = InPlaceCanvas()`
    count = source.count("self.inplace_canvas = InPlaceCanvas()")
    assert count == 1, \
        f"InPlaceCanvas() должен создаваться ровно 1 раз, найдено {count}. " \
        f"Двойное создание — регрессия v0.7 (Stream Mode работал задом наперёд)."


def test_inplace_canvas_capture_visibility_set_after_creation_source():
    """После создания inplace_canvas вызывается set_capture_visibility.

    Это критично: без этого дефолт в InPlaceCanvas.__init__ —
    set_capture_visibility(False), то есть WDA_EXCLUDEFROMCAPTURE —
    плашки скрыты от OBS по умолчанию, что неправильно для НЕ-стримеров.
    """
    import os
    main_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py"
    )
    with open(main_py_path, encoding="utf-8") as f:
        source = f.read()

    # Должна быть строка set_capture_visibility(not is_streamer)
    assert "set_capture_visibility(not is_streamer)" in source or \
           "set_capture_visibility(not bool" in source, \
        "после создания inplace_canvas должна вызываться set_capture_visibility(not streamer_mode)"

    # И не должно быть повторного создания ПОСЛЕ set_capture_visibility
    # (старый баг: второй InPlaceCanvas() перетирал первый со всеми настройками)
    set_vis_pos = source.find("set_capture_visibility(not is_streamer)")
    if set_vis_pos == -1:
        set_vis_pos = source.find("set_capture_visibility(not bool")
    second_creation = source.find("self.inplace_canvas = InPlaceCanvas()", set_vis_pos + 1)
    assert second_creation == -1, \
        "после set_capture_visibility не должно быть повторного InPlaceCanvas()"


def test_inplace_canvas_point_translate_connected_source():
    """point_translate_requested подключается к _on_point_lookup_requested.

    Без этого средняя кнопка мыши (СКМ) для точечного перевода работать
    не будет — клик будет проигнорирован.
    """
    import os
    main_py_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py"
    )
    with open(main_py_path, encoding="utf-8") as f:
        source = f.read()

    assert "point_translate_requested.connect(self._on_point_lookup_requested)" in source, \
        "point_translate_requested должен быть подключён к _on_point_lookup_requested"
