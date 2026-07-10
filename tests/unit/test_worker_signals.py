import asyncio
from types import SimpleNamespace

from app.workers import signals


def test_install_shutdown_signal_handlers_falls_back_to_signal_module(monkeypatch):
    stop_event = asyncio.Event()
    shutdown_signal = object()
    registered_handlers = []

    class Loop:
        def add_signal_handler(self, received_signal, callback):
            assert received_signal is shutdown_signal
            raise NotImplementedError

        def call_soon_threadsafe(self, callback):
            callback()

    monkeypatch.setattr(signals.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(
        signals,
        "signal",
        SimpleNamespace(
            SIGINT=None,
            SIGTERM=None,
            SIGBREAK=shutdown_signal,
            signal=lambda received_signal, callback: registered_handlers.append((received_signal, callback)),
        ),
    )

    signals.install_shutdown_signal_handlers(stop_event)
    registered_handlers[0][1]()

    assert registered_handlers[0][0] is shutdown_signal
    assert stop_event.is_set()
