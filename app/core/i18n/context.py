from contextvars import ContextVar, Token

from app.core.i18n.locale import DEFAULT_LOCALE

_current_locale: ContextVar[str] = ContextVar("current_locale", default=DEFAULT_LOCALE)
_current_log_locale: ContextVar[str | None] = ContextVar("current_log_locale", default=None)


def set_current_locale(locale: str) -> Token[str]:
    return _current_locale.set(locale)


def reset_current_locale(token: Token[str]) -> None:
    _current_locale.reset(token)


def get_current_locale() -> str:
    return _current_locale.get()


def set_current_log_locale(locale: str | None) -> Token[str | None]:
    return _current_log_locale.set(locale)


def reset_current_log_locale(token: Token[str | None]) -> None:
    _current_log_locale.reset(token)


def get_current_log_locale() -> str | None:
    return _current_log_locale.get()
