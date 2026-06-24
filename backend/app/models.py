import uuid

from sqlmodel import Field, SQLModel


# A single trivial table so Alembic has something to migrate — exercises the
# same migration path as the real app without any domain meaning.
class Ping(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    note: str = "ok"
