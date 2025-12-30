from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AgentFile
from app.schemas import AgentFileCreate, AgentFileRead, AgentFileUpdate

router = APIRouter(prefix="/api/agent-files", tags=["agent-files"])


@router.get("", response_model=list[AgentFileRead])
async def list_agent_files(db: AsyncSession = Depends(get_db)):
    """List all agent files."""
    result = await db.execute(select(AgentFile))
    return result.scalars().all()


@router.post("", response_model=AgentFileRead, status_code=201)
async def create_agent_file(agent_file: AgentFileCreate, db: AsyncSession = Depends(get_db)):
    """Create a new agent file."""
    # Check if agent file with this name already exists
    result = await db.execute(select(AgentFile).where(AgentFile.name == agent_file.name))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail=f"Agent file with name '{agent_file.name}' already exists")

    db_agent_file = AgentFile(**agent_file.model_dump())
    db.add(db_agent_file)
    await db.commit()
    await db.refresh(db_agent_file)
    return db_agent_file


@router.get("/{agent_file_id}", response_model=AgentFileRead)
async def get_agent_file(agent_file_id: str, db: AsyncSession = Depends(get_db)):
    """Get a specific agent file by ID."""
    result = await db.execute(select(AgentFile).where(AgentFile.id == agent_file_id))
    agent_file = result.scalar_one_or_none()
    if not agent_file:
        raise HTTPException(status_code=404, detail="Agent file not found")
    return agent_file


@router.get("/by-name/{name}", response_model=AgentFileRead)
async def get_agent_file_by_name(name: str, db: AsyncSession = Depends(get_db)):
    """Get a specific agent file by name."""
    result = await db.execute(select(AgentFile).where(AgentFile.name == name))
    agent_file = result.scalar_one_or_none()
    if not agent_file:
        raise HTTPException(status_code=404, detail="Agent file not found")
    return agent_file


@router.patch("/{agent_file_id}", response_model=AgentFileRead)
async def update_agent_file(
    agent_file_id: str,
    update: AgentFileUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update an agent file."""
    result = await db.execute(select(AgentFile).where(AgentFile.id == agent_file_id))
    agent_file = result.scalar_one_or_none()
    if not agent_file:
        raise HTTPException(status_code=404, detail="Agent file not found")

    update_data = update.model_dump(exclude_unset=True)

    # Check if name is being changed and if it conflicts
    if "name" in update_data and update_data["name"] != agent_file.name:
        result = await db.execute(select(AgentFile).where(AgentFile.name == update_data["name"]))
        existing = result.scalar_one_or_none()
        if existing:
            raise HTTPException(status_code=400, detail=f"Agent file with name '{update_data['name']}' already exists")

    for key, value in update_data.items():
        setattr(agent_file, key, value)

    await db.commit()
    await db.refresh(agent_file)
    return agent_file


@router.delete("/{agent_file_id}", status_code=204)
async def delete_agent_file(agent_file_id: str, db: AsyncSession = Depends(get_db)):
    """Delete an agent file."""
    result = await db.execute(select(AgentFile).where(AgentFile.id == agent_file_id))
    agent_file = result.scalar_one_or_none()
    if not agent_file:
        raise HTTPException(status_code=404, detail="Agent file not found")

    await db.delete(agent_file)
    await db.commit()


@router.post("/batch", response_model=list[AgentFileRead])
async def get_agent_files_batch(agent_file_ids: list[str], db: AsyncSession = Depends(get_db)):
    """Get multiple agent files by their IDs. Used by runners to fetch agent files for a job."""
    if not agent_file_ids:
        return []

    result = await db.execute(select(AgentFile).where(AgentFile.id.in_(agent_file_ids)))
    agent_files = result.scalars().all()

    # Maintain order of requested IDs
    agent_files_dict = {af.id: af for af in agent_files}
    ordered_files = []
    for aid in agent_file_ids:
        if aid in agent_files_dict:
            ordered_files.append(agent_files_dict[aid])

    return ordered_files
