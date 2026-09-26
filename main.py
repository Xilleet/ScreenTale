"""ScreenTale v0.4.3 — точка входа и контроллер приложения."""
import ctypes
import datetime
import multiprocessing
import os
import sys
import time
import traceback
from difflib import SequenceMatcher

# --- Логирование ДО ВСЕХ тяжёлых импортов ---
# В windowed-сборке sys.stdout/sys.stderr равны None: любой print/warning
# от импортируемых модулей роняет процесс. Валидируем потоки до того,
# как хоть один импорт попробует в них писать.
from backend.config import get_app_dir, get_data_dir
from backend.logging_setup import set_verbose, setup_logging, vlog

setup_logging(get_data_dir())

import pyperclip
from PySide6.QtCore import (
    QDir,
    QLockFile,
    QObject,
    QRect,
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
    ENGINE_LABELS,
    LOCAL_ENGINES,
    ModelManager,
    clear_all_cache,
    delete_model_cache,
    get_available_offline_engine,
    is_model_cached,
    is_network_error,
    translate_online,
)
from frontend.screen_selector import ScreenSelector
from frontend.settings_window import SettingsWindow
from frontend.theme import apply_theme
from frontend.translate_window import TranslateWindow

# ---- Кэш HuggingFace: всегда локально (портативность + кириллица в путях) ----
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_local_hf = os.path.join(get_data_dir(), "hf_cache")
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
    update_available = Signal(dict)  # <--- сигнал найденного обновления

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
        self.trans_win.pause_requested.connect(lambda: self._on_hotkey("pause"))
        self.trans_win.retry_requested.connect(self._retry_last_translation)
        self.settings_win = SettingsWindow(self.settings, self.hotkeys, on_exit=self.exit_app)
        self._auto_paused = False

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
        preferred_ocr = self.settings.get("ocr_engine", "windows")
        self.settings_win.ocr_pill.set_state("busy", "Загрузка OCR-модели…")
        self.ocr = OcrWorker(bool(self.settings.get("gpu", False)), preferred_engine=preferred_ocr)
        self.ocr.state_changed.connect(self.settings_win.ocr_pill.set_state)
        self.ocr.cuda_status.connect(self.settings_win.set_gpu_available)
        self.ocr.gpu_result.connect(self._on_gpu_result)
        self.ocr.read_result.connect(self._on_read_result)
        self.ocr.start()

        # --- Менеджер локальных моделей ---
        self._req_seq = 0
        self._last_source = ""
        self._fallback_active = False
        self._pending_translations = {}
        self._last_shown_seq = 0
        self._pending_fallback = None

        # FIFO-очередь ожидания загрузки модели
        self._loading_queue = []
        self._model_loading = False

        self.model_manager = ModelManager(bool(self.settings.get("gpu", False)))
        self.model_manager.progress_started.connect(self.settings_win.model_load_started)
        self.model_manager.progress_started.connect(self._on_model_load_started)
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
        # engine = self.settings.get("translator", "google")
        # if engine in LOCAL_ENGINES:
        #     self.model_manager.load(engine)

        # грузим в память ТОЛЬКО если модель уже реально скачана
        engine = self.settings.get("translator", "google")
        if engine in LOCAL_ENGINES:
            is_c, _ = is_model_cached(engine)
            if is_c:
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

        self.update_available.connect(self._on_update_available)
        from backend.updater import UpdateCheckTask
        task = UpdateCheckTask(lambda data: self.update_available.emit(data))
        QThreadPool.globalInstance().start(task)

        self.settings_win.update_banner.update_clicked.connect(self._start_auto_update)
        self.settings_win.update_banner.snooze_clicked.connect(self._on_update_snoozed)

        

    def _toggle_pause_auto(self):
        if not self._auto_active:
            self.trans_win.show_translation("[Авто-режим не запущен]")
            return

        self._auto_paused = not self._auto_paused
        self.trans_win.set_auto_pause_state(self._auto_paused)

        if self._auto_paused:
            self._auto_timer.stop()
            self.trans_win.set_status("busy", "Авто: Пауза")
            self.trans_win.show_translation("[Авто-режим на паузе (Alt+P для продолжения)]")
            print("[auto] режим поставлен на паузу")
        else:
            self.trans_win.set_status("ok", "Авто: Активен")
            self.trans_win.show_translation("[Авто-режим возобновлён]")
            print("[auto] режим снят с паузы")
            # Мгновенно читаем кадр, который уже есть на экране, не ожидая прокрутки:
            if self._auto_bbox is not None:
                self._last_auto_text = ""
                self._last_sent_auto_text = ""
                self.ocr.read(self._auto_bbox, context="auto")

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
            self._loading_queue.clear()  # сбрасываем FIFO-очередь
            self.trans_win.set_status("off", "Ожидание")
            self.trans_win.show_translation("[Перевод остановлен]")
        elif action == "clear":
            self.trans_win.clear_history()
            self.trans_win.show_translation("[История очищена]")
        elif action == "ghost":
            self.trans_win.toggle_ghost_mode()
        elif action == "pause":
            self._toggle_pause_auto()
        

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
            # 1. Если этот текст уже был только что переведен — игнорируем
            if getattr(self, "_last_sent_auto_text", "") == norm:
                return
            # 2. Печатающийся текст (эффект пишущей машинки)
            if self._last_auto_text and norm.startswith(self._last_auto_text):
                self._last_auto_text = norm
                self._pending_auto_text = text
                self._auto_timer.start(int(self.settings.get("auto_delay_ms", 800)))
                return
            # 3. Дедупликация схожести > 96%
            if self._last_auto_text and SequenceMatcher(
                    None, norm, self._last_auto_text).ratio() > 0.96:
                return
            self._last_auto_text = norm
            self._pending_auto_text = text
            self._auto_timer.start(int(self.settings.get("auto_delay_ms", 800)))
            return

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
        self._last_sent_auto_text = ""
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
        self._loading_queue.clear()  # сбрасываем FIFO-очередь
        self._last_shown_seq = self._req_seq
        # Снять запрет на перетаскивание — без авто-режима нет bbox
        self.trans_win.set_forbidden_rect(None)
        self.trans_win.hide()   # как в v0.3.2: выключение скрывает окно
        self._auto_paused = False
        self.trans_win.set_auto_pause_state(False)

    def _on_region_changed(self):
        if not self._auto_active or self._auto_bbox is None or self._auto_paused:
            return
        if not self.trans_win.isVisible() or self.trans_win.force_hidden:
            return
        print("[auto] region_changed -> ocr.read()")
        self.ocr.read(self._auto_bbox, context="auto")


    def _check_window_in_bbox(self) -> bool:
        """Возвращает True, если frameGeometry окна перевода пересекается
        с текущим bbox OCR. Используется для предупреждения юзера.
        """
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
        norm = " ".join(text.lower().split())
        # Защита от повторной отправки дубликата
        if getattr(self, "_last_sent_auto_text", "") == norm:
            return
        self._last_sent_auto_text = norm
        self._last_source = text
        self._request_translation(text)

    # ---------- перевод ----------
    def _request_translation(self, text):
        if not text.strip():
            return

        # --- ЗАЩИТА: Лимит очереди ---
        MAX_PENDING = 3 # нужны тесты, ранее было 2, но возможно поставить и 4
        untranslated = [s for s, data in self._pending_translations.items() if data[3] is None]
        if len(untranslated) >= MAX_PENDING:
            # Сбрасываем самый старый зависший промежуточный кадр
            oldest_seq = min(untranslated)
            self._pending_translations[oldest_seq][3] = "[skip]"
            self._flush_pending_translations()

        self._req_seq += 1
        seq = self._req_seq
        engine = self.settings.get("translator", "google")
        print(f"[ctrl] запрос перевода: движок={engine}, seq={seq}")
        self._pending_translations[seq] = [
            time.strftime("%H:%M:%S"), time.monotonic(), text, None]
        self.trans_win.set_status("busy", "Перевод…")

        if engine in LOCAL_ENGINES:
            if self._model_loading:
                print(f"[ctrl] модель загружается -> seq={seq} добавлен в FIFO-очередь")
                self._loading_queue.append(seq)
                return
            self.model_manager.translate(text, seq)
        else:
            task = _OnlineTask(engine, text, seq,
                               self.translation_ready.emit,
                               self.translation_failed.emit)
            QThreadPool.globalInstance().start(task)

    def _apply_translation_result(self, seq, text):
        # Если этот запрос уже был показан или пропущен — тихо игнорируем запоздалый ответ:
        if seq <= self._last_shown_seq:
            return
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
        now_mono = time.monotonic()
        while True:
            next_seq = self._last_shown_seq + 1
            entry = self._pending_translations.get(next_seq)
            if entry is None:
                return
            timestamp, sent_mono, source, translation = entry
            if translation is None:
                if next_seq in self._loading_queue:
                    return
                if now_mono - sent_mono < TRANSLATION_TIMEOUT_SEC:
                    return
                print(f"[ctrl] seq={next_seq} ожидание истекло "
                      f"({now_mono - sent_mono:.1f} сек) — помечаем как нет перевода")
                self.trans_win.show_translation(f"[Нет перевода: {source[:30]}...]")
                del self._pending_translations[next_seq]
                self._last_shown_seq = next_seq
                continue

            # 1. Если это тихий пропуск (после сбоя сети) — просто удаляем и идем дальше
            if translation == "[skip]":
                del self._pending_translations[next_seq]
                self._last_shown_seq = next_seq
                continue

            # 2. Служебные сообщения идут в тост
            if translation.startswith("["):
                self.trans_win.show_translation(translation)
            else:
                self.trans_win.show_translation(f"({timestamp}) {translation}")

            del self._pending_translations[next_seq]
            self._last_shown_seq = next_seq

    def _on_translation_failed(self, seq, engine, error):
        print(f"[ctrl] сбой перевода seq={seq} (движок {engine}): {error}")

        # 1. ОБЯЗАТЕЛЬНО записываем ошибку в буфер, чтобы очередь не застревала на этом номере!
        if seq in self._pending_translations:
            self._pending_translations[seq][3] = f"[Ошибка ({engine}): {_short(error)}]"
        self._flush_pending_translations()
        
        # 2. Продвигаем очередь дальше
        self._flush_pending_translations()

        # 3. Обновляем статус только если это актуальный текущий запрос
        if seq == self._req_seq:
            self.trans_win.set_status("error", "Ошибка")

        # 4. Логика умного переключения на офлайн (Fallback) при падении сети
        if self._fallback_active or not is_network_error(error):
            return

        available_engine = get_available_offline_engine(preferred="opus")
        if available_engine is None:
            self._apply_translation_result(
                seq, "[Интернет недоступен, а офлайн-модель не скачана. Скачайте её в Настройках]")
            self.trans_win.set_status("error", "Нет сети и модели")
            return

        # Если упала сеть и есть офлайн-модель — тихо переключаемся, не мусоря юзеру ошибками
        if not self._fallback_active and is_network_error(error):
            available_engine = get_available_offline_engine(preferred="opus")
            if available_engine is not None:
                # 1. Помечаем упавший онлайн-запрос как тихий пропуск [skip]
                if seq in self._pending_translations:
                    self._pending_translations[seq][3] = "[skip]"
                self._flush_pending_translations()

                # 2. Бесшовно переводим через офлайн-модель
                self._fallback_active = True
                self._pending_fallback = self._last_source
                self.trans_win.show_translation(
                    f"[Интернет недоступен — переключаюсь на офлайн ({ENGINE_LABELS.get(available_engine)})...]"
                )
                self.trans_win.set_status("busy", "Переключение на офлайн…")
                self.settings.set("translator", available_engine)
                return

        self._fallback_active = True
        self._pending_fallback = self._last_source
        self.trans_win.show_translation(
            f"[Интернет недоступен — переключаюсь на офлайн ({ENGINE_LABELS.get(available_engine)})...]"
        )
        self.trans_win.set_status("busy", "Переключение на офлайн…")
        self.settings.set("translator", available_engine)
        
    def _on_model_load_started(self):
        """Модель начала скачивание/загрузку в память."""
        self._model_loading = True
        print("[ctrl] статус модели: загрузка началась -> включен режим накопления очереди")

    def _on_model_ready(self, engine_id):
        self._model_loading = False
        print(f"[ctrl] модель {engine_id} готова к работе!")
        self.trans_win.set_status("ok", "Модель готова")

        # 1. Разгружаем накопившуюся FIFO-очередь строго по порядку
        if self._loading_queue:
            print(f"[ctrl] разгрузка FIFO-очереди ({len(self._loading_queue)} фраз)...")
            while self._loading_queue:
                q_seq = self._loading_queue.pop(0)
                if q_seq in self._pending_translations:
                    q_text = self._pending_translations[q_seq][2]
                    self.model_manager.translate(q_text, q_seq)

        # 2. Обработка отложенного текста fallback
        if self._fallback_active and self._pending_fallback:
            if engine_id not in LOCAL_ENGINES:
                self._fallback_active = False
                self._pending_fallback = None
                return
            text = self._pending_fallback
            self._pending_fallback = None
            self._fallback_active = False
            self._request_translation(text)

    def _on_model_failed(self, engine_id, error):
        self._model_loading = False
        # При ошибке загрузки сбрасываем всю накопившуюся очередь с понятным сообщением
        if self._loading_queue:
            print(f"[ctrl] сбой модели {engine_id} -> сброс {len(self._loading_queue)} запросов в очереди")
            while self._loading_queue:
                q_seq = self._loading_queue.pop(0)
                self._apply_translation_result(
                    q_seq, f"[Перевод не удался: сбой загрузки модели {engine_id}]")

        if self._fallback_active:
            self._fallback_active = False
            self._pending_fallback = None
            self.trans_win.show_translation(
                f"[Не удалось переключиться на офлайн-модель: {_short(error)}]")
            self.trans_win.set_status("error", "Модель не загрузилась")

    # ---------- настройки ----------
    def _on_setting_changed(self, key, value):
        if key == "gpu":
            self.ocr.request_gpu(bool(value))
        elif key == "translator":
            # Сбрасываем зависшие хвосты старого движка:
            self._pending_translations.clear()
            self._loading_queue.clear()
            self._last_shown_seq = self._req_seq
            self.trans_win.set_status("off", "Ожидание")

            if value in LOCAL_ENGINES:
                is_c, _ = is_model_cached(value)
                if is_c:
                    self.model_manager.load(value)
                else:
                    self.model_manager.unload()
            else:
                self.model_manager.unload()
                self.settings_win.model_finished("off", "Локальная модель не загружена")
        elif key == "ocr_engine":
            self.ocr.request_engine(value)

    def _on_delete_model(self, engine_id):
        self.model_manager.unload()
        delete_model_cache(engine_id)
        self.settings_win._update_cache_display()
        self.settings_win.toast.show_toast("Модель успешно удалена")

    def _on_clear_all_cache(self):
        self.model_manager.unload()
        clear_all_cache()
        self.settings_win._update_cache_display()
        self.settings_win.toast.show_toast("Кэш моделей полностью очищен")

    def _on_gpu_result(self, success, is_gpu, message):
        actual = bool(success and is_gpu)
        if bool(self.settings.get("gpu")) != actual:
            self.settings.set("gpu", actual)
        if success:
            self.model_manager.set_device(is_gpu)

    # ---------- иконка и трей ----------
    def _get_icon(self):
        # 1. Проверяем в корне приложения (dev-режим или рядом с exe)
        icon_path = os.path.join(get_app_dir(), "ScreenTale.ico")
        if os.path.exists(icon_path):
            return QIcon(icon_path)

        # 2. Проверяем внутри папки _internal PyInstaller (sys._MEIPASS)
        if hasattr(sys, "_MEIPASS"):
            internal_icon = os.path.join(sys._MEIPASS, "ScreenTale.ico")
            if os.path.exists(internal_icon):
                return QIcon(internal_icon)

        # 3. Запасная программная отрисовка (если иконка вообще отсутствует)
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#e08e45"))  # наш фирменный янтарь
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(0, 0, 64, 64, 12, 12)
        painter.setPen(QColor("#1a1816"))
        painter.setFont(QFont("Segoe UI", 26, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "ST")
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

    # ---------- Автообновление в 1 клик ----------

    def _on_update_available(self, manifest_data: dict):
        """Проверяет дату отсрочки и показывает встроенный баннер."""
        snooze_str = self.settings.get("update_snooze_until")
        if snooze_str:
            try:
                snooze_date = datetime.date.fromisoformat(str(snooze_str))
                today = datetime.datetime.now(datetime.timezone.utc).date()
                if today < snooze_date:
                    print("[updater] показ обновления отложен по таймеру 7 дней")
                    return
            except Exception:
                pass

        # Показываем красивый баннер прямо в окне настроек
        self.settings_win.update_banner.show_update(manifest_data)

    def _on_update_snoozed(self, manifest_data: dict):
        """Откладывает показ обновления ровно на 7 дней."""
        today = datetime.datetime.now(datetime.timezone.utc).date()
        snooze_date = (today + datetime.timedelta(days=7)).isoformat()
        self.settings.set("update_snooze_until", snooze_date)
        self.settings_win.toast.show_toast("Напоминание об обновлении отложено на 7 дней")

    def _start_auto_update(self, manifest_data: dict):
        from backend.updater import UpdateDownloadWorker
        # Переводим баннер в режим загрузки
        self.settings_win.update_banner.set_downloading_state("Подключение к репозиторию…")
        self.trans_win.set_status("busy", "Обновление…")

        self._update_worker = UpdateDownloadWorker(manifest_data)
        self._update_worker.progress.connect(self._on_update_progress)
        self._update_worker.failed.connect(self._on_update_failed)
        self._update_worker.finished.connect(self._on_update_ready_to_restart)
        self._update_worker.start_download()

    def _on_update_progress(self, percent: int, label: str):
        self.settings_win.update_banner.update_progress(percent, label)

    def _on_update_failed(self, error_msg: str):
        self.trans_win.set_status("error", "Ошибка обновления")
        self.settings_win.toast.show_toast(f"Ошибка обновления: {_short(error_msg)}", ms=5000)
        # Возвращаем кнопки в баннере обратно (или скрываем баннер)
        self.settings_win.update_banner.hide()

    def _on_update_ready_to_restart(self):
        self.settings_win.update_banner.update_progress(100, "Обновление готово! Перезапуск...")
        self.settings_win.toast.show_toast("Перезапуск программы…")
        QTimer.singleShot(1000, self.exit_app)

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
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            input("\n[Enter] — закрыть")
        except (EOFError, OSError):
            pass

