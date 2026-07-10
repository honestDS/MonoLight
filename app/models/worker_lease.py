from datetime import datetime

from sqlmodel import Column, DateTime, Field, SQLModel

from app.core.utils.time import get_local_time


class WorkerLease(SQLModel, table=True):
    __tablename__ = "worker_lease"

    worker_name: str = Field(primary_key=True, max_length=100)
    owner_id: str = Field(index=True, max_length=100)
    lease_until: datetime = Field(sa_column=Column(DateTime(timezone=True), index=True))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
