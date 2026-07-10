from sqlmodel import Field, SQLModel


class WorkerLease(SQLModel, table=True):
    __tablename__ = "worker_lease"

    worker_name: str = Field(primary_key=True, max_length=100)
    owner_id: str = Field(index=True, max_length=100)
    lease_until: int = Field(index=True)
    updated_at: int
