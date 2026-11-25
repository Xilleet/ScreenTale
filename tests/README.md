# ScreenTale Tests

Характеризационные и unit-тесты. Короткие тесты, каждая
регрессия = отдельная функция с понятным именем.

## Запуск

```bash
# Установить pytest (если ещё нет)
pip install pytest pytest-qt

# Все тесты
python run_tests.py

# Конкретный файл
python run_tests.py tests/test_opacity.py

# По ключевому слову
python run_tests.py -k opacity

# С покрытием (нужен pytest-cov)
python run_tests.py --cov=backend --cov=frontend --cov-report=term-missing
```

Тесты запускаются в **offscreen-режиме Qt** — дисплей не требуется. Можно
гонять на Linux-сервере без X-сервера, в Docker, в CI.

## Что тестируется

| Файл | Что ловит |
|------|-----------|
| `test_config.py` | SettingsManager: загрузка/сохранение, debounce, миграция `opacity` → `frame_opacity` для апгрейда v0.7 → v0.8 |
| `test_opacity.py` | **Регрессия бага «текст выезжает за углы»**: `QGraphicsOpacityEffect` на QTextEdit создавал прямоугольный pixmap, не уважающий `border-radius` — проверяем, что текст_opacity применяется через `QTextCharFormat.setForeground(QColor с alpha)` |
| `test_inplace.py` | `cluster_lines` (склейка/разделение блоков, фильтр шума, кап 12), `find_optimal_font_size` (бинарный поиск кегля) |
| `test_ocr.py` | `normalize_ocr_text`: склейка дефисом разорванных слов, нормализация кавычек, исправление O→0 в числах, схлопывание пробелов, склейка CJK без пробела |
| `test_translation.py` | `parse_structured_translation`: разбор ответа LLM по тегам `[1] [2] ...`, фоллбэк на переносы строк, обработка пропущенных блоков |
| `test_languages.py` | Реестр 12 языков: BCP-47 для Windows OCR, Flores-200 для NLLB, коды Google, `build_llm_system_prompt` |
| `test_i18n.py` | `t()`, `load_locale`, fallback на en для неизвестных языков, `detect_system_ui_lang` |

## Структура

```
tests/
├── conftest.py           # общие фикстуры: qapp, temp_data_dir, settings
├── test_config.py        # SettingsManager
├── test_opacity.py       # регрессия opacity-бага (главный повод для этой папки)
├── test_inplace.py       # cluster_lines + find_optimal_font_size
├── test_ocr.py           # normalize_ocr_text
├── test_translation.py   # parse_structured_translation
├── test_languages.py     # реестр LANGUAGES
└── test_i18n.py          # t(), load_locale
```

## Фикстуры (см. `conftest.py`)

- `qapp` — сессионная `QApplication` (offscreen). Создаётся одна на весь прогон.
- `temp_data_dir` — изолированная папка `data/` для каждого теста. Без этого
  `SettingsManager` пишет в боевую `data/config.json` пользователя — тесты на
  dev-машине ломали бы реальные настройки.
- `settings` — готовый `SettingsManager` с `temp_data_dir`.

## Как добавить новый тест

1. Если тестируешь новую подсистему — создай `tests/test_<name>.py`.
2. Если нужна Qt-фикстура — `qapp` параметром в функцию теста.
3. Если тестируешь `SettingsManager` — `settings` параметром.
4. Имя теста — `test_<что_проверяет>` (например, `test_text_opacity_zero_alpha_at_0`).
5. Каждый тест — самодостаточный, не зависит от порядка выполнения.

## CI-friendly

`run_tests.py` возвращает код 0 при успехе, 1 при провале — можно
использовать в GitHub Actions:

```yaml
- name: Tests
  run: python run_tests.py
```
