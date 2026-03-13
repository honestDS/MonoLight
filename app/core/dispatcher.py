import logging
from app.schemas.message import UniversalMessageModel

logger = logging.getLogger(__name__)

class Dispatcher:
    def __init__(self):
        self.middlewares = []

    async def dispatch(self, message: UniversalMessageModel):
        logger.info(f'Dispatching message from {message.platform}: {message.sender_id}')
        return True
