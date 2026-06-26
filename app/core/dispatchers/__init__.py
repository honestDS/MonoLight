from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.dispatchers.non_stream import NonStreamDispatcherMixin
from app.core.dispatchers.shared import DispatcherValidationMixin
from app.core.dispatchers.stream import StreamDispatcherMixin


class ChatDispatcher(BackgroundDispatcherMixin, DispatcherValidationMixin, NonStreamDispatcherMixin, StreamDispatcherMixin):
    pass


__all__ = ["ChatDispatcher"]
