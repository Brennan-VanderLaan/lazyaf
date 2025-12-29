from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Runner
from app.schemas import RunnerRead

router = APIRouter(prefix="/api/runners", tags=["runners"])


class ScaleRequest(BaseModel):
    count: int


@router.get("", response_model=list[RunnerRead])
async def list_runners(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Runner))
    return result.scalars().all()


@router.post("/scale")
async def scale_runners(request: ScaleRequest, db: AsyncSession = Depends(get_db)):
    # TODO: Implement runner pool scaling with Docker
    return {"message": f"Scaling to {request.count} runners", "current": 0, "target": request.count}
