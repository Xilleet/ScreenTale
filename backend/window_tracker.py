"""Системный трекер окон Windows: умная фильтрация игр (в стиле Discord/OBS) и геометрия DWM."""
import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QRect

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
dwmapi = ctypes.windll.dwmapi if sys.platform == "win32" else None

# Системные константы Win32
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
DWMWA_CLOAKED = 14
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# Чёрный список системных утилит, фоновых хостов и консолей
SYSTEM_EXCLUDES = {
    "explorer.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "systemsettings.exe",
    "applicationframehost.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "lockapp.exe",
    "taskmgr.exe",
    "screentale.exe",
    "python.exe",
    "py.exe",
    "nvidia overlay.exe",
    "nvidia share.exe",
    "rtss.exe",
    "gamebar.exe",
    "gamebarft.exe",
    "cmd.exe",
    "powershell.exe",
    "conhost.exe",
}


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

        kernel32 = ctypes.windll.kernel32
        # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
        h_process = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h_process:
            return ""

        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
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
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
        )
        if hr == 0:
            w = max(1, r.right - r.left)
            h = max(1, r.bottom - r.top)
            return QRect(r.left, r.top, w, h)
    except Exception:
        pass
    return None


def get_running_games() -> list[WindowInfo]:
    """Возвращает список ТОЛЬКО реальных игр и главных окон приложений (Discord/OBS стиль)."""
    if not user32:
        return []

    raw_results = []

    def enum_windows_callback(hwnd, _lparam):
        # 1. Проверка базовой видимости
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True

        # 2. Исключаем дочерние диалоговые всплывашки (у главного окна нет владельца)
        if user32.GetWindow(hwnd, GW_OWNER) != 0:
            return True

        # 3. Исключаем оверлеи и ToolWindow (если они явно не помечены как AppWindow)
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
            return True

        # 4. Исключаем скрытые Windows 10/11 UWP приложения (DWM Cloaked)
        if dwmapi:
            cloaked = wintypes.DWORD(0)
            if (
                dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0
                and cloaked.value != 0
            ):
                return True

        # 5. Проверяем заголовок
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()
        if not title:
            return True

        # 6. Фильтр системных процессов и самого ScreenTale
        exe = get_window_exe_name(hwnd)
        if not exe or exe.lower() in SYSTEM_EXCLUDES:
            return True

        # 7. Отсекаем невидимые и крошечные окна (меньше 160x160 px)
        rect = get_window_exact_rect(hwnd)
        if not rect or rect.width() < 160 or rect.height() < 160:
            return True

        raw_results.append(WindowInfo(hwnd=hwnd, title=title, exe_name=exe, rect=rect))
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)

    # 8. Дедупликация: для одного приложения оставляем только самое большое (главное) окно
    unique_apps: dict[str, WindowInfo] = {}
    for w in raw_results:
        key = w.exe_name.lower()
        current_area = w.rect.width() * w.rect.height()
        if key not in unique_apps:
            unique_apps[key] = w
        else:
            prev_area = unique_apps[key].rect.width() * unique_apps[key].rect.height()
            if current_area > prev_area:
                unique_apps[key] = w

    return list(unique_apps.values())


def get_active_window() -> WindowInfo | None:
    """Возвращает информацию о текущем активном окне в фокусе."""
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