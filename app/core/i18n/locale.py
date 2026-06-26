from pathlib import Path

DEFAULT_LOCALE = "zh"


def get_available_locales() -> tuple[str, ...]:
    locales_dir = Path(__file__).parent / "locales"
    locales = sorted(path.name for path in locales_dir.iterdir() if path.is_dir() and not path.name.startswith("__"))
    if DEFAULT_LOCALE not in locales:
        locales.insert(0, DEFAULT_LOCALE)
    return tuple(locales)


SUPPORTED_LOCALES = get_available_locales()


def normalize_locale(raw: str | None) -> str:
    if not raw:
        return DEFAULT_LOCALE

    lang = raw.split(",", 1)[0].split(";", 1)[0].replace("_", "-").split("-", 1)[0].strip().lower()

    if lang in SUPPORTED_LOCALES:
        return lang

    return DEFAULT_LOCALE
