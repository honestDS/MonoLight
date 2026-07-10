import asyncio
import signal


def install_shutdown_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_shutdown(_signum=None, _frame=None) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        shutdown_signal = getattr(signal, signal_name, None)
        if shutdown_signal is None:
            continue
        try:
            loop.add_signal_handler(shutdown_signal, stop_event.set)
            continue
        except (NotImplementedError, RuntimeError):
            pass
        try:
            signal.signal(shutdown_signal, request_shutdown)
        except (OSError, RuntimeError, ValueError):
            continue
