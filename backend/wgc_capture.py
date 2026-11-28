"""WGC window-targeted capture via windows-capture package.

устраняет self-feeding — overlay окно НЕ попадает в кадр, потому что
WGC захватывает composited surface конкретного game-window, а overlay —
отдельное top-level окно. Не нужен WDA_EXCLUDEFROMCAPTURE (который молча
падает на Win < 11 24H2).
"""
import sys
import threading

from PIL import Image

HAS_WGC = False
if sys.platform == "win32":
    try:
        from windows_capture import Frame, WindowsCapture
        HAS_WGC = True
    except ImportError:
        pass


class _FrameMailbox:
    """Thread-safe single-frame mailbox. Latest frame wins, no backlog.

    WGC capture thread публикует кадры ~10 fps (throttled via
    minimum_update_interval). OCR worker читает по требованию — если
    новый кадр не пришёл с прошлого чтения, отдаёт предыдущий.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None       # numpy.ndarray (height, width, 4) BGRA
        self._width = 0
        self._height = 0

    def publish(self, frame_buffer, width: int, height: int) -> None:
        """Store latest frame (called from WGC capture thread).

        Копируем буфер: frame_buffer — это view в mapped GPU memory,
        который Rust-backend может переработать на следующем кадре.
        """
        with self._lock:
            self._frame = frame_buffer.copy()
            self._width = width
            self._height = height

    def acquire(self):
        """Get latest frame (called from OCR worker thread).

        Returns (numpy.ndarray, width, height) or None if no frame yet.
        """
        with self._lock:
            if self._frame is None:
                return None
            # Отдаём копию — OCR worker работает в своём потоке, mailbox
            # может получить новый кадр пока OCR ещё обрабатывает старый.
            return self._frame.copy(), self._width, self._height


class WgcWindowCapture:
    """Захват конкретного окна через Windows.Graphics.Capture.

    Жизненный цикл:
        capture = WgcWindowCapture(hwnd)
        capture.start()          # фоновый поток WGC
        img = capture.crop_region(bbox)   # PIL Image (RGB) или None
        capture.stop()           # остановить WGC поток

    Координаты: bbox — screen-logical (left, top, right, bottom).
    WGC frame — window-relative physical pixels. Конвертация через
    window_rect (logical screen coords) + dpi_scale.
    """

    def __init__(self, hwnd: int, min_interval_ms: int = 100):
        if not HAS_WGC:
            raise RuntimeError(
                "windows-capture не установлен. pip install windows-capture")
        if sys.platform != "win32":
            raise RuntimeError("WGC доступен только на Windows")

        self._hwnd = int(hwnd)
        self._mailbox = _FrameMailbox()
        self._capture_control = None

        self._capture = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=min_interval_ms,
            window_hwnd=self._hwnd,
        )

        @self._capture.event
        def on_frame_arrived(frame: Frame, capture_control):
            self._mailbox.publish(frame.frame_buffer, frame.width, frame.height)

        @self._capture.event
        def on_closed():
            print(f"[wgc] capture session закрыта (hwnd={self._hwnd})")

    def start(self) -> bool:
        """Запустить захват на отдельном потоке. Возвращает True при успехе."""
        try:
            self._capture_control = self._capture.start_free_threaded()
            print(f"[wgc] захват окна hwnd={self._hwnd} запущен")
            return True
        except Exception as e:
            print(f"[wgc] не удалось стартовать захват hwnd={self._hwnd}: {e}")
            self._capture_control = None
            return False

    def stop(self) -> None:
        """Остановить захват."""
        if self._capture_control is not None:
            try:
                self._capture_control.stop()
            except Exception:
                pass
            self._capture_control = None

    def has_frame(self) -> bool:
        """True, если в mailbox есть хотя бы один кадр."""
        return self._mailbox.acquire() is not None

    def get_full_frame(self) -> Image.Image | None:
        """Весь кадр окна как PIL Image (RGB). None если кадра ещё нет."""
        result = self._mailbox.acquire()
        if result is None:
            return None
        buf, w, h = result  # noqa: RUF059
        # buf: (h, w, 4) BGRA. Конвертируем в RGB.
        rgb = buf[:, :, :3][:, :, ::-1]   # BGR -> RGB, drop alpha
        return Image.fromarray(rgb, "RGB")

    def crop_region(self, bbox_physical: tuple[int, int, int, int]) -> Image.Image | None:
        """Вырезать регион из последнего кадра.

        bbox_physical: (left, top, right, bottom) — window-relative physical
        pixels (уже пересчитанный caller'ом из screen-logical через
        window_rect и dpi_scale).

        Returns PIL Image (RGB) или None если кадра нет или bbox невалидный.
        """
        result = self._mailbox.acquire()
        if result is None:
            return None
        buf, w, h = result

        left, top, right, bottom = bbox_physical
        # Clamp к границам кадра
        left = max(0, min(int(left), w))
        top = max(0, min(int(top), h))
        right = max(0, min(int(right), w))
        bottom = max(0, min(int(bottom), h))

        if right <= left or bottom <= top:
            return None

        # buf: (height, width, 4) BGRA. Crop: [top:bottom, left:right, :]
        cropped = buf[top:bottom, left:right, :]
        rgb = cropped[:, :, :3][:, :, ::-1]   # BGR -> RGB
        return Image.fromarray(rgb, "RGB")


def is_wgc_available() -> bool:
    """True, если windows-capture установлен и платформа — Windows."""
    return HAS_WGC


def screen_to_window_physical(
    bbox_screen_logical: tuple[int, int, int, int],
    window_rect_logical,
    dpi_scale: float,
) -> tuple[int, int, int, int]:
    """Конвертирует screen-logical bbox в window-relative physical coords.

    bbox_screen_logical: (left, top, right, bottom) — абсолютные экранные
        логические координаты (то, что приходит в ocr.read()).
    window_rect_logical: QRect или объект с .x() .y() — позиция окна на
        экране в логических координатах.
    dpi_scale: devicePixelRatio монитора, на котором находится bbox.

    Returns (left, top, right, bottom) — relative to window top-left,
    in physical pixels (для crop_region).
    """
    bx_left, bx_top, bx_right, bx_bottom = bbox_screen_logical
    win_x = window_rect_logical.x()
    win_y = window_rect_logical.y()

    rel_left = int((bx_left - win_x) * dpi_scale)
    rel_top = int((bx_top - win_y) * dpi_scale)
    rel_right = int((bx_right - win_x) * dpi_scale)
    rel_bottom = int((bx_bottom - win_y) * dpi_scale)

    return (rel_left, rel_top, rel_right, rel_bottom)
