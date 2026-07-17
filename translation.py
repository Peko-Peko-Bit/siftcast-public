import os
import time
import requests

DEEPL_API_KEY = os.getenv('DEEPL_API_KEY')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL_TRANSLATE = os.getenv('OPENROUTER_MODEL_TRANSLATE', 'google/gemma-3-27b-it')

_deepl_quota_exceeded_at = None  # クォータ超過時刻。24時間は再試行せず、以降は自動復帰を試す
DEEPL_QUOTA_RETRY_AFTER = 24 * 3600
DEEPL_API_URL = "https://api-free.deepl.com/v2/translate"
DEEPL_LANG_MAP = {"ja": "JA", "en": "EN", "es": "ES", "ko": "KO", "zh": "ZH", "zh-TW": "ZH"}

LANG_MAP_GOOGLE = {
    "ja": "ja",
    "en": "en",
    "es": "es",
    "ko": "ko",
    "zh-TW": "zh-TW",
    "zh": "zh-CN",
}


class TranslationError(Exception):
    pass


class QuotaExceededError(TranslationError):
    pass


def _deepl_quota_blocked() -> bool:
    """True while the quota-exceeded flag is fresh; auto-expires so DeepL resumes after monthly reset."""
    global _deepl_quota_exceeded_at
    if _deepl_quota_exceeded_at is None:
        return False
    if time.time() - _deepl_quota_exceeded_at < DEEPL_QUOTA_RETRY_AFTER:
        return True
    _deepl_quota_exceeded_at = None
    return False


def _translate_deepl(text: str, target_lang: str) -> str:
    global _deepl_quota_exceeded_at
    if _deepl_quota_blocked():
        raise QuotaExceededError("DeepL quota exceeded (cached)")
    if not DEEPL_API_KEY:
        raise TranslationError("DEEPL_API_KEY not set")

    deepl_lang = DEEPL_LANG_MAP.get(target_lang)
    if not deepl_lang:
        raise TranslationError(f"Unsupported target_lang for DeepL: {target_lang}")

    response = requests.post(
        DEEPL_API_URL,
        headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
        json={"text": [text], "target_lang": deepl_lang},
        timeout=10
    )
    if response.status_code == 456:
        _deepl_quota_exceeded_at = time.time()
        raise QuotaExceededError("DeepL quota exceeded")
    response.raise_for_status()
    return response.json()["translations"][0]["text"]


def _translate_google(text: str, target_lang: str) -> str:
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
    if not api_key:
        raise TranslationError("GOOGLE_TRANSLATE_API_KEY not set")

    google_lang = LANG_MAP_GOOGLE.get(target_lang, target_lang)
    response = requests.post(
        "https://translation.googleapis.com/language/translate/v2",
        params={"key": api_key},
        json={
            "q": text,
            "target": google_lang,
            "format": "text"
        },
        timeout=10
    )
    response.raise_for_status()
    return response.json()["data"]["translations"][0]["translatedText"]


def _translate_gemma(text: str, target_lang: str) -> str:
    if not OPENROUTER_API_KEY:
        raise TranslationError("OPENROUTER_API_KEY not set")

    lang_names = {
        "ja": "Japanese", "en": "English", "es": "Spanish",
        "ko": "Korean", "zh": "Simplified Chinese", "zh-TW": "Traditional Chinese"
    }
    lang_name = lang_names.get(target_lang, target_lang)
    prompt = (
        f"Translate the following text to {lang_name}.\n"
        f"Output only the translated text, no explanations.\n\n"
        f"{text}"
    )

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5001",
            "X-Title": "SiftCast"
        },
        json={"model": OPENROUTER_MODEL_TRANSLATE, "messages": [{"role": "user", "content": prompt}]},
        timeout=30
    )
    response.raise_for_status()
    return response.json()['choices'][0]['message']['content'].strip()


def translate_text(text: str, target_lang: str, engine: str = "auto") -> tuple:
    """Translate text. Returns (translated_text, engine_used).
    Always returns a string; falls back to original on failure."""
    if not text:
        return text, "none"

    if engine == "deepl":
        try:
            return _translate_deepl(text, target_lang), "deepl"
        except Exception as e:
            print(f"[translation] DeepL error: {e}")
            return text, "none"

    if engine == "google":
        try:
            return _translate_google(text, target_lang), "google"
        except Exception as e:
            print(f"[translation] Google error: {e}")
            return text, "none"

    if engine == "gemma":
        try:
            return _translate_gemma(text, target_lang), "gemma"
        except Exception as e:
            print(f"[translation] Gemma error: {e}")
            return text, "none"

    # engine="auto": DeepL → Google → Gemma fallback chain
    try:
        result = _translate_deepl(text, target_lang)
        return result, "deepl"
    except Exception as e:
        print(f"[translation] DeepL error, falling back to Google: {e}")

    try:
        result = _translate_google(text, target_lang)
        return result, "google"
    except Exception as e:
        print(f"[translation] Google error, falling back to Gemma: {e}")

    try:
        result = _translate_gemma(text, target_lang)
        return result, "gemma"
    except Exception as e:
        print(f"[translation] Gemma fallback error: {e}")
        return text, "none"


def translate_titles(titles: list, target_lang: str, engine: str = "auto"):
    """Translate a list of title strings. Preserves order.
    Returns None when every engine failed, so callers can skip caching."""
    global _deepl_quota_exceeded_at
    if not titles:
        return titles
    if engine == "gemma":
        engine = "auto"  # タイトル一括翻訳では Gemma を使用しない（遅延大）

    # DeepL batch API for auto/deepl
    if engine in ("auto", "deepl"):
        deepl_lang = DEEPL_LANG_MAP.get(target_lang)
        if DEEPL_API_KEY and deepl_lang and not _deepl_quota_blocked():
            try:
                response = requests.post(
                    DEEPL_API_URL,
                    headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                    json={"text": titles, "target_lang": deepl_lang},
                    timeout=15
                )
                if response.status_code == 456:
                    _deepl_quota_exceeded_at = time.time()
                    raise QuotaExceededError("DeepL quota exceeded")
                response.raise_for_status()
                return [t["text"] for t in response.json()["translations"]]
            except QuotaExceededError:
                if engine == "deepl":
                    return None
                print(f"[translation] DeepL quota exceeded for titles, falling back to Google batch")
            except Exception as e:
                print(f"[translation] DeepL batch titles error: {e}")
                if engine == "deepl":
                    return None

    # Google: batch
    try:
        api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY")
        if not api_key:
            raise TranslationError("GOOGLE_TRANSLATE_API_KEY not set")
        google_lang = LANG_MAP_GOOGLE.get(target_lang, target_lang)
        response = requests.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": api_key},
            json={"q": titles, "target": google_lang, "format": "text"},
            timeout=15
        )
        response.raise_for_status()
        return [t["translatedText"] for t in response.json()["data"]["translations"]]
    except Exception as e:
        print(f"[translation] Google batch titles error: {e}")
        return None
