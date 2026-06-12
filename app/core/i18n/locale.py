SUPPORTED_LOCALES = ("zh", "en")
DEFAULT_LOCALE = "zh"


def normalize_locale(raw: str | None) -> str:
    if not raw:
        return DEFAULT_LOCALE

    lang = raw.split("-")[0].lower()

    if lang in SUPPORTED_LOCALES:
        return lang

    return DEFAULT_LOCALE
