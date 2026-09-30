import os
import sys

from PySide6.QtCore import QPoint, Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from backend.config import APP_VERSION, get_app_dir
from backend.hotkeys import HotkeyManager
from backend.languages import LANGUAGES, get_lang_name, get_win_ocr_tag
from backend.logging_setup import set_verbose
from backend.ocr_engines import WindowsOcrEngine
from backend.runtime_manager import (
    BACKENDS_CONFIG,
    detect_best_backend,
    get_installed_backend,
    is_backend_supported,
)
from backend.translators import ENGINE_LABELS
from frontend.theme import PALETTE, apply_theme
from frontend.widgets import (
    BusyBar,
    HotkeyRecorder,
    SegmentedControl,
    StatusPill,
    Toast,
    ToggleSwitch,
)

TRANSLATORS = [
    ("google", "Google"),
    ("mymemory", "MyMemory"),
    ("opus", "Opus-MT"),
    ("nllb", "NLLB-200"),
    ("qwen", "LLM"),
]

TRANSLATOR_HINTS = {
    "google": "Онлайн-сервис Google. Требуется интернет, качество хорошее.",
    "mymemory": "Онлайн-сервис MyMemory. Есть лимиты запросов, качество среднее.",
    "opus": "Локальная модель Opus-MT (~300 МБ). Только en→ru, работает офлайн.",
    "nllb": "Локальная модель NLLB-200 (~2.5 ГБ). Работает офлайн, качество выше.",
    "qwen": "Локальная нейросеть (GGUF). Работает полностью офлайн на вашей видеокарте или процессоре через движок llama.cpp.",
}

OCR_ENGINES = [
    ("windows", "Windows OCR"),
    ("easyocr", "EasyOCR (PyTorch)"),
    ("rapidocr", "RapidOCR"),
]

OCR_HINTS = {
    "windows": "Нативный системный движок Windows 10/11. Молниеносное чтение (~15 мс), 0 МБ VRAM.",
    "easyocr": "Классический движок на PyTorch. Задержка ~500 мс.",
    "rapidocr": "Легковесный ONNX-движок. Высокая точность на сложных стилизованных шрифтах, иероглифах и манге.",
}

OCR_DIRECTIONS = [
    ("horizontal", "→  Горизонтальный"),
    ("vertical", "↓  Вертикальный (Tategaki)"),
]

# ============================================================
# Встроенный баннер обновления внизу окна настроек
# ============================================================
class _UpdateBanner(QFrame):
    update_clicked = Signal(dict)
    snooze_clicked = Signal(dict)
    close_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("UpdateBanner")
        self._manifest_data = {}
        self.hide()

        # Явный стильный контейнер с янтарной рамкой
        self.setStyleSheet("""
            QFrame#UpdateBanner {
                background-color: #221f1c;
                border: 1px solid #e08e45;
                border-radius: 10px;
            }
        """)

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(8)

        # Верхняя строчка: Заголовок + Крестик закрытия
        top_h = QHBoxLayout()
        top_h.setContentsMargins(0, 0, 0, 0)
        self.lbl_title = QLabel("Доступно обновление ScreenTale")
        # Принудительно делаем текст заголовка светлым и читаемым
        self.lbl_title.setStyleSheet("font-weight: 600; font-size: 13px; color: #f2ede4; background: transparent;")
        top_h.addWidget(self.lbl_title, 1)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("BannerCloseBtn")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet("""
            QPushButton#BannerCloseBtn {
                background: transparent;
                border: none;
                border-radius: 4px;
                color: #9c9388;
                font-size: 13px;
                font-weight: bold;
                padding: 0px;
            }
            QPushButton#BannerCloseBtn:hover {
                background: rgba(224, 142, 69, 0.25);
                color: #f2ede4;
            }
        """)
        self.btn_close.clicked.connect(self._on_close)
        top_h.addWidget(self.btn_close)
        v.addLayout(top_h)

        # Полоса прогресса и статус
        self.progress_bar = BusyBar()
        self.progress_bar.hide()
        v.addWidget(self.progress_bar)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("Hint")
        self.lbl_status.hide()
        v.addWidget(self.lbl_status)

        # Нижняя строчка: Кнопки действий
        self.btn_layout = QHBoxLayout()
        self.btn_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_layout.setSpacing(8)

        self.btn_update = QPushButton("⚡ Обновить сейчас")
        self.btn_update.setObjectName("UpdateBtn")
        self.btn_update.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_update.setStyleSheet("""
            QPushButton#UpdateBtn {
                background-color: #e08e45;
                color: #1a1816;
                font-weight: 600;
                border: none;
                border-radius: 7px;
                padding: 6px 14px;
            }
            QPushButton#UpdateBtn:hover {
                background-color: #f59e0b;
            }
        """)
        self.btn_update.clicked.connect(self._on_update)

        self.btn_snooze = QPushButton("Напомнить через 7 дней")
        self.btn_snooze.setObjectName("BannerSnoozeBtn")
        self.btn_snooze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_snooze.setStyleSheet("""
            QPushButton#BannerSnoozeBtn {
                background: transparent;
                border: 1px solid #3d3731;
                border-radius: 7px;
                color: #f2ede4;
                font-size: 12px;
                padding: 6px 12px;
            }
            QPushButton#BannerSnoozeBtn:hover {
                background: #2c2824;
                border-color: #e08e45;
            }
        """)
        self.btn_snooze.clicked.connect(self._on_snooze)

        self.btn_layout.addWidget(self.btn_update)
        self.btn_layout.addWidget(self.btn_snooze)
        self.btn_layout.addStretch(1)
        v.addLayout(self.btn_layout)

    def show_update(self, manifest_data: dict):
        self._manifest_data = manifest_data
        ver = manifest_data.get("version", "")
        self.lbl_title.setText(f"Доступна новая версия ScreenTale v{ver}!")
        self.show()

    def set_downloading_state(self, status_text: str):
        self.btn_update.hide()
        self.btn_snooze.hide()
        self.btn_close.hide()
        self.progress_bar.show()
        self.progress_bar.start_indeterminate()
        self.lbl_status.show()
        self.lbl_status.setText(status_text)

    def update_progress(self, percent: int, label: str):
        if percent < 0:
            self.progress_bar.start_indeterminate()
        else:
            self.progress_bar.set_value(percent)
        self.lbl_status.setText(label)

    def _on_update(self):
        self.update_clicked.emit(self._manifest_data)

    def _on_snooze(self):
        self.snooze_clicked.emit(self._manifest_data)
        self.hide()

    def _on_close(self):
        self.close_clicked.emit()
        self.hide()


class SettingsWindow(QWidget):
    download_gguf_requested = Signal(dict)
    delete_gguf_requested = Signal(str)
    clear_all_cache_requested = Signal()
    install_runtime_requested = Signal(str)

    HOTKEY_ACTIONS = (
        ("single", "Перевести выделенную область"),
        ("auto", "Авто-перевод: вкл/выкл"),
        ("pause", "Пауза авто-перевода"),
        ("toggle_window", "Показать/скрыть окно перевода"),
        ("stop", "Остановить текущий перевод"),
        ("clear", "Очистить историю переводов"),
        ("ghost", "Сквозной клик (Ghost mode): вкл/выкл"),
    )

    def __init__(self, settings, hotkeys: HotkeyManager, on_exit=None, hide_on_close=True):
        super().__init__()
        self.settings = settings
        self.hotkeys = hotkeys
        self.on_exit = on_exit
        self._hide_on_close = hide_on_close
        self._action_titles = dict(self.HOTKEY_ACTIONS)
        self._gpu_available = True

        self.setWindowTitle("ScreenTale — Настройки")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(840, 600)

        self._drag_pos = None

        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.container = QFrame()
        self.container.setObjectName("SettingsContainer")
        outer_layout.addWidget(self.container)

        root = QHBoxLayout(self.container)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        right_panel = QWidget()
        rv = QVBoxLayout(right_panel)
        rv.setContentsMargins(0, 0, 0, 14)
        rv.setSpacing(6)
        rv.addWidget(self._build_top_bar())
        rv.addWidget(self._build_content(), 1)

        self.update_banner = _UpdateBanner(self)
        rv.addWidget(self.update_banner)

        root.addWidget(right_panel, 1)

        self.toast = Toast(self, left_offset=200)
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(450)
        self._saved_timer.timeout.connect(lambda: self.toast.show_toast("Настройки сохранены"))

        self.settings.changed.connect(self._on_setting_changed)
        self._sync_from_settings()

        if sys.platform == "win32":
            try:
                import ctypes
                val = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), 33, ctypes.byref(val), ctypes.sizeof(val)
                )
            except Exception:
                pass

    def _build_top_bar(self):
        """Верхняя плашка с кнопками «Свернуть» и «Закрыть»."""
        top_bar = QWidget()
        top_bar.setFixedHeight(38)
        h = QHBoxLayout(top_bar)
        h.setContentsMargins(0, 5, 8, 0)
        h.setSpacing(4)
        h.addStretch(1)

        btn_min = QPushButton("—")
        btn_min.setObjectName("WinBtn")
        btn_min.setToolTip("Свернуть")
        btn_min.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_min.clicked.connect(self.showMinimized)

        btn_close = QPushButton("✕")
        btn_close.setObjectName("WinBtnClose")
        btn_close.setToolTip("Закрыть в трей")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.clicked.connect(self.hide)

        h.addWidget(btn_min)
        h.addWidget(btn_close)
        return top_bar

    # ---------------- каркас ----------------
    def _build_sidebar(self):
        frame = QFrame()
        frame.setObjectName("Sidebar")
        frame.setFixedWidth(200)
        v = QVBoxLayout(frame)
        v.setContentsMargins(12, 16, 12, 14)
        v.setSpacing(4)

        # Горизонтальный блок: Логотип + (Название + Версия)
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(8)

        # 1. Поиск и отрисовка logo.png
        logo_path = os.path.join(get_app_dir(), "logo.png")
        if not os.path.exists(logo_path) and hasattr(sys, "_MEIPASS"):
            logo_path = os.path.join(sys._MEIPASS, "logo.png")

        if os.path.exists(logo_path):
            logo_lbl = QLabel()
            pix = QPixmap(logo_path).scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            logo_lbl.setPixmap(pix)
            logo_lbl.setFixedSize(64, 64)
            header_layout.addWidget(logo_lbl)

        # 2. Текстовая колонка
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        title = QLabel("ScreenTale")
        title.setObjectName("AppTitle")
        ver = QLabel(f"версия {APP_VERSION}")
        ver.setObjectName("Version")

        text_layout.addWidget(title)
        text_layout.addWidget(ver)
        header_layout.addLayout(text_layout, 1)

        v.addLayout(header_layout)
        v.addSpacing(14)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        for name in ("Общие", "Перевод", "Горячие клавиши", "Продвинутые", "О программе"):
            self.nav.addItem(QListWidgetItem(name))
        v.addWidget(self.nav, 1)

        btn_exit = QPushButton("Выйти из программы")
        btn_exit.setObjectName("Danger")
        btn_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_exit.clicked.connect(self._handle_exit)
        v.addWidget(btn_exit)
        return frame

    def _open_windows_language_settings(self):
        """Открывает окно 'Язык и регион' в параметрах Windows 10/11."""
        try:
            os.startfile("ms-settings:regionlanguage")
        except Exception as e:
            self.toast.show_toast(f"Не удалось открыть параметры: {e}")

    def _copy_powershell_ocr_cmd(self):
        """Копирует команду PowerShell для установки пакета в буфер обмена."""
        src_lang = self.settings.get("src_lang", "en")
        tag = get_win_ocr_tag(src_lang)
        cmd = f'Add-WindowsCapability -Online -Name "Language.OCR~~~{tag}~0.0.1.0"'
        QApplication.instance().clipboard().setText(cmd)
        self.toast.show_toast("Команда скопирована! Запустите PowerShell от админа и вставьте (Ctrl+V).", ms=3500)

    def _update_ocr_lang_warning(self):
        """Проверяет наличие системного пакета Windows OCR и обновляет плашку."""
        engine = self.settings.get("ocr_engine", "windows")
        src_lang = self.settings.get("src_lang", "en")

        if engine != "windows":
            self.ocr_lang_warn_frame.hide()
            return

        tag = get_win_ocr_tag(src_lang)
        win_eng = WindowsOcrEngine(default_lang=tag)
        ok, _ = win_eng.check_language_support(tag)

        if not ok:
            lang_name = get_lang_name(src_lang)
            
            if src_lang in ("ja", "zh"):
                fallback_note = "Сейчас сканирование временно выполняет встроенный RapidOCR (Азия / Манга).\n"
            else:
                fallback_note = "Без пакета распознавание текста на этом языке может работать некорректно.\n"

            self.lbl_ocr_warn_title.setText(f"⚠️ Пакет Windows OCR не найден: [{lang_name} ({tag})]")
            self.lbl_ocr_warn_text.setText(
                f"{fallback_note}"
                f"Установите компонент распознавания в Windows для быстрого чтения:"
            )
            self.ocr_lang_warn_frame.show()
        else:
            self.ocr_lang_warn_frame.hide()

    def _build_content(self):
        self.pages = QStackedWidget()
        self.pages.addWidget(self._wrap(self._page_general()))
        self.pages.addWidget(self._wrap(self._page_translation()))
        self.pages.addWidget(self._wrap(self._page_hotkeys()))
        self.pages.addWidget(self._wrap(self._page_advanced()))
        self.pages.addWidget(self._wrap(self._page_about()))
        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)
        return self.pages

    def _wrap(self, page):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _update_vram_monitor(self):
        from backend.translators import get_vram_info
        info = get_vram_info()
        if info is not None:
            used_b, total_b, gpu_name = info
            used_gb = used_b / (1024 ** 3)
            total_gb = total_b / (1024 ** 3)
            pct = min(int((used_b / max(total_b, 1)) * 100), 100)
            name_str = f" · {gpu_name}" if gpu_name else ""
            self.lbl_vram_text.setText(f"Видеопамять (VRAM): {used_gb:.1f} / {total_gb:.1f} ГБ ({pct}%){name_str}")
            self.vram_bar.set_value(pct)
            self.vram_widget.show()
        else:
            self.vram_widget.hide()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_vram_timer"):
            self._update_vram_monitor()
            self._vram_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_vram_timer"):
            self._vram_timer.stop()

    def _on_lang_combo_changed(self):
        src = self.combo_src_lang.currentData()
        dst = self.combo_dst_lang.currentData()
        if src and dst:
            cur_src = self.settings.get("src_lang", "en")
            cur_dst = self.settings.get("dst_lang", "ru")
            if src != cur_src or dst != cur_dst:
                self.settings.set_language_pair(src, dst)
                self._saved_timer.start()

    def _update_lang_lock_state(self):
        """Блокирует или разблокирует выбор языков в зависимости от выбранного движка."""
        engine = self.settings.get("translator", "google")
        is_opus = (engine == "opus")

        if is_opus:
            # Принудительно выставляем EN -> RU
            self.settings.set_language_pair("en", "ru")
            self.combo_src_lang.setEnabled(False)
            self.combo_dst_lang.setEnabled(False)
            self.btn_swap_langs.setEnabled(False)
            self.lbl_lang_lock_hint.show()
        else:
            self.combo_src_lang.setEnabled(True)
            self.combo_dst_lang.setEnabled(True)
            self.btn_swap_langs.setEnabled(True)
            self.lbl_lang_lock_hint.hide()

    def _on_swap_langs_clicked(self):
        src = self.combo_src_lang.currentData()
        dst = self.combo_dst_lang.currentData()
        if src and dst:
            self.settings.set_language_pair(dst, src)
            self._saved_timer.start()

    # ---------------- хелперы ----------------
    def _card(self, title=None):
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 16)
        v.setSpacing(10)
        if title:
            t = QLabel(title)
            t.setObjectName("CardTitle")
            v.addWidget(t)
        return card, v

    def _option_row(self, text, control, hint=None):
        w = QWidget()
        w.setObjectName("Row")
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        left.addWidget(QLabel(text))
        if hint:
            hl = QLabel(hint)
            hl.setObjectName("Hint")
            hl.setWordWrap(True)
            left.addWidget(hl)
        h.addLayout(left, 1)
        h.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return w

    def _page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(14)
        return page, v

    def _build_catalog_item_row(self, item: dict) -> QWidget:
        w = QWidget()
        w.setObjectName("Row")
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 4, 0, 4)
        h.setSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(2)
        t_row = QHBoxLayout()
        t_lbl = QLabel(f"<b>{item['title']}</b> ({item['approx_size']})")
        b_lbl = QLabel(item["badge"])
        b_lbl.setStyleSheet("color: #9c9388; font-size: 11px;")
        t_row.addWidget(t_lbl)
        t_row.addWidget(b_lbl)
        t_row.addStretch(1)

        d_lbl = QLabel(item["desc"])
        d_lbl.setObjectName("Hint")
        d_lbl.setWordWrap(True)

        left.addLayout(t_row)
        left.addWidget(d_lbl)
        h.addLayout(left, 1)

        btn_dl = QPushButton("⬇ Скачать")
        btn_dl.setObjectName("Ghost")
        btn_dl.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_dl.clicked.connect(lambda _=False, it=item: self.download_gguf_requested.emit(it))

        btn_del = QPushButton("Удалить")
        btn_del.setObjectName("Danger")
        btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_del.clicked.connect(lambda _=False, fn=item["filename"]: self._confirm_delete_gguf(fn))

        h.addWidget(btn_dl)
        h.addWidget(btn_del)

        w._btn_dl = btn_dl
        w._btn_del = btn_del
        return w

    def _open_models_folder(self):
        from backend.llama_server import get_models_dir
        os.startfile(get_models_dir())

    def _confirm_delete_gguf(self, filename: str):
        ans = QMessageBox.question(
            self,
            "Удаление модели",
            f"Удалить файл модели {filename} с диска?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.delete_gguf_requested.emit(filename)

    def _on_gguf_combo_changed(self, index):
        filename = self.gguf_combo.currentText()
        if filename and filename != self.settings.get("selected_gguf"):
            self.settings.set("selected_gguf", filename)
            self._saved_timer.start()
            # Не вызываем _update_cache_display() целиком, чтобы не сбрасывать комбобокс во время выбора!

    # ---------------- страница: Общие ----------------
    def _page_general(self):
        page, v = self._page()

        card, cv = self._card("Внешний вид")

        self.theme_seg = SegmentedControl([
            ("dark", "Янтарная"),
            ("dark_classic", "Тёмная"),
            ("light", "Светлая")
        ])
        self.theme_seg.setMinimumWidth(260)
        cv.addWidget(self._option_row("Тема оформления", self.theme_seg))
        self.theme_seg.valueChanged.connect(self._on_theme_changed)
        frow = QWidget()
        frow.setObjectName("Row")
        fh = QHBoxLayout(frow)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(10)
        fh.addWidget(QLabel("Размер шрифта"))
        fh.addStretch(1)
        self.font_slider = QSlider(Qt.Orientation.Horizontal)
        self.font_slider.setRange(10, 28)
        self.font_slider.setMinimumWidth(200)
        self.font_val = QLabel("14")
        self.font_val.setObjectName("Hint")
        self.font_val.setFixedWidth(26)
        self.font_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        fh.addWidget(self.font_slider)
        fh.addWidget(self.font_val)
        cv.addWidget(frow)

        self.preview_frame = QFrame()
        self.preview_frame.setObjectName("Preview")
        pv = QVBoxLayout(self.preview_frame)
        pv.setContentsMargins(14, 10, 14, 10)
        self.preview_label = QLabel(
            "The quick brown fox jumps over the lazy dog.\n"
            "Быстрый перевод текста с экрана — 0123.")
        self.preview_label.setWordWrap(True)
        pv.addWidget(self.preview_label)
        cv.addWidget(self.preview_frame)

        self.outline_toggle = ToggleSwitch()
        cv.addWidget(self._option_row(
            "Контрастная обводка текста",
            self.outline_toggle,
            "Тонкий темный контур букв для 100% читаемости на снегу и ярком фоне игры при высокой прозрачности."
        ))

        orow = QWidget()
        orow.setObjectName("Row")
        oh = QHBoxLayout(orow)
        oh.setContentsMargins(0, 0, 0, 0)
        oh.setSpacing(10)
        oh.addWidget(QLabel("Прозрачность окна перевода"))
        oh.addStretch(1)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.setMinimumWidth(200)
        self.opacity_val = QLabel("95 %")
        self.opacity_val.setObjectName("Hint")
        self.opacity_val.setFixedWidth(38)
        self.opacity_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        oh.addWidget(self.opacity_slider)
        oh.addWidget(self.opacity_val)
        cv.addWidget(orow)

        v.addWidget(card)

        card2, c2 = self._card("Поведение")
        self.autocopy_toggle = ToggleSwitch()
        c2.addWidget(self._option_row(
            "Копировать перевод в буфер обмена",
            self.autocopy_toggle,
            "Результат перевода всегда доступен по Ctrl+V."))
        v.addWidget(card2)
        v.addStretch(1)

        self.font_slider.valueChanged.connect(self._on_font_slider)
        self.opacity_slider.valueChanged.connect(self._on_opacity_slider)
        self.autocopy_toggle.toggled.connect(self._on_autocopy)
        self.outline_toggle.toggled.connect(self._on_outline_toggled)
        return page

    def _on_theme_changed(self, ident):
        apply_theme(QApplication.instance(), ident)
        self._update_preview_font(self.font_slider.value())  # перечитать цвет текста превью
        self.settings.set("theme", ident)
        self._saved_timer.start()

    def _on_font_slider(self, value):
        self.font_val.setText(str(value))
        self._update_preview_font(value)
        self.settings.set("font_size", value)
        self._saved_timer.start()

    def _on_auto_delay_slider(self, value):
        val = (value // 50) * 50  # округляем с шагом 50 мс
        self.auto_delay_val.setText(f"{val} мс")
        self.settings.set("auto_delay_ms", val)
        self._saved_timer.start()

    def _update_preview_font(self, size):
        self.preview_label.setStyleSheet(
            f"font-size: {int(size)}px; color: {PALETTE['text']};")

    def _on_opacity_slider(self, value):
        self.opacity_val.setText(f"{value} %")
        self.settings.set("opacity", value / 100.0)
        self._saved_timer.start()

    def _on_autocopy(self, checked):
        self.settings.set("auto_copy", bool(checked))
        self._saved_timer.start()

    def _on_verbose_toggled(self, checked):
        self.settings.set("verbose_log", bool(checked))
        set_verbose(bool(checked))   # сразу применить к глобальному флагу logging_setup
        if checked:
            self.toast.show_toast(
                "Подробный лог включён — не забудь выключить после отладки",
                ms=4000)
        else:
            self._saved_timer.start()

# ---------------- страница: Перевод ----------------

    def _page_translation(self):
        page, v = self._page()

        # ========================================================
        # Карточка 0: Языковая пара (Any-to-Any)
        # ========================================================
        card_lang, c_lang = self._card("Языковая пара")

        # Подсказка о блокировке для Opus-MT
        self.lbl_lang_lock_hint = QLabel("🔒 Opus-MT поддерживает только перевод с английского на русский (EN ➔ RU)")
        self.lbl_lang_lock_hint.setObjectName("Hint")
        self.lbl_lang_lock_hint.setStyleSheet("color: #e08e45; font-size: 11px;")
        self.lbl_lang_lock_hint.setWordWrap(True)
        self.lbl_lang_lock_hint.hide()  # По умолчанию скрыта
        c_lang.addWidget(self.lbl_lang_lock_hint)

        pair_row = QWidget()
        pair_row.setObjectName("Row")
        p_lay = QHBoxLayout(pair_row)
        p_lay.setContentsMargins(0, 4, 0, 4)
        p_lay.setSpacing(10)

        # 1. Селектор исходного языка (С какого читаем)
        v_src = QVBoxLayout()
        v_src.setSpacing(4)
        lbl_src = QLabel("Исходный язык:")
        lbl_src.setObjectName("Hint")
        self.combo_src_lang = QComboBox()
        self.combo_src_lang.setObjectName("LangCombo")
        self.combo_src_lang.setMinimumWidth(180)
        self.combo_src_lang.setMaxVisibleItems(14)

        # ФИКС МЕРЦАНИЯ И ПРОСВЕЧИВАНИЯ
        src_view = QListView()
        src_view.setUniformItemSizes(True)
        self.combo_src_lang.setView(src_view)
        if self.combo_src_lang.view().window():
            self.combo_src_lang.view().window().setWindowFlags(
                Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
            )
        self.combo_src_lang.wheelEvent = lambda event: event.ignore()
        v_src.addWidget(lbl_src)
        v_src.addWidget(self.combo_src_lang)

        # 2. Кнопка быстрой смены мест (⇄)
        self.btn_swap_langs = QPushButton("⇄")
        self.btn_swap_langs.setObjectName("Ghost")
        self.btn_swap_langs.setToolTip("Поменять языки местами")
        self.btn_swap_langs.setFixedSize(36, 32)
        self.btn_swap_langs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_swap_langs.clicked.connect(self._on_swap_langs_clicked)

        # 3. Селектор целевого языка (На какой переводим)
        v_dst = QVBoxLayout()
        v_dst.setSpacing(4)
        lbl_dst = QLabel("Целевой язык:")
        lbl_dst.setObjectName("Hint")
        self.combo_dst_lang = QComboBox()
        self.combo_dst_lang.setObjectName("LangCombo")
        self.combo_dst_lang.setMinimumWidth(180)
        self.combo_dst_lang.setMaxVisibleItems(14)

        # ФИКС МЕРЦАНИЯ И ПРОСВЕЧИВАНИЯ
        dst_view = QListView()
        dst_view.setUniformItemSizes(True)
        self.combo_dst_lang.setView(dst_view)
        if self.combo_dst_lang.view().window():
            self.combo_dst_lang.view().window().setWindowFlags(
                Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint
            )
        self.combo_dst_lang.wheelEvent = lambda event: event.ignore()
        v_dst.addWidget(lbl_dst)
        v_dst.addWidget(self.combo_dst_lang)

        # Заполняем оба списка 12 языками
        for code, info in LANGUAGES.items():
            item_text = f"[{code.upper()}]  {info['name']}"
            self.combo_src_lang.addItem(item_text, code)
            self.combo_dst_lang.addItem(item_text, code)

        p_lay.addLayout(v_src, 1)
        p_lay.addWidget(self.btn_swap_langs, 0, Qt.AlignmentFlag.AlignBottom)
        p_lay.addLayout(v_dst, 1)
        c_lang.addWidget(pair_row)

        # ========================================================
        # Баннер отсутствия языкового пакета Windows OCR (Внутри карточки языков)
        # ========================================================
        self.ocr_lang_warn_frame = QFrame()
        self.ocr_lang_warn_frame.setObjectName("OcrWarnFrame")
        self.ocr_lang_warn_frame.setStyleSheet("""
            QFrame#OcrWarnFrame {
                background-color: #24201c;
                border: 1px solid #e08e45;
                border-radius: 9px;
            }
            QPushButton#OcrPrimaryBtn {
                background-color: #e08e45;
                color: #1a1816;
                font-weight: 600;
                font-size: 12px;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton#OcrPrimaryBtn:hover {
                background-color: #f59e0b;
            }
            QPushButton#OcrSecondaryBtn {
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 6px;
                color: #f2ede4;
                font-size: 12px;
                padding: 6px 12px;
            }
            QPushButton#OcrSecondaryBtn:hover {
                background: rgba(224, 142, 69, 0.15);
                border-color: #e08e45;
            }
        """)

        warn_v = QVBoxLayout(self.ocr_lang_warn_frame)
        warn_v.setContentsMargins(14, 12, 14, 12)
        warn_v.setSpacing(6)

        # 1. Заголовок предупреждения
        self.lbl_ocr_warn_title = QLabel("⚠️ Пакет Windows OCR не установлен")
        self.lbl_ocr_warn_title.setStyleSheet("font-weight: 600; color: #e08e45; font-size: 13px;")
        warn_v.addWidget(self.lbl_ocr_warn_title)

        # 2. Описание проблемы
        self.lbl_ocr_warn_text = QLabel()
        self.lbl_ocr_warn_text.setObjectName("Hint")
        self.lbl_ocr_warn_text.setWordWrap(True)
        self.lbl_ocr_warn_text.setStyleSheet("color: #d6cfc7; font-size: 12px; line-height: 1.4;")
        warn_v.addWidget(self.lbl_ocr_warn_text)

        # 3. Кнопки действий
        warn_btn_row = QHBoxLayout()
        warn_btn_row.setContentsMargins(0, 4, 0, 0)
        warn_btn_row.setSpacing(8)

        btn_open_win_settings = QPushButton("Открыть параметры Windows")
        btn_open_win_settings.setObjectName("OcrPrimaryBtn")
        btn_open_win_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_win_settings.clicked.connect(self._open_windows_language_settings)

        btn_copy_ps_cmd = QPushButton("Скопировать команду PowerShell")
        btn_copy_ps_cmd.setObjectName("OcrSecondaryBtn")
        btn_copy_ps_cmd.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_copy_ps_cmd.clicked.connect(self._copy_powershell_ocr_cmd)

        warn_btn_row.addWidget(btn_open_win_settings)
        warn_btn_row.addWidget(btn_copy_ps_cmd)
        warn_btn_row.addStretch(1)
        warn_v.addLayout(warn_btn_row)

        self.ocr_lang_warn_frame.hide()
        c_lang.addWidget(self.ocr_lang_warn_frame)

        v.addWidget(card_lang)

        self.combo_src_lang.currentIndexChanged.connect(self._on_lang_combo_changed)
        self.combo_dst_lang.currentIndexChanged.connect(self._on_lang_combo_changed)

        # ========================================================
        # Карточка 1: Движок перевода
        # ========================================================
        card, cv = self._card("Движок перевода")
        self.translator_seg = SegmentedControl(TRANSLATORS)
        cv.addWidget(self.translator_seg)

        self.translator_hint = QLabel()
        self.translator_hint.setObjectName("Hint")
        self.translator_hint.setWordWrap(True)
        cv.addWidget(self.translator_hint)

        # Виджет монитора VRAM (над комбобоксом и под описанием движка)
        self.vram_widget = QWidget()
        self.vram_widget.setObjectName("Row")
        vram_lay = QVBoxLayout(self.vram_widget)
        vram_lay.setContentsMargins(0, 6, 0, 6)
        vram_lay.setSpacing(4)

        vram_header = QHBoxLayout()
        vram_header.setContentsMargins(0, 0, 0, 0)
        self.lbl_vram_text = QLabel("Видеопамять (VRAM): —")
        self.lbl_vram_text.setObjectName("Hint")
        self.lbl_vram_text.setStyleSheet("font-size: 11px; color: #9c9388;")
        vram_header.addWidget(self.lbl_vram_text)
        vram_header.addStretch(1)

        self.vram_bar = BusyBar()
        self.vram_bar.setFixedHeight(5)

        vram_lay.addLayout(vram_header)
        vram_lay.addWidget(self.vram_bar)
        cv.addWidget(self.vram_widget)

        # Таймер опроса VRAM каждые 3 секунды (работает ТОЛЬКО при открытых настройках)
        self._vram_timer = QTimer(self)
        self._vram_timer.setInterval(3000)
        self._vram_timer.timeout.connect(self._update_vram_monitor)

        # 1. Управление моделями Opus / NLLB (Статус + Кнопки)
        self.model_ctrl_row = QWidget()
        self.model_ctrl_row.setObjectName("Row")
        mch = QHBoxLayout(self.model_ctrl_row)
        mch.setContentsMargins(0, 4, 0, 0)
        mch.setSpacing(10)

        self.model_pill = StatusPill()
        self.model_pill.set_state("off", "Локальная модель не загружена")
        mch.addWidget(self.model_pill, 1)

        self.btn_download_model = QPushButton("⬇ Скачать модель")
        self.btn_download_model.setObjectName("Ghost")
        self.btn_download_model.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_download_model.clicked.connect(self._on_download_clicked)
        self.btn_download_model.hide()

        self.btn_delete_model = QPushButton("Удалить")
        self.btn_delete_model.setObjectName("Danger")
        self.btn_delete_model.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_delete_model.clicked.connect(self._on_delete_model_clicked)
        self.btn_delete_model.hide()

        mch.addWidget(self.btn_download_model)
        mch.addWidget(self.btn_delete_model)
        cv.addWidget(self.model_ctrl_row)

        self.model_bar = BusyBar()
        self.model_bar.hide()
        cv.addWidget(self.model_bar)

        self.model_hint = QLabel("")
        self.model_hint.setObjectName("Hint")
        self.model_hint.setWordWrap(True)
        self.model_hint.hide()
        cv.addWidget(self.model_hint)

        # --- Блок управления движком llama.cpp ---
        self.runtime_ctrl_row = QWidget()
        self.runtime_ctrl_row.setObjectName("Row")
        r_lay = QHBoxLayout(self.runtime_ctrl_row)
        r_lay.setContentsMargins(0, 6, 0, 0)
        r_lay.setSpacing(10)

        self.runtime_pill = StatusPill()
        r_lay.addWidget(self.runtime_pill, 1)

        self.btn_install_runtime = QPushButton("Установить движок для LLM")
        self.btn_install_runtime.setObjectName("Ghost")
        self.btn_install_runtime.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_install_runtime.clicked.connect(self._show_runtime_menu)
        r_lay.addWidget(self.btn_install_runtime)

        cv.addWidget(self.runtime_ctrl_row)

        self.runtime_bar = BusyBar()
        self.runtime_bar.hide()
        cv.addWidget(self.runtime_bar)

        self.runtime_hint = QLabel("")
        self.runtime_hint.setObjectName("Hint")
        self.runtime_hint.setWordWrap(True)
        self.runtime_hint.hide()
        cv.addWidget(self.runtime_hint)
        # ----------------------------------------

        # 2. Выбор GGUF файла (ComboBox + кнопка папки)
        self.gguf_select_row = QWidget()
        self.gguf_select_row.setObjectName("Row")
        gh = QHBoxLayout(self.gguf_select_row)
        gh.setContentsMargins(0, 6, 0, 0)
        gh.setSpacing(10)

        self.gguf_combo = QComboBox()
        self.gguf_combo.setObjectName("GgufCombo")
        self.gguf_combo.setMinimumWidth(220)
        self.gguf_combo.wheelEvent = lambda event: event.ignore()
        self.gguf_combo.currentIndexChanged.connect(self._on_gguf_combo_changed)

        self.btn_open_models_dir = QPushButton("📂 Папка models")
        self.btn_open_models_dir.setObjectName("Ghost")
        self.btn_open_models_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open_models_dir.clicked.connect(self._open_models_folder)

        gh.addWidget(QLabel("Файл модели:"))
        gh.addWidget(self.gguf_combo, 1)
        gh.addWidget(self.btn_open_models_dir)
        cv.addWidget(self.gguf_select_row)

        # 3. Каталог проверенных GGUF-моделей
        self.catalog_frame = QFrame()
        self.catalog_frame.setObjectName("Card")
        cat_v = QVBoxLayout(self.catalog_frame)
        cat_v.setContentsMargins(12, 10, 12, 12)
        cat_v.setSpacing(8)

        lbl_cat_title = QLabel("Каталог проверенных моделей GGUF")
        lbl_cat_title.setStyleSheet("font-weight: 600; color: #e08e45; font-size: 12px;")
        cat_v.addWidget(lbl_cat_title)

        self.catalog_rows = {}
        from backend.llama_server import GGUF_CATALOG
        for item in GGUF_CATALOG:
            crow = self._build_catalog_item_row(item)
            cat_v.addWidget(crow)
            self.catalog_rows[item["filename"]] = crow

        cv.addWidget(self.catalog_frame)

        # 4. Общий кэш на диске
        cache_row = QWidget()
        cache_row.setObjectName("Row")
        ch = QHBoxLayout(cache_row)
        ch.setContentsMargins(0, 8, 0, 0)
        ch.setSpacing(10)

        self.lbl_cache_total = QLabel("Локальные модели на диске: 0 МБ")
        self.lbl_cache_total.setObjectName("Hint")
        self.lbl_cache_total.setToolTip(
            "В Windows кэш может дублировать файлы и накапливать старые ревизии весов.\n"
            "Кнопка «Очистить весь кэш» позволяет легко сбросить все накопленные дубликаты."
        )
        ch.addWidget(self.lbl_cache_total, 1)

        self.btn_clear_all = QPushButton("Очистить весь кэш")
        self.btn_clear_all.setObjectName("Ghost")
        self.btn_clear_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clear_all.clicked.connect(self._on_clear_all_cache_clicked)
        ch.addWidget(self.btn_clear_all)

        cv.addWidget(cache_row)

        self.translator_seg.valueChanged.connect(self._on_translator_changed)
        v.addWidget(card)

        # ========================================================
        # Карточка 2: Распознавание текста (OCR)
        # ========================================================
        card_ocr, c_ocr = self._card("Распознавание текста (OCR)")
        self.ocr_seg = SegmentedControl(OCR_ENGINES)
        c_ocr.addWidget(self.ocr_seg)

        self.ocr_hint = QLabel()
        self.ocr_hint.setObjectName("Hint")
        self.ocr_hint.setWordWrap(True)
        c_ocr.addWidget(self.ocr_hint)

        self.dir_seg = SegmentedControl(OCR_DIRECTIONS, vertical=True)
        self.dir_seg.setMinimumWidth(210)
        self.dir_row = self._option_row(
            "Направление текста",
            self.dir_seg,
            "Режим Tategaki (сверху-вниз, справа-налево) для японских новелл и манги."
        )
        c_ocr.addWidget(self.dir_row)

        self.ocr_pill = StatusPill()
        c_ocr.addWidget(self.ocr_pill)

        self.ocr_seg.valueChanged.connect(self._on_ocr_changed)
        self.dir_seg.valueChanged.connect(self._on_dir_changed)
        v.addWidget(card_ocr)

        # ========================================================
        # Карточка 3: Производительность
        # ========================================================
        card2, c2 = self._card("Производительность")
        self.gpu_toggle = ToggleSwitch()
        c2.addWidget(self._option_row(
            "Ускорение на GPU (NVIDIA CUDA)",
            self.gpu_toggle,
            "Переключение перезапускает OCR-движок. Требуется CUDA."))
        
        self.gpu_pill = StatusPill()
        c2.addWidget(self.gpu_pill)

        self.gpu_bar = BusyBar()
        self.gpu_bar.hide()
        c2.addWidget(self.gpu_bar)
        self.gpu_toggle.toggled.connect(self._on_gpu_toggled)
        v.addWidget(card2)

        v.addStretch(1)
        return page

    def _update_cache_display(self):
        from backend.llama_server import GGUF_CATALOG, get_installed_models
        from backend.translators import (
            _MODEL_SPECS,
            ENGINE_QWEN,
            LOCAL_ENGINES,
            format_size,
            get_total_cache_size,
            is_model_cached,
        )

        engine = self.settings.get("translator", "google")
        total_size = get_total_cache_size()
        self.lbl_cache_total.setText(f"Локальные модели на диске: {format_size(total_size)}")
        self.btn_clear_all.setEnabled(total_size > 0)

        # Режим Qwen / GGUF
        if engine == ENGINE_QWEN:
            self.model_ctrl_row.hide()
            self.runtime_ctrl_row.show()
            self.gguf_select_row.show()
            self.catalog_frame.show()

            # Обновляем статус рантайма
            installed_backend = get_installed_backend()
            if installed_backend and installed_backend in BACKENDS_CONFIG:
                b_name = BACKENDS_CONFIG[installed_backend]["title"].split("(")[0].strip()
                self.runtime_pill.set_state("ok", f"Движок llama.cpp: {b_name}")
                self.btn_install_runtime.setText("Сменить движок")
            else:
                self.runtime_pill.set_state("off", "Движок llama.cpp: не установлен")
                self.btn_install_runtime.setText("Установить движок")

            installed = get_installed_models()
            installed_names = [m["filename"] for m in installed]

            # 1. Обновляем элементы комбобокса ТОЛЬКО если список файлов на диске реально изменился:
            current_items = [self.gguf_combo.itemText(i) for i in range(self.gguf_combo.count())]
            if current_items != installed_names:
                self.gguf_combo.blockSignals(True)
                self.gguf_combo.clear()
                for m in installed:
                    self.gguf_combo.addItem(m["filename"])
                self.gguf_combo.blockSignals(False)

            # 2. Ищем сохранённую модель без чувствительности к регистру букв:
            cur_selected = str(self.settings.get("selected_gguf", "")).strip()
            match_idx = -1
            for i in range(self.gguf_combo.count()):
                if self.gguf_combo.itemText(i).lower() == cur_selected.lower():
                    match_idx = i
                    break

            self.gguf_combo.blockSignals(True)
            if match_idx >= 0:
                self.gguf_combo.setCurrentIndex(match_idx)
            elif self.gguf_combo.count() > 0:
                self.gguf_combo.setCurrentIndex(0)
                self.settings.set("selected_gguf", self.gguf_combo.currentText())
            self.gguf_combo.blockSignals(False)

            # 3. Обновляем кнопки каталога (Скачать / Удалить)
            for item in GGUF_CATALOG:
                fn = item["filename"]
                row = self.catalog_rows.get(fn)
                if row:
                    # Проверяем наличие файла без учета регистра
                    is_inst = any(fn.lower() == name.lower() for name in installed_names)
                    row._btn_dl.setVisible(not is_inst)
                    row._btn_del.setVisible(is_inst)

            return

        # Стандартный режим Opus / NLLB
        self.runtime_ctrl_row.hide() 
        self.runtime_bar.hide()      
        self.runtime_hint.hide()     
        self.gguf_select_row.hide()
        self.catalog_frame.hide()

        if engine not in LOCAL_ENGINES:
            self.model_ctrl_row.hide()
            self.btn_download_model.hide()
            self.btn_delete_model.hide()
            return

        self.model_ctrl_row.show()
        is_cached, size = is_model_cached(engine)
        spec = _MODEL_SPECS.get(engine, {})
        approx = spec.get("approx_size", "")

        if is_cached:
            self.model_pill.set_state("ok", f"Скачана и готова ({format_size(size)})")
            self.btn_download_model.hide()
            self.btn_delete_model.show()
        else:
            self.model_pill.set_state("off", f"Не скачана ({approx})")
            self.btn_download_model.setText(f"⬇ Скачать ({approx})")
            self.btn_download_model.show()
            self.btn_delete_model.hide()

    def _on_download_clicked(self):
        engine = self.settings.get("translator", "opus")
        self.btn_download_model.setEnabled(False)
        self.download_model_requested.emit(engine)

    def _on_delete_model_clicked(self):
        from backend.translators import ENGINE_LABELS, format_size, is_model_cached
        engine = self.settings.get("translator", "opus")
        _, size = is_model_cached(engine)
        title = ENGINE_LABELS.get(engine, engine)

        ans = QMessageBox.question(
            self,
            "Удаление модели",
            f"Удалить локальную модель {title} ({format_size(size)}) с диска?\n"
            f"При следующем использовании её потребуется скачать заново.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.delete_model_requested.emit(engine)

    def _on_clear_all_cache_clicked(self):
        from backend.translators import format_size, get_total_cache_size
        total = get_total_cache_size()
        ans = QMessageBox.question(
            self,
            "Очистка кэша",
            f"Удалить все скачанные локальные модели ({format_size(total)})?\n"
            f"Кэш будет полностью очищен.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.clear_all_cache_requested.emit()

    def model_finished(self, state, message):
        self.model_bar.hide()
        self.model_hint.hide()
        self.btn_download_model.setEnabled(True)
        self.model_pill.set_state(state, message)
        self._update_cache_display()

    def _on_translator_changed(self, ident):
        self.translator_hint.setText(TRANSLATOR_HINTS.get(ident, ""))
        self.settings.set("translator", ident)
        self._update_lang_lock_state()
        self._saved_timer.start()
        # TODO(этап 2): контроллер подписан на settings.changed('translator')
        # и в QThread загрузит/выгрузит локальную модель (с прогрессом в gpu_bar).

    def _on_ocr_changed(self, ident):
        self.ocr_hint.setText(OCR_HINTS.get(ident, ""))
        self.settings.set("ocr_engine", ident)
        # Показываем Tategaki только для RapidOCR:
        self.dir_row.setVisible(ident == "rapidocr")
        self._update_ocr_lang_warning()
        self._saved_timer.start()

    def on_active_ocr_changed(self, engine_id: str):
        """Реагирует на фактическую смену движка под капотом (например, при авто-откате)."""
        self.dir_row.setVisible(engine_id == "rapidocr")

    def _on_dir_changed(self, ident):
        self.settings.set("ocr_direction", ident)
        self._saved_timer.start()

    def _on_gpu_toggled(self, checked):
        self.settings.set("gpu", bool(checked))
        self.gpu_pill.set_state(
            "ok" if checked else "off",
            "GPU: ускорение активно" if checked else "CPU: стандартный режим"
        )
        self._saved_timer.start()
        # Если активен EasyOCR — отправляем запрос воркеру:
        if self.settings.get("ocr_engine") == "easyocr":
            self.ocr.request_gpu(bool(checked))

    def gpu_apply_finished(self, success: bool, is_gpu: bool, message: str = ""):
        """Шов для бэкенда: OCR-воркер закончил переключение."""
        self.gpu_toggle.setEnabled(self._gpu_available)
        self.gpu_bar.hide()
        self.gpu_toggle.blockSignals(True)
        self.gpu_toggle.setChecked(bool(is_gpu))
        self.gpu_toggle.blockSignals(False)
        if success and is_gpu:
            self.gpu_pill.set_state("ok", "GPU: ускорение активно")
        elif success:
            self.gpu_pill.set_state("off", "CPU: стандартный режим")
        else:
            self.gpu_pill.set_state("error", f"Ошибка: {message}")
            
        # ---------------- статус локальной модели (швы для контроллера) ----------------
    def model_load_started(self):
        self.model_pill.set_state("busy", "Загрузка локальной модели…")
        self.model_hint.show()
        self.model_hint.setText("Подготовка…")
        self.model_bar.show()
        self.model_bar.start_indeterminate()

    def model_progress(self, percent, label):
        if not self.model_bar.isVisible():
            self.model_bar.show()
            self.model_hint.show()
        if percent < 0:
            self.model_bar.start_indeterminate()
        else:
            self.model_bar.set_value(percent)
        self.model_hint.setText(label)

    def _show_runtime_menu(self):
        """Умное меню выбора бэкенда с проверкой железа и отметкой активного."""
        menu = QMenu(self)
        best = detect_best_backend()
        best_title = BACKENDS_CONFIG.get(best, {}).get("title", best).split("(")[0].strip()
        current_backend = get_installed_backend()

        # 1. Автоопределение
        act_auto = menu.addAction(f"Авто: {best_title} (Рекомендуется)")
        act_auto.triggered.connect(lambda: self.install_runtime_requested.emit(best))
        menu.addSeparator()

        # 2. Список доступных бэкендов
        for b_id, cfg in BACKENDS_CONFIG.items():
            supported, reason = is_backend_supported(b_id)
            title = cfg["title"]
            is_active = (b_id == current_backend)

            # Формируем читаемую подпись
            if is_active:
                label = f"✓ {title} (Установлен)"
            elif not supported:
                label = f"  {title} (Недоступно: {reason})"
            else:
                label = f"  {title}"

            act = menu.addAction(label)

            if not supported:
                # Если железо не поддерживает — выключаем пункт и вешаем подсказку
                act.setEnabled(False)
                act.setToolTip(f"Невозможно запустить: {reason}")
            else:
                act.triggered.connect(lambda _=False, b=b_id: self.install_runtime_requested.emit(b))

        self.btn_install_runtime.setEnabled(True)

        # Выравнивание строго под кнопкой по правому краю
        menu_width = menu.sizeHint().width()
        btn_pos = self.btn_install_runtime.mapToGlobal(QPoint(0, 0))
        x = btn_pos.x() + self.btn_install_runtime.width() - menu_width
        y = btn_pos.y() + self.btn_install_runtime.height() + 4

        menu.exec(QPoint(x, y))

    def runtime_load_started(self):
        self.runtime_pill.set_state("busy", "Установка движка…")
        self.btn_install_runtime.setEnabled(False)
        self.runtime_hint.show()
        self.runtime_hint.setText("Подготовка…")
        self.runtime_bar.show()
        self.runtime_bar.start_indeterminate()

    def runtime_progress(self, percent: int, label: str):
        if not self.runtime_bar.isVisible():
            self.runtime_bar.show()
            self.runtime_hint.show()
        if percent < 0:
            self.runtime_bar.start_indeterminate()
        else:
            self.runtime_bar.set_value(percent)
        self.runtime_hint.setText(label)

    def runtime_finished(self, backend_id: str):
        self.runtime_bar.hide()
        self.runtime_hint.hide()
        self.btn_install_runtime.setEnabled(True)
        self._update_cache_display()

    def runtime_failed(self, backend_id: str, error: str):
        self.runtime_bar.hide()
        self.runtime_hint.setText(f"Ошибка: {error}")
        self.runtime_hint.show()
        self.btn_install_runtime.setEnabled(True)
        self.runtime_pill.set_state("error", "Сбой установки")

    def model_loaded(self, engine_id):
        self.model_finished("ok", f"Модель готова: {ENGINE_LABELS.get(engine_id, engine_id)}")

    def model_failed(self, engine_id, error):
        self.model_finished("error", f"Ошибка загрузки модели: {error}")

    def set_gpu_available(self, available: bool):
        """Вызывается, когда OCR-воркер проверил наличие CUDA."""
        self._gpu_available = bool(available)
        if available:
            self.gpu_toggle.setEnabled(True)
            self.gpu_toggle.setToolTip("")
            return
        self.gpu_toggle.blockSignals(True)
        self.gpu_toggle.setChecked(False)
        self.gpu_toggle.setEnabled(False)
        self.gpu_toggle.blockSignals(False)
        self.gpu_toggle.setToolTip("CUDA не обнаружена — доступен только CPU")

    # ---------------- страница: Горячие клавиши ----------------
    def _page_hotkeys(self):
        page, v = self._page()
        card, cv = self._card("Горячие клавиши")

        hint = QLabel("Кликните по кнопке и нажмите новое сочетание. Esc — отмена. "
                      "Нужен Ctrl или Alt (либо F-клавиша).")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        cv.addWidget(hint)

        self.recorders = {}
        for action, title in self.HOTKEY_ACTIONS:
            rec = HotkeyRecorder()
            rec.sequenceCaptured.connect(
                lambda label, mods, vk, a=action: self._apply_hotkey(a, label, mods, vk))
            self.recorders[action] = rec
            cv.addWidget(self._option_row(title, rec))

        reset_row = QWidget()
        reset_row.setObjectName("Row")
        rh = QHBoxLayout(reset_row)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addStretch(1)
        btn_reset = QPushButton("Вернуть стандартные")
        btn_reset.setObjectName("Ghost")
        btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_reset.clicked.connect(self._reset_hotkeys)
        rh.addWidget(btn_reset)
        cv.addWidget(reset_row)

        v.addWidget(card)
        v.addStretch(1)
        return page

    def _apply_hotkey(self, action, label, mods, vk):
        rec = self.recorders[action]
        old = dict(self.settings.get("hotkeys")[action])

        conflict = next(
            (a for a, hk in self.settings.get("hotkeys").items()
             if a != action and hk["mods"] == mods and hk["vk"] == vk),
            None)
        if conflict:
            rec.set_display(old["label"])
            rec.show_error(f"Уже занято: «{self._action_titles.get(conflict, conflict)}»")
            return

        self.hotkeys.unregister(action)
        if self.hotkeys.register(action, mods, vk):
            self.settings.set_hotkey(action, label, mods, vk)
            self._saved_timer.start()
        else:
            self.hotkeys.register(action, old["mods"], old["vk"])
            rec.set_display(old["label"])
            rec.show_error("Сочетание занято другой программой")

    def _reset_hotkeys(self):
        self.hotkeys.shutdown()
        self.settings.reset_defaults(["hotkeys"])   # changed -> обновит рекордеры
        report = self.hotkeys.apply_all(self.settings.get("hotkeys"))
        if not all(report.values()):
            self.toast.show_toast("Часть стандартных сочетаний занята другими программами")
        else:
            self._saved_timer.start()

    def _page_advanced(self):
        page, v = self._page()

        # ========================================================
        # Карточка 1: Процессор и нейросети
        # ========================================================
        card_cpu, c_cpu = self._card("Процессор и нейросети")

        import os
        total_cores = os.cpu_count() or 4
        self._auto_threads = max(1, total_cores // 2)

        # 1. Заголовок
        lbl_threads = QLabel("Потоки процессора (CPU Threads)")
        c_cpu.addWidget(lbl_threads)

        # 2. Полноразмерный слайдер + значение
        s_row = QHBoxLayout()
        s_row.setContentsMargins(0, 0, 0, 0)
        s_row.setSpacing(12)

        self.threads_slider = QSlider(Qt.Orientation.Horizontal)
        self.threads_slider.setRange(0, total_cores)  # 0 = Авто
        self.threads_slider.setSingleStep(1)

        self.threads_val = QLabel("Авто")
        self.threads_val.setObjectName("Hint")
        self.threads_val.setFixedWidth(75)
        self.threads_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        s_row.addWidget(self.threads_slider, 1)
        s_row.addWidget(self.threads_val)
        c_cpu.addLayout(s_row)

        # 3. Развернутое описание под слайдером на всю ширину
        thl = QLabel(
            "Выделенные ядра для нейросети. 'Авто' оставляет половину ядер процессора для системы и игр (левое положение).\n"
            "⚠️ Внимание: изменение значения перезапускает локальный сервер нейросети (занимает 1–3 сек)."
        )
        thl.setObjectName("Hint")
        thl.setWordWrap(True)
        c_cpu.addWidget(thl)
        v.addWidget(card_cpu)

        # ========================================================
        # Карточка 2: Авто-режим захвата
        # ========================================================
        card_auto, c_auto = self._card("Авто-режим захвата")

        # 1. Заголовок
        lbl_delay = QLabel("Задержка распознавания (Дебаунс)")
        c_auto.addWidget(lbl_delay)

        # 2. Полноразмерный слайдер + значение
        d_row = QHBoxLayout()
        d_row.setContentsMargins(0, 0, 0, 0)
        d_row.setSpacing(12)

        self.auto_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self.auto_delay_slider.setRange(200, 2000)
        self.auto_delay_slider.setSingleStep(50)

        self.auto_delay_val = QLabel("800 мс")
        self.auto_delay_val.setObjectName("Hint")
        self.auto_delay_val.setFixedWidth(75)
        self.auto_delay_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        d_row.addWidget(self.auto_delay_slider, 1)
        d_row.addWidget(self.auto_delay_val)
        c_auto.addLayout(d_row)

        # 3. Описание снизу
        dhl = QLabel("Пауза для стабилизации текста перед отправкой для перевода (200–2000 мс).")
        dhl.setObjectName("Hint")
        dhl.setWordWrap(True)
        c_auto.addWidget(dhl)
        v.addWidget(card_auto)

        # ========================================================
        # Карточка 3: Диагностика и логирование
        # ========================================================
        card_diag, c_diag = self._card("Диагностика")
        self.verbose_toggle = ToggleSwitch()
        c_diag.addWidget(self._option_row(
            "Подробный лог (для отладки)",
            self.verbose_toggle,
            "В app.log пишется полный текст OCR и переводов без обрезки."
            "Включайте только при поиске ошибок."))
        v.addWidget(card_diag)

        # Подключаем сигналы
        # Создаем таймер отложенного применения (debounce) для потоков CPU
        self._threads_timer = QTimer(self)
        self._threads_timer.setSingleShot(True)
        self._threads_timer.setInterval(1000)  # Ждем 1 сек после остановки слайдера для кол-ва ядер CPU
        self._threads_timer.timeout.connect(self._apply_threads_setting)

        # Текст на слайдере меняем на лету, а сервер перезапускаем ТОЛЬКО при отпускании мыши
        self.threads_slider.valueChanged.connect(self._on_threads_slider_changed)
        self.threads_slider.sliderReleased.connect(self._apply_threads_setting)
        self.auto_delay_slider.valueChanged.connect(self._on_auto_delay_slider)
        self.verbose_toggle.toggled.connect(self._on_verbose_toggled)

        v.addStretch(1)
        return page

    def _update_threads_display(self, value):
        """Мгновенно обновляет подпись рядом со слайдером без лагов."""
        if value == 0:
            self.threads_val.setText(f"Авто ({getattr(self, '_auto_threads', 4)})")
        else:
            self.threads_val.setText(f"{value} яд.")

    def _on_threads_slider_changed(self, value):
        """Срабатывает при движении: обновляет текст и взводит таймер."""
        self._update_threads_display(value)
        # Если юзер еще двигает ползунок — перезапуск откладывается
        self._threads_timer.start()

    def _apply_threads_setting(self):
        """Применяет настройку ТОЛЬКО когда юзер остановился или отпустил мышь."""
        self._threads_timer.stop()
        new_val = self.threads_slider.value()
        current_saved = self.settings.get("cpu_threads", 0)

        # ЗАЩИТА: если покрутил туда-обратно и вернул то же значение — сервер НЕ трогаем!
        if new_val == current_saved:
            return

        self.settings.set("cpu_threads", new_val)
        self._saved_timer.start()

    # ---------------- страница: О программе ----------------
    def _page_about(self):
        page, v = self._page()
        card, cv = self._card("О программе")

        cv.addWidget(QLabel(f"ScreenTale {APP_VERSION}"))
        cv.addWidget(QLabel(f"Python {os.sys.version.split()[0]} · PySide6 · Qt {qVersion()}"))

        btns = QWidget()
        btns.setObjectName("Row")
        bh = QHBoxLayout(btns)
        bh.setContentsMargins(0, 0, 0, 0)
        b_folder = QPushButton("Открыть папку приложения")
        b_folder.setObjectName("Ghost")
        b_folder.clicked.connect(lambda: os.startfile(get_app_dir()))
        b_sysinfo = QPushButton("Скопировать данные о системе")
        b_sysinfo.setObjectName("Ghost")
        b_sysinfo.clicked.connect(self._copy_sysinfo)
        bh.addWidget(b_folder)
        bh.addWidget(b_sysinfo)
        bh.addStretch(1)
        cv.addWidget(btns)

        # 🥚 Пасхалка: карточка благодарности тестировщику
        card_thanks, cv_thanks = self._card("Особая благодарность")
        thanks = QLabel(
            "Главному тестировщику — за выдержку, мужество и страдания в версии 0.4."
        )
        thanks.setObjectName("Hint")
        thanks.setWordWrap(True)
        cv_thanks.addWidget(thanks)

        v.addWidget(card)
        v.addWidget(card_thanks)
        v.addStretch(1)
        return page

    def _copy_sysinfo(self):
        from PySide6 import __version__ as pyside_ver
        QApplication.instance().clipboard().setText(
            f"ScreenTale {APP_VERSION}\n"
            f"Python: {os.sys.version.split()[0]}\n"
            f"PySide6: {pyside_ver}\nQt: {qVersion()}")
        self.toast.show_toast("Скопировано в буфер обмена")

    def _on_outline_toggled(self, checked):
        self.settings.set("text_outline", bool(checked))
        self._update_preview_outline(bool(checked))
        self._saved_timer.start()

    def _update_preview_outline(self, enabled: bool):
        """Интерактивно обновляет контур в блоке Preview настроек."""
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        if enabled:
            shadow = QGraphicsDropShadowEffect(self.preview_label)
            shadow.setBlurRadius(3)
            shadow.setColor(QColor(0, 0, 0, 240))
            shadow.setOffset(0, 0)
            self.preview_label.setGraphicsEffect(shadow)
        else:
            self.preview_label.setGraphicsEffect(None)

# ---------------- реакция на settings.changed ----------------
    def _sync_from_settings(self):
        self.theme_seg.set_value(self.settings.get("theme", "dark"))
        self._on_setting_changed("font_size", self.settings.get("font_size"))
        self._on_setting_changed("opacity", self.settings.get("opacity"))
        self._on_setting_changed("auto_copy", self.settings.get("auto_copy"))
        self._on_setting_changed("translator", self.settings.get("translator"))
        self._on_setting_changed("hotkeys", self.settings.get("hotkeys"))
        self._on_setting_changed("verbose_log", self.settings.get("verbose_log"))
        self._on_setting_changed("auto_delay_ms", self.settings.get("auto_delay_ms", 800))
        is_gpu = bool(self.settings.get("gpu"))
        self.gpu_toggle.blockSignals(True)
        self.gpu_toggle.setChecked(is_gpu)
        self.gpu_toggle.blockSignals(False)
        self.gpu_pill.set_state(
            "ok" if is_gpu else "off",
            "GPU: ускорение активно" if is_gpu else "CPU: стандартный режим",
        )
        self._update_cache_display()
        self._on_setting_changed("ocr_engine", self.settings.get("ocr_engine", "windows"))
        self._on_setting_changed("ocr_direction", self.settings.get("ocr_direction", "horizontal"))
        current_ocr = self.settings.get("ocr_engine", "windows")
        self._on_setting_changed("ocr_engine", current_ocr)
        self.dir_row.setVisible(current_ocr == "rapidocr")
        self._on_setting_changed("src_lang", self.settings.get("src_lang", "en"))
        self._on_setting_changed("dst_lang", self.settings.get("dst_lang", "ru"))
        self._on_setting_changed("cpu_threads", self.settings.get("cpu_threads", 0))
        self._update_lang_lock_state()
        self._update_ocr_lang_warning()
        self._on_setting_changed("text_outline", self.settings.get("text_outline", False))

    def _on_setting_changed(self, key, value):
        if key == "font_size":
            self.font_slider.blockSignals(True)
            self.font_slider.setValue(int(value))
            self.font_val.setText(str(value))
            self.font_slider.blockSignals(False)
            self._update_preview_font(int(value))
        elif key == "opacity":
            pct = round(value * 100)
            self.opacity_slider.blockSignals(True)
            self.opacity_slider.setValue(pct)
            self.opacity_val.setText(f"{pct} %")
            self.opacity_slider.blockSignals(False)
        elif key == "auto_copy":
            self.autocopy_toggle.blockSignals(True)
            self.autocopy_toggle.setChecked(bool(value))
            self.autocopy_toggle.blockSignals(False)
        elif key == "translator":
            self.translator_seg.set_value(value)
            self.translator_hint.setText(TRANSLATOR_HINTS.get(value, ""))
            self._update_cache_display()
        elif key == "hotkeys" or key.startswith("hotkeys."):
            hks = self.settings.get("hotkeys")
            for action, rec in self.recorders.items():
                rec.set_display(hks.get(action, {}).get("label", "—"))
        elif key == "verbose_log":
            self.verbose_toggle.blockSignals(True)
            self.verbose_toggle.setChecked(bool(value))
            self.verbose_toggle.blockSignals(False)
            set_verbose(bool(value))
        elif key == "auto_delay_ms":
            val = int(value)
            self.auto_delay_slider.blockSignals(True)
            self.auto_delay_slider.setValue(val)
            self.auto_delay_val.setText(f"{val} мс")
            self.auto_delay_slider.blockSignals(False)
        elif key == "ocr_engine":
            self._update_ocr_lang_warning()
            self.ocr_seg.set_value(value)
            self.ocr_hint.setText(OCR_HINTS.get(value, ""))
            self.dir_row.setVisible(value == "rapidocr")
        elif key == "ocr_direction":
            self.dir_seg.set_value(value)
        elif key == "src_lang":
            self._update_ocr_lang_warning()
            idx = self.combo_src_lang.findData(value)
            if idx >= 0:
                self.combo_src_lang.blockSignals(True)
                self.combo_src_lang.setCurrentIndex(idx)
                self.combo_src_lang.blockSignals(False)
        elif key == "dst_lang":
            idx = self.combo_dst_lang.findData(value)
            if idx >= 0:
                self.combo_dst_lang.blockSignals(True)
                self.combo_dst_lang.setCurrentIndex(idx)
                self.combo_dst_lang.blockSignals(False)
        elif key == "cpu_threads":
            val = int(value)
            self.threads_slider.blockSignals(True)
            self.threads_slider.setValue(val)
            self._update_threads_display(val)
            self.threads_slider.blockSignals(False)
            if val == 0:
                self.threads_val.setText(f"Авто ({getattr(self, '_auto_threads', 4)})")
            else:
                self.threads_val.setText(f"{val} яд.")
            self.threads_slider.blockSignals(False)
        elif key == "text_outline":
            self.outline_toggle.blockSignals(True)
            self.outline_toggle.setChecked(bool(value))
            self.outline_toggle.blockSignals(False)
            self._update_preview_outline(bool(value))

    # ---------------- служебное ----------------
    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.toast.reposition()

    def closeEvent(self, event):
        if self._hide_on_close:
            event.ignore()
            self.hide()
        else:
            event.accept()

    def _handle_exit(self):
        self.settings.save()
        if self.on_exit:
            self.on_exit()
        else:
            QApplication.instance().quit()

    # ---------------- перетаскивание окна (Drag) ----------------
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        event.accept()