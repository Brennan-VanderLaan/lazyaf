from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Deprecated: local path to repo (kept for migration, will be removed)
    path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Real remote URL (GitHub/GitLab) - used for landing changes
    remote_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    # Whether the repo has been ingested into internal git server
    is_ingested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cards: Mapped[list["Card"]] = relationship("Card", back_populates="repo", cascade="all, delete-orphan")

    @property
    def internal_git_url(self) -> str:
        """URL to clone from internal git server."""
        return f"/git/{self.id}.git"

    def get_internal_git_url(self, base_url: str) -> str:
        """Get full internal git URL with base."""
        return f"{base_url}/git/{self.id}.git"
