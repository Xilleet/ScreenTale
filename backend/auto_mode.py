"""Авто-режим: следит за областью и детектит «кадр изменился и стабилизировался»."""
import time

import numpy as np
from PIL import ImageGrab
from PySide6.QtCore import QThread, Signal

from backend.ocr import get_screen_scale

DIFF_THRESHOLD = 0.5   # как в v0.3.2
POLL_DELAY = 0.4       # период опроса, сек
SLICE = 0.05           # гранулярность сна — отзывчивая остановка


class AutoModeWorker(QThread):
    region_changed = Signal()

    def __init__(self, bbox):
        super().__init__()
        self._bbox = bbox

    def run(self):
        last_processed = None
        prev = None
        while not self.isInterruptionRequested():
            try:
                img = self._grab()
            except Exception:
                self._sleep(POLL_DELAY)
                continue

            # Первый кадр или смена размера области — читаем сразу
            if last_processed is None or prev is None or last_processed.shape != img.shape:
                last_processed = img
                prev = img
                self.region_changed.emit()
                print("[auto] воркер: первый кадр -> region_changed")
                self._sleep(POLL_DELAY)
                continue

            diff_last = np.mean(np.abs(img.astype(np.int16) - last_processed.astype(np.int16)))
            if diff_last < DIFF_THRESHOLD:
                prev = img                     # ничего не поменялось
                self._sleep(POLL_DELAY)
                continue

            diff_prev = np.mean(np.abs(img.astype(np.int16) - prev.astype(np.int16)))
            if diff_prev >= DIFF_THRESHOLD:
                prev = img                     # ещё меняется — ждём стабилизации
                self._sleep(POLL_DELAY)
                continue

            # Изменился и зафиксировался — пора читать
            last_processed = img
            prev = img
            self.region_changed.emit()
            print("[auto] воркер: кадр изменился -> region_changed") 
            self._sleep(POLL_DELAY)

    def _grab(self):
        left, top, right, bottom = self._bbox
        scale = get_screen_scale()
        physical = (int(left * scale), int(top * scale),
                    int(right * scale), int(bottom * scale))
        return np.array(ImageGrab.grab(bbox=physical))

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.isInterruptionRequested():
            time.sleep(SLICE)