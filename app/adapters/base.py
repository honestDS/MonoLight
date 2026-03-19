from abc import ABC, abstractmethod


class BaseAdapter(ABC):
    @abstractmethod
    async def connect(self):
        pass

    @abstractmethod
    async def send(self, target_id: str, content: str):
        pass
