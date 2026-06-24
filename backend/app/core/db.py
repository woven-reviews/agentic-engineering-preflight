from sqlmodel import Session, create_engine

from app.core.config import settings

engine = create_engine(str(settings.SQLALCHEMY_DATABASE_URI))


def init_db(session: Session) -> None:
    # Tables are created by Alembic migrations (alembic upgrade head in
    # prestart.sh). Nothing to seed for the preflight check.
    pass
