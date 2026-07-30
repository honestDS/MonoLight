import asyncio
import sys

WINDOWS_SELECTOR_EVENT_LOOP_FACTORY = "app.core.event_loop:create_windows_selector_event_loop"


def get_uvicorn_loop() -> str:
    if sys.platform == "win32":
        return WINDOWS_SELECTOR_EVENT_LOOP_FACTORY
    return "auto"


def create_windows_selector_event_loop() -> asyncio.SelectorEventLoop:
    # Avoids the Windows Proactor shutdown race where a remote reset can make socket.shutdown()
    # raise WinError 10054 during cleanup; this does not suppress business exceptions.
    return asyncio.SelectorEventLoop()
