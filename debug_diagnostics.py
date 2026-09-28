"""Автономный модуль диагностики ScreenTale и сборщик репортов."""
import json
import os
import platform
import sys
import zipfile

from backend.config import APP_VERSION, CONFIG_PATH, get_app_dir, get_data_dir


def check_system_environment() -> dict:
    """Собирает данные об ОС, Python и системных библиотеках."""
    report = {
        "screentale_version": APP_VERSION,
        "python_version": sys.version,
        "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
        "architecture": platform.machine(),
    }

    # Проверка Visual C++ Redistributable (vcruntime140.dll)
    sys32 = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32")
    report["has_vcruntime140"] = os.path.isfile(os.path.join(sys32, "vcruntime140.dll"))

    return report


def check_hardware_capabilities() -> dict:
    """Проверяет видеокарту, VRAM, драйверы и поддержку Vulkan/CUDA."""
    from backend.runtime_manager import is_backend_supported
    from backend.translators import get_vram_info

    info = {
        "cuda12_supported": is_backend_supported("cuda12"),
        "vulkan_supported": is_backend_supported("vulkan"),
        "cpu_supported": is_backend_supported("cpu"),
    }

    vram = get_vram_info()
    if vram:
        used_b, total_b, gpu_name = vram
        info["gpu_name"] = gpu_name
        info["vram_used_gb"] = round(used_b / (1024**3), 2)
        info["vram_total_gb"] = round(total_b / (1024**3), 2)
    else:
        info["gpu_name"] = "Не определена (NVML недоступен)"

    return info


def check_ocr_subsystems() -> dict:
    """Проверяет языковые пакеты Windows OCR и RapidOCR."""
    ocr_info = {}

    # 1. Windows OCR
    try:
        from backend.ocr_engines import WindowsOcrEngine
        win_eng = WindowsOcrEngine()
        ocr_info["windows_ocr_available"] = win_eng.is_available()
        ocr_info["windows_ocr_languages"] = win_eng.get_installed_languages()
    except Exception as e:
        ocr_info["windows_ocr_available"] = False
        ocr_info["windows_ocr_error"] = str(e)

    # 2. RapidOCR (ONNX)
    try:
        from backend.ocr_engines import RapidOcrEngine
        rapid_eng = RapidOcrEngine()
        ocr_info["rapidocr_available"] = rapid_eng.is_available()
    except Exception as e:
        ocr_info["rapidocr_available"] = False
        ocr_info["rapidocr_error"] = str(e)

    return ocr_info


def check_llama_engine() -> dict:
    """Проверяет состояние llama-server.exe, установленный бэкенд и GGUF модели."""
    from backend.llama_server import get_installed_models
    from backend.runtime_manager import (
        get_installed_backend,
        get_server_exe,
        run_smoke_test,
    )

    exe = get_server_exe()
    installed_backend = get_installed_backend()

    info = {
        "server_exe_exists": os.path.isfile(exe),
        "installed_backend": installed_backend or "Не установлен",
    }

    if os.path.isfile(exe):
        ok, msg = run_smoke_test()
        info["smoke_test_ok"] = ok
        info["smoke_test_message"] = msg
    else:
        info["smoke_test_ok"] = False
        info["smoke_test_message"] = "Бинарник отсутствует"

    # Список моделей
    models = get_installed_models()
    info["installed_models"] = [m["filename"] for m in models]

    return info


def run_diagnostics(export_zip: bool = True) -> str:
    """Запускает полную проверку, выводит отчет в консоль и упаковывает репорт."""
    print("\n=======================================================")
    print(f"      ScreenTale Doctor — Диагностика системы (v{APP_VERSION})")
    print("=======================================================\n")

    # 1. Сбор данных
    print("[1/4] Проверка операционной системы...")
    sys_env = check_system_environment()
    print(f"  -> ОС: {sys_env['os']}")
    has_vc = "✓ Найден" if sys_env["has_vcruntime140"] else "✗ НЕ НАЙДЕН (Нужен Visual C++)"
    print(f"  -> VCRuntime140.dll: {has_vc}")

    print("\n[2/4] Проверка графики и аппаратного ускорения...")
    hw = check_hardware_capabilities()
    print(f"  -> Видеокарта: {hw['gpu_name']}")
    if "vram_total_gb" in hw:
        print(f"  -> VRAM: {hw['vram_used_gb']} / {hw['vram_total_gb']} ГБ")

    cuda_ok, cuda_reason = hw["cuda12_supported"]
    cuda_status = "✓ Поддерживается" if cuda_ok else f"✗ {cuda_reason}"
    print(f"  -> CUDA 12: {cuda_status}")

    vulkan_ok, vulkan_reason = hw["vulkan_supported"]
    vulkan_status = "✓ Поддерживается" if vulkan_ok else f"✗ {vulkan_reason}"
    print(f"  -> Vulkan:  {vulkan_status}")

    print("\n[3/4] Проверка модулей OCR...")
    ocr = check_ocr_subsystems()
    win_ocr_langs = ", ".join(ocr.get("windows_ocr_languages", [])) or "Нет установленных языков!"
    win_status = "✓ Активен" if ocr["windows_ocr_available"] else "✗ Недоступен"
    print(f"  -> Windows OCR: {win_status} (Языки: {win_ocr_langs})")
    rapid_status = "✓ Готов" if ocr["rapidocr_available"] else "✗ Недоступен"
    print(f"  -> RapidOCR (ONNX): {rapid_status}")

    print("\n[4/4] Проверка рантайма локальной нейросети (llama.cpp)...")
    llama = check_llama_engine()
    print(f"  -> Установленный бэкенд: {llama['installed_backend']}")
    smoke_status = "✓ Пройден успешно" if llama["smoke_test_ok"] else f"✗ ОШИБКА: {llama['smoke_test_message']}"
    print(f"  -> Smoke Test (Запуск сервера): {smoke_status}")
    print(f"  -> Найдено GGUF-моделей: {len(llama['installed_models'])} шт.")

    # 2. Формирование сводного JSON файла
    report_dict = {
        "system": sys_env,
        "hardware": hw,
        "ocr": ocr,
        "llama": llama,
    }

    data_dir = get_data_dir()
    doctor_file = os.path.join(data_dir, "doctor_report.json")
    with open(doctor_file, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=4, ensure_ascii=False)

    zip_report_path = os.path.join(get_app_dir(), "ScreenTale_Report.zip")

    if export_zip:
        print("\n-------------------------------------------------------")
        print("Упаковка логов и отчета в ScreenTale_Report.zip...")

        files_to_pack = [
            (doctor_file, "doctor_report.json"),
            (os.path.join(data_dir, "app.log"), "app.log"),
            (os.path.join(data_dir, "app_1.log"), "app_1.log"),
            (os.path.join(data_dir, "llama_server.log"), "llama_server.log"),
            (CONFIG_PATH, "config.json"),
        ]

        with zipfile.ZipFile(zip_report_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path, arc_name in files_to_pack:
                if os.path.isfile(file_path):
                    try:
                        zf.write(file_path, arc_name)
                    except OSError:
                        pass

        print("\n[OK] Готово! Архив для отправки автору создан:")
        print(f"     -> {zip_report_path}")
        print("=======================================================\n")

    return zip_report_path


if __name__ == "__main__":
    run_diagnostics(export_zip=True)
    try:
        input("Нажмите [Enter], чтобы закрыть окно...")
    except (EOFError, OSError):
        pass