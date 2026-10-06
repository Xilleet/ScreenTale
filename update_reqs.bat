@echo off
chcp 65001 > nul

cd /d "%~dp0"

echo ==========================================
echo   Генерация requirements.txt (pipreqs)
echo ==========================================
echo.

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

pip install -q pipreqs

echo Сканирование файлов .py и сборка зависимостей...
:: Игнорируем виртуальное окружение и папку data с кэшами моделей
pipreqs --encoding=utf-8 --force --ignore data,.venv,build,dist,venv .

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ==========================================
    echo   [OK] requirements.txt успешно обновлен!
    echo ==========================================
) else (
    echo.
    echo   [!] Ошибка при создании requirements.txt.
)

echo.
pause