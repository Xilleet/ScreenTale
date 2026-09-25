"""Настройки приложения: единый источник правды (без виджетов)."""
import copy
import json
import os
import sys

from PySide6.QtCore import QObject, Signal

APP_VERSION = "0.4.5-dev"


def get_app_dir() -> str:
    """Каталог приложения.
    frozen: папка с .exe.
    dev: корень проекта (папка с main.py), а не backend/, где лежит config.py.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # __file__ = <проект>/backend/config.py -> два dirname = корень проекта
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_data_dir() -> str:
    """Папка для всех пользовательских данных (кэш, логи, настройки)."""
    d = os.path.join(get_app_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d

CONFIG_PATH = os.path.join(get_data_dir(), "config.json")

DEFAULTS = {
    "font_size": 14,
    "theme": "dark",     
    "opacity": 0.95,
    "translator": "google",   # google | mymemory | opus | nllb
    "gpu": False,
    "auto_copy": True,
    "auto_delay_ms": 800,
    "verbose_log": False,   # тумблер "Подробный лог" в Настройках → О программе
    "hotkeys": {
        # mods — флаги RegisterHotKey, vk — виртуальный код Windows
        "single":        {"label": "Alt+Q",  "mods": 0x0001, "vk": 0x51},
        "auto":          {"label": "Alt+W",  "mods": 0x0001, "vk": 0x57},
        "toggle_window": {"label": "Ctrl+`", "mods": 0x0002, "vk": 0xC0},
        "stop":          {"label": "Alt+C",  "mods": 0x0001, "vk": 0x43},
        "clear":         {"label": "Alt+X",  "mods": 0x0001, "vk": 0x58},
        "ghost":         {"label": "Alt+G",  "mods": 0x0001, "vk": 0x47},
    },
}

# миграция имён из v0.3.2
_OLD_TRANSLATOR_MAP = {
    "Google": "google",
    "MyMemory": "mymemory",
    "Локально (Быстро - 300МБ)": "opus",
    "Локально (Качественно - 2.5ГБ)": "nllb",
}
_VALID_TRANSLATORS = {"google", "mymemory", "opus", "nllb"}


class SettingsManager(QObject):
    """Любой подписчик (UI, OCR-воркер, движок перевода) реагирует через changed."""

    changed = Signal(str, object)  # (ключ, новое значение)

    def __init__(self):
        super().__init__()
        self._values = copy.deepcopy(DEFAULTS)
        self._first_run = not os.path.exists(CONFIG_PATH)
        self._load()

    @property
    def is_first_run(self) -> bool:                        
        return self._first_run
        
    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value, save=True):
        if self._values.get(key) == value:
            return
        self._values[key] = value
        if save:
            self.save()
        self.changed.emit(key, value)

    def set_hotkey(self, action, label, mods, vk):
        hotkeys = self._values.setdefault("hotkeys", {})
        cur = hotkeys.get(action, {})
        if cur.get("mods") == mods and cur.get("vk") == vk:
            return
        hotkeys[action] = {"label": label, "mods": mods, "vk": vk}
        self.save()
        self.changed.emit(f"hotkeys.{action}", hotkeys[action])

    def reset_defaults(self, keys=None):
        # Всегда эмитим changed, даже если значение совпало — иначе UI не
        # обновится при нажатии "Вернуть стандартные" (set() с тем же значением no-op).
        target_keys = list(keys or DEFAULTS.keys())
        for key in target_keys:
            self._values[key] = copy.deepcopy(DEFAULTS[key])
        self.save()
        for key in target_keys:
            self.changed.emit(key, self._values[key])

    def save(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self._values, f, indent=4, ensure_ascii=False)
        except OSError as _e:
            # Диск защищён от записи / путь утерян / нет места — раньше юзер
            # менял настройки, закрывал прогу и обнаруживал при след. запуске
            # старые значения. Теперь причина видна в app.log.
            print(f"[warn] не удалось сохранить настройки: {_e}")

    def _load(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return

        for key in ("theme", "font_size", "opacity", "gpu", "auto_copy",
                    "auto_delay_ms", "verbose_log"):
            if key in data:
                self._values[key] = data[key]

        tr = data.get("translator")
        if isinstance(tr, str):
            tr = _OLD_TRANSLATOR_MAP.get(tr, tr)
            if tr in _VALID_TRANSLATORS:
                self._values["translator"] = tr

        saved_hotkeys = data.get("hotkeys")
        if isinstance(saved_hotkeys, dict):
            for action, hk in saved_hotkeys.items():
                if action in self._values["hotkeys"] and isinstance(hk, dict):
                    self._values["hotkeys"][action].update(hk)