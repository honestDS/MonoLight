from contextvars import ContextVar

from app.core.i18n.locale import DEFAULT_LOCALE

_current_locale: ContextVar[str] = ContextVar("current_locale", default=DEFAULT_LOCALE)


def set_current_locale(locale: str) -> None:
    _current_locale.set(locale)


def get_current_locale() -> str:
    return _current_locale.get()
