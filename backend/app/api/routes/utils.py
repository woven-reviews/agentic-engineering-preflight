from fastapi import APIRouter
from sqlmodel import Session, select

from app.core.db import engine

router = APIRouter(prefix="/utils", tags=["utils"])


@router.get("/health-check/")
async def health_check() -> bool:
    return True


@router.get("/db-check/")
def db_check() -> dict[str, str]:
    # Round-trips to Postgres so the frontend can confirm the whole stack —
    # not just the API process — is wired up.
    with Session(engine) as session:
        session.exec(select(1))
    return {"database": "ok"}
