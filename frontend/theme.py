"""Дизайн-токены и глобальный QSS. Цвета нигде больше не хардкодим."""

PALETTES = {
    # 1. Новая янтарная тема по умолчанию (ScreenTale)
    "dark": {
        "bg":          "#1a1816",  # глубокий теплый угольный
        "bg_sidebar":  "#141211",
        "bg_card":     "#221f1c",
        "bg_hover":    "#2c2824",
        "stroke":      "#3d3731",
        "text":        "#f2ede4",  # цвет пергамента
        "text_dim":    "#9c9388",
        "accent":      "#e08e45",  # теплый янтарь
        "accent_soft": "rgba(224, 142, 69, 0.15)",
        "green":       "#4ade80",
        "yellow":      "#f59e0b",
        "red":         "#ef4444",
        "btn_hover":   "#36312b",
        "preview_bg":  "#121110",
        "toast_bg":    "rgba(34, 31, 28, 240)",
    },
    # 2. Старая проверенная тёмная тема (Холодный графит и индиго)
    "dark_classic": {
        "bg":          "#18191f",  # строгий холодный графит
        "bg_sidebar":  "#131418",  # контрастный темный сайдбар
        "bg_card":     "#22242c",  # матовые графитовые карточки
        "bg_hover":    "#2b2e38",  # мягкий ховер
        "stroke":      "#383b47",  # четкие тонкие границы
        "text":        "#f1f3f7",  # чистый холодный белый текст
        "text_dim":    "#949ba8",  # читаемый вторичный текст
        "accent":      "#7c83f6",  # фирменный индиго-сапфир
        "accent_soft": "rgba(124, 131, 246, 0.15)",
        "green":       "#34d399",
        "yellow":      "#fbbf24",
        "red":         "#f87171",
        "btn_hover":   "#333742",
        "preview_bg":  "#121317",
        "toast_bg":    "rgba(28, 30, 38, 245)",
    },
    # 3. Светлая тема (Высококонтрастная теплая бумага)
    "light": {
        "bg":          "#f7f6f3",  # мягкий цвет светлой бумаги
        "bg_sidebar":  "#eeece6",  # затемненный сайдбар
        "bg_card":     "#ffffff",  # белые карточки
        "bg_hover":    "#e4e1d8",  # заметный ховер
        "stroke":      "#d6d2c4",  # мягкие границы
        "text":        "#1c1917",  # глубокий угольный текст (максимальная читаемость)
        "text_dim":    "#57534e",  # контрастный вторичный текст
        "accent":      "#d97706",  # сочный янтарный акцент
        "accent_soft": "rgba(217, 119, 6, 0.12)",
        "green":       "#15803d",
        "yellow":      "#b45309",
        "red":         "#dc2626",
        "btn_hover":   "#e6e3da",
        "preview_bg":  "#ffffff",
        "toast_bg":    "rgba(255, 255, 255, 250)",
    },
}

# Живой словарь: мутируем на месте, чтобы все импорты видели смену темы.
PALETTE = dict(PALETTES["dark"])
_current = "dark"


def set_palette(name: str):
    global _current
    if name not in PALETTES or name == _current:
        return
    _current = name
    PALETTE.clear()
    PALETTE.update(PALETTES[name])


def current_theme() -> str:
    return _current


_QSS = """
QWidget { background-color: @bg; color: @text; font-family: 'Segoe UI'; font-size: 13px; }
QLabel { background: transparent; }
QLabel#AppTitle { font-size: 15px; font-weight: 600; }
QLabel#Version { color: @text_dim; font-size: 11px; }
QLabel#CardTitle { font-weight: 600; }
QLabel#Hint { color: @text_dim; font-size: 12px; }

QFrame#Sidebar {
    background-color: @bg_sidebar;
    border-right: 1px solid @stroke;
    border-top-left-radius: 11px;
    border-bottom-left-radius: 11px;
}
QFrame#Card { background-color: @bg_card; border: 1px solid @stroke; border-radius: 10px; }
QFrame#Preview { background-color: @preview_bg; border: 1px solid @stroke; border-radius: 10px; }
QWidget#Row { background: transparent; }

QListWidget#Nav { background: transparent; border: none; outline: 0; }
QListWidget#Nav::item { color: @text_dim; padding: 9px 12px; border-radius: 8px; }
QListWidget#Nav::item:hover { background: @bg_hover; color: @text; }
QListWidget#Nav::item:selected { background: @accent_soft; color: @text; }

QSlider::groove:horizontal { height: 5px; background: @bg_hover; border-radius: 2px; }
QSlider::sub-page:horizontal { background: @accent; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 15px; margin: -5px 0; border-radius: 7px; }
QSlider::handle:horizontal:hover { background: #ffffff; border: 1px solid @accent; }

QPushButton { background-color: @bg_hover; border: 1px solid @stroke; border-radius: 8px; padding: 8px 14px; }
QPushButton:hover { background-color: @btn_hover; }
QPushButton:pressed { background-color: @stroke; }
QPushButton:disabled { color: @text_dim; }
QPushButton#Danger { background: transparent; border-color: rgba(229,72,77,0.45); color: @red; }
QPushButton#Danger:hover { background: rgba(229,72,77,0.12); }
QPushButton#Ghost { background: transparent; border-color: transparent; color: @accent; }
QPushButton#Ghost:hover { background: @bg_hover; border-color: @stroke; }
QPushButton#Ghost:pressed { background: @stroke; }
QPushButton#Recorder { background: @bg; font-family: 'Consolas'; font-size: 13px; min-width: 180px; padding: 8px 16px; }
QPushButton#Recorder:hover { border-color: @accent; }
QPushButton#Recorder:checked { border-color: @accent; color: @accent; background: @accent_soft; }
QPushButton#Recorder[error="true"] { border-color: @red; color: @red; }

QFrame#Segmented { background-color: @bg; border: 1px solid @stroke; border-radius: 9px; }
QPushButton#SegBtn { 
    background: transparent; 
    border: none; 
    border-radius: 6px; 
    padding: 6px 10px; 
    color: @text_dim; 
    min-width: 74px; 
}
QPushButton#SegBtn:hover { color: @text; }
QPushButton#SegBtn:checked { background: @bg_hover; color: @text; font-weight: 600; }

QLabel#Toast { background: @toast_bg; border: 1px solid @stroke; border-radius: 8px; padding: 9px 16px; font-size: 12px; }

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: @stroke; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: @text_dim; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: @stroke; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QPushButton#WinBtn {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: @text_dim;
    font-size: 13px;
    font-weight: bold;
    min-width: 36px;
    max-width: 36px;
    min-height: 28px;
    max-height: 28px;
    padding: 0px;
}
QPushButton#WinBtn:hover {
    background-color: @bg_hover;
    color: @text;
}
QPushButton#WinBtnClose {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: @text_dim;
    font-size: 13px;
    font-weight: bold;
    min-width: 36px;
    max-width: 36px;
    min-height: 28px;
    max-height: 28px;
    padding: 0px;
}
QPushButton#WinBtnClose:hover {
    background-color: @red;
    color: #ffffff;
}

QFrame#SettingsContainer {
    background-color: @bg;
    border: 1px solid @stroke;
    border-radius: 12px;
}

QFrame#UpdateBanner {
    background-color: @bg_card;
    border: 1px solid @accent;
    border-radius: 10px;
}
/* Заголовок баннера адаптируется под тему (@text) */
QFrame#UpdateBanner QLabel {
    color: @text;
    font-weight: 600;
    font-size: 13px;
    background: transparent;
}
/* Кнопка «Обновить сейчас» остается фирменной янтарной */
QPushButton#UpdateBtn:hover {
    background-color: #f59e0b;
}
QPushButton#UpdateBtn {
    background-color: @accent;
    color: #1a1816;
    font-weight: 600;
    border: none;
    border-radius: 7px;
    padding: 6px 14px;
}
QPushButton#UpdateBtn:hover {
    background-color: #f59e0b;
}
"""

def build_qss() -> str:
    qss = _QSS
    # длинные ключи первыми, чтобы @bg не перезаписал @bg_sidebar и т.п.
    for key in sorted(PALETTE, key=len, reverse=True):
        qss = qss.replace("@" + key, PALETTE[key])
    return qss


def apply_theme(app, name: str):
    """Сменить тему на лету: палитра + глобальный QSS."""
    set_palette(name)
    app.setStyleSheet(build_qss())
    for w in app.allWidgets():
        w.update()