WEB_SESSION_SOURCES = frozenset({"http", "ws"})


def is_web_session_source(source: str | None) -> bool:
    return (source or "http") in WEB_SESSION_SOURCES


def default_show_tool_calls_for_source(source: str | None) -> bool:
    return is_web_session_source(source)
