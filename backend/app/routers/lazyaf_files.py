"""
Router for repo-defined LazyAF files (.lazyaf/ directory).

Reads pipelines and agents from the repository's .lazyaf/ directory:
- .lazyaf/pipelines/*.yaml - Pipeline definitions
- .lazyaf/agents/*.yaml - Agent definitions
"""

import json
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Repo, Pipeline, PipelineRun
from app.services.git_server import git_repo_manager
from app.services.pipeline_executor import pipeline_executor
from app.schemas.lazyaf_yaml import (
    AgentYaml,
    PipelineYaml,
    RepoAgentResponse,
    RepoPipelineResponse,
)

router = APIRouter(prefix="/api/repos/{repo_id}/lazyaf", tags=["lazyaf-files"])


async def get_repo_or_404(db: AsyncSession, repo_id: str) -> Repo:
    """Get repo or raise 404."""
    result = await db.execute(select(Repo).where(Repo.id == repo_id))
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repo


@router.get("/agents", response_model=list[RepoAgentResponse])
async def list_repo_agents(
    repo_id: str,
    branch: str = Query(None, description="Branch to read from (defaults to repo default)"),
    db: AsyncSession = Depends(get_db),
):
    """
    List agents defined in .lazyaf/agents/ directory.

    Returns agents with their source marked as 'repo'.
    """
    repo = await get_repo_or_404(db, repo_id)
    target_branch = branch or repo.default_branch

    if not target_branch:
        return []

    # List .lazyaf/agents/ directory
    files = git_repo_manager.list_directory(repo_id, target_branch, ".lazyaf/agents")

    if not files:
        return []

    agents = []
    for filename in files:
        if not (filename.endswith('.yaml') or filename.endswith('.yml')):
            continue

        content = git_repo_manager.get_file_content(
            repo_id, target_branch, f".lazyaf/agents/{filename}"
        )
        if not content:
            continue

        try:
            data = yaml.safe_load(content.decode('utf-8'))
            agent = AgentYaml(**data)
            agents.append(RepoAgentResponse(
                name=agent.name,
                description=agent.description,
                prompt_template=agent.prompt_template,
                source="repo",
                branch=target_branch,
                filename=filename,
            ))
        except Exception as e:
            # Log but continue - skip malformed files
            print(f"[lazyaf_files] Error parsing {filename}: {e}")
            continue

    return agents


@router.get("/agents/{agent_name}", response_model=RepoAgentResponse)
async def get_repo_agent(
    repo_id: str,
    agent_name: str,
    branch: str = Query(None, description="Branch to read from (defaults to repo default)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get a specific repo-defined agent by name.

    Searches for {agent_name}.yaml or {agent_name}.yml in .lazyaf/agents/.
    """
    repo = await get_repo_or_404(db, repo_id)
    target_branch = branch or repo.default_branch

    if not target_branch:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Try both .yaml and .yml extensions
    for ext in ['.yaml', '.yml']:
        filename = f"{agent_name}{ext}"
        content = git_repo_manager.get_file_content(
            repo_id, target_branch, f".lazyaf/agents/{filename}"
        )
        if content:
            try:
                data = yaml.safe_load(content.decode('utf-8'))
                agent = AgentYaml(**data)
                return RepoAgentResponse(
                    name=agent.name,
                    description=agent.description,
                    prompt_template=agent.prompt_template,
                    source="repo",
                    branch=target_branch,
                    filename=filename,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error parsing agent file: {e}")

    raise HTTPException(status_code=404, detail="Agent not found")


@router.get("/pipelines", response_model=list[RepoPipelineResponse])
async def list_repo_pipelines(
    repo_id: str,
    branch: str = Query(None, description="Branch to read from (defaults to repo default)"),
    db: AsyncSession = Depends(get_db),
):
    """
    List pipelines defined in .lazyaf/pipelines/ directory.

    Returns pipelines with their source marked as 'repo'.
    """
    repo = await get_repo_or_404(db, repo_id)
    target_branch = branch or repo.default_branch

    if not target_branch:
        return []

    # List .lazyaf/pipelines/ directory
    files = git_repo_manager.list_directory(repo_id, target_branch, ".lazyaf/pipelines")

    if not files:
        return []

    pipelines = []
    for filename in files:
        if not (filename.endswith('.yaml') or filename.endswith('.yml')):
            continue

        content = git_repo_manager.get_file_content(
            repo_id, target_branch, f".lazyaf/pipelines/{filename}"
        )
        if not content:
            continue

        try:
            data = yaml.safe_load(content.decode('utf-8'))
            pipeline = PipelineYaml(**data)
            pipelines.append(RepoPipelineResponse(
                name=pipeline.name,
                description=pipeline.description,
                steps=[step.model_dump() for step in pipeline.steps],
                source="repo",
                branch=target_branch,
                filename=filename,
            ))
        except Exception as e:
            # Log but continue - skip malformed files
            print(f"[lazyaf_files] Error parsing {filename}: {e}")
            continue

    return pipelines


@router.get("/pipelines/{pipeline_name}", response_model=RepoPipelineResponse)
async def get_repo_pipeline(
    repo_id: str,
    pipeline_name: str,
    branch: str = Query(None, description="Branch to read from (defaults to repo default)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get a specific repo-defined pipeline by name.

    Searches for {pipeline_name}.yaml or {pipeline_name}.yml in .lazyaf/pipelines/.
    """
    repo = await get_repo_or_404(db, repo_id)
    target_branch = branch or repo.default_branch

    if not target_branch:
        raise HTTPException(status_code=404, detail="Pipeline not found")

    # Try both .yaml and .yml extensions
    for ext in ['.yaml', '.yml']:
        filename = f"{pipeline_name}{ext}"
        content = git_repo_manager.get_file_content(
            repo_id, target_branch, f".lazyaf/pipelines/{filename}"
        )
        if content:
            try:
                data = yaml.safe_load(content.decode('utf-8'))
                pipeline = PipelineYaml(**data)
                return RepoPipelineResponse(
                    name=pipeline.name,
                    description=pipeline.description,
                    steps=[step.model_dump() for step in pipeline.steps],
                    source="repo",
                    branch=target_branch,
                    filename=filename,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error parsing pipeline file: {e}")

    raise HTTPException(status_code=404, detail="Pipeline not found")


@router.post("/pipelines/{pipeline_name}/run")
async def run_repo_pipeline(
    repo_id: str,
    pipeline_name: str,
    branch: str = Query(None, description="Branch to read from (defaults to repo default)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Run a repo-defined pipeline.

    Creates or updates a platform pipeline from the repo definition, then runs it.
    """
    repo = await get_repo_or_404(db, repo_id)
    target_branch = branch or repo.default_branch

    if not target_branch:
        raise HTTPException(status_code=400, detail="No branch specified and repo has no default branch")

    # Find and parse the pipeline
    pipeline_data = None
    for ext in ['.yaml', '.yml']:
        filename = f"{pipeline_name}{ext}"
        content = git_repo_manager.get_file_content(
            repo_id, target_branch, f".lazyaf/pipelines/{filename}"
        )
        if content:
            try:
                data = yaml.safe_load(content.decode('utf-8'))
                pipeline_data = PipelineYaml(**data)
                break
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Error parsing pipeline file: {e}")

    if not pipeline_data:
        raise HTTPException(status_code=404, detail="Pipeline not found in repo")

    # Check if a platform pipeline with same name exists for this repo
    result = await db.execute(
        select(Pipeline)
        .where(Pipeline.repo_id == repo_id)
        .where(Pipeline.name == f"[repo] {pipeline_data.name}")
    )
    platform_pipeline = result.scalar_one_or_none()

    # Convert steps to the format expected by Pipeline model
    steps = [step.model_dump() for step in pipeline_data.steps]

    if platform_pipeline:
        # Update existing pipeline with latest definition
        platform_pipeline.description = pipeline_data.description
        platform_pipeline.steps = json.dumps(steps)
    else:
        # Create new platform pipeline from repo definition
        import uuid
        platform_pipeline = Pipeline(
            id=str(uuid.uuid4()),
            repo_id=repo_id,
            name=f"[repo] {pipeline_data.name}",
            description=pipeline_data.description,
            steps=json.dumps(steps),
            is_template=False,
        )
        db.add(platform_pipeline)

    await db.commit()
    await db.refresh(platform_pipeline)

    # Run the pipeline
    try:
        run = await pipeline_executor.start_run(
            db=db,
            pipeline=platform_pipeline,
            trigger_type="manual",
            trigger_ref=f"repo:{target_branch}",
        )
        return {
            "pipeline_id": platform_pipeline.id,
            "run_id": run.id,
            "status": run.status,
            "message": f"Started pipeline run for '{pipeline_data.name}'"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start pipeline: {e}")
