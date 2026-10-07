@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ===================================================
echo   Автоматическая сборка ScreenTale (Release Build)
echo ===================================================
echo.

:: 1. Проверка и создание виртуального окружения
if not exist ".venv\Scripts\python.exe" (
    echo [1/6] Виртуальное окружение не найдено. Создаем новое .venv...
    python -m venv .venv
) else (
    echo [1/6] Виртуальное окружение уже существует.
)

:: 2. Актуализация зависимостей (update_reqs.bat)
if exist "update_reqs.bat" (
    echo.
    echo [2/6] Запуск update_reqs.bat для обновления списка библиотек...
    call update_reqs.bat
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo   [!] Ошибка при выполнении update_reqs.bat. Сборка остановлена.
        pause
        exit /b %ERRORLEVEL%
    )
) else (
    echo.
    echo [2/6] update_reqs.bat не найден, пропускаем...
)

:: 3. Установка и обновление пакетов из requirements.txt
echo.
echo [3/6] Проверка и установка пакетов из requirements.txt...
.venv\Scripts\python -m pip install --upgrade pip -q
.venv\Scripts\pip install -r requirements.txt -q
.venv\Scripts\pip install pyinstaller -q

:: 4. Автоматический аудит и тесты (test_suite.py)
echo.
echo [4/6] Запуск автоматического тестирования (test_suite.py)...
echo.
:: Команда "echo |" автоматически прожимает Enter в конце теста
echo | .venv\Scripts\python.exe test_suite.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   ====================================================
    echo   [!] СБОРКА ОСТАНОВЛЕНА: Тесты test_suite.py не пройдены!
    echo   Исправьте ошибки перед созданием релизного билда.
    echo   ====================================================
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo   [✓] Все тесты пройдены успешно! Переходим к компиляции...
echo.

:: 5. Сборка проекта через PyInstaller
echo [5/6] Запуск сборки PyInstaller...
echo.

if exist "ScreenTale.spec" (
    .venv\Scripts\pyinstaller --noconfirm --clean ScreenTale.spec
) else (
    .venv\Scripts\pyinstaller --noconfirm --onedir --console --name "ScreenTale" --icon "ScreenTale.ico" --add-data "ScreenTale.ico;." --add-data "frontend/icons;frontend/icons" --add-data "locales;locales" --add-data "logo.png;." --clean main.py
)

:: Подстраховка: копируем скрипт диагностики в корень рядом с exe
if exist "debug_diagnostics.py" (
    copy /y "debug_diagnostics.py" "dist\ScreenTale\debug_diagnostics.py" > nul
)

:: Копируем папку locales в корень рядом с exe (чтобы файлы перевода были открыты и редактируемы)
if exist "locales" (
    xcopy "locales" "dist\ScreenTale\locales\" /s /e /y /q > nul
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   [!] Ошибка при сборке PyInstaller.
    pause
    exit /b %ERRORLEVEL%
)

:: 6. Архивация в ZIP, расчет SHA-256 и генерация manifest.json
echo.
echo [6/6] Упаковка в ZIP, расчет SHA-256 и генерация manifest.json...

.venv\Scripts\python -c "import os, shutil, hashlib, json, datetime; from backend.config import APP_VERSION; dist='dist'; app_folder=os.path.join(dist, 'ScreenTale'); zip_base=f'ScreenTale_v{APP_VERSION}'; zip_path=os.path.join(dist, f'{zip_base}.zip'); print(f'  -> Упаковка {zip_base}.zip...'); shutil.make_archive(os.path.join(dist, zip_base), 'zip', root_dir=dist, base_dir='ScreenTale'); h=hashlib.sha256(); f=open(zip_path,'rb'); [h.update(c) for c in iter(lambda: f.read(65536), b'')]; f.close(); digest=h.hexdigest(); print(f'  -> Вычислен SHA-256: {digest}'); manifest={'version': APP_VERSION, 'release_date': datetime.date.today().isoformat(), 'archive_name': f'{zip_base}.zip', 'download_url': f'https://github.com/Xilleet/ScreenTale/releases/download/v{APP_VERSION}/{zip_base}.zip', 'sha256': digest, 'size_bytes': os.path.getsize(zip_path)}; open(os.path.join(dist, 'manifest.json'), 'w', encoding='utf-8').write(json.dumps(manifest, indent=4, ensure_ascii=False)); print('  -> Файл manifest.json успешно сформирован!')"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ===================================================
    echo   [OK] Всё готово к релизу!
    echo.
    echo   Файлы для публикации на GitHub лежат в папке dist/:
    echo     1. ScreenTale_vX.X.X.zip (архив с программой)
    echo     2. manifest.json         (манифест с SHA-256)
    echo ===================================================
) else (
    echo.
    echo   [!] Ошибка при создании архива или манифеста.
)

echo.
pause