"""ScreenTale v0.4.3 — точка входа и контроллер приложения."""
import multiprocessing
import os
import sys
import time
from difflib import SequenceMatcher

# --- Логирование ДО ВСЕХ тяжёлых импортов ---
# В windowed-сборке sys.stdout/sys.stderr равны None: любой print/warning
# от импортируемых модулей роняет процесс. Валидируем потоки до того,
# как хоть один импорт попробует в них писать.
from backend.config import get_app_dir
from backend.logging_setup import set_verbose, setup_logging, vlog

setup_logging(get_app_dir())

import pyperclip
from PySide6.QtCore import (
    QDir,
    QLockFile,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from backend.auto_mode import AutoModeWorker
from backend.config import SettingsManager
from backend.hotkeys import HotkeyManager
from backend.ocr import OcrWorker
from backend.translators import (
    LOCAL_ENGINES,
    ModelManager,
    is_network_error,
    translate_online,
)
from frontend.screen_selector import ScreenSelector
from frontend.settings_window import SettingsWindow
from frontend.theme import apply_theme
from frontend.translate_window import TranslateWindow

# ---- Кэш HuggingFace: всегда локально (портативность + кириллица в путях) ----
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_local_hf = os.path.join(get_app_dir(), "hf_cache")
os.environ["HF_HOME"] = _local_hf
os.makedirs(_local_hf, exist_ok=True)


class _OnlineTask(QRunnable):
    """Онлайн-перевод в пуле потоков; результаты — через сигналы контроллера."""

    def __init__(self, engine, text, seq, report_ready, report_failed):
        super().__init__()
        self.engine = engine
        self.text = text
        self.seq = seq
        self._report_ready = report_ready
        self._report_failed = report_failed

    def run(self):
        try:
            result = translate_online(self.engine, self.text)
            self._report_ready(self.seq, result)
        except Exception as e:
            self._report_failed(self.seq, self.engine, str(e))

TRANSLATION_TIMEOUT_SEC = 15   # если seq ждёт дольше — пометить как «(нет перевода)»

def _short(text, limit=180):
    """Ошибки в одну строку и без простыней."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


class AppController(QObject):
    translation_ready = Signal(int, str)          # seq, текст
    translation_failed = Signal(int, str, str)    # seq, engine, ошибка

    def __init__(self, app):
        super().__init__()
        self.app = app

        self.settings = SettingsManager()
        self.hotkeys = HotkeyManager()
        apply_theme(app, self.settings.get("theme", "dark"))

        # Хоткеи
        report = self.hotkeys.apply_all(self.settings.get("hotkeys"))
        for action, ok in report.items():
            print(f"[hotkey] {action}: {'OK' if ok else 'ЗАНЯТО другой программой'}")
        self.hotkeys.triggered.connect(self._on_hotkey)

        # Окна
        self.trans_win = TranslateWindow(self.settings)
        self._last_bbox = None
        self.trans_win.stop_requested.connect(lambda: self._on_hotkey("stop"))
        self.trans_win.clear_requested.connect(lambda: self._on_hotkey("clear"))
        self.trans_win.retry_requested.connect(self._retry_last_translation)
        self.settings_win = SettingsWindow(self.settings, self.hotkeys, on_exit=self.exit_app)

        # Применить verbose-флаг из настроек к logging_setup (глобальный флаг VERBOSE)
        set_verbose(bool(self.settings.get("verbose_log", False)))

        # --- OCR-воркер ---
        self._is_selecting = False
        self.selector = None
        # --- предзагрузка тяжёлых библиотек в главном потоке ---
        # Импорт torch из двух потоков одновременно (OCR + модели) рушит
        # процесс на Windows. Грузим один раз здесь: дальше оба воркера
        # получат уже загруженный модуль из sys.modules мгновенно.
        try:
            import torch  # noqa: F401
        except Exception as _e:
            # torch может отсутствовать в урезанной сборке или не грузиться
            # из-за битых DLL. Дальше OCR/ModelManager тоже упадут — без этого
            # в логе причина вообще не видна.
            print(f"[warn] torch не загрузился в основном потоке: {_e}")
        self.settings_win.gpu_pill.set_state("busy", "Загрузка OCR-модели…")
        self.ocr = OcrWorker(bool(self.settings.get("gpu", False)))
        self.ocr.state_changed.connect(self.settings_win.gpu_pill.set_state)
        self.ocr.cuda_status.connect(self.settings_win.set_gpu_available)
        self.ocr.gpu_result.connect(self._on_gpu_result)
        self.ocr.read_result.connect(self._on_read_result)
        self.ocr.start()

        # --- Менеджер локальных моделей ---
        self._req_seq = 0
        self._last_source = ""
        self._fallback_active = False
        # Буфер переводов с метками времени: {seq: [timestamp, source, translation|None]}
        # Позволяет показывать устаревшие переводы в правильном порядке с меткой
        # времени отправки — как в чате. Юзер не теряет контекст в динамичных
        # субтитрах / визуальных новеллах.
        self._pending_translations = {}
        self._last_shown_seq = 0
        self._pending_fallback = None

        self.model_manager = ModelManager(bool(self.settings.get("gpu", False)))
        self.model_manager.progress_started.connect(self.settings_win.model_load_started)
        self.model_manager.progress.connect(self.settings_win.model_progress)
        self.model_manager.ready.connect(self.settings_win.model_loaded)
        self.model_manager.ready.connect(self._on_model_ready)
        self.model_manager.failed.connect(self.settings_win.model_failed)
        self.model_manager.failed.connect(self._on_model_failed)
        self.model_manager.translation_result.connect(self._apply_translation_result)
        self.model_manager.start()

           # --- Авто-режим ---
        self._auto_active = False
        self._auto_worker = None
        self._auto_bbox = None
        self._last_auto_text = ""
        self._pending_auto_text = ""
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._execute_auto_translate)

        # Если в конфиге стоит локальный движок — грузим сразу (как в v0.3.2)
        engine = self.settings.get("translator", "google")
        if engine in LOCAL_ENGINES:
            self.model_manager.load(engine)

        self.settings.changed.connect(self._on_setting_changed)
        self.translation_ready.connect(self._apply_translation_result)
        self.translation_failed.connect(self._on_translation_failed)

        self._setup_tray()
        if self.settings.is_first_run:
            self.show_settings()
        else:
            self.tray.showMessage(
                "ScreenTale",
                "Приложение запущено и работает в фоновом режиме",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )

    # ---------- хоткеи ----------
    def _on_hotkey(self, action):
        if action == "toggle_window":
            self.trans_win.toggle_visible()
        elif action == "single":
            self._start_selection()
        elif action == "auto":
            self._toggle_auto_mode()
        elif action == "stop":
            self._req_seq += 1
            self._pending_translations.clear()
            self.trans_win.set_status("off", "Ожидание")
            self.trans_win.show_translation("[Перевод остановлен]")
        elif action == "clear":
            self.trans_win.clear_history()
            self.trans_win.show_translation("[История очищена]")
        elif action == "ghost":
            self.trans_win.toggle_ghost_mode()

    # ---------- выделение области и OCR ----------
    def _start_selection(self):
        if self._is_selecting:
            return
        self._is_selecting = True
        # Запоминаем, было ли окно открыто, и временно скрываем его на время выделения
        self._was_trans_win_visible = self.trans_win.isVisible() and not self.trans_win.force_hidden
        if self._was_trans_win_visible:
            self.trans_win.hide()

        # Если решим сбрасывать призрака при новом выделении — раскомментировать эти 2 строки:
        # if getattr(self.trans_win, "_ghost_mode", False):
        #     self.trans_win.toggle_ghost_mode()
        self.selector = ScreenSelector(self._on_area_selected, on_cancel=self._on_selection_cancel)

    def _on_area_selected(self, bbox):
        self._is_selecting = False
        self.selector = None

        # 1. Вычисляем идеальную позицию окна снаружи bbox (где больше места: сверху/снизу/сбоку)
        x, y, _ = self._compute_trans_window_pos(bbox)

        # 2. Включаем запретную зону: окно физически не сможет заехать внутрь bbox (как в авто-режиме)
        from PySide6.QtCore import QRect
        bbox_rect = QRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
        self.trans_win.set_forbidden_rect(bbox_rect)

        # 3. Перемещаем окно в безопасную позицию снаружи выделения
        self.trans_win.move(x, y)

        # 4. Показываем окно
        if not self.trans_win.force_hidden:
            self.trans_win.show()

        # 5. Отправляем в OCR
        self.ocr.read(bbox)

        self._last_bbox = bbox
        self.ocr.read(bbox)

    def _on_selection_cancel(self):
        self._is_selecting = False
        self.selector = None
        # При отмене выделения (Esc) восстанавливаем окно, если оно было открыто
        if getattr(self, "_was_trans_win_visible", False) and not self.trans_win.force_hidden:
            self.trans_win.show()

    def _on_read_result(self, bbox, text, context):
        print(f"[ctrl] read_result ctx={context}: {text[:50]!r}")
        vlog(f"[ctrl] read_result (full) ctx={context}: {text!r}")
        if context == "auto":
            if text.startswith("["):
                return
            norm = " ".join(text.lower().split())
            # Если новый текст НАЧИНАЕТСЯ с предыдущего — это «печатается»
            # (Many people dre → Many people dream of...). НЕ отправляем
            # новый seq, перезапускаем debounce. Ждём, пока текст
            # «успокоится» — отправляем только финальный вариант.
            # Это убирает промежуточные seq при печати в реальном времени.
            if self._last_auto_text and norm.startswith(self._last_auto_text):
                self._last_auto_text = norm
                self._pending_auto_text = text
                self._auto_timer.start(int(self.settings.get("auto_delay_ms", 800)))
                return
            # Стандартная дедупликация: если текст почти не изменился (97%) — игнор
            if self._last_auto_text and SequenceMatcher(
                    None, norm, self._last_auto_text).ratio() > 0.97:
                return
            self._last_auto_text = norm
            self._pending_auto_text = text
            self._auto_timer.start(int(self.settings.get("auto_delay_ms", 800)))
            return

        # одиночный режим: показать перевод в текущей позиции окна
        self.trans_win.show_translation(text)
        if text.startswith("["):
            return
        self._last_source = text
        self._request_translation(text)

        # ---------- авто-режим ----------
    def _toggle_auto_mode(self):
        if self._auto_active:
            self._stop_auto_mode()
        elif not self._is_selecting:
            self._is_selecting = True
            # Прячем окно на время выбора области авто-режима
            self._was_trans_win_visible = self.trans_win.isVisible() and not self.trans_win.force_hidden
            if self._was_trans_win_visible:
                self.trans_win.hide()

            # Если решим сбрасывать призрака при новом выделении — раскомментировать эти 2 строки:
            # if getattr(self.trans_win, "_ghost_mode", False):
            #     self.trans_win.toggle_ghost_mode()

            self.selector = ScreenSelector(self._on_auto_area_selected,
                                           on_cancel=self._on_selection_cancel)

    def _on_auto_area_selected(self, bbox):
        # Окно перевода должно всплыть над/под bbox — не попадая в кадр OCR.
        # Не сбрасываем force_hidden принудительно: если юзер сам скрыл окно
        # (правый клик, Ctrl+`), уважаем это и не показываем.
        print(f"[auto] область выбрана: {bbox}, force_hidden={self.trans_win.force_hidden}")
        self._is_selecting = False
        self.selector = None
        self._auto_bbox = bbox
        self._last_auto_text = ""
        self._auto_active = True
        self._auto_worker = AutoModeWorker(bbox)
        self._auto_worker.region_changed.connect(self._on_region_changed)
        self._auto_worker.start()
        # Если юзер сам скрыл окно (force_hidden=True) — уважаем это:
        # окно НЕ показываем, но и не молчим. Toast в трее объясняет,
        # что произошло и как вернуть окно. Auto_worker уже запущен выше —
        # переводы пойдут в show_translation, добавятся в document. Когда
        # юзер нажмёт Ctrl+` и окно появится — он увидит накопленные переводы.
        if self.trans_win.force_hidden:
            print("[auto] окно перевода скрыто юзером — показываю toast в трее")
            self.tray.showMessage(
                "ScreenTale",
                "Окно перевода скрыто. Показать — Ctrl+`\n"
                "Авто-режим работает, переводы копируются в буфер.",
                QSystemTrayIcon.MessageIcon.Information,
                5000)
            return
        # Окно не скрыто юзером — позиционируем над/под/слева/справа от bbox
        x, y, intersects = self._compute_trans_window_pos(bbox)
        # Запретить перетаскивание окна в bbox (прилипание к границе снаружи)
        from PySide6.QtCore import QRect
        bbox_rect = QRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
        self.trans_win.set_forbidden_rect(bbox_rect)
        self.trans_win.show_translation(
            "[Авто-режим включён: отслеживаю область. Alt+W — выключить]",
            x=x, y=y)
        if intersects:
            # Ни одна сторона не вместила — окно наезжает на bbox
            print("[auto] WARN: ни одна сторона не вместила окно — наезжаем на bbox")
            self.tray.showMessage(
                "ScreenTale",
                "Окно перевода наезжает на область OCR.\n"
                "Сдвиньте окно или уменьшите область OCR (Alt+W).",
                QSystemTrayIcon.MessageIcon.Warning,
                6000)
        self._auto_bbox = bbox
        self._last_bbox = bbox

    def _compute_trans_window_pos(self, bbox):
        """Позиция (x, y) окна перевода — выбирает сторону с максимальным
        свободным местом из 4 (сверху/снизу/слева/справа). Окно не должно
        перекрывать bbox, чтобы не попасть в кадр OCR.

        Возвращает (x, y, intersects_bbox):
          intersects_bbox=True — ни одна сторона не вместила, окно наезжает
          на bbox. В этом случае вызывающий код покажет toast-предупреждение.
        """
        from PySide6.QtWidgets import QApplication
        left, top, right, bottom = bbox
        win_w, win_h = 420, 120   # из translate_window.py
        SHADOW_MARGIN = 10
        GAP = 10
        needed_v = win_h + SHADOW_MARGIN * 2 + GAP   # ~150 px
        needed_h = win_w + SHADOW_MARGIN * 2 + GAP   # ~450 px

        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            screen_left, screen_top = avail.left(), avail.top()
            screen_right, screen_bottom = avail.right(), avail.bottom()
        else:
            screen_left, screen_top = 0, 0
            screen_right, screen_bottom = 9999, 9999

        # Кандидаты: (сторона, x, y, свободное_место)
        # Y-координата для левой/правой стороны — выровнять по верху bbox
        # X-координата для верхней/нижней стороны — выровнять по леву bbox
        candidates = []
        # Сверху: окно под (top - needed_v), Y-старт с учётом тени
        free_top = top - screen_top
        if free_top >= needed_v:
            candidates.append(("сверху", left + 10 - SHADOW_MARGIN,
                                top - GAP - win_h - SHADOW_MARGIN, free_top))
        # Снизу
        free_bottom = screen_bottom - bottom
        if free_bottom >= needed_v:
            candidates.append(("снизу", left + 10 - SHADOW_MARGIN,
                                bottom + GAP - SHADOW_MARGIN, free_bottom))
        # Слева: окно левее bbox, выровнять по верху
        free_left = left - screen_left
        if free_left >= needed_h:
            candidates.append(("слева", left - GAP - win_w - SHADOW_MARGIN,
                                top + 10 - SHADOW_MARGIN, free_left))
        # Справа
        free_right = screen_right - right
        if free_right >= needed_h:
            candidates.append(("справа", right + GAP - SHADOW_MARGIN,
                                top + 10 - SHADOW_MARGIN, free_right))

        if candidates:
            # Выбрать сторону с максимальным свободным местом
            best = max(candidates, key=lambda c: c[3])
            side, x, y, _ = best
            # Финальная проверка границ экрана для X
            if screen is not None:
                avail = screen.availableGeometry()
                max_x = avail.right() - win_w - SHADOW_MARGIN
                x = min(x, max_x)
                x = max(x, avail.left())
            print(f"[auto] позиция окна перевода: ({x}, {y}) — {side} bbox")
            return x, y, False

        # Ни одна сторона не вместила — выбираем наименее плохую
        # (где максимальный зазор), даже если она наезжает на bbox
        fallback = [
            ("сверху", left + 10 - SHADOW_MARGIN,
             top - GAP - win_h - SHADOW_MARGIN, free_top),
            ("снизу", left + 10 - SHADOW_MARGIN,
             bottom + GAP - SHADOW_MARGIN, free_bottom),
            ("слева", left - GAP - win_w - SHADOW_MARGIN,
             top + 10 - SHADOW_MARGIN, free_left),
            ("справа", right + GAP - SHADOW_MARGIN,
             top + 10 - SHADOW_MARGIN, free_right),
        ]
        best = max(fallback, key=lambda c: c[3])
        side, x, y, _ = best
        print(f"[auto] позиция окна: ({x}, {y}) — {side} bbox (ПЕРЕСЕЧЕНИЕ!)")
        return x, y, True

    def _stop_auto_mode(self):
        self._auto_active = False
        self._auto_timer.stop()
        if self._auto_worker is not None:
            self._auto_worker.requestInterruption()
            if not self._auto_worker.wait(2000):
                self._auto_worker.terminate()
                self._auto_worker.wait(1000)
            self._auto_worker = None
        self._last_auto_text = ""
        self._pending_auto_text = ""
        # Очистить буфер переводов и счётчик показанных — между сессиями
        # авто-режима не копим устаревшие переводы. _req_seq НЕ сбрасываем,
        # чтобы переводы от старой сессии (если придут с задержкой) не попали
        # в новую сессию: их seq < следующего _req_seq новой сессии, а
        # _last_shown_seq будет 0 → буфер не застрянет.
        self._pending_translations.clear()
        # Синхронизировать _last_shown_seq с _req_seq — чтобы в новой
        # сессии _flush_pending искал первый новый seq, а не было seq=1.
        # Раньше был = 0, и при новой сессии (seq=7+) буфер
        # искал seq=1, не находил и вечно ждал — ничего не показывалось.
        self._last_shown_seq = self._req_seq
        # Снять запрет на перетаскивание — без авто-режима нет bbox
        self.trans_win.set_forbidden_rect(None)
        self.trans_win.hide()   # как в v0.3.2: выключение скрывает окно

    def _on_region_changed(self):
        if not self._auto_active or self._auto_bbox is None:
            return
        # Если окно скрыто юзером — не тратим ресурсы на распознавание
        if not self.trans_win.isVisible() or self.trans_win.force_hidden:
            return
        print("[auto] region_changed -> ocr.read()")
        self.ocr.read(self._auto_bbox, context="auto")


    def _check_window_in_bbox(self) -> bool:
        """Возвращает True, если frameGeometry окна перевода пересекается
        с текущим bbox OCR. Используется для предупреждения юзера.
        """
        from PySide6.QtCore import QRect
        if self.trans_win is None or self._auto_bbox is None:
            return False
        if not self.trans_win.isVisible():
            return False
        win_rect = self.trans_win.frameGeometry()
        bbox = self._auto_bbox
        bbox_rect = QRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
        return win_rect.intersects(bbox_rect)

    def _warn_window_in_bbox(self):
        """Показать toast в трее, если окно перевода пересекает bbox.
        Не спамим — не чаще раза в 30 секунд."""
        import time
        now = time.monotonic()
        last = getattr(self, "_last_bbox_warning", 0)
        if now - last < 30.0:
            return   # уже предупреждали недавно
        self._last_bbox_warning = now
        print("[auto] WARN: окно перевода пересекает bbox OCR")
        self.tray.showMessage(
            "ScreenTale",
            "Окно перевода пересекает область OCR.\n"
            "OCR будет читать и перевод из окна — сдвиньте его\n"
            "или выключите авто-режим (Alt+W).",
            QSystemTrayIcon.MessageIcon.Warning,
            6000)

    def _execute_auto_translate(self):
        text = self._pending_auto_text
        print(f"[ctrl] дебаунс-таймер: {text[:50]!r}")
        vlog(f"[ctrl] дебаунс-таймер (full): {text!r}")
        self._pending_auto_text = ""
        if not text.strip():
            return
        self._last_source = text
        self._request_translation(text)

    # ---------- перевод ----------
    def _request_translation(self, text):
        if not text.strip():
            return
        self._req_seq += 1
        seq = self._req_seq
        engine = self.settings.get("translator", "google")
        print(f"[ctrl] запрос перевода: движок={engine}, seq={seq}")
        # Записать в буфер: (strftime для метки, monotonic для таймаута,
        # исходный текст, перевод=None)
        self._pending_translations[seq] = [
            time.strftime("%H:%M:%S"), time.monotonic(), text, None]
        # Статус: отправили в перевод — показать «Перевод…»
        self.trans_win.set_status("busy", "Перевод…")
        if engine in LOCAL_ENGINES:
            self.model_manager.translate(text, seq)
        else:
            task = _OnlineTask(engine, text, seq,
                               self.translation_ready.emit,
                               self.translation_failed.emit)
            QThreadPool.globalInstance().start(task)

    def _apply_translation_result(self, seq, text):
        print(f"[ctrl] перевод seq={seq} (ожидался {self._req_seq}): {text[:50]!r}")
        vlog(f"[ctrl] перевод (full) seq={seq} (ожидался {self._req_seq}): {text!r}")
        # Служебные сообщения (начинаются с "[") — показываем как notice,
        # не путаем с обычным переводом и не пишем в буфер.
        if text.startswith("["):
            self.trans_win.show_translation(text)
            return
        # Записать перевод в буфер (или обновить существующую запись).
        # Если seq нет в буфере — это маловероятно (мог быть сброшен в _stop),
        # но защитимся: используем текущее время как timestamp.
        if seq in self._pending_translations:
            self._pending_translations[seq][3] = text
        else:
            # seq нет в буфере — вряд ли такое бывает, но защитимся.
            self._pending_translations[seq] = [
                time.strftime("%H:%M:%S"), time.monotonic(), "?", text]
        # Показать все переводы по порядку, начиная с _last_shown_seq + 1,
        # пока не наткнёмся на seq без перевода (ждём его).
        self._flush_pending_translations()
        # Если это текущий seq — статус «Готово» + копирование в буфер обмена.
        if seq == self._req_seq:
            self.trans_win.set_status("ok", "Готово")
            if self.settings.get("auto_copy", True):
                try:
                    pyperclip.copy(text)
                except Exception as _e:
                    print(f"[warn] не удалось скопировать перевод в буфер: {_e}")

    def _flush_pending_translations(self):
        """Показать переводы из буфера в порядке seq, начиная с
        _last_shown_seq + 1. Если seq ожидается дольше
        TRANSLATION_TIMEOUT_SEC — помечаем как «(нет перевода)» и
        продолжаем со следующего (чтобы буфер не блокировался
        навсегда, если переводчик не ответил на какой-то seq).

        Формат: "[HH:MM:SS] translation". Для потерянных seq:
        "[HH:MM:SS] (нет перевода: source)".
        """
        now_mono = time.monotonic()
        while True:
            next_seq = self._last_shown_seq + 1
            entry = self._pending_translations.get(next_seq)
            if entry is None:
                return   # нет такой записи (возможно, ещё не отправлена)
            # Формат: [strftime, monotonic, source, translation]
            timestamp, sent_mono, source, translation = entry
            if translation is None:
                # Перевода ещё нет. Проверим, не истекло ли время ожидания.
                if now_mono - sent_mono < TRANSLATION_TIMEOUT_SEC:
                    return   # ещё ждём
                # Время истекло — помечаем как потерянный, продолжаем
                print(f"[ctrl] seq={next_seq} ожидание истекло "
                      f"({now_mono - sent_mono:.1f} сек) — помечаем как нет перевода")
                self.trans_win.show_translation(
                    f"({timestamp}) (нет перевода: {source[:30]}...)")
                del self._pending_translations[next_seq]
                self._last_shown_seq = next_seq
                continue
            # Перевод есть — показать с меткой времени отправки
            self.trans_win.show_translation(f"({timestamp}) {translation}")
            del self._pending_translations[next_seq]
            self._last_shown_seq = next_seq

    def _on_translation_failed(self, seq, engine, error):
        if seq != self._req_seq:
            return
        # Статус: ошибка — показать «Ошибка» (auto-reset в off через 3 сек)
        self.trans_win.set_status("error", "Ошибка")
        if self._fallback_active or not is_network_error(error):
            self._apply_translation_result(seq, f"[Ошибка перевода: {_short(error)}]")
            return
        # Сеть лежит: уведомляем и переключаемся на простую офлайн-модель
        self._fallback_active = True
        self._pending_fallback = self._last_source
        self.trans_win.show_translation("[Интернет недоступен — переключаюсь на офлайн-модель…]")
        # Статус: переключение модели — это долгий процесс (скачивание/загрузка opus).
        # Без явного статуса юзер видит «Ожидание» и думает, что прога зависла —
        # перевыделяет область, плодя дубликаты. Явный «занято» успокаивает.
        self.trans_win.set_status("busy", "Переключение на офлайн-модель…")
        self.settings.set("translator", "opus")   # -> changed -> load -> ready -> перевод
        
    def _on_model_ready(self, engine_id):
        # Гонка: сеть упала -> контроллер сам переключил на 'opus' (fallback).
        # Пока opus качался, юзер мог вручную выбрать другой движок в UI.
        # Тогда ready приходит для нового движка, а _pending_fallback хранит
        # текст, который мы хотели перевести именно через opus. Переводить его
        # через другой движок — неожиданно для юзера. Отменяем fallback.
        if not self._fallback_active or not self._pending_fallback:
            return
        if engine_id != "opus":
            # Юзер сам переключил движок — откатываем флаги, не переводим.
            # Юзер знает, что делает: если ему нужен перевод — нажмёт Alt+Q снова.
            self._fallback_active = False
            self._pending_fallback = None
            self.trans_win.show_translation(
                "[Переключение на офлайн-модель отменено — выбран другой движок]")
            return
        # opus загрузился — переводим отложенный текст
        text = self._pending_fallback
        self._pending_fallback = None
        self._fallback_active = False
        # Статус: модель готова — перевыпуск отложенного запроса.
        # _request_translation сам поставит «Перевод…» дальше.
        self.trans_win.set_status("ok", "Модель готова")
        self._request_translation(text)

    def _on_model_failed(self, engine_id, error):
        """Офлайн-модель не загрузилась (например, её нет в кэше и нет сети)."""
        if self._fallback_active:
            self._fallback_active = False
            self._pending_fallback = None
            self.trans_win.show_translation(
                f"[Не удалось переключиться на офлайн-модель: {_short(error)}]")
            # Статус: модель не загрузилась — авто-сброс в 'off' через 3 сек
            self.trans_win.set_status("error", "Модель не загрузилась")

    # ---------- настройки ----------
    def _on_setting_changed(self, key, value):
        if key == "gpu":
            self.ocr.request_gpu(bool(value))
        elif key == "translator":
            if value in LOCAL_ENGINES:
                self.model_manager.load(value)
            else:
                self.model_manager.unload()
                self.settings_win.model_finished("off", "Локальная модель не загружена")

    def _on_gpu_result(self, success, is_gpu, message):
        actual = bool(success and is_gpu)
        if bool(self.settings.get("gpu")) != actual:
            self.settings.set("gpu", actual)
        if success:
            self.model_manager.set_device(is_gpu)

    # ---------- иконка и трей ----------
    def _get_icon(self):
        icon_path = os.path.join(get_app_dir(), "ScreenTale.ico")
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#e74c3c"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, 64, 64, 10, 10)
        painter.setPen(QColor("white"))
        painter.setFont(QFont("Arial", 28, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "T")
        painter.end()
        return QIcon(pixmap)

    def _setup_tray(self):
        icon = self._get_icon()
        self.app.setWindowIcon(icon)
        self.settings_win.setWindowIcon(icon)

        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("ScreenTale")

        menu = QMenu()
        act_settings = menu.addAction("Настройки")
        act_settings.triggered.connect(self.show_settings)
        act_toggle = menu.addAction("Показать/скрыть окно перевода")
        act_toggle.triggered.connect(self.trans_win.toggle_visible)
        menu.addSeparator()
        act_exit = menu.addAction("Выход")
        act_exit.triggered.connect(self.exit_app)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_settings()

    def show_settings(self):
        self.settings_win.show()
        self.settings_win.activateWindow()

    # ---------- выход ----------
    def exit_app(self):
        if self._auto_worker is not None:
            self._auto_worker.requestInterruption()
            self._auto_worker.wait(2000)
        self.ocr.stop()
        self.model_manager.stop()
        self.hotkeys.shutdown()
        self.settings.save()
        # Онлайн-перевод крутится в глобальном QThreadPool. Если выйти,
        # пока таска ещё в полёте — она попытается эмитнуть сигнал в уже
        # уничтоженный AppController → segfault. Ждём максимум 2 сек.
        QThreadPool.globalInstance().waitForDone(2000)
        if getattr(self, "tray", None) is not None:
            self.tray.hide()
        self.app.quit()

    def _retry_last_translation(self):
        if self._last_bbox is not None:
            self.trans_win.set_status("busy", "Повтор…")
            print(f"[ctrl] повтор перевода для bbox={self._last_bbox}")
            self.ocr.read(self._last_bbox, context="single")
        else:
            self.trans_win.show_translation("[Нет сохранённой области для повтора]")

def main():
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("screentale.app.1")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # setStaleLockTime(0) убрано: теперь зависший процесс через дефолтные 30 сек
    # освободит lock автоматически (Qt проверяет PID процесса-владельца).
    lock = QLockFile(os.path.join(QDir.tempPath(), "screen_translator.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(
            None,
            "ScreenTale",
            "Приложение уже запущено — ищите его в системном трее.",
        )
        return

    AppController(app)
    sys.exit(app.exec())

if __name__ == "__main__":
    multiprocessing.freeze_support()
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            input("\n[Enter] — закрыть")
        except (EOFError, OSError):
            pass

