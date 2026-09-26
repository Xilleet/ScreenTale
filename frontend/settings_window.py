import os
import sys

from PySide6.QtCore import Qt, QTimer, Signal, qVersion
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
from backend.logging_setup import set_verbose
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
]

TRANSLATOR_HINTS = {
    "google": "Онлайн-сервис Google. Требуется интернет, качество хорошее.",
    "mymemory": "Онлайн-сервис MyMemory. Есть лимиты запросов, качество среднее.",
    "opus": "Локальная модель Opus-MT (~300 МБ). Только en→ru, работает офлайн.",
    "nllb": "Локальная модель NLLB-200 (~2.5 ГБ). Работает офлайн, качество выше.",
}

OCR_ENGINES = [
    ("windows", "Windows OCR (Быстрый)"),
    ("easyocr", "EasyOCR (PyTorch)"),
]

OCR_HINTS = {
    "windows": "Нативный системный движок Windows 10/11. Молниеносное чтение, 0 МБ VRAM.",
    "easyocr": "Классический движок на PyTorch.",
}

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
            self.progress_bar.set_value(label) # or set_value(percent)
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
    download_model_requested = Signal(str)
    delete_model_requested = Signal(str)
    clear_all_cache_requested = Signal()

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
        self.setFixedSize(760, 580)

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
        for name in ("Общие", "Перевод", "Горячие клавиши", "О программе"):
            self.nav.addItem(QListWidgetItem(name))
        v.addWidget(self.nav, 1)

        btn_exit = QPushButton("Выйти из программы")
        btn_exit.setObjectName("Danger")
        btn_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_exit.clicked.connect(self._handle_exit)
        v.addWidget(btn_exit)
        return frame

    def _build_content(self):
        self.pages = QStackedWidget()
        self.pages.addWidget(self._wrap(self._page_general()))
        self.pages.addWidget(self._wrap(self._page_translation()))
        self.pages.addWidget(self._wrap(self._page_hotkeys()))
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

        card, cv = self._card("Движок перевода")
        self.translator_seg = SegmentedControl(TRANSLATORS)
        cv.addWidget(self.translator_seg)
        self.translator_hint = QLabel()
        self.translator_hint.setObjectName("Hint")
        self.translator_hint.setWordWrap(True)
        cv.addWidget(self.translator_hint)

        # Ряд управления локальной моделью (Статус + кнопки Скачать/Удалить)
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

        # Разделитель и блок общего кэша
        cache_row = QWidget()
        cache_row.setObjectName("Row")
        ch = QHBoxLayout(cache_row)
        ch.setContentsMargins(0, 8, 0, 0)
        ch.setSpacing(10)

        self.lbl_cache_total = QLabel("Локальные модели на диске: 0 МБ")
        self.lbl_cache_total.setObjectName("Hint")
        # Честное пояснение при наведении:
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

# РАЗМЕЩАЕМ МЕЖДУ ПЕРЕВОДОМ И ПРОИЗВОДИТЕЛЬНОСТЬЮ:
        card_ocr, c_ocr = self._card("Распознавание текста (OCR)")
        self.ocr_seg = SegmentedControl(OCR_ENGINES)
        c_ocr.addWidget(self.ocr_seg)

        self.ocr_hint = QLabel()
        self.ocr_hint.setObjectName("Hint")
        self.ocr_hint.setWordWrap(True)
        c_ocr.addWidget(self.ocr_hint)

        self.ocr_pill = StatusPill()
        c_ocr.addWidget(self.ocr_pill)

        self.ocr_seg.valueChanged.connect(self._on_ocr_changed)
        v.addWidget(card_ocr)

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

        # Карточка Авто-режима (из Шага 1)
        card_auto, c_auto = self._card("Авто-режим")
        drow = QWidget()
        drow.setObjectName("Row")
        dh = QHBoxLayout(drow)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.setSpacing(10)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        left.addWidget(QLabel("Задержка распознавания"))
        hl = QLabel("Пауза для стабилизации текста перед отправкой в перевод (200–2000 мс).")
        hl.setObjectName("Hint")
        hl.setWordWrap(True)
        left.addWidget(hl)
        dh.addLayout(left, 1)

        self.auto_delay_slider = QSlider(Qt.Orientation.Horizontal)
        self.auto_delay_slider.setRange(200, 2000)
        self.auto_delay_slider.setSingleStep(50)
        self.auto_delay_slider.setMinimumWidth(180)

        self.auto_delay_val = QLabel("800 мс")
        self.auto_delay_val.setObjectName("Hint")
        self.auto_delay_val.setFixedWidth(54)
        self.auto_delay_val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        dh.addWidget(self.auto_delay_slider)
        dh.addWidget(self.auto_delay_val)
        c_auto.addWidget(drow)
        v.addWidget(card_auto)

        self.auto_delay_slider.valueChanged.connect(self._on_auto_delay_slider)
        v.addStretch(1)
        return page

    def _update_cache_display(self):
        """Обновляет статус выбранной модели и общий размер кэша."""
        from backend.translators import (
            _MODEL_SPECS,
            LOCAL_ENGINES,
            format_size,
            get_total_cache_size,
            is_model_cached,
        )
        # 1. Общий кэш
        total_size = get_total_cache_size()
        self.lbl_cache_total.setText(f"Локальные модели на диске: {format_size(total_size)}")
        self.btn_clear_all.setEnabled(total_size > 0)

        # 2. Статус текущего выбранного движка
        engine = self.settings.get("translator", "google")
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
        self._saved_timer.start()
        # TODO(этап 2): контроллер подписан на settings.changed('translator')
        # и в QThread загрузит/выгрузит локальную модель (с прогрессом в gpu_bar).

    def _on_ocr_changed(self, ident):
        self.ocr_hint.setText(OCR_HINTS.get(ident, ""))
        self.settings.set("ocr_engine", ident)
        self._saved_timer.start()

    def _on_gpu_toggled(self, checked):
        self.gpu_toggle.setEnabled(False)
        self.gpu_bar.start_indeterminate()
        self.gpu_pill.set_state("busy", "Перезапуск OCR-движка…")
        self.settings.set("gpu", bool(checked))
        # Завершение придёт извне: контроллер -> gpu_apply_finished()

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

        card_diag, cv_diag = self._card("Диагностика")
        self.verbose_toggle = ToggleSwitch()
        cv_diag.addWidget(self._option_row(
            "Подробный лог (для отладки)",
            self.verbose_toggle,
            "В app.log пишется полный текст OCR и переводов без обрезки. "
            "Включи, если что-то сломалось — пришли log автору. "
            "Не забудь выключить после отладки."))
        self.verbose_toggle.toggled.connect(self._on_verbose_toggled)

        # 🥚 Пасхалка: карточка благодарности тестировщику
        card_thanks, cv_thanks = self._card("Особая благодарность")
        thanks = QLabel(
            "Главному тестировщику — за выдержку, мужество и страдания в версии 0.4."
        )
        thanks.setObjectName("Hint")
        thanks.setWordWrap(True)
        cv_thanks.addWidget(thanks)

        v.addWidget(card)
        v.addWidget(card_diag)
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
            self.ocr_seg.set_value(value)
            self.ocr_hint.setText(OCR_HINTS.get(value, ""))

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