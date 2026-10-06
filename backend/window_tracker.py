"""Системный трекер окон Windows: поиск процессов, геометрия DWM и привязка оверлея."""
import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QRect

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
dwmapi = ctypes.windll.dwmapi if sys.platform == "win32" else None


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe_name: str
    rect: QRect


def get_window_exe_name(hwnd: int) -> str:
    """Возвращает имя исполняемого файла (.exe) по дескриптору окна HWND."""
    if not user32:
        return ""
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        h_process = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h_process:
            return ""

        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        # QueryFullProcessImageNameW возвращает полный путь к exe
        if kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
            kernel32.CloseHandle(h_process)
            return os.path.basename(buf.value)
        
        kernel32.CloseHandle(h_process)
    except Exception:
        pass
    return ""


def get_window_exact_rect(hwnd: int) -> QRect | None:
    """Возвращает точные видимые пиксели окна через DWM (отсекая системные тени Windows 10/11)."""
    if not dwmapi:
        return None
    try:
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        r = RECT()
        # 9 = DWMWA_EXTENDED_FRAME_BOUNDS
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)
        )
        if hr == 0:
            w = max(1, r.right - r.left)
            h = max(1, r.bottom - r.top)
            return QRect(r.left, r.top, w, h)
    except Exception:
        pass
    return None


def get_running_games() -> list[WindowInfo]:
    """Возвращает список видимых пользовательских окон и игр (как в меню Discord/OBS)."""
    if not user32:
        return []

    results = []

    def enum_windows_callback(hwnd, _lparam):
        # Отсекаем невидимые, свернутые и системные оверлеи
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True

        # Читаем заголовок окна
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()

        # Игнорируем фоновые системные панели и ScreenTale
        if not title or title in ("Program Manager", "Settings", "ScreenTale", "ScreenTale — Настройки"):
            return True

        exe = get_window_exe_name(hwnd)
        if not exe or exe.lower() in ("explorer.exe", "shellexperiencehost.exe", "textinputhost.exe"):
            return True

        rect = get_window_exact_rect(hwnd)
        if rect and rect.width() > 100 and rect.height() > 100:
            results.append(WindowInfo(hwnd=hwnd, title=title, exe_name=exe, rect=rect))

        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
    return results


def get_active_window() -> WindowInfo | None:
    """Возвращает информацию о текущем окне на переднем плане (фокусе игрока)."""
    if not user32:
        return None
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        title_len = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, title_buf, title_len + 1)
        title = title_buf.value.strip()

        exe = get_window_exe_name(hwnd)
        rect = get_window_exact_rect(hwnd) or QRect(0, 0, 1920, 1080)

        return WindowInfo(hwnd=hwnd, title=title, exe_name=exe, rect=rect)
    except Exception:
        return None


# ============================================================
# Автономный тест: смотрим, какие окна видит скрипт прямо сейчас
# ============================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Тестирование WindowTracker (Поиск окон в стиле Discord)")
    print("=" * 60)

    windows = get_running_games()
    print(f"\nНайдено видимых окон: {len(windows)}\n")
    for idx, w in enumerate(windows, 1):
        print(f"  [{idx}] {w.title} ({w.exe_name})")
        print(f"      -> Точные видимые границы DWM: {w.rect}")

    active = get_active_window()
    if active:
        print(f"\nТекущее активное окно прямо сейчас:\n  -> {active.title} [{active.exe_name}]")
    print("\n" + "=" * 60 + "\n")