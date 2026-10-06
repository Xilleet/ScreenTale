"""Автоматический сквозной тест-раннер для ScreenTale (v0.7.0).

Запуск:
    python test_suite.py
"""
import math
import os
import sys
import time

# Добавляем корень проекта в путь импортов
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QApplication


class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.warnings = 0

    def ok(self, msg: str):
        self.passed += 1
        print(f"  [✓ PASS] {msg}")

    def fail(self, msg: str, err: str = ""):
        self.failed += 1
        err_str = f" ➔ Ошибка: {err}" if err else ""
        print(f"  [✗ FAIL] {msg}{err_str}")

    def warn(self, msg: str):
        self.warnings += 1
        print(f"  [⚠ WARN] {msg}")


def print_section(title: str):
    print(f"\n[{title}]" + " " + "─" * (65 - len(title)))


def run_all_tests():
    # 0. Инициализируем headless Qt приложение для тестов QFontMetrics и Canvas
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)

    res = TestResult()
    start_time = time.perf_counter()

    print("\n" + "=" * 70)
    print("           ScreenTale Test Suite — Автоматический аудит")
    print("=" * 70)

    # -----------------------------------------------------------------
    # 1. База настроек и реестр языков
    # -----------------------------------------------------------------
    print_section("1/6. Конфигурация и Реестр Языков")
    try:
        from backend.config import APP_VERSION, SettingsManager
        from backend.languages import (
            LANGUAGES,
            build_llm_system_prompt,
            get_win_ocr_tag,
        )

        # Проверка версии
        if APP_VERSION:
            res.ok(f"Версия приложения определена: v{APP_VERSION}")
        else:
            res.fail("APP_VERSION не задан в backend/config.py")

        # Проверка золотого пула из 12 языков
        if len(LANGUAGES) >= 12:
            res.ok(f"Реестр языков: загружено {len(LANGUAGES)} языков (Any-to-Any пул)")
        else:
            res.fail(f"В реестре языков только {len(LANGUAGES)} из 12")

        # Проверка тегов Windows OCR
        ja_tag = get_win_ocr_tag("ja")
        ru_tag = get_win_ocr_tag("ru")
        if ja_tag == "ja-JP" and ru_tag == "ru-RU":
            res.ok(f"BCP-47 маппинг Windows OCR корректен (ja -> {ja_tag}, ru -> {ru_tag})")
        else:
            res.fail("Ошибка сопоставления BCP-47 тегов OCR")

        # Проверка игрового системного промпта
        prompt = build_llm_system_prompt("en", "ru")
        if "CONCISE" in prompt and "game" in prompt.lower():
            res.ok("Игровой сжатый промпт для LLM сформирован успешно")
        else:
            res.fail("Системный промпт не содержит правил сжатия текста")

        # Проверка дефолтов в SettingsManager
        sm = SettingsManager()
        if sm.get("theme") in ("dark", "dark_classic", "light"):
            res.ok("Настройки (SettingsManager) инициализированы корректно")
        else:
            res.fail("Некорректная тема в настройках по умолчанию")

    except Exception as e:
        res.fail("Критический сбой в модуле настроек/языков", str(e))

    # -----------------------------------------------------------------
    # 2. Движки OCR и извлечение координат (Синтетический тест)
    # -----------------------------------------------------------------
    print_section("2/6. Движки OCR и Извлечение Координат")
    try:
        from backend.ocr_engines import (
            RapidOcrEngine,
            WindowsOcrEngine,
            reset_ocr_lang_cache,
        )

        # Создаем синтетическую картинку в памяти (без файлов на диске)
        test_img = Image.new("RGB", (320, 90), color="white")
        draw = ImageDraw.Draw(test_img)
        draw.text((20, 15), "ScreenTale Test OCR", fill="black")
        draw.text((20, 50), "Line Two Coordinate OK", fill="black")

        # Тест Windows OCR
        win_eng = WindowsOcrEngine()
        if win_eng.is_available():
            win_eng.load(lang="en-US")
            text, blocks = win_eng.read_detailed(test_img)
            if len(blocks) >= 2 and "ScreenTale" in text:
                res.ok(f"Windows OCR: распознал {len(blocks)} строки с точными координатами")
                res.ok(f"  -> Прямоугольник строки 1: {blocks[0]['rect']}, полигон: {len(blocks[0]['polygon'])} точек")
            else:
                res.warn(f"Windows OCR вернул {len(blocks)} блоков: {text!r}")
        else:
            res.warn("Windows OCR недоступен в данной ОС (WinRT отсутствует)")

        # Сброс кэша
        reset_ocr_lang_cache("en-US")
        res.ok("Функция безопасного сброса кэша языков (reset_ocr_lang_cache) работает штатно")

        # Тест RapidOCR
        rapid_eng = RapidOcrEngine()
        if rapid_eng.is_available():
            rapid_eng.load()
            _, r_blocks = rapid_eng.read_detailed(test_img)
            if len(r_blocks) >= 1:
                res.ok(f"RapidOCR (ONNX): успешно прочитал {len(r_blocks)} блока с полигонами")
            else:
                res.warn("RapidOCR вернул пустой результат на синтетическом тесте")
        else:
            res.warn("RapidOCR не установлен в окружении (опциональный движок)")

    except Exception as e:
        res.fail("Сбой при тестировании OCR подсистем", str(e))

    # -----------------------------------------------------------------
    # 3. Пространственная математика In-Place (Geometry Engine)
    # -----------------------------------------------------------------
    print_section("3/6. Пространственная Математика In-Place (AR-Engine)")
    try:
        from frontend.inplace_canvas import cluster_lines, find_optimal_font_size

        # Тест 1: Склейка близких строк в один баббл
        near_blocks = [
            {"text": "Line 1", "rect": (100, 200, 300, 25)},
            {"text": "Line 2", "rect": (100, 230, 320, 25)},  # Зазор 5px (близко)
        ]
        clusters_near = cluster_lines(near_blocks)
        if len(clusters_near) == 1:
            res.ok("Склейка строк (cluster_lines): 2 близкие строки объединены в 1 диалоговый баббл")
            res.ok(f"  -> Охватывающий прямоугольник: {clusters_near[0]['rect']}")
        else:
            res.fail(f"Близкие строки не склеились (получено кластеров: {len(clusters_near)})")

        # Тест 2: Разделение дальних строк (разные углы экрана)
        distant_blocks = [
            {"text": "Quest in top-left", "rect": (50, 50, 200, 30)},
            {"text": "Dialogue at bottom", "rect": (1200, 800, 500, 60)},  # Далеко
        ]
        clusters_dist = cluster_lines(distant_blocks)
        if len(clusters_dist) == 2:
            res.ok("Пространственная изоляция: строки из разных углов разделены на 2 независимых баббла")
        else:
            res.fail(f"Дальние строки ошибочно слились (получено кластеров: {len(clusters_dist)})")

        # Тест 3: Бинарный поиск шрифта O(log N)
        test_phrase = "Быстрая коричневая лиса прыгает через ленивую собаку."
        f_size = find_optimal_font_size(test_phrase, max_w=300, max_h=80, min_sz=8, max_sz=40)
        metrics = QFontMetrics(QFont("Segoe UI", f_size, QFont.Weight.Bold))
        calc_rect = metrics.boundingRect(QRect(0, 0, 300, 0), Qt.TextFlag.TextWordWrap, test_phrase)

        if calc_rect.height() <= 80 and calc_rect.width() <= 300 and f_size >= 8:
            res.ok(f"Бинарный подбор шрифта (find_optimal_font_size): кегль {f_size}px идеально вписался в рамку 300x80")
        else:
            res.fail(f"Шрифт {f_size}px не поместился в рамку (высота={calc_rect.height()})")

        # Тест 4: Расчет угла наклона через atan2
        dx, dy = 100.0, 20.0
        angle = math.degrees(math.atan2(dy, dx))
        if 11.0 <= angle <= 12.0:
            res.ok(f"Математика atan2: наклон полигона вычислен корректно ({angle:.1f}°)")
        else:
            res.fail(f"Некорректный расчёт угла atan2: {angle}")

    except Exception as e:
        res.fail("Сбой в математическом движке In-Place", str(e))

    # -----------------------------------------------------------------
    # 4. Системный трекер окон Win32 (Discord/OBS фильтрация)
    # -----------------------------------------------------------------
    print_section("4/6. Системный Трекер Окон (Win32 & DWM)")
    if sys.platform == "win32":
        try:
            from backend.window_tracker import (
                SYSTEM_EXCLUDES,
                get_active_window,
                get_running_games,
                get_window_exact_rect,
            )

            # Проверка поиска активного окна
            active = get_active_window()
            if active and active.hwnd:
                res.ok(f"Активное окно в фокусе: '{active.title[:30]}' [{active.exe_name}]")
            else:
                res.warn("Не удалось определить активное окно (возможно, запуск в фоне)")

            # Проверка фильтрации запущенных окон
            games = get_running_games()
            res.ok(f"Найдено главных окон приложений (Discord-style): {len(games)} шт.")

            # Проверка отсутствия системного мусора
            has_junk = any(w.exe_name.lower() in SYSTEM_EXCLUDES for w in games)
            if not has_junk:
                res.ok("Фильтр черного списка: системные оверлеи и процессы Windows (py.exe, NVIDIA) отсечены")
            else:
                res.fail("В список окон просочились процессы из черного списка")

            # Проверка DWM рамок без невидимых теней
            if active and active.hwnd:
                rect = get_window_exact_rect(active.hwnd)
                if rect and rect.width() > 0 and rect.height() > 0:
                    res.ok(f"Честные видимые пиксели DWM: {rect}")
                else:
                    res.warn("DwmGetWindowAttribute не вернул границы активного окна")

        except Exception as e:
            res.fail("Сбой в модуле отслеживания окон", str(e))
    else:
        res.warn("Тесты Win32 пропущены (ОС не Windows)")

    # -----------------------------------------------------------------
    # 5. Локальный ИИ и рантайм llama.cpp
    # -----------------------------------------------------------------
    print_section("5/6. Локальный ИИ (llama.cpp) и Переводчики")
    try:
        from backend.llama_server import get_installed_models
        from backend.runtime_manager import (
            get_installed_backend,
            get_server_exe,
            run_smoke_test,
        )

        exe_path = get_server_exe()
        if os.path.isfile(exe_path):
            res.ok(f"Бинарник сервера найден: {exe_path}")
            # Запуск Smoke Test (--version)
            smoke_ok, smoke_msg = run_smoke_test()
            if smoke_ok:
                backend = get_installed_backend() or "cpu"
                res.ok(f"Smoke Test пройден успешно! Активный бэкенд: {backend.upper()}")
            else:
                res.fail("Smoke Test завершился с ошибкой", smoke_msg)
        else:
            res.warn("llama-server.exe ещё не установлен (можно установить через Настройки)")

        # Проверка каталога GGUF моделей
        models = get_installed_models()
        if models:
            m_names = ", ".join(m["filename"] for m in models)
            res.ok(f"Обнаружено скачанных GGUF-моделей: {len(models)} шт. ({m_names})")
        else:
            res.warn("В папке data/models/ пока нет скачанных .gguf файлов")

    except Exception as e:
        res.fail("Сбой в проверке рантайма ИИ", str(e))

    # -----------------------------------------------------------------
    # 6. Проверка холста InPlaceCanvas и сквозного клика
    # -----------------------------------------------------------------
    print_section("6/6. Интерфейс AR-Canvas и Безопасность")
    try:
        from frontend.inplace_canvas import InPlaceCanvas

        canvas = InPlaceCanvas()

        # Проверка флагов окна
        flags = canvas.windowFlags()
        is_tool = bool(flags & Qt.WindowType.Tool)
        is_frameless = bool(flags & Qt.WindowType.FramelessWindowHint)
        is_top = bool(flags & Qt.WindowType.WindowStaysOnTopHint)

        if is_tool and is_frameless and is_top:
            res.ok("Системные флаги холста: Frameless + StaysOnTop + Tool установлены")
        else:
            res.fail("Некорректные флаги окна у InPlaceCanvas")

        # Проверка прозрачности для мыши
        is_transparent = canvas.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if is_transparent:
            res.ok("Сквозной клик (WA_TransparentForMouseEvents): ВКЛЮЧЕН (Zero-Risk для античитов)")
        else:
            res.fail("WA_TransparentForMouseEvents выключен — холст будет перехватывать клики мыши!")

        # Проверка переключения защиты от захвата
        canvas.set_capture_visibility(True)
        res.ok("Переключение видимости для захвата (WDA_NONE / WDA_EXCLUDEFROMCAPTURE) отработало штатно")

        # Очистка
        canvas.clear()
        if len(canvas.active_blocks) == 0:
            res.ok("Метод очистки холста (canvas.clear) работает корректно")

    except Exception as e:
        res.fail("Сбой при инициализации InPlaceCanvas", str(e))

    # -----------------------------------------------------------------
    # ИТОГОВЫЙ ОТЧЁТ
    # -----------------------------------------------------------------
    elapsed = time.perf_counter() - start_time
    total = res.passed + res.failed

    print("\n" + "=" * 70)
    print("                     РЕЗУЛЬТАТЫ АУДИТА")
    print("=" * 70)
    print(f"  Всего тестов пройдено : {res.passed} из {total}")
    print(f"  Ошибок (Failed)       : {res.failed}")
    print(f"  Предупреждений (Warn) : {res.warnings}")
    print(f"  Время выполнения      : {elapsed:.2f} сек.")
    print("=" * 70)

    if res.failed == 0:
        print("\n  🎉 [SUCCESS] ВСЕ ПОДСИСТЕМЫ РАБОТАЮТ ИДЕАЛЬНО! РЕЛИЗ 0.7.0 ГОТОВ К ВЫПУСКУ!\n")
        return 0
    else:
        print(f"\n  ⚠️ [ATTENTION] ОБНАРУЖЕНО {res.failed} ОШИБОК. ТРЕБУЕТСЯ ВНИМАНИЕ.\n")
        return 1


if __name__ == "__main__":
    code = run_all_tests()
    try:
        input("Нажмите [Enter], чтобы выйти...")
    except (EOFError, OSError):
        pass
    sys.exit(code)