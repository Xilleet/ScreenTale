"""ScreenTale v0.5.0 — точка входа и контроллер приложения."""
import ctypes
import datetime
import multiprocessing
import os
import sys
import time
import traceback
from difflib import SequenceMatcher

# --- Логирование ДО ВСЕХ тяжёлых импортов ---
from backend.config import APP_VERSION, get_app_dir, get_data_dir
from backend.logging_setup import set_verbose, setup_logging, vlog

setup_logging(get_data_dir())

import pyperclip
from PySide6.QtCore import (
    QDir,
    QLockFile,
    QObject,
    QPoint,
    QRect,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from backend.auto_mode import AutoModeWorker
from backend.config import SettingsManager
from backend.hotkeys import HotkeyManager
from backend.ocr import OcrWorker
from backend.runtime_manager import RuntimeDownloadWorker
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
from backend.updater import UpdateCheckTask, UpdateDownloadWorker
from frontend.screen_selector import ScreenSelector
from frontend.settings_window import SettingsWindow
from frontend.theme import apply_theme
from frontend.translate_window import TranslateWindow

# ---- Кэш HuggingFace: всегда локально (портативность + кириллица в путях) ----
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_local_hf = os.path.join(get_data_dir(), "hf_cache")
os.environ["HF_HOME"] = _local_hf
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.makedirs(_local_hf, exist_ok=True)


class _OnlineTask(QRunnable):
    """Онлайн-перевод в пуле потоков; результаты — через сигналы контроллера."""

    def __init__(self, engine, text, seq, report_ready, report_failed, src_lang="auto", dst_lang="ru"):
        super().__init__()
        self.engine = engine
        self.text = text
        self.seq = seq
        self._report_ready = report_ready
        self._report_failed = report_failed
        self.src_lang = src_lang
        self.dst_lang = dst_lang

    def run(self):
        try:
            result = translate_online(self.engine, self.text, src_lang=self.src_lang, dst_lang=self.dst_lang)
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
    update_available = Signal(dict)

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

        # Применить verbose-флаг из настроек
        set_verbose(bool(self.settings.get("verbose_log", False)))

        # --- OCR-воркер ---
        self._is_selecting = False
        self.selector = None

        preferred_ocr = self.settings.get("ocr_engine", "windows")
        preferred_dir = self.settings.get("ocr_direction", "horizontal")
        self.settings_win.ocr_pill.set_state("busy", "Загрузка OCR-модели…")
        self.ocr = OcrWorker(
            bool(self.settings.get("gpu", False)),
            preferred_engine=preferred_ocr,
            preferred_direction=preferred_dir,
            src_lang=self.settings.get("src_lang", "en"),
        )
        self.ocr.state_changed.connect(self.settings_win.ocr_pill.set_state)
        self.ocr.engine_changed.connect(self.settings_win.on_active_ocr_changed)
        from backend.translators import get_vram_info
        # Честно проверяем наличие видеокарты через системный драйвер:
        has_gpu = (get_vram_info() is not None)
        self.settings_win.set_gpu_available(has_gpu)
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

        # Загрузка локального переводчика, если он скачан
        engine = self.settings.get("translator", "google")
        if engine in LOCAL_ENGINES:
            is_c, _ = is_model_cached(engine)
            if is_c:
                m_file = self.settings.get("selected_gguf", "") if engine == "qwen" else ""
                n_threads = int(self.settings.get("cpu_threads", 0))
                self.model_manager.load(engine, model_filename=m_file, n_threads=n_threads)

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
        task = UpdateCheckTask(lambda data: self.update_available.emit(data))
        QThreadPool.globalInstance().start(task)

        self.settings_win.update_banner.update_clicked.connect(self._start_auto_update)
        self.settings_win.update_banner.snooze_clicked.connect(self._on_update_snoozed)

        self.settings_win.download_gguf_requested.connect(self._on_download_gguf)
        self.settings_win.install_runtime_requested.connect(self._on_install_runtime)
        self.settings_win.delete_gguf_requested.connect(self._on_delete_gguf)

        self.trans_win.toolbar.lang_pair_clicked.connect(self.settings.set_language_pair)
        self.trans_win.toolbar.open_settings_clicked.connect(self.show_settings)

    def _toggle_pause_auto(self):
        if not self._auto_active:
            self.trans_win.show_translation("[Авто-режим не запущен]")
            return

        self._auto_paused = not self._auto_paused
        self.trans_win.set_paused_mode(self._auto_paused)

        if self._auto_paused:
            self._auto_timer.stop()
            print("[auto] режим на паузе — окно скрыто, тулбар активен")
        else:
            print("[auto] режим снят с паузы — окно восстановлено")
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
        elif action == "pause":
            self._toggle_pause_auto()
        elif action == "stop":
            self._req_seq += 1
            self._pending_translations.clear()
            self._loading_queue.clear()
            self.trans_win.set_status("off", "Ожидание")
            self.trans_win.show_translation("[Перевод остановлен]")
        elif action == "clear":
            self.trans_win.clear_history()
            self.trans_win.show_translation("[История очищена]")
        elif action == "ghost":
            self.trans_win.toggle_ghost_mode()

    def _on_install_runtime(self, backend_id: str):
        print(f"[ctrl] Запрос на установку рантайма: {backend_id}")
        self.settings_win.runtime_load_started()

        # Если llama-server сейчас запущен — глушим перед перезаписью
        if self.settings.get("translator") == "qwen":
            self.model_manager.unload()

        self._runtime_worker = RuntimeDownloadWorker(backend_id)
        self._runtime_worker.progress.connect(self.settings_win.runtime_progress)
        self._runtime_worker.failed.connect(self._on_runtime_failed)
        self._runtime_worker.finished.connect(self._on_runtime_finished)
        self._runtime_worker.start_download()

    def _on_runtime_failed(self, backend_id: str, error: str):
        print(f"[ctrl] Ошибка установки рантайма {backend_id}: {error}")
        self.settings_win.runtime_failed(backend_id, error)
        self.settings_win.toast.show_toast(f"Сбой установки движка: {error}", ms=4000)

    def _on_runtime_finished(self, backend_id: str):
        print(f"[ctrl] Рантайм {backend_id} успешно установлен и прошел Smoke Test!")
        self.settings_win.runtime_finished(backend_id)
        self.settings_win.toast.show_toast(f"Движок {backend_id.upper()} готов к работе!")

        # Если у нас сейчас выбран Qwen и выбрана модель — запускаем сервер
        if self.settings.get("translator") == "qwen":
            selected_model = self.settings.get("selected_gguf", "")
            if selected_model:
                use_gpu = bool(self.settings.get("gpu", True))
                self.model_manager.load("qwen", use_gpu=use_gpu, model_filename=selected_model)

    def _on_download_gguf(self, item_dict: dict):
        from backend.llama_server import GgufDownloadWorker
        self.settings_win.model_load_started()

        self._gguf_worker = GgufDownloadWorker(item_dict)
        self._gguf_worker.progress.connect(self.settings_win.model_progress)
        self._gguf_worker.failed.connect(lambda fn, err: self.settings_win.model_failed("qwen", err))
        self._gguf_worker.finished.connect(self._on_gguf_download_finished)
        self._gguf_worker.start_download()

    def _on_gguf_download_finished(self, filename: str):
        self.settings.set("selected_gguf", filename)
        self.settings_win.model_loaded("qwen")
        self.settings_win._update_cache_display()
        self.settings_win.toast.show_toast(f"Модель {filename} успешно скачана!")
        # Если qwen сейчас выбран — сразу запускаем сервер
        if self.settings.get("translator") == "qwen":
            self.model_manager.load("qwen", model_filename=filename)

    def _on_delete_gguf(self, filename: str):
        from backend.llama_server import delete_gguf_model
        if self.settings.get("translator") == "qwen":
            self.model_manager.unload()

        delete_gguf_model(filename)
        self.settings_win._update_cache_display()
        self.settings_win.toast.show_toast(f"Модель {filename} удалена")

    # ---------- выделение области и OCR ----------
    def _start_selection(self):
        if self._is_selecting:
            return
        self._is_selecting = True
        self._was_trans_win_visible = self.trans_win.isVisible() and not self.trans_win.force_hidden
        if self._was_trans_win_visible:
            self.trans_win.hide()

        self.selector = ScreenSelector(self._on_area_selected, on_cancel=self._on_selection_cancel)

    def _on_area_selected(self, bbox):
        self._is_selecting = False
        self.selector = None

        x, y, _ = self._compute_trans_window_pos(bbox)
        bbox_rect = QRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
        self.trans_win.set_forbidden_rect(bbox_rect)
        self.trans_win.move(x, y)

        if not self.trans_win.force_hidden:
            self.trans_win.show()

        self._last_bbox = bbox
        self.ocr.read(bbox)

    def _on_selection_cancel(self):
        self._is_selecting = False
        self.selector = None
        if getattr(self, "_was_trans_win_visible", False) and not self.trans_win.force_hidden:
            self.trans_win.show()

    def _on_read_result(self, bbox, text, context):
        print(f"[ctrl] read_result ctx={context}: {text[:50]!r}")
        vlog(f"[ctrl] read_result (full) ctx={context}: {text!r}")
        if context == "auto":
            if text.startswith("["):
                return
            norm = " ".join(text.lower().split())
            if getattr(self, "_last_sent_auto_text", "") == norm:
                return
            if self._last_auto_text and norm.startswith(self._last_auto_text):
                self._last_auto_text = norm
                self._pending_auto_text = text
                self._auto_timer.start(int(self.settings.get("auto_delay_ms", 800)))
                return
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
            self._was_trans_win_visible = self.trans_win.isVisible() and not self.trans_win.force_hidden
            if self._was_trans_win_visible:
                self.trans_win.hide()

            self.selector = ScreenSelector(self._on_auto_area_selected,
                                           on_cancel=self._on_selection_cancel)

    def _on_auto_area_selected(self, bbox):
        print(f"[auto] область выбрана: {bbox}, force_hidden={self.trans_win.force_hidden}")
        self._is_selecting = False
        self.selector = None
        self._auto_bbox = bbox
        self._last_auto_text = ""
        self._auto_active = True
        self._auto_worker = AutoModeWorker(bbox)
        self._auto_worker.region_changed.connect(self._on_region_changed)
        self._auto_worker.start()

        if self.trans_win.force_hidden:
            self.tray.showMessage(
                "ScreenTale",
                "Окно перевода скрыто. Показать — Ctrl+`\n"
                "Авто-режим работает, переводы копируются в буфер.",
                QSystemTrayIcon.MessageIcon.Information,
                5000)
            return

        x, y, intersects = self._compute_trans_window_pos(bbox)
        bbox_rect = QRect(bbox[0], bbox[1], bbox[2] - bbox[0], bbox[3] - bbox[1])
        self.trans_win.set_forbidden_rect(bbox_rect)
        self.trans_win.show_translation(
            "[Авто-режим включён: отслеживаю область. Alt+W — выключить]",
            x=x, y=y)
        if intersects:
            self.tray.showMessage(
                "ScreenTale",
                "Окно перевода наезжает на область OCR.\n"
                "Сдвиньте окно или уменьшите область OCR (Alt+W).",
                QSystemTrayIcon.MessageIcon.Warning,
                6000)
        self._auto_bbox = bbox
        self._last_bbox = bbox

    def _compute_trans_window_pos(self, bbox):
        left, top, right, bottom = bbox
        win_w, win_h = 420, 120
        SHADOW_MARGIN = 10
        GAP = 10
        needed_v = win_h + SHADOW_MARGIN * 2 + GAP
        needed_h = win_w + SHADOW_MARGIN * 2 + GAP

        # ФИКС: определяем конкретный монитор под рамкой, а не слепо primaryScreen()
        cx = int((left + right) / 2)
        cy = int((top + bottom) / 2)
        screen = QApplication.screenAt(QPoint(cx, cy)) or QApplication.primaryScreen()

        if screen is not None:
            avail = screen.availableGeometry()
            screen_left, screen_top = avail.left(), avail.top()
            screen_right, screen_bottom = avail.right(), avail.bottom()
        else:
            screen_left, screen_top = 0, 0
            screen_right, screen_bottom = 9999, 9999

        candidates = []
        free_top = top - screen_top
        if free_top >= needed_v:
            candidates.append(("сверху", left + 10 - SHADOW_MARGIN,
                                top - GAP - win_h - SHADOW_MARGIN, free_top))
        free_bottom = screen_bottom - bottom
        if free_bottom >= needed_v:
            candidates.append(("снизу", left + 10 - SHADOW_MARGIN,
                                bottom + GAP - SHADOW_MARGIN, free_bottom))
        free_left = left - screen_left
        if free_left >= needed_h:
            candidates.append(("слева", left - GAP - win_w - SHADOW_MARGIN,
                                top + 10 - SHADOW_MARGIN, free_left))
        free_right = screen_right - right
        if free_right >= needed_h:
            candidates.append(("справа", right + GAP - SHADOW_MARGIN,
                                top + 10 - SHADOW_MARGIN, free_right))

        if candidates:
            best = max(candidates, key=lambda c: c[3])
            _side, x, y, _ = best
            if screen is not None:
                avail = screen.availableGeometry()
                max_x = avail.right() - win_w - SHADOW_MARGIN
                x = min(x, max_x)
                x = max(x, avail.left())
            return x, y, False

        fallback = [
            ("сверху", left + 10 - SHADOW_MARGIN, top - GAP - win_h - SHADOW_MARGIN, free_top),
            ("снизу", left + 10 - SHADOW_MARGIN, bottom + GAP - SHADOW_MARGIN, free_bottom),
            ("слева", left - GAP - win_w - SHADOW_MARGIN, top + 10 - SHADOW_MARGIN, free_left),
            ("справа", right + GAP - SHADOW_MARGIN, top + 10 - SHADOW_MARGIN, free_right),
        ]
        best = max(fallback, key=lambda c: c[3])
        _side, x, y, _ = best
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
        self._pending_translations.clear()
        self._loading_queue.clear()
        self._last_shown_seq = self._req_seq
        self.trans_win.set_forbidden_rect(None)

        self.trans_win.hide()
        self.trans_win.toolbar.hide()
        self._auto_paused = False
        self.trans_win.toolbar.set_pause_active(False)

    def _on_region_changed(self):
        if not self._auto_active or self._auto_bbox is None or self._auto_paused:
            return
        if not self.trans_win.isVisible() or self.trans_win.force_hidden:
            return
        print("[auto] region_changed -> ocr.read()")
        self.ocr.read(self._auto_bbox, context="auto")

    def _execute_auto_translate(self):
        text = self._pending_auto_text
        print(f"[ctrl] дебаунс-таймер: {text[:50]!r}")
        vlog(f"[ctrl] дебаунс-таймер (full): {text!r}")
        self._pending_auto_text = ""
        if not text.strip():
            return
        norm = " ".join(text.lower().split())
        if getattr(self, "_last_sent_auto_text", "") == norm:
            return
        self._last_sent_auto_text = norm
        self._last_source = text
        self._request_translation(text)

    # ---------- перевод ----------
    def _request_translation(self, text):
        if not text.strip():
            return

        # Получаем выбранную языковую пару из настроек
        src_lang = self.settings.get("src_lang", "en")
        dst_lang = self.settings.get("dst_lang", "ru")

        #  ЗАЩИТА: если исходный и целевой языки совпадают (например, RU -> RU)
        if src_lang == dst_lang:
            print(f"[ctrl] Исходный и целевой язык совпадают ({src_lang.upper()}) -> мгновенный вывод оригинала")
            self._req_seq += 1
            self._apply_translation_result(self._req_seq, text)
            return

        # Защита от перегрузки очереди (Backpressure, до 3 реплик)
        MAX_PENDING = 3
        untranslated = [s for s, data in self._pending_translations.items() if data[3] is None]
        if len(untranslated) >= MAX_PENDING:
            oldest_seq = min(untranslated)
            self._pending_translations[oldest_seq][3] = "[skip]"
            self._flush_pending_translations()

        self._req_seq += 1
        seq = self._req_seq
        engine = self.settings.get("translator", "google")

        print(f"[ctrl] запрос перевода ({src_lang.upper()} -> {dst_lang.upper()}): движок={engine}, seq={seq}")
        self._pending_translations[seq] = [
            time.strftime("%H:%M:%S"), time.monotonic(), text, None]
        self.trans_win.set_status("busy", "Перевод…")

        if engine in LOCAL_ENGINES:
            if self._model_loading:
                print(f"[ctrl] модель загружается -> seq={seq} добавлен в FIFO-очередь")
                self._loading_queue.append(seq)
                return
            # Передаем языки в локальный менеджер (LLM / NLLB / Opus)
            self.model_manager.translate(text, seq, src_lang=src_lang, dst_lang=dst_lang)
        else:
            # Передаем языки в онлайн-таску (Google / MyMemory)
            task = _OnlineTask(engine, text, seq,
                               self.translation_ready.emit,
                               self.translation_failed.emit,
                               src_lang=src_lang, dst_lang=dst_lang)
            QThreadPool.globalInstance().start(task)

    def _apply_translation_result(self, seq, text):
        if seq <= self._last_shown_seq:
            return

        print(f"[ctrl] перевод seq={seq} (ожидался {self._req_seq}): {text[:50]!r}")
        vlog(f"[ctrl] перевод (full) seq={seq} (ожидался {self._req_seq}): {text!r}")

        if text.startswith("["):
            self.trans_win.show_translation(text)
            return

        if seq in self._pending_translations:
            self._pending_translations[seq][3] = text
        else:
            self._pending_translations[seq] = [
                time.strftime("%H:%M:%S"), time.monotonic(), "?", text]

        self._flush_pending_translations()

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
                self.trans_win.show_translation(f"[Нет перевода: {source[:30]}...]")
                del self._pending_translations[next_seq]
                self._last_shown_seq = next_seq
                continue

            if translation == "[skip]":
                del self._pending_translations[next_seq]
                self._last_shown_seq = next_seq
                continue

            if translation.startswith("["):
                self.trans_win.show_translation(translation)
            else:
                self.trans_win.show_translation(f"({timestamp}) {translation}")

            del self._pending_translations[next_seq]
            self._last_shown_seq = next_seq

    def _on_translation_failed(self, seq, engine, error):
        print(f"[ctrl] сбой перевода seq={seq} (движок {engine}): {error}")

        if seq in self._pending_translations:
            self._pending_translations[seq][3] = f"[Ошибка ({engine}): {_short(error)}]"
        self._flush_pending_translations()

        if seq == self._req_seq:
            self.trans_win.set_status("error", "Ошибка")

        if self._fallback_active or not is_network_error(error):
            return

        available_engine = get_available_offline_engine(src_lang=self.settings.get("src_lang", "en"), 
                                                dst_lang=self.settings.get("dst_lang", "ru"), 
                                                preferred="opus")
        if available_engine is None:
            self._apply_translation_result(
                seq, "[Интернет недоступен, а офлайн-модель не скачана. Скачайте её в Настройках]")
            self.trans_win.set_status("error", "Нет сети и модели")
            return

        self._fallback_active = True
        self._pending_fallback = self._last_source
        self.trans_win.show_translation(
            f"[Интернет недоступен — переключаюсь на офлайн ({ENGINE_LABELS.get(available_engine)})...]"
        )
        self.trans_win.set_status("busy", "Переключение на офлайн…")
        self.settings.set("translator", available_engine)

    def _on_model_load_started(self):
        self._model_loading = True
        print("[ctrl] статус модели: загрузка началась -> включен режим накопления очереди")

    def _on_model_ready(self, engine_id):
        self._model_loading = False
        print(f"[ctrl] модель {engine_id} готова к работе!")
        self.trans_win.set_status("ok", "Модель готова")

        if self._loading_queue:
            print(f"[ctrl] разгрузка FIFO-очереди ({len(self._loading_queue)} фраз)...")
            while self._loading_queue:
                q_seq = self._loading_queue.pop(0)
                if q_seq in self._pending_translations:
                    q_text = self._pending_translations[q_seq][2]
                    self.model_manager.translate(q_text, q_seq)

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
        if self._loading_queue:
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
            # 1. ОБЯЗАТЕЛЬНО отправляем команду переключения в менеджер моделей:
            self.model_manager.set_device(bool(value))
            if self.settings.get("ocr_engine") == "easyocr":
                self.ocr.request_gpu(bool(value))

        elif key == "translator":
            self._pending_translations.clear()
            self._loading_queue.clear()
            self._last_shown_seq = self._req_seq
            self.trans_win.set_status("off", "Ожидание")

            if value in LOCAL_ENGINES:
                m_file = self.settings.get("selected_gguf", "") if value == "qwen" else ""
                n_threads = int(self.settings.get("cpu_threads", 0))
                is_c, _ = is_model_cached(value, filename=m_file)
                if is_c:
                    use_gpu = bool(self.settings.get("gpu", True))
                    self.model_manager.load(value, use_gpu=use_gpu, model_filename=m_file, n_threads=n_threads)
                else:
                    self.model_manager.unload()
            else:
                self.model_manager.unload()
                self.settings_win.model_finished("off", "Локальная модель не загружена")

        elif key == "ocr_engine":
            self.ocr.request_engine(value)
        elif key == "ocr_direction":
            self.ocr.request_direction(value)
        elif key == "selected_gguf" and self.settings.get("translator") == "qwen":
            use_gpu = bool(self.settings.get("gpu", True))
            self.model_manager.load("qwen", use_gpu=use_gpu, model_filename=value)
        elif key == "src_lang":
            self.ocr.request_language(value)
            src = value
            dst = self.settings.get("dst_lang", "ru")
            self.trans_win.toolbar.update_lang_badge(src, dst)
        elif key in ("dst_lang", "language_pair"):
            src = self.settings.get("src_lang", "en")
            dst = self.settings.get("dst_lang", "ru")
            self.trans_win.toolbar.update_lang_badge(src, dst)
        elif key == "cpu_threads":
            # Если сейчас работает Qwen — перезапускаем сервер с новым количеством потоков
            if self.settings.get("translator") == "qwen":
                m_file = self.settings.get("selected_gguf", "")
                use_gpu = bool(self.settings.get("gpu", True))
                self.model_manager.load("qwen", use_gpu=use_gpu, model_filename=m_file, n_threads=int(value))

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
        # OCR больше не имеет права перезаписывать состояние видеопамяти для моделей!
        # ModelManager управляется строго через self.settings.get("gpu")
        pass

    # ---------- иконка и трей ----------
    def _get_icon(self):
        icon_path = os.path.join(get_app_dir(), "ScreenTale.ico")
        if os.path.exists(icon_path):
            return QIcon(icon_path)

        if hasattr(sys, "_MEIPASS"):
            internal_icon = os.path.join(sys._MEIPASS, "ScreenTale.ico")
            if os.path.exists(internal_icon):
                return QIcon(internal_icon)

        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#e08e45"))
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
        menu.setWindowFlags(menu.windowFlags() | Qt.WindowType.FramelessWindowHint)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # 1. Шапка меню: Слева название+версия, Справа логотип (28x28)
        header_widget = QWidget()
        header_widget.setObjectName("TrayHeader")
        h_lay = QHBoxLayout(header_widget)
        h_lay.setContentsMargins(10, 6, 10, 6)
        h_lay.setSpacing(10)

        # Текст (Название + Версия)
        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)

        lbl_title = QLabel("ScreenTale")
        lbl_title.setObjectName("TrayTitle")
        lbl_ver = QLabel(f"версия {APP_VERSION}")
        lbl_ver.setObjectName("TrayVersion")

        text_box.addWidget(lbl_title)
        text_box.addWidget(lbl_ver)
        h_lay.addLayout(text_box, 1)

        # Логотип справа
        logo_path = os.path.join(get_app_dir(), "logo.png")
        if not os.path.exists(logo_path) and hasattr(sys, "_MEIPASS"):
            logo_path = os.path.join(sys._MEIPASS, "logo.png")

        if os.path.exists(logo_path):
            lbl_logo = QLabel()
            lbl_logo.setStyleSheet("background: transparent;")
            pix = QPixmap(logo_path).scaled(
                28, 28,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            lbl_logo.setPixmap(pix)
            h_lay.addWidget(lbl_logo, 0, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)

        act_header = QWidgetAction(menu)
        act_header.setDefaultWidget(header_widget)
        menu.addAction(act_header)

        menu.addSeparator()

        # 2. Основные действия
        act_settings = menu.addAction("Настройки")
        act_settings.triggered.connect(self.show_settings)

        act_toggle = menu.addAction("Окно перевода\tCtrl + `")
        act_toggle.triggered.connect(self.trans_win.toggle_visible)

        act_single = menu.addAction("Выделить область\tAlt + Q")
        act_single.triggered.connect(self._start_selection)

        act_auto = menu.addAction("Авто-режим\tAlt + W")
        act_auto.triggered.connect(self._toggle_auto_mode)

        menu.addSeparator()

        # 3. Выход
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

        self.settings_win.update_banner.show_update(manifest_data)

    def _on_update_snoozed(self, manifest_data: dict):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        snooze_date = (today + datetime.timedelta(days=7)).isoformat()
        self.settings.set("update_snooze_until", snooze_date)
        self.settings_win.toast.show_toast("Напоминание об обновлении отложено на 7 дней")

    def _start_auto_update(self, manifest_data: dict):
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
        self.settings_win.update_banner.hide()

    def _on_update_ready_to_restart(self):
        self.settings_win.update_banner.update_progress(100, "Обновление готово! Перезапуск...")
        self.settings_win.toast.show_toast("Перезапуск программы…")
        QTimer.singleShot(1000, self.exit_app)

    # ---------- выход ----------
    def exit_app(self):
        # 1. Сначала ПРЯЧЕМ весь интерфейс, чтобы юзер видел мгновенный отклик
        self.trans_win.hide()
        self.settings_win.hide()
        if getattr(self, "tray", None) is not None:
            self.tray.hide()

        # 2. Мягко останавливаем воркеры
        if self._auto_worker is not None:
            self._auto_worker.requestInterruption()
            self._auto_worker.wait(2000)
        self.ocr.stop()
        self.model_manager.stop()
        self.hotkeys.shutdown()
        
        # вызываем force_save() вместо save()
        self.settings.force_save() 
        
        QThreadPool.globalInstance().waitForDone(2000)
        self.app.quit()

    def _retry_last_translation(self):
        if self._last_bbox is not None:
            if not self.trans_win.isVisible():
                self.trans_win.show()
                self.trans_win.toolbar.show()
            self.trans_win.set_status("busy", "Повтор…")
            print(f"[ctrl] повтор перевода для bbox={self._last_bbox}")
            self.ocr.read(self._last_bbox, context="single")
        else:
            self.trans_win.show_translation("[Нет сохранённой области для повтора]")


def main():
    # Флаг быстрой консольной диагностики: python main.py --doctor
    if "--doctor" in sys.argv:
        from debug_diagnostics import run_diagnostics
        run_diagnostics(export_zip=True)
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("screentale.app.1")
    except Exception:
        pass

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)

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