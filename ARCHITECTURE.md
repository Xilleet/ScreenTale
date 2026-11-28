# ScreenTale Architecture

This document describes the internal architecture of ScreenTale — an on-screen
AR companion for story-driven games and visual novels. It targets contributors:
it explains which layers the application is composed of, how the modules talk
to each other, and which extension points exist for adding new OCR engines,
translators and GGUF models.

> Russian version: [`ARCHITECTURE_ru.md`](ARCHITECTURE_ru.md).
> User-facing README: [`README.md`](README.md).

## Overview

ScreenTale is a Python desktop application (PySide6 / Qt 6) that reads text
from the game screen via OCR, translates it with a cloud or local engine,
and renders the translation as a translucent overlay. The application
operates **strictly through the Windows transparent-system-layer** — it does
not inject into the game's memory or touch DirectX / OpenGL, which makes it
safe with respect to anti-cheats.

The application has two equally important output modes:

* **Chat (`overlay_mode = "chat"`)** — a classic scrolling history window
  with a docked toolbar and a Ghost Mode (click-through).
* **In-Place (`overlay_mode = "inplace"`)** — a full-screen transparent
  `InPlaceCanvas` that draws translation plates over the original text,
  with background color matching ("Chameleon"), spatial rotation (`atan2`)
  and binary-search O(log N) font fitting.

OCR runs on three interchangeable strategy engines; translation runs on
five (Google, MyMemory, Opus-MT, NLLB-200, Qwen via `llama-server.exe`).
Local LLM translation launches the official `llama.cpp` runtime which the
application downloads and installs automatically (CUDA 12 / Vulkan / CPU
AVX2), with a mandatory Smoke Test before use.

## Package Structure

```
ScreenTale/
├── main.py                       # Entry point and AppController — orchestrator
├── requirements.txt              # PySide6, deep_translator, rapidocr, easyocr, winocr, transformers, torch
├── ScreenTale.spec               # PyInstaller spec (.exe build)
├── build.bat / check.bat / update_reqs.bat
├── logo.png / ScreenTale.ico     # Brand assets
├── debug_diagnostics.py         # System info collector for bug reports
├── test_suite.py                # Built-in OCR/translation self-test
│
├── locales/                      # UI localization JSON files
│   ├── en.json                   # Base (fallback) UI language
│   └── ru.json                   # Russian localization
│
├── backend/                      # Business logic (pure strategies have no Qt imports)
│   ├── __init__.py
│   ├── config.py                 # SettingsManager(QObject) — settings + debounced-save
│   ├── logging_setup.py          # setup_logging, _Tee (stdout/stderr → file+console), vlog
│   ├── i18n.py                   # load_locale, t(), detect_system_ui_lang, get_available_ui_languages
│   ├── languages.py              # 12-language registry: name, win_ocr, nllb, google, build_llm_system_prompt
│   ├── hotkeys.py                # HotkeyManager + _NativeFilter — RegisterHotKey via QAbstractNativeEventFilter
│   ├── window_tracker.py         # Win32 EnumWindows + DWM filtering, get_active_window, get_running_games
│   ├── ocr_engines.py            # OCR strategies: BaseOcrEngine, WindowsOcrEngine, RapidOcrEngine, EasyOcrEngine
│   ├── ocr.py                    # OcrWorker(QThread) — queue tasks, DPI scaling, normalization, fallback
│   ├── translators.py            # translate_online, ModelManager(QThread), _OpusTranslator, _NllbTranslator, NVML
│   ├── llama_server.py           # LlamaServerTranslator (subprocess), GGUF_CATALOG, GgufDownloadWorker, Job Object
│   ├── runtime_manager.py        # detect_best_backend, BACKENDS_CONFIG, RuntimeDownloadWorker, run_smoke_test
│   ├── auto_mode.py              # AutoModeWorker(QThread) — detect "frame changed and stabilized"
│   └── updater.py                # UpdateCheckTask(QRunnable), UpdateDownloadWorker(QThread), SHA-256
│
└── frontend/                    # Presentation layer (PySide6)
    ├── __init__.py
    ├── theme.py                  # PALETTES (dark/dark_classic/light), apply_theme, QSS with @-placeholders
    ├── widgets.py                # ToggleSwitch, SegmentedControl, HotkeyRecorder, StatusPill, BusyBar, Toast
    ├── screen_selector.py        # ScreenSelector — full-screen rectangle-selection overlay
    ├── inplace_canvas.py         # InPlaceCanvas — AR canvas, cluster_lines, atan2 rotation, Chameleon, Spring Pop-In
    ├── translate_window.py       # TranslateWindow (chat), FloatingToolbar, ResizeGrip, ghost mode, lang menu MRU
    └── settings_window.py        # SettingsWindow (5 tabs: General/Translation/Hotkeys/Advanced/About)
```

## Dependency Flow

Dependencies flow **strictly inward** — outer layers depend on inner ones,
never the reverse:

```
                    ┌────────────────────────────────────────────┐
                    │              main.py                        │  ← Entry point
                    │   AppController(QObject) — orchestrator     │
                    │   wires workers to windows and toolbar       │
                    └──────────────┬─────────────────────────────┘
                                   │
                    ┌──────────────▼─────────────────────────────┐
                    │              frontend/                       │  ← Presentation
                    │   SettingsWindow, TranslateWindow,           │
                    │   InPlaceCanvas, ScreenSelector, widgets     │
                    └──────────────┬─────────────────────────────┘
                                   │
                    ┌──────────────▼─────────────────────────────┐
                    │              backend/                        │  ← Business logic
                    │   ocr / translators / llama_server /          │
                    │   runtime_manager / auto_mode / hotkeys       │
                    └──────────────┬─────────────────────────────┘
                                   │
                    ┌──────────────▼─────────────────────────────┐
                    │   config / i18n / languages / logging_setup  │  ← Base services
                    └────────────────────────────────────────────┘
```

* `main.py` is the only layer that knows about both `frontend` and `backend`.
  `AppController` owns all workers and windows and connects their signals.
* `frontend/` — Qt windows and widgets. Subscribes to `SettingsManager`
  signals, displays worker progress via slots, never makes blocking calls.
* `backend/` — Qt workers (`QThread`) and pure strategies (`ABC`). The
  strategies (`ocr_engines`, `translators`) have no Qt imports in the base
  classes; Qt imports appear only in `OcrWorker` / `ModelManager` /
  `AutoModeWorker` (their signal declarations).
* Base services (`config`, `i18n`, `languages`, `logging_setup`) are leaf
  modules that everything else depends on; they themselves depend only on
  stdlib and (in `config.py`) Qt's `QObject`/`QTimer`/`Signal`.

## Module Map

| Module | Role | Key entities |
|--------|------|---------------|
| `main.py` | Entry point, orchestrator | `AppController`, `_OnlineTask(QRunnable)`, `parse_structured_translation` |
| `backend/config.py` | Settings + paths | `SettingsManager`, `DEFAULTS`, `get_app_dir`, `get_data_dir`, `APP_VERSION = "0.7-beta"` |
| `backend/logging_setup.py` | Logging | `setup_logging`, `_Tee`, `vlog`, `set_verbose` |
| `backend/i18n.py` | UI localization | `load_locale`, `t()`, `detect_system_ui_lang`, `get_available_ui_languages` |
| `backend/languages.py` | Language registry | `LANGUAGES` (12), `get_win_ocr_tag`, `get_google_code`, `get_nllb_code`, `build_llm_system_prompt` |
| `backend/hotkeys.py` | Global hotkeys | `HotkeyManager`, `_NativeFilter`, WM_HOTKEY |
| `backend/window_tracker.py` | Win32 window tracking | `get_running_games`, `get_active_window`, `get_window_exact_rect`, `WindowInfo` |
| `backend/ocr_engines.py` | OCR strategies | `BaseOcrEngine`, `WindowsOcrEngine`, `RapidOcrEngine`, `EasyOcrEngine` |
| `backend/ocr.py` | OCR worker | `OcrWorker(QThread)`, `normalize_ocr_text`, `get_screen_scale`, `_preprocess_for_ocr` |
| `backend/translators.py` | Translators + model cache | `translate_online`, `ModelManager(QThread)`, `_OpusTranslator`, `_NllbTranslator`, `get_vram_info`, `_DownloadTracker`, `_ProgressTqdm` |
| `backend/llama_server.py` | llama.cpp wrapper + GGUF catalog | `LlamaServerTranslator`, `GGUF_CATALOG`, `GgufDownloadWorker`, `_assign_to_kill_job` |
| `backend/runtime_manager.py` | Runtime installation | `detect_best_backend`, `BACKENDS_CONFIG`, `RuntimeDownloadWorker`, `run_smoke_test`, `kill_llama_server` |
| `backend/auto_mode.py` | Auto-scanning | `AutoModeWorker(QThread)` |
| `backend/updater.py` | Auto-update | `UpdateCheckTask(QRunnable)`, `UpdateDownloadWorker(QThread)`, `calculate_sha256` |
| `frontend/theme.py` | Palettes + QSS | `PALETTES`, `PALETTE`, `apply_theme`, `build_qss` |
| `frontend/widgets.py` | Base widgets | `ToggleSwitch`, `SegmentedControl`, `HotkeyRecorder`, `StatusPill`, `BusyBar`, `Toast`, `GlowingPinButton` |
| `frontend/screen_selector.py` | Selection overlay | `ScreenSelector` |
| `frontend/inplace_canvas.py` | In-Place AR canvas | `InPlaceCanvas`, `InPlaceBlock`, `cluster_lines`, `sample_background_color`, `find_optimal_font_size` |
| `frontend/translate_window.py` | Chat window + toolbar | `TranslateWindow`, `FloatingToolbar`, `_GripButton`, `ResizeGrip`, `CustomTextEdit`, `_NoticeToast` |
| `frontend/settings_window.py` | Settings window | `SettingsWindow`, `_UpdateBanner` |

## Application Lifecycle

`main.py` is the entry point. Initialization order:

1. **`setup_logging(get_data_dir())`** is called BEFORE all heavy imports,
   so that any `print()` from any library lands in `data/app.log`.
   `sys.stdout` and `sys.stderr` are replaced with `_Tee`, which writes
   to both the real stream and the file; `faulthandler` catches native
   crashes.
2. Import `pyperclip`, `PySide6`, all `backend.*` and `frontend.*`.
3. Create `QApplication`, install `QLockFile` (prevents two running copies).
4. Construct `AppController(app)` — its `__init__` does the rest of the work:
   * create `SettingsManager` and initialize UI language
     (`detect_system_ui_lang()` via `GetUserDefaultUILanguage` if `ui_lang = "auto"`);
   * apply the theme (`apply_theme(app, settings.get("theme"))`);
   * register global hotkeys (`hotkeys.apply_all(...)`);
   * create `TranslateWindow`, `InPlaceCanvas`, `SettingsWindow`;
   * if `translator == "qwen"`, kill any zombie `llama-server.exe` from
     previous crashes (`kill_llama_server()`);
   * start `OcrWorker` and `ModelManager` (`QThread.start()`);
   * if a local engine is selected and its model is cached, enqueue
     `model_manager.load(...)` into the worker's task queue;
   * install `_focus_timer` (200 ms) to track Alt+Tab — hide the overlay
     when the pinned game loses focus;
   * schedule `QTimer.singleShot(3000, _check_updates)` for a background
     `manifest.json` check against GitHub;
   * if this is the first run (`settings.is_first_run`), open
     `SettingsWindow`; otherwise show a tray toast.

5. The application enters `app.exec()`. Everything from here is signal-driven.

### "Alt+Q → OCR → Translate → Render" flow in In-Place mode

```
Alt+Q (RegisterHotKey) ──▶ _NativeFilter.nativeEventFilter
                              │  WM_HOTKEY (0x0312)
                              ▼
                       HotkeyManager.triggered("single")
                              │  Qt Signal
                              ▼
                       AppController._on_hotkey("single")
                              │
                       ┌──────▼───────────────────────────────────────────┐
                       │ Inplace mode?                                      │
                       │   yes → get_active_window() → get_window_exact_rect │
                       │         → ocr.read(bbox, context="single")         │
                       │   no  → ScreenSelector → callback(bbox)             │
                       │         → ocr.read(bbox)                            │
                       └──────┬────────────────────────────────────────────┘
                              ▼
                  OcrWorker._do_read(bbox, context)
                  ├─ get_screen_scale(bbox) → devicePixelRatio
                  ├─ ImageGrab.grab(physical_bbox, all_screens=True)
                  ├─ _preprocess_for_ocr(img) → Lanczos upscale
                  ├─ engine.read_detailed(img) → (text, raw_blocks)
                  ├─ normalize_ocr_text(text)  → text
                  └─ remap block coordinates to screen pixels
                              │  Signal read_result(bbox, text, blocks, ctx)
                              ▼
                  AppController._on_read_result
                  ├─ context == "point_lookup" → nearest cluster under cursor
                  ├─ context == "auto"         → compare with previous frame, auto-translate
                  └─ otherwise                 → enqueue translation into QThreadPool / ModelManager
                              │
                              ▼
                  translate_online (Google) OR ModelManager.translate (local)
                              │  Signal translation_ready(seq, text)
                              ▼
                  AppController._apply_translation_result
                              │  Route by overlay_mode
                              ▼
                  ┌───────────────────────────┐
                  │ mode == "chat"             │  → TranslateWindow.show_translation(text)
                  │ mode == "inplace"          │  → InPlaceCanvas.display_translation(blocks, text)
                  └───────────────────────────┘
```

`InPlaceCanvas.display_translation` calls `cluster_lines(blocks)`, splits
the translation across the clusters, and renders each as an independent
plate with `sample_background_color` (Chameleon), `find_optimal_font_size`
(binary search O(log N)) and `atan2` rotation computed from the first
polygon edge.

## Concurrency Model (Threading and Qt)

All heavy work runs in `QThread` workers. The GUI thread never blocks on
OCR / translation / downloads. The worker template:

```python
class XxxWorker(QThread):
    progress = Signal(int, str)
    finished = Signal(...)

    def __init__(self, ...):
        self._tasks: queue.Queue = queue.Queue()
        self._stop_flag = False

    # Public API (thread-safe — called from GUI thread)
    def do_something(self, arg):
        self._tasks.put(("do_something", arg))

    # Internal loop (worker thread)
    def run(self):
        while not self._stop_flag:
            try:
                task = self._tasks.get(timeout=0.3)
            except queue.Empty:
                continue
            kind = task[0]
            if kind == "stop":
                break
            if kind == "do_something":
                self._do_do_something(task[1])
```

### Active QThread workers

| Worker | File | Thread-safe API | Signals to GUI thread |
|--------|------|-----------------|-----------------------|
| `OcrWorker` | `backend/ocr.py` | `read(bbox, ctx)`, `request_engine(name)`, `request_language(lang)`, `request_direction(d)`, `request_gpu(bool)`, `stop()` | `state_changed(str,str)`, `read_result(tuple,str,list,str)`, `gpu_result(bool,bool,str)`, `engine_changed(str)` |
| `ModelManager` | `backend/translators.py` | `load(engine_id, ...)`, `unload()`, `translate(text, seq, src, dst)`, `set_device(bool)`, `stop()` | `progress_started()`, `progress(int,str)`, `ready(str)`, `failed(str,str)`, `translation_result(int,str)` |
| `AutoModeWorker` | `backend/auto_mode.py` | `requestInterruption()` (Qt) | `region_changed()` |
| `RuntimeDownloadWorker` | `backend/runtime_manager.py` | `start_download()`, `cancel()` | `progress(int,str)`, `finished(str)`, `failed(str,str)` |
| `GgufDownloadWorker` | `backend/llama_server.py` | `start_download()`, `cancel()` | `progress(int,str)`, `finished(str)`, `failed(str,str)` |
| `UpdateDownloadWorker` | `backend/updater.py` | `start_download()` | `progress(int,str)`, `finished()`, `failed(str)` |

In addition:

* `UpdateCheckTask(QRunnable)` is a short background task on
  `QThreadPool.globalInstance()` (HTTP request for `manifest.json`).
* `_OnlineTask(QRunnable)` runs online translation through `QThreadPool`
  for concurrent processing of multiple requests.
* `_DownloadTracker` + `_ProgressTqdm` (a tqdm subclass) aggregate progress
  across all files of a HuggingFace repository into a single percent value
  (see "Translation Pipeline" for details).
* The `FloatingToolbar` is a separate top-level window (`Qt.Tool`), **not**
  a Qt child of `TranslateWindow`; they communicate via regular signals,
  not via parent/child.

### Per-key debounce

`SettingsManager._save_timer` (`QTimer`, single-shot, 500 ms): every
`set(key, value)` arms the timer instead of writing immediately. The actual
disk write happens only after the thread has been quiet for half a
second. `force_save()` is a synchronous write used on application exit.

`AppController._auto_timer` is a single-shot timer on `auto_delay_ms`
(default 800 ms) between `AutoModeWorker.region_changed` and the actual
OCR trigger, to avoid spamming translations on frame jitter.

`SettingsWindow._vram_timer` is a repeating 3000 ms timer that polls VRAM
through `get_vram_info()` (NVML); it is active only while the settings
window is visible (`showEvent`/`hideEvent`).

### Worker → GUI signal/slot chain

All workers emit signals with typed arguments; `AppController` connects
them to slots on `SettingsWindow` and `TranslateWindow`. Because workers
live in a different thread, Qt uses `AutoConnection` → `QueuedConnection`
under the hood — the slot is invoked in the GUI thread, so no manual
`QMetaObject.invokeMethod` is needed.

## OCR Pipeline (Engines and Strategies)

### Base interface

`backend/ocr_engines.py` defines `BaseOcrEngine(ABC)`:

```python
class BaseOcrEngine(ABC):
    engine_id: str
    display_name: str

    @abstractmethod
    def is_available(self) -> bool: ...
    @abstractmethod
    def load(self, use_gpu: bool = False, lang: str = "en") -> bool: ...
    @abstractmethod
    def read(self, img: Image.Image) -> str: ...

    def read_detailed(self, img: Image.Image) -> tuple[str, list[dict]]:
        """Returns (full_text, [ {"text", "rect": (x,y,w,h), "polygon": [[x,y]...]} ])

        rect and polygon are in local coordinates of the original image
        (before preprocessing/upscale).
        """
        # Default implementation wraps read() into a single block over the whole image.

    def get_installed_languages(self) -> list[str]: ...
    def check_language_support(self, lang: str) -> tuple[bool, str]: ...
```

Concrete strategies:

| Class | `engine_id` | Technology | When to use |
|-------|-------------|------------|-------------|
| `WindowsOcrEngine` | `"windows"` | WinRT `OcrEngine` (via `winocr`) | Default; 10–25 ms, 0 MB VRAM, requires a Windows OCR language pack |
| `RapidOcrEngine` | `"rapidocr"` | `rapidocr_onnxruntime` (PaddleOCR on ONNX) | Stylized fonts, CJK glyphs, vertical text (Tategaki) |
| `EasyOcrEngine` | `"easyocr"` | `easyocr` on PyTorch | Last resort; slower than the others, but does not require Windows language packs |

Each strategy implements `read_detailed` differently:

* **WindowsOcrEngine** calls `winocr.recognize_pil_sync(img, lang)`,
  merges the words of each line into a `bounding_rect` (min by X/Y,
  max by X+W/Y+H) and builds a 4-point polygon.
* **RapidOcrEngine** runs `result, _ = engine(img_np)`, where each
  item is `[box, text, score]` and `box` is a 4-point polygon. When
  `direction == "vertical"` it additionally runs `_sort_tategaki`:
  sort by `cx` (right-to-left column order), group into columns
  (`abs(cx - col_avg_x) < col_avg_w * 0.7`), within a column sort by
  `cy` (top to bottom). Columns are joined with `"\n"`.
* **EasyOcrEngine** runs `reader.readtext(np.array(img), detail=1)`
  and converts each box to `(x1, y1, w, h)` plus polygon.

### Worker

`OcrWorker(QThread)` in `backend/ocr.py` wraps the strategies with
production-grade logic:

1. **Frame capture** with DPI awareness: `get_screen_scale(bbox)` queries
   `QApplication.screenAt(QPoint(center)).devicePixelRatio()`. This
   solves the 4K 150% + 1080p 100% multi-monitor setup. `physical_bbox`
   — multiplied by `dpi_scale` — is passed to
   `ImageGrab.grab(bbox=physical, all_screens=True)`.
2. **Preprocessing**: `_preprocess_for_ocr(img)` — if the frame is
   smaller than 40×40 (WinRT minimum), upscale with
   `Image.Resampling.LANCZOS` by `max(40/w, 40/h) * 1.2`. Otherwise
   adaptive upscale for small fonts (×2.0 when `h ≤ 120`, ×1.5 when
   `h ≤ 250`).
3. **Recognition** with `time.perf_counter()` timing.
4. **Text normalization** — `normalize_ocr_text(text)`:
   * merge hyphen-broken words (`infor-\nmation` → `information`);
   * single `\n` → space, double `\n\n` (paragraphs) preserved;
   * normalize quotes / apostrophes / dashes to ASCII;
   * fix the common OCR error `O→0` in numbers (`2O24` → `2024`);
   * collapse repeated whitespace.
5. **Coordinate remap** from local (after upscale) to absolute screen
   pixels: `total_scale = prep_scale * dpi_scale`,
   `abs_x = left + rx / total_scale`.

### Engine fallback chain

`_init_engine(engine_name, use_gpu)` implements the priority fallback:

```
windows ──(language pack missing OR WinRT unavailable)──▶ rapidocr ──(library not importable)──▶ easyocr
```

The same fallback triggers automatically on language change
(`_do_set_language`): if no Windows OCR pack exists for the selected
language, `OcrWorker` switches to `RapidOcrEngine` without restarting
the application and emits `engine_changed("rapidocr")`.

### Language support cache

`_lang_cache: dict[str, bool]` caches results of
`WinrtOcrEngine.is_language_supported()` under `_winrt_lock` (an
`RLock` — WinRT is not thread-safe). `reset_ocr_lang_cache(tag)` is
called from `SettingsWindow` when the user clicks "Recheck status"
after installing the OCR pack.

## Translation Pipeline

### Online engines

`backend/translators.py:translate_online(engine, text, src_lang, dst_lang)`
is a blocking call (run from a `QThreadPool` task `_OnlineTask` in
`main.py`):

```python
def translate_online(engine, text, src_lang="auto", dst_lang="ru"):
    import deep_translator
    src = get_google_code(src_lang) if src_lang != "auto" else "auto"
    dst = get_google_code(dst_lang)
    for attempt in range(2):                # one retry
        try:
            if engine == ENGINE_MYMEMORY:
                # MyMemory does not accept "auto" — substitute "en"
                return deep_translator.MyMemoryTranslator(
                    source="en" if src == "auto" else src, target=dst).translate(text)
            return deep_translator.GoogleTranslator(source=src, target=dst).translate(text)
        except Exception as e:
            last_error = e
            if attempt == 0:
                time.sleep(1.5)
    raise last_error
```

`is_network_error(text)` is a heuristic for auto-fallback to a local
model: it scans the error message for markers like `"connection"`,
`"timeout"`, `"429"`, `"rate limit"`, etc.

### Local models

`ModelManager(QThread)` is the single owner of local models in the
application. A `"load"` task switches the engine:

| `engine_id` | What gets loaded | Where it lives |
|-------------|-------------------|----------------|
| `ENGINE_OPUS = "opus"` | `Helsinki-NLP/opus-mt-en-ru` (transformers, ~300 MB) | `data/hf_cache/hub/models--Helsinki-NLP--opus-mt-en-ru/` |
| `ENGINE_NLLB = "nllb"` | `facebook/nllb-200-distilled-600M` (~2.5 GB) | `data/hf_cache/hub/...` |
| `ENGINE_QWEN = "qwen"` | GGUF via `llama-server.exe` | `data/models/*.gguf` + `llama/llama-server.exe` |

For Opus / NLLB the download runs in a **separate background thread**
(`_download_bg`) — otherwise `snapshot_download` from `huggingface_hub`
would block the worker's queue. After the download completes, a
`"load_weights"` task is put back into the `ModelManager` queue
(thread-safe), so PyTorch initialization runs in the correct thread.

Progress tracking is via `_ProgressTqdm(tqdm_base)`: `huggingface_hub`
accepts our tqdm subclass in `snapshot_download(tqdm_class=_ProgressTqdm)`,
which delegates to `_DownloadTracker`. The tracker sums bytes across all
files of the repository into a single percent value with a smoothed speed
(EMA `0.7 × old + 0.3 × new`).

### Local translators

`_OpusTranslator` wraps a `transformers` Opus-MT model. **Supports only
EN → RU**; any other pair returns an informative message. Long text is
split into sentences (`_split_into_sentences` by `. ! ? …`) and
translated one sentence at a time (the `max_length=512` tokens limit).

`_NllbTranslator(_OpusTranslator)` overrides `translate`, using
Flores-200 codes (`get_nllb_code`) and `forced_bos_token_id` for the
target language. Works any-to-any for the 12 languages registered in
`LANGUAGES`.

`LlamaServerTranslator` in `backend/llama_server.py` — see the next section.

### VRAM monitoring

`get_vram_info() -> tuple[int, int, str] | None` returns
`(used_bytes, total_bytes, gpu_name)`:

1. First via `ctypes.WinDLL("nvml.dll")` (the native NVIDIA driver, no
   torch dependency). `nvmlInit_v2()` is called exactly once per session
   (`_nvml_initialized`); `nvmlShutdown` is NOT called, to avoid
   breaking concurrent VRAM monitoring in
   `SettingsWindow._update_vram_monitor`.
2. Fallback: `torch.cuda.mem_get_info()`, but only if `torch` is already
   imported in the process (to avoid loading it just for one query).

### Local engine selection fallback

`get_available_offline_engine(src, dst, preferred="opus")` is a smart
choice:

* EN → RU: if `opus` is cached, use it (the fastest, ~300 MB).
* Any other direction: `opus` is useless, look for `qwen` (llama.cpp),
  then `nllb`.

## Runtime Manager (llama.cpp)

`backend/runtime_manager.py` is responsible for installing the official
`llama.cpp` build for the user's hardware.

### Pinned version

```python
LLAMA_PINNED_TAG = "b11224"   # a specific verified llama.cpp tag
GITHUB_RELEASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_PINNED_TAG}/"
```

`BACKENDS_CONFIG` describes three options:

| ID | Title | Files | Target hardware |
|----|-------|-------|-----------------|
| `cuda12` | NVIDIA CUDA 12.4 | `llama-b11224-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip` | NVIDIA GeForce GTX/RTX |
| `vulkan` | Vulkan | `llama-b11224-bin-win-vulkan-x64.zip` | AMD Radeon, Intel Arc/Iris |
| `cpu` | CPU AVX2 | `llama-b11224-bin-win-cpu-x64.zip` | Any modern CPU |

### Auto-detection

`detect_best_backend()` checks the hardware in three steps:

1. **NVML** (`ctypes.WinDLL("nvml.dll").nvmlInit_v2() == 0`) — NVIDIA
   driver is present → `cuda12`. NOTE: `nvmlShutdown` is intentionally
   NOT called, to keep the VRAM monitor in `translators.py` working.
2. **Vulkan** (`ctypes.WinDLL("vulkan-1.dll")`) — system Vulkan driver
   is present → `vulkan`.
3. Fallback → `cpu`.

`is_backend_supported(backend_id)` is the same check, but for a
specific ID — used by `SettingsWindow` to disable buttons for backends
that don't exist on the user's machine.

### Smoke Test

`run_smoke_test()` runs `llama-server.exe --version` with
`creationflags=_CREATE_NO_WINDOW = 0x08000000` (no console window) and
`timeout=6`. Based on the return code it produces a human-readable
message:

* `0xC0000135` (`3221225781`) — Visual C++ Redistributable not found;
* `0xC000001D` (`3221225501`) — the CPU does not support this binary's
  AVX2 instructions;
* `0` — Smoke Test passed, the runtime is usable.

### Installation

`RuntimeDownloadWorker(QObject)`:

1. `clean_llama_dir()` — `taskkill /F /IM llama-server.exe /T`, then
   full removal of all files in `llama/`.
2. For each file in `BACKENDS_CONFIG[backend]["files"]`: download in
   256 KB blocks via `urllib.request.urlopen(req, timeout=30)`, emit
   progress via `Signal(int, str)`.
3. Unpack `zipfile.ZipFile.extractall(get_llama_dir())`.
4. `run_smoke_test()`. On failure emit `failed(backend_id, "Smoke
   Test failed: ...")`; the files are kept for manual diagnostics.
5. On success — write a marker to `llama/.backend` (future runs detect
   the type in O(1), without scanning DLLs).

`get_installed_backend()` is a fallback for manual installation: if
the marker is missing, the type is inferred from the presence of
`ggml-cuda.dll` / `ggml-vulkan.dll` and the marker is written.

### Launching llama-server

`backend/llama_server.py:LlamaServerTranslator`:

* `_free_port()` — `socket.socket().bind(("127.0.0.1", 0))` → a free
  port.
* `_build_cmd(model)`:
  ```
  llama-server.exe -m <model.gguf> -ngl 99 (GPU) | 0 (CPU)
                   -c 2048 -np 1
                   -t <threads = max(1, cpu_count//2 - 1)>
                   --host 127.0.0.1 --port <free_port>
  ```
* `subprocess.Popen(cmd, ..., creationflags=_CREATE_NO_WINDOW)`.
* `_assign_to_kill_job(proc)` — a Windows Job Object with the flag
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000`:
  if `ScreenTale.exe` crashes or is killed via Task Manager, the Job
  Object closes and the kernel automatically kills `llama-server.exe`
  — **protection against zombie processes** that would otherwise hold
  2–8 GB of VRAM.
* `start(timeout=180.0)` — waits for `/health` to return HTTP 200 in a
  loop (sleeping 0.4 s between attempts). After 200 OK it measures the
  cold-start time of the LLM in VRAM via `time.perf_counter()` and
  writes it to the log.
* `translate(text, src, dst)` — POST to `/v1/chat/completions` with
  `temperature=0.2`, `max_tokens=512`, `stream=False`, system prompt
  from `build_llm_system_prompt` (preserve `[1] [2] ...` numbering,
  auto-fix OCR glitches, keep the character's tone, output only the
  translation).
* `atexit.register(self.close)` — guaranteed cleanup on exit.

### GGUF catalog

`GGUF_CATALOG` is a hard-coded list of verified models:

| ID | Filename | Title | Badge | Size |
|----|----------|-------|-------|------|
| `hy-mt2-1.8b` | `hy-mt2-1.8b-q4_k_m.gguf` | Hy-MT2 1.8B (Tencent) | ⚡ Weak PC / Laptop | ~1.2 GB |
| `qwen2.5-7b` | `qwen2.5-7b-instruct-q4_k_m.gguf` | Qwen 2.5 7B (Instruct) | 👑 Maximum quality | ~4.5 GB |
| `sakura-4b` | `sakura-4b-q4_k_m.gguf` | Sakura 4B (Galgame / VN) | 🌸 Japanese VNs / Manga | ~2.5 GB |

Each model is a direct HuggingFace link (no intermediary redirects).
`GgufDownloadWorker(QObject)` downloads to a temporary `.part` file,
then atomically renames on success.

## In-Place AR Canvas

`frontend/inplace_canvas.py:InPlaceCanvas(QWidget)` is a full-screen
transparent layer on top of the desktop and games. It performs native
text replacement right in the game frame.

### Window parameters

```python
Qt.WindowType.FramelessWindowHint
| Qt.WindowType.WindowStaysOnTopHint
| Qt.WindowType.Tool
WA_TranslucentBackground = True
WA_NoSystemBackground = True
WA_TransparentForMouseEvents = True   # mouse passes through
```

Geometry is `QApplication.primaryScreen().virtualGeometry()` (the
virtual desktop spanning all monitors).

### Click-through (HTTRANSPARENT)

`nativeEvent` intercepts `windows_generic_MSG`:

```python
if msg.message == 0x0084:  # WM_NCHITTEST
    return True, -1   # HTTRANSPARENT — the click falls through
```

This is the architectural cornerstone: ScreenTale **does not inject into
the game and does not hook DirectX/OpenGL**. All clicks on the canvas
area fall through to the game. Anti-cheats do not see ScreenTale as an
injection — because there is none.

### Capture protection (Stream Mode)

`set_capture_visibility(visible_to_capture)` toggles the Win32
`SetWindowDisplayAffinity`:

* `WDA_NONE = 0x00000000` — visible to ShareX / OBS;
* `WDA_EXCLUDEFROMCAPTURE = 0x00000011` — **hidden from screenshots**,
  but visible on the user's physical monitor.

`SettingsWindow` emits `settings.changed("streamer_mode", bool)` →
`AppController` calls
`inplace_canvas.set_capture_visibility(not streamer_mode)`. The
streamer sees the plates; OBS / Discord / ShareX do not.

### Block clustering

`cluster_lines(blocks, max_v_gap_ratio=1.15)` is the main heuristic of
In-Place mode. Input is a list of `{"text", "rect", "polygon"}` from
`OcrWorker.read_result`. The algorithm:

1. **Noise filter** — drops single-character OCR artifacts
   (`"i"`, `"e"`, `"o"`, `"\\"`), except dialogue punctuation
   (`"!"`, `"?"`, `"—"`, `"-"`).
2. **Sort** — top-to-bottom by Y, then left-to-right by X.
3. **Grouping** — for each block, find an existing cluster that
   satisfies one of two rules:
   * **same_line** (same horizontal line): vertical overlap ≥ 0.6 ×
     min height AND horizontal gap ∈ `[-5, 2 × avg_h]`;
   * **next_line** (next line of the same paragraph/bubble):
     `-0.2 × avg_h ≤ v_gap ≤ max_v_gap_ratio × avg_h`,
     horizontal overlap ≥ 45% of min width,
     cumulative cluster height ≤ 140 px.
4. **Cap at 12 plates** — if there are more clusters, keep the 12 with
   the longest text, then restore natural reading order by Y.

### Chameleon background sampling

`sample_background_color(rect: QRect) -> QColor` —
`ImageGrab.grab(bbox=rect, all_screens=True)`, reads 4 pixels at 3 px
inset from each corner, averages RGB, alpha 240/255 (a matte backdrop).

### Binary-search font fitting O(log N)

`find_optimal_font_size(text, max_w, max_h, min_sz=9, max_sz=40)` —
binary search. At each step:
`QFontMetrics(QFont("Segoe UI", mid, Bold)).boundingRect(QRect(0, 0,
max_w, 0), TextWordWrap, text)`. If width and height fit — `mid` works,
go up; otherwise go down. Returns the maximum font size that fits.

### Spatial rotation (atan2)

In `display_structured_translation` each cluster computes a text
rotation angle from the first polygon edge:

```python
poly = c["blocks"][0]["polygon"]
dx, dy = poly[1][0] - poly[0][0], poly[1][1] - poly[0][1]
angle = math.degrees(math.atan2(dy, dx))
if abs(angle) >= 1.5:
    paint_angle = angle
```

In `paintEvent` for each plate: `p.translate(cx, cy)`,
`p.rotate(b.angle)`, draw a local `QRect(-w//2, -h//2, w, h)`,
`p.restore()`. The text rotates together with the canvas.

### Animations

* Spring Pop-In: `QPropertyAnimation(self, b"windowOpacity")`,
  180 ms, `OutCubic`, 0.0 → 1.0.
* Fade-out on right-click: 120 ms, 1.0 → 0.0, on `finished` →
  `self.clear()`.
* Auto-hide after 12 s: `_fade_timer (single-shot, 12000) →
  fade_out_and_clear`.

### Point translation (middle mouse button)

`_mouse_timer (25 ms)` polls `GetAsyncKeyState`:

* `VK_RBUTTON (0x02)` → `fade_out_and_clear` (instantly dismiss plates).
* `VK_MBUTTON (0x04)` — debounce via `_was_m_down`, emit
  `point_translate_requested(QPoint)` → `AppController` triggers an OCR
  of the whole area under the cursor, `cluster_lines` finds the nearest
  cluster, translates only that one.

## Auto Mode

`backend/auto_mode.py:AutoModeWorker(QThread)` detects frame changes
inside a fixed region.

Algorithm (`run()`):

1. `_grab()` — `ImageGrab.grab(physical_bbox, all_screens=True)` with
   DPI awareness, returns `np.ndarray`.
2. **First frame** or region size change → `region_changed.emit()`,
   store as `last_processed` and `prev`.
3. Compute `diff_last = mean(|img - last_processed|)` — if
   `< DIFF_THRESHOLD = 0.5`, the frame has not changed, sleep
   `POLL_DELAY = 0.4 s`.
4. If it has changed, compute `diff_prev = mean(|img - prev|)` — if
   `>= DIFF_THRESHOLD`, the frame is still jittering, update `prev`,
   wait.
5. **Frame changed and stabilized** → `region_changed.emit()`,
   `last_processed = prev = img`.

`_sleep(seconds)` is a granular sleep with `SLICE = 0.05 s` that checks
`isInterruptionRequested()` so the thread can be stopped within one
slice.

`AppController` catches `region_changed`, restarts `_auto_timer` on
`auto_delay_ms` (default 800 ms), and on timeout calls
`ocr.read(bbox, context="auto")`. This produces the "showed the player
a dialogue → frame stabilized for 800 ms → translate" cadence.

## Global Hotkeys

`backend/hotkeys.py:HotkeyManager(QObject)` is a thin wrapper around
Win32 `RegisterHotKey`. **Does not require admin rights** and does not
intercept input (unlike the `keyboard` library).

Registration:

```python
user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk)
```

`MOD_NOREPEAT = 0x4000` — protection against auto-repeat while the key
is held.

Delivery into Qt is via `_NativeFilter(QAbstractNativeEventFilter)`:

```python
def nativeEventFilter(self, event_type, message):
    if event_type == b"windows_generic_MSG":
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY:   # 0x0312
            self._manager._on_wm_hotkey(int(msg.wParam))
    return False, 0
```

`_on_wm_hotkey` maps `hotkey_id → action` and emits
`triggered(action)`. `AppController._on_hotkey(action)` routes by
action: `toggle_window` / `single` / `auto` / `pause` / `stop` /
`clear` / `ghost`.

`HotkeyRecorder` (`frontend/widgets.py`) is a record button: click →
listening mode for Qt keys; Esc cancels; lost focus cancels; a captured
combination emits `sequenceCaptured(label, native_mods, native_vk)`. It
rejects bare letters/digits (requires Ctrl/Alt/Win or F1..F24 or `` ` ``).

## Win32 Window Tracking

`backend/window_tracker.py` filters games "Discord/OBS-style": only
real applications, no system noise.

`get_running_games()`:

1. `EnumWindows` + a `WNDENUMPROC` callback.
2. For each HWND check:
   * `IsWindowVisible` and not `IsIconic` (skip minimized windows);
   * `GetWindow(hwnd, GW_OWNER) == 0` (only main windows, not child
     dialogs);
   * `WS_EX_TOOLWINDOW` without `WS_EX_APPWINDOW` (overlays) — skip;
   * `DwmGetWindowAttribute(DWMWA_CLOAKED)` — hidden Windows 10/11
     UWP processes (running in the background) — skip;
   * empty title — skip;
   * `.exe` in `SYSTEM_EXCLUDES` (explorer, nvidia overlay, rtss,
     gamebar, taskmgr, **screentale.exe**, python.exe, cmd, powershell,
     conhost) — skip;
   * window smaller than 160×160 px — skip.
3. **Deduplication** — for one `.exe`, keep only the largest window by
   area.

`get_window_exact_rect(hwnd)` — `DwmGetWindowAttribute(
DWMWA_EXTENDED_FRAME_BOUNDS)` — strips Windows 10/11 system shadows
(which `GetWindowRect` includes).

`get_window_exe_path(hwnd)` — `OpenProcess(0x1000)` +
`QueryFullProcessImageNameW` — the full path to the `.exe` (used for
icon extraction in `SettingsWindow._show_game_picker_menu`).

## Settings and i18n

### SettingsManager

`backend/config.py:SettingsManager(QObject)` is the single source of
truth for settings.

* **Storage** — `data/config.json` (UTF-8, indent=4).
* **API** — `get(key, default)`, `set(key, value, save=True)`,
  `set_hotkey(action, label, mods, vk)`, `set_language_pair(src, dst)`,
  `reset_defaults(keys)`.
* **Signal** — `changed = Signal(str, object)` is emitted on every
  `set`. Subscribers: `SettingsWindow._on_setting_changed`,
  `TranslateWindow._on_setting_changed`,
  `AppController._on_setting_changed`.
* **Debounced save** — `save()` arms `_save_timer (500 ms)`, the
  actual write happens in the `_do_save` slot. `force_save()` is a
  synchronous write (called on application exit).
* **Migration** — `_OLD_TRANSLATOR_MAP` renames v0.3.2 names
  (`"Google"` → `"google"`, `"Локально (Качественно - 2.5ГБ)"` →
  `"nllb"`). Valid set — `_VALID_TRANSLATORS`.
* **`is_first_run`** — `not os.path.exists(CONFIG_PATH)`; if True,
  `AppController` opens `SettingsWindow` instead of showing a tray
  toast.

`DEFAULTS` defines the `config.json` schema (all keys with defaults).
Schema changes are backward-compatible: unknown keys in an existing
file are preserved, missing keys fall back to `DEFAULTS`.

### UI localization

`backend/i18n.py`:

* `load_locale(code)` loads `en.json` as `_fallback_translations`, then
  `code.json` as `_translations`. If the target file is missing,
  `_translations = _fallback_translations` (a clean fallback).
* `t(key, default="")` looks up in current → fallback → default → key.
* `detect_system_ui_lang()` — `GetUserDefaultUILanguage() & 0x3FF`:
  Russian (`0x19`) / Ukrainian (`0x22`) / Belarusian (`0x23`) → `"ru"`,
  otherwise `"en"`.
* `get_available_ui_languages()` scans `locales/` and returns
  `[(code, name), ...]`. Custom languages are added by simply dropping
  `<code>.json` into `locales/` — the UI picks them up automatically
  (each file should contain `_language_name`).

### Translation languages

`backend/languages.py:LANGUAGES` is the registry of 12 "golden"
languages with cross-engine mappings:

```python
"ja": {
    "name": "Японский",
    "name_en": "Japanese",
    "flag": "🇯🇵",
    "win_ocr": "ja-JP",   # BCP-47 for Windows OCR
    "nllb":   "jpn_Jpan", # Flores-200 for NLLB
    "google": "ja",       # code for deep_translator
}
```

`build_llm_system_prompt(src, dst)` dynamically generates the system
prompt for `llama-server` with instructions: "concise, compact, punchy",
preserve `[1] [2] ...` numbering, auto-fix OCR glitches, keep the
character's tone, output only the translation.

## Security

### Transparent-system-layer (Zero-Risk for anti-cheats)

ScreenTale fundamentally **does not inject into the game**:

* All plates are drawn on ScreenTale's own top-level windows
  (`InPlaceCanvas` / `TranslateWindow`) on top of the desktop.
* Clicks on the plate area fall through to the game via `nativeEvent`
  → `WM_NCHITTEST` → `HTTRANSPARENT (-1)`.
* No `SetWindowsHookEx`, no DLL injection, no reading of the game
  process memory, no DirectX/OpenGL swapchain hooks.
* The game window is used only as a **rectangle source** for
  `ImageGrab.grab(bbox=...)` — ScreenTale works with what Windows
  itself drew on the screen.

Any anti-cheat that checks `OpenProcess` / `ReadProcessMemory` / DX
hooks will not see ScreenTale, because ScreenTale does none of that.

### Stream Mode (capture protection)

`SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`:
* `0x00000000` (`WDA_NONE`) — visible to OBS / ShareX / Discord.
* `0x00000011` (`WDA_EXCLUDEFROMCAPTURE`) — plates are visible on the
  streamer's physical monitor, but **excluded from OBS / Discord
  capture**. Documented Win32 API, supported since Windows 10 2004.

This protection is for streamers: viewers don't see translation plates,
but the streamer does. System-level screen capture (ShareX) also
respects this affinity.

### Job Object (zombie-process protection)

`llama-server.exe` is a separate subprocess. If `ScreenTale.exe`
crashes or the user kills it via Task Manager, `llama-server.exe` would
otherwise stay in memory and hold 2–8 GB of VRAM.

`_assign_to_kill_job(proc)` solves this: a single `CreateJobObjectW`
with the flag `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000` is created
on the first LLM launch. All subsequent `llama-server.exe` processes
are assigned to it. When the parent (ScreenTale) dies, the Windows
kernel automatically kills the whole Job Object group.

### SHA-256 verified updates

`backend/updater.py:calculate_sha256(file_path)` reads the file in
64 KB chunks via `hashlib.sha256`. After downloading `update.zip` the
hash is compared with `manifest.json["sha256"]` from the server. On
mismatch the file is deleted and `failed("SHA-256 checksum mismatch!
File corrupted.")` is emitted.

`manifest.json` is fetched from
`https://github.com/Xilleet/ScreenTale/releases/latest/download/manifest.json`.
No third-party CDN redirects.

### Pinned llama.cpp

`LLAMA_PINNED_TAG = "b11224"` is a fixed tag. The URL is built as
`releases/download/{tag}/{file}`, no "latest" that could shift. If a
supply-chain attack happens in the `llama.cpp` repository, users will
get incompatible binaries (the Smoke Test catches that); the SHA-256
of llama.cpp binaries is not verified, but Smoke Test rejects
incompatible builds.

### No shell injection

All `subprocess` calls use an **argument list**, not a string:

```python
subprocess.Popen([exe, "-m", model, "-ngl", "99", ...], ...)
subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe", "/T"], ...)
subprocess.Popen(["cmd.exe", "/c", updater_bat], ...)
```

`updater.bat` is generated locally with concrete paths (no user input)
and `chcp 65001` (UTF-8) for correct Cyrillic.

### URI safety

`UpdateDownloadWorker` uses `urllib.request` (not `os.system` or
`eval`) and sets `User-Agent: ScreenTale-App`. No `shell=True` anywhere
in the code.

## Error Handling

ScreenTale does not have a formal domain-exception hierarchy (unlike
Senpy). Instead it uses a **graceful-fallback** strategy:

| What fails | Reaction |
|-----------|---------|
| Windows OCR language pack not installed | Auto-switch to `RapidOcrEngine`, emit `engine_changed("rapidocr")` |
| RapidOCR not importable | Fall further back to `EasyOcrEngine` |
| All OCR engines unavailable | `state_changed("error", "No available OCR engines")` |
| Network failure of Google Translate | `is_network_error()` → auto-fallback to a local engine |
| Opus-MT chosen for a non-EN→RU pair | Return an informative message instead of a translation |
| `llama-server` fails to start within 180 s | `RuntimeError`, emit `failed(engine_id, msg)` |
| Smoke Test fails | `RuntimeDownloadWorker` emits `failed` with a human-readable reason (no VC++ Redist / no AVX2) |
| SHA-256 of update.zip mismatch | File deleted, `failed("SHA-256 mismatch")` emitted |
| User-facing `[...]` toast in `show_translation` | Caught as `_NoticeToast` instead of chat history |

All exceptions in workers are caught inside `run()` / `_do_*` methods
and converted to `failed` / `state_changed("error", ...)` signals —
the GUI never crashes due to a worker exception.

## Logging

`backend/logging_setup.py`:

* `setup_logging(app_dir)` rotates logs (`app.log` → `app_1.log` →
  `app_2.log`, `max_backups=2`), then opens a fresh line-buffered
  `app.log`.
* `_Tee` is a Tee object with `threading.Lock` that replaces
  `sys.stdout` and `sys.stderr`. Any `print()` from any library lands
  in the log + in the real stream (if there is one — in a windowed
  build `sys.stdout` may be `None`, in which case we write only to
  the file).
* `faulthandler.enable(file=_log_file)` — native C++ crashes (e.g.
  segfaults in `llama-server.exe` dependencies) write a traceback to
  the log.
* `vlog(*args)` is a verbose log; `vlog("...")` writes only if
  `VERBOSE = True` (set from `AppController` via
  `set_verbose(settings.get("verbose_log"))`). Used for full OCR /
  translation text; the nearby `print()` leaves a 60-character
  truncated version for the normal mode.

## Configuration

### `data/config.json`

```jsonc
{
  "font_size": 14,
  "theme": "dark",              // "dark" | "dark_classic" | "light"
  "frame_opacity": 0.78,        // 0.0 .. 1.00 — прозрачность подложки окна
  "text_opacity": 1.0,          // 0.0 .. 1.00 — прозрачность текста (независимо от подложки)
  "adaptive_opacity": false,    // авто-подстройка прозрачности подложки под фон (chat-режим)
  "src_lang": "en",             // any from LANGUAGES
  "dst_lang": "ru",
  "recent_pairs": [["en","ru"], ["ja","ru"], ["zh","ru"], ["ko","ru"]],
  "ocr_engine": "windows",      // "windows" | "rapidocr" | "easyocr"
  "ocr_direction": "horizontal",// "horizontal" | "vertical"
  "translator": "google",       // "google" | "mymemory" | "opus" | "nllb" | "qwen"
  "selected_gguf": "",          // filename inside data/models
  "gpu": true,
  "auto_copy": true,
  "auto_delay_ms": 800,         // 200 .. 2000
  "cpu_threads": 0,             // 0 = auto (cpu_count//2 - 1)
  "verbose_log": false,
  "update_snooze_until": "",    // ISO date string — snooze баннера обновлений
  "overlay_mode": "chat",       // "chat" | "inplace"
  // welcome_completed удалён — не использовался.
  "streamer_mode": false,       // SetWindowDisplayAffinity
  "ui_lang": "auto",            // "auto" | "en" | "ru" | any code from locales/
  "hotkeys": {
    "single":        {"label": "Alt+Q",  "mods": 0x0001, "vk": 0x51},
    "auto":          {"label": "Alt+W",  "mods": 0x0001, "vk": 0x57},
    "pause":         {"label": "Alt+P",  "mods": 0x0001, "vk": 0x50},
    "toggle_window": {"label": "Ctrl+`", "mods": 0x0002, "vk": 0xC0},
    "stop":          {"label": "Alt+C",  "mods": 0x0001, "vk": 0x43},
    "clear":         {"label": "Alt+X",  "mods": 0x0001, "vk": 0x58},
    "ghost":         {"label": "Alt+G",  "mods": 0x0001, "vk": 0x47}
  }
}
```

`mods` are Win32 flags `MOD_ALT = 0x0001`, `MOD_CONTROL = 0x0002`,
`MOD_SHIFT = 0x0004`, `MOD_WIN = 0x0008`. `vk` is the virtual key code
(layout-independent). `HotkeyRecorder` converts Qt keys to native
`mods` / `vk` via `_qt_mods_to_native()` and `e.nativeVirtualKey()`.

### `manifest.json` (on GitHub Releases)

```jsonc
{
  "version": "0.7.1",
  "download_url": "https://github.com/Xilleet/ScreenTale/releases/download/v0.7.1/ScreenTale_v0.7.1.zip",
  "sha256": "<lowercase hex>",
  "changelog": "..."
}
```

`UpdateCheckTask` parses `version` via `_parse_version(v_str)` (strips
`v` and `-beta` suffixes, splits by `.` → `(major, minor, patch)`).
Tuple comparison. If the remote version is strictly greater than the
local one, `update_available(manifest_data)` is emitted.

## Extension Points

### Adding a new OCR engine

1. Create a subclass of `BaseOcrEngine` in `backend/ocr_engines.py`:

   ```python
   class MyOcrEngine(BaseOcrEngine):
       engine_id = "myocr"
       display_name = "My OCR"

       def is_available(self) -> bool:
           try:
               import my_ocr_lib
               return True
           except ImportError:
               return False

       def load(self, use_gpu=False, lang="en") -> bool:
           # Initialize the engine. Return True on success.
           ...

       def read(self, img: Image.Image) -> str:
           # Flat reading for the Chat mode.
           ...

       def read_detailed(self, img) -> tuple[str, list[dict]]:
           # IMPORTANT for In-Place: return (text, [{"text","rect","polygon"}])
           # rect in coordinates of the original image (before upscale).
           ...
   ```

2. Add a branch for `engine_id` in `OcrWorker._init_engine()`.
3. Add the new option to `DEFAULTS["ocr_engine"]` in `config.py` (or
   use `request_engine("myocr")` via `Settings`).
4. In `SettingsWindow._page_translation`, add a button to the
   `SegmentedControl` (the `OCR_ENGINES` list).
5. Add `hint.ocr.myocr` with a description to `locales/*.json`.

### Adding a new online translator

1. In `backend/translators.py`:

   ```python
   ENGINE_MYENGINE = "myengine"
   ENGINE_LABELS[ENGINE_MYENGINE] = "My Engine"
   _MODEL_SPECS[ENGINE_MYENGINE] = {"repo": None, "approx_size": "cloud"}

   # In translate_online() — a new branch:
   if engine == ENGINE_MYENGINE:
       return deep_translator.MyEngineTranslator(
           source=src, target=dst).translate(text)
   ```

2. Add `"myengine"` to `_VALID_TRANSLATORS` in `config.py`.
3. In `SettingsWindow._page_translation` — add a button to
   `translator_seg`.

### Adding a new GGUF model

1. Append an entry to `GGUF_CATALOG` in `backend/llama_server.py`:

   ```python
   {
       "id": "my-model-3b",
       "filename": "my-model-3b-q4_k_m.gguf",
       "title": "My Model 3B",
       "badge": "🎯 Targeted use case",
       "approx_size": "~2.0 GB",
       "url": "https://huggingface.co/<user>/<repo>/resolve/main/<file>.gguf",
       "desc": "Description shown in the catalog."
   }
   ```

2. `SettingsWindow._build_catalog_item_row` will automatically render
   the new card with a "Download" button.
3. No changes to `runtime_manager` or `LlamaServerTranslator` are
   needed — any `llama.cpp`-compatible GGUF works.

### Adding a new language

1. In `backend/languages.py:LANGUAGES` add an entry:

   ```python
   "uk": {
       "name": "Украинский",
       "name_en": "Ukrainian",
       "flag": "🇺🇦",
       "win_ocr": "uk-UA",
       "nllb":   "ukr_Cyrl",
       "google": "uk"
   }
   ```

2. UI combo boxes in `SettingsWindow` automatically pick up the entry
   from `LANGUAGES` (via `_populate_lang_combos`).
3. Windows OCR will require installing the language pack via
   `Add-WindowsCapability` (the UI shows the ready-to-use PowerShell
   command).
4. For **UI** localization, add `locales/uk.json` (a copy of `ru.json`
   with translated strings) — `get_available_ui_languages` will find
   it automatically.

### Adding a new theme

1. In `frontend/theme.py:PALETTES` add a new key with the same set of
   tokens (`bg`, `bg_sidebar`, `bg_card`, `bg_hover`, `stroke`, `text`,
   `text_dim`, `accent`, `accent_soft`, `green`, `yellow`, `red`,
   `btn_hover`, `preview_bg`, `toast_bg`).
2. In `SettingsWindow._page_general` add a button to `theme_seg`
   (`SegmentedControl`).

## Build

`ScreenTale.spec` is the PyInstaller spec. Key points:

* `hidden_imports` — `winocr`, `winrt.*` (Windows OCR), `torch`,
  `torchvision`, `easyocr`, `transformers`, `huggingface_hub`,
  `deep_translator`, `bs4`, `soupsieve`, `pyperclip`, `PIL`,
  `PIL.Image`, `PIL.ImageGrab`. Without explicit listing PyInstaller
  would skip them.
* `datas` — `ScreenTale.ico`, `logo.png`, `frontend/icons/`,
  `locales/`.
* `collect_all('rapidocr_onnxruntime')` and `collect_all('onnxruntime')`
  collect all DLLs / manifests / data of ONNX Runtime.
* Metadata for `transformers`, `huggingface_hub`, `tqdm`, `regex`,
  `requests` (via `copy_metadata`).

`build.bat` is a PyInstaller wrapper with flags
`--noconfirm --clean --windowed` (no console window).

## Auto-Update

`backend/updater.py:UpdateDownloadWorker`:

1. Download `update.zip` in 64 KB blocks, progress → `Signal(int, str)`.
2. SHA-256 verification (`manifest["sha256"]`).
3. Generate `updater.bat`:

   ```bat
   @echo off
   chcp 65001 > nul
   :wait_process
   timeout /t 1 /nobreak > nul
   tasklist /FI "IMAGENAME eq ScreenTale.exe" 2>NUL | find /I /N "ScreenTale.exe">NUL
   if "%ERRORLEVEL%"=="0" goto wait_process

   powershell -Command "Expand-Archive -Path '<zip>' -DestinationPath '<extracted>' -Force"
   xcopy "<extracted>\ScreenTale\*" "<app_dir>\\" /s /e /y /q > nul
   start "" "<app_dir>\ScreenTale.exe"
   del /f /q "%~f0" > nul 2>&1
   ```

4. `subprocess.Popen(["cmd.exe", "/c", updater_bat],
   creationflags=_CREATE_NO_WINDOW, close_fds=True)` — the updater
   waits for ScreenTale to exit, unpacks the archive, overwrites the
   files, launches the new version, and deletes itself.

`SettingsWindow._UpdateBanner` is an amber banner at the bottom of
the window with "⚡ Update now" and "Snooze for 7 days" buttons
(snooze writes a date to `update_snooze_until`).

## Window Modes and Their Geometry

```
                        ┌──────────────────────────────┐
                        │  InPlaceCanvas (fullscreen)   │
                        │  HTTRANSPARENT + stays-on-top  │
                        │  virtualGeometry of all mons  │
                        └──────────────────────────────┘
                                     ▲
                                     │ point_translate_requested(QPoint)
                                     │
        ┌────────────────────────────┴────────────────────┐
        │                                                 │
┌──────────────────┐                          ┌──────────────────┐
│  TranslateWindow  │   ⊣⊣⊣ drag-with-         │  FloatingToolbar │
│  (chat overlay)   │ ⊣⊣⊣ forbidden-rect       │  (Qt.Tool, top-   │
│  Frameless +      │                          │   level, separate │
│  stays-on-top     │                          │   window)         │
│  + Drop shadow    │                          │  8 buttons + grip │
└──────────────────┘                          └──────────────────┘
        ▲                                                 ▲
        │ SettingsManager.changed (font_size, opacity, ...)│
        │                                                 │
┌──────────────────────────────────────────────────────────────────┐
│                      SettingsWindow (840x600 frameless)            │
│  Sidebar (200px) │  QStackedWidget (5 pages)                        │
│  + nav + GlowPin │  + UpdateBanner + Toast                           │
│  + ui_lang menu  │                                                 │
└──────────────────────────────────────────────────────────────────┘
```

* `TranslateWindow` — chat window; `CustomTextEdit` forwards mouse
  events to the parent for drag; `ResizeGrip` sits in the bottom-right
  corner; `_NoticeToast` for status lines `[...]`; `StatusPill` at
  bottom-left.
* `FloatingToolbar` — separate top-level window; `paintEvent` draws
  a custom `#1e1b18` rounded rect (because `WA_TranslucentBackground`
  + stylesheet background flicker on a `Qt.Tool` window). `_GripButton`
  is the `⋮⋮` handle: drag > 3 px manhattan → undock + free flight;
  pure click → `toggle_dock()`. `on_drag_finished` — post-release
  magnetic docking: if the toolbar is dropped in the forbidden zone
  (inside the card rect +4 px) → snap to the nearest of 4 anchors;
  if <35 px from an anchor → magnetic snap; otherwise free flight.
* `SettingsWindow` — frameless 840×600 with Win11 rounded corners
  (`DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE, 2)`),
  custom `WM_SYSCOMMAND/SC_MINIMIZE` handling for a custom minimize
  animation (`QParallelAnimationGroup` on `windowOpacity` + `pos`).
  `nativeEvent` catches `0xF020` (SC_MINIMIZE) → `_animate_minimize()`,
  returns `(True, 0)` (swallows the default).
* `ScreenSelector` — full-screen `WA_TranslucentBackground` overlay
  with `CrossCursor`, draws 80% alpha dimming + a transparent rectangle
  via `CompositionMode_Clear` (Snipping Tool style). Minimum selection
  size 20×20 px.

## Contributor Cheat Sheet

* **Heavy operations — only in `QThread` workers.** Never call
  `time.sleep()`, `requests.get()`, `subprocess.run()`,
  `model.generate()` directly from the GUI thread — wrap them in a
  worker or a `QThreadPool` task.
* **Cross-thread communication — only via signals.** Workers emit
  typed `Signal(...)`, the GUI connects slots. Qt itself handles
  `QueuedConnection` between threads.
* **Win32 calls — via `ctypes.windll` + `creationflags=_CREATE_NO_WINDOW`.**
  Never leave a stray console window in production.
* **Coordinates — always physical pixels.** `get_screen_scale`
  multiplies logical coordinates by `devicePixelRatio` before
  `ImageGrab.grab`. Without this, on a 4K 150% monitor you would
  capture a quarter of the desired area.
* **Settings writes — only via `SettingsManager.set`.** Do not write
  to `config.json` directly — `SettingsManager` provides debounce,
  emits `changed`, and handles backward compatibility.
* **Logging — `print()` for short, `vlog()` for long.** `print()` is
  always written; `vlog()` is gated by the verbose flag. The short
  version in `print()` is at most 60 characters + `!r` so the log does
  not bloat.

See also the user-facing [`README.md`](README.md) (features, hotkeys,
installation) and [`ARCHITECTURE_ru.md`](ARCHITECTURE_ru.md) (the Russian
version of this document).
