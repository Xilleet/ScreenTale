"""Глобальные хоткеи Windows: RegisterHotKey + QAbstractNativeEventFilter.
Не требует прав администратора и не вмешивается в ввод (в отличие от keyboard)."""
import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal
from PySide6.QtWidgets import QApplication

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
else:
    user32 = None

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000


class _NativeFilter(QAbstractNativeEventFilter):
    def __init__(self, manager):
        super().__init__()
        self._manager = manager

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self._manager._on_wm_hotkey(int(msg.wParam))
        return False, 0


class HotkeyManager(QObject):
    """Регистрация глобальных хоткеев. action -> (id, mods, vk)."""

    triggered = Signal(str)  # имя действия

    def __init__(self):
        super().__init__()
        self._bindings = {}   # action -> (id, mods, vk)
        self._by_id = {}      # id -> action
        self._next_id = 1
        self._filter = _NativeFilter(self)
        QApplication.instance().installNativeEventFilter(self._filter)

    def register(self, action, mods, vk) -> bool:
        if user32 is None:
            return False
        self.unregister(action)
        hotkey_id = self._next_id
        if not user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
            return False
        self._next_id += 1
        self._bindings[action] = (hotkey_id, mods, vk)
        self._by_id[hotkey_id] = action
        return True

    def unregister(self, action):
        binding = self._bindings.pop(action, None)
        if binding:
            user32.UnregisterHotKey(None, binding[0])
            self._by_id.pop(binding[0], None)

    def apply_all(self, hotkey_settings: dict) -> dict:
        """Применить словарь из SettingsManager: {action: {label, mods, vk}}."""
        return {action: self.register(action, hk["mods"], hk["vk"])
                for action, hk in hotkey_settings.items()}

    def _on_wm_hotkey(self, hotkey_id):
        action = self._by_id.get(hotkey_id)
        if action:
            self.triggered.emit(action)

    def shutdown(self):
        for action in list(self._bindings):
            self.unregister(action)