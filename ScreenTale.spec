# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# 1. Явно указываем скрытые библиотеки, которые импортируются внутри потоков/функций
hidden_imports = [
    'winocr',
    'rapidocr_onnxruntime',
    'onnxruntime',
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
    'pyperclip',
    'PIL',
    'PIL.Image',
    'PIL.ImageGrab',
]

# 2. Обязательные метаданные для HuggingFace и иконки приложения
datas = [
    ('ScreenTale.ico', '.'),
    ('logo.png', '.'),
    ('frontend/icons', 'frontend/icons'),
]
for pkg in ['transformers', 'huggingface_hub', 'tqdm', 'regex', 'requests', 'rapidocr_onnxruntime']:
    try:
        datas += collect_data_files(pkg)
        datas += copy_metadata(pkg)
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
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

# 3. Файлы, которые должны лежать строго в корне рядом с ScreenTale.exe (не в _internal)
root_files = [
    ('debug_diagnostics.py', 'debug_diagnostics.py', 'DATA'),
]

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    root_files,  # <--- Добавляем файл напрямую в корень папки сборки
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ScreenTale',
)