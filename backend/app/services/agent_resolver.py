"""
Agent resolution service.

Resolves agent references to actual agent definitions.
Repo-defined agents override platform-defined agents with the same name.

Priority order:
1. Repo-defined agents (.lazyaf/agents/{name}.yaml)
2. Platform-defined agents (database AgentFile table)
"""

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentFile
from app.services.git_server import git_repo_manager


class AgentResolver:
    """
    Resolves agent references to their definitions.

    When an agent is referenced by name (e.g., "test-fixer"), this service
    checks the repository first, then falls back to platform agents.
    """

    async def resolve_agent(
        self,
        db: AsyncSession,
        repo_id: str,
        branch: str,
        agent_name: str,
    ) -> dict | None:
        """
        Resolve an agent reference to its definition.

        Checks repo first, then falls back to platform.

        Args:
            db: Database session
            repo_id: Repository ID
            branch: Branch to read repo agents from
            agent_name: Name of the agent to resolve

        Returns:
            dict with: name, description, prompt_template, source ('repo' or 'platform')
            or None if agent not found
        """
        # 1. Try repo-defined agent
        repo_agent = self._get_repo_agent(repo_id, branch, agent_name)
        if repo_agent:
            return {**repo_agent, "source": "repo"}

        # 2. Fall back to platform agent
        result = await db.execute(
            select(AgentFile).where(AgentFile.name == agent_name)
        )
        platform_agent = result.scalar_one_or_none()
        if platform_agent:
            return {
                "name": platform_agent.name,
                "description": platform_agent.description,
                "prompt_template": platform_agent.content,
                "source": "platform",
            }

        return None

    async def resolve_agents(
        self,
        db: AsyncSession,
        repo_id: str,
        branch: str,
        agent_names: list[str],
    ) -> list[dict]:
        """
        Resolve multiple agent references.

        Args:
            db: Database session
            repo_id: Repository ID
            branch: Branch to read repo agents from
            agent_names: List of agent names to resolve

        Returns:
            List of resolved agent dicts (skips agents that can't be resolved)
        """
        resolved = []
        for name in agent_names:
            agent = await self.resolve_agent(db, repo_id, branch, name)
            if agent:
                resolved.append(agent)
        return resolved

    async def list_all_agents(
        self,
        db: AsyncSession,
        repo_id: str | None,
        branch: str | None,
    ) -> list[dict]:
        """
        List all available agents (repo + platform, with repo overriding platform).

        Args:
            db: Database session
            repo_id: Repository ID (optional - if None, only returns platform agents)
            branch: Branch to read repo agents from (optional)

        Returns:
            List of agent dicts with source info
        """
        agents_by_name = {}

        # 1. Get platform agents first
        result = await db.execute(select(AgentFile))
        platform_agents = result.scalars().all()

        for agent in platform_agents:
            agents_by_name[agent.name] = {
                "name": agent.name,
                "description": agent.description,
                "prompt_template": agent.content,
                "source": "platform",
            }

        # 2. Get repo agents (override platform agents with same name)
        if repo_id and branch:
            repo_agents = self._list_repo_agents(repo_id, branch)
            for agent in repo_agents:
                agents_by_name[agent["name"]] = {**agent, "source": "repo"}

        return list(agents_by_name.values())

    def _get_repo_agent(self, repo_id: str, branch: str, agent_name: str) -> dict | None:
        """Get agent from repo's .lazyaf/agents/ directory."""
        for ext in ['.yaml', '.yml']:
            content = git_repo_manager.get_file_content(
                repo_id, branch, f".lazyaf/agents/{agent_name}{ext}"
            )
            if content:
                try:
                    data = yaml.safe_load(content.decode('utf-8'))
                    return {
                        "name": data.get("name", agent_name),
                        "description": data.get("description"),
                        "prompt_template": data.get("prompt_template", ""),
                    }
                except Exception:
                    pass
        return None

    def _list_repo_agents(self, repo_id: str, branch: str) -> list[dict]:
        """List all agents from repo's .lazyaf/agents/ directory."""
        agents = []

        files = git_repo_manager.list_directory(repo_id, branch, ".lazyaf/agents")
        if not files:
            return agents

        for filename in files:
            if not (filename.endswith('.yaml') or filename.endswith('.yml')):
                continue

            content = git_repo_manager.get_file_content(
                repo_id, branch, f".lazyaf/agents/{filename}"
            )
            if not content:
                continue

            try:
                data = yaml.safe_load(content.decode('utf-8'))
                agents.append({
                    "name": data.get("name", filename.rsplit('.', 1)[0]),
                    "description": data.get("description"),
                    "prompt_template": data.get("prompt_template", ""),
                })
            except Exception:
                continue

        return agents


# Global agent resolver instance
agent_resolver = AgentResolver()
