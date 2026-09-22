@echo off
chcp 65001 > nul
echo ========================================
echo   Проверка ScreenTale через Ruff
echo ========================================
echo.

:: Запускаем ruff, пишем результат в ruff_report.txt (включая ошибки)
python -m ruff check main.py backend frontend > ruff_report.txt 2>&1
set ERR=%ERRORLEVEL%

:: Выводим содержимое файла прямо в окно консоли
type ruff_report.txt

echo.
echo ----------------------------------------
if %ERR% EQU 0 (
    echo [OK] Ошибок не найдено! Отчет записан в ruff_report.txt
) else (
    echo [!] Найдены замечания. Отчет записан в ruff_report.txt
)
echo ----------------------------------------
echo.
pause