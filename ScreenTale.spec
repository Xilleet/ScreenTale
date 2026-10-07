# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

# 1. Скрытые модули (включая зависимости deep_translator и winrt)
hidden_imports = [
    'winocr',
    'winrt',
    'winrt.system',
    'winrt.windows.foundation',
    'winrt.windows.foundation.collections',
    'winrt.windows.globalization',
    'winrt.windows.graphics.imaging',
    'winrt.windows.media.ocr',
    'winrt.windows.storage.streams',
    'torch',
    'torchvision',
    'easyocr',
    'transformers',
    'huggingface_hub',
    'deep_translator',
    'bs4',                 
    'soupsieve',           
    'pyperclip',
    'PIL',
    'PIL.Image',
    'PIL.ImageGrab',
]

# 2. Обязательные ресурсы приложения
datas = [
    ('ScreenTale.ico', '.'),
    ('logo.png', '.'),
    ('frontend/icons', 'frontend/icons'),
    ('locales', 'locales'), # <--- Языковые файлы интерфейса (en.json, ru.json)
]
binaries = []

# 3. Полный сборщик для ONNX Runtime и OCR (собирает DLL, манифесты и данные)
for pkg in ['rapidocr_onnxruntime', 'onnxruntime']:
    try:
        p_datas, p_binaries, p_hidden = collect_all(pkg)
        datas += p_datas
        binaries += p_binaries
        hidden_imports += p_hidden
    except Exception:
        pass

# Метаданные для нейросетевых библиотек
for pkg in ['transformers', 'huggingface_hub', 'tqdm', 'regex', 'requests']:
    try:
        datas += collect_data_files(pkg)
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,       # <--- Передаём собранные C++ DLL онникса
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ScreenTale',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ScreenTale.ico'],
)

# Файлы, которые должны лежать строго в корне рядом с ScreenTale.exe
root_files = [
    ('debug_diagnostics.py', 'debug_diagnostics.py', 'DATA'),
]

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    root_files,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ScreenTale',
)