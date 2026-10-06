"""Единый реестр языков и сопоставлений для ScreenTale (Any-to-Any)."""

# Золотой пул из 12 поддерживаемых языков
LANGUAGES = {
    "ru": {
        "name": "Русский",
        "name_en": "Russian",
        "flag": "🇷🇺",
        "win_ocr": "ru-RU",
        "nllb": "rus_Cyrl",
        "google": "ru",
    },
    "en": {
        "name": "Английский",
        "name_en": "English",
        "flag": "🇬🇧",
        "win_ocr": "en-US",
        "nllb": "eng_Latn",
        "google": "en",
    },
    "ja": {
        "name": "Японский",
        "name_en": "Japanese",
        "flag": "🇯🇵",
        "win_ocr": "ja-JP",
        "nllb": "jpn_Jpan",
        "google": "ja",
    },
    "zh": {
        "name": "Китайский",
        "name_en": "Chinese (Simplified)",
        "flag": "🇨🇳",
        "win_ocr": "zh-CN",
        "nllb": "zho_Hans",
        "google": "zh-CN",
    },
    "ko": {
        "name": "Корейский",
        "name_en": "Korean",
        "flag": "🇰🇷",
        "win_ocr": "ko-KR",
        "nllb": "kor_Hang",
        "google": "ko",
    },
    "es": {
        "name": "Испанский",
        "name_en": "Spanish",
        "flag": "🇪🇸",
        "win_ocr": "es-ES",
        "nllb": "spa_Latn",
        "google": "es",
    },
    "pt": {
        "name": "Португальский",
        "name_en": "Portuguese (Brazil)",
        "flag": "🇧🇷",
        "win_ocr": "pt-BR",
        "nllb": "por_Latn",
        "google": "pt",
    },
    "fr": {
        "name": "Французский",
        "name_en": "French",
        "flag": "🇫🇷",
        "win_ocr": "fr-FR",
        "nllb": "fra_Latn",
        "google": "fr",
    },
    "de": {
        "name": "Немецкий",
        "name_en": "German",
        "flag": "🇩🇪",
        "win_ocr": "de-DE",
        "nllb": "deu_Latn",
        "google": "de",
    },
    "it": {
        "name": "Итальянский",
        "name_en": "Italian",
        "flag": "🇮🇹",
        "win_ocr": "it-IT",
        "nllb": "ita_Latn",
        "google": "it",
    },
    "pl": {
        "name": "Польский",
        "name_en": "Polish",
        "flag": "🇵🇱",
        "win_ocr": "pl-PL",
        "nllb": "pol_Latn",
        "google": "pl",
    },
    "tr": {
        "name": "Турецкий",
        "name_en": "Turkish",
        "flag": "🇹🇷",
        "win_ocr": "tr-TR",
        "nllb": "tur_Latn",
        "google": "tr",
    },
}

DEFAULT_SRC = "en"
DEFAULT_DST = "ru"


def get_lang_name(code: str) -> str:
    """Возвращает русское название языка или код."""
    return LANGUAGES.get(code, {}).get("name", code)


def get_lang_flag(code: str) -> str:
    """Возвращает эмодзи флага."""
    return LANGUAGES.get(code, {}).get("flag", "🌐")


def get_win_ocr_tag(code: str) -> str:
    """Возвращает BCP-47 языковой тег для Windows OCR (например, ja-JP)."""
    return LANGUAGES.get(code, {}).get("win_ocr", "en-US")


def get_google_code(code: str) -> str:
    """Возвращает код для Google Translate / deep_translator."""
    return LANGUAGES.get(code, {}).get("google", code)


def get_nllb_code(code: str) -> str:
    """Возвращает код Flores-200 для NLLB."""
    return LANGUAGES.get(code, {}).get("nllb", "eng_Latn")


def build_llm_system_prompt(src_code: str, dst_code: str) -> str:
    """Генерирует лаконичный системный промпт для игровой локализации и субтитров."""
    src_info = LANGUAGES.get(src_code, {})
    dst_info = LANGUAGES.get(dst_code, {})

    dst_name = dst_info.get("name_en", "Russian")
    src_name = src_info.get("name_en", "the original language")

    return (
        f"You are a professional game subtitle and UI localization translator. "
        f"Translate the given text from {src_name} into natural, fluent {dst_name}.\n"
        f"- Keep the translation CONCISE, compact, and punchy to fit strict game UI limits.\n"
        f"- Drop unnecessary pronouns and filler words without losing core meaning.\n"
        f"- Fix minor OCR glitches silently. Keep the character's tone and emotion.\n"
        f"- Output ONLY the final translation, without notes, explanations, or quotes."
    )

def format_pair_badge(src_code: str, dst_code: str) -> str:
    """Краткий бейдж для кнопки тулбара: 🇬🇧 ➔ 🇷🇺"""
    src_flag = get_lang_flag(src_code)
    dst_flag = get_lang_flag(dst_code)
    return f"{src_flag} ➔ {dst_flag}"


def format_pair_menu_item(src_code: str, dst_code: str) -> str:
    """Читаемый пункт меню: 🇬🇧 EN ➔ 🇷🇺 RU"""
    src_flag = get_lang_flag(src_code)
    dst_flag = get_lang_flag(dst_code)
    return f"{src_flag} {src_code.upper()} ➔ {dst_flag} {dst_code.upper()}"