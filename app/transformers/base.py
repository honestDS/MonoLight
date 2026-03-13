from abc import ABC, abstractmethod
from app.schemas.message import UniversalMessageModel

class BaseTransformer(ABC):
    @abstractmethod
    def transform(self, raw_data: any) -> UniversalMessageModel:
        pass
