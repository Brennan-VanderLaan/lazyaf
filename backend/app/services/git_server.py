"""
Git server service - manages bare repos and HTTP smart protocol.
"""

import os
import shutil
from pathlib import Path
from io import BytesIO

from dulwich.repo import Repo as DulwichRepo
from dulwich.pack import Pack
from dulwich.protocol import ReceivableProtocol
from dulwich.server import Backend, DictBackend, ReceivePackHandler, UploadPackHandler


# Storage directory for bare repos
GIT_REPOS_DIR = Path(__file__).parent.parent.parent / "git_repos"


class GitRepoManager:
    """Manages bare git repositories for LazyAF."""

    def __init__(self, repos_dir: Path = GIT_REPOS_DIR):
        self.repos_dir = repos_dir
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    def get_repo_path(self, repo_id: str) -> Path:
        """Get the path to a bare repo."""
        return self.repos_dir / f"{repo_id}.git"

    def repo_exists(self, repo_id: str) -> bool:
        """Check if a repo exists."""
        return self.get_repo_path(repo_id).exists()

    def create_bare_repo(self, repo_id: str) -> Path:
        """Create a new bare git repository."""
        repo_path = self.get_repo_path(repo_id)
        if repo_path.exists():
            raise ValueError(f"Repository {repo_id} already exists")

        # Create the directory first - dulwich init_bare on Windows
        # needs the directory to exist before initializing
        repo_path.mkdir(parents=True, exist_ok=True)

        # Create bare repo using dulwich
        DulwichRepo.init_bare(str(repo_path))
        return repo_path

    def delete_repo(self, repo_id: str) -> bool:
        """Delete a repository."""
        repo_path = self.get_repo_path(repo_id)
        if not repo_path.exists():
            return False
        shutil.rmtree(repo_path)
        return True

    def get_repo(self, repo_id: str) -> DulwichRepo | None:
        """Get a dulwich Repo object."""
        repo_path = self.get_repo_path(repo_id)
        if not repo_path.exists():
            return None
        return DulwichRepo(str(repo_path))

    def list_repos(self) -> list[str]:
        """List all repository IDs."""
        repos = []
        for path in self.repos_dir.iterdir():
            if path.is_dir() and path.suffix == ".git":
                repos.append(path.stem)
        return repos

    def get_refs(self, repo_id: str) -> dict[bytes, bytes]:
        """Get all refs for a repo."""
        repo = self.get_repo(repo_id)
        if not repo:
            return {}
        return repo.get_refs()

    def get_default_branch(self, repo_id: str) -> str | None:
        """Get the default branch (HEAD) for a repo."""
        repo = self.get_repo(repo_id)
        if not repo:
            return None
        try:
            head_ref = repo.refs.read_ref(b"HEAD")
            if head_ref and head_ref.startswith(b"ref: refs/heads/"):
                return head_ref[16:].decode("utf-8")
        except Exception:
            pass
        return None


class HTTPGitBackend:
    """HTTP smart protocol handler for git operations."""

    def __init__(self, repo_manager: GitRepoManager):
        self.repo_manager = repo_manager

    def get_info_refs(self, repo_id: str, service: str) -> tuple[bytes, str]:
        """
        Handle GET /info/refs?service=git-upload-pack or git-receive-pack
        Returns (content, content_type)
        """
        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        if service not in ("git-upload-pack", "git-receive-pack"):
            raise ValueError(f"Invalid service: {service}")

        content_type = f"application/x-{service}-advertisement"

        # Build packet-line response
        output = BytesIO()

        # Service announcement
        service_line = f"# service={service}\n".encode()
        output.write(pkt_line(service_line))
        output.write(b"0000")  # Flush packet

        # Get refs
        refs = repo.get_refs()

        if not refs:
            # Empty repo - send capabilities with zero-id
            if service == "git-upload-pack":
                caps = b"multi_ack thin-pack side-band side-band-64k ofs-delta shallow no-progress"
            else:
                caps = b"report-status delete-refs side-band-64k"
            zero_id = b"0" * 40
            output.write(pkt_line(zero_id + b" capabilities^{}\x00" + caps + b"\n"))
        else:
            # Send refs with capabilities on first line
            first = True
            if service == "git-upload-pack":
                caps = b"multi_ack thin-pack side-band side-band-64k ofs-delta shallow no-progress include-tag"
            else:
                caps = b"report-status delete-refs side-band-64k"

            # HEAD first if it exists
            if b"HEAD" in refs:
                ref_line = refs[b"HEAD"].hex().encode() + b" HEAD"
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

            # Then all other refs
            for ref_name, sha in sorted(refs.items()):
                if ref_name == b"HEAD":
                    continue
                ref_line = sha.hex().encode() + b" " + ref_name
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

        output.write(b"0000")  # Final flush
        return output.getvalue(), content_type

    def handle_upload_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-upload-pack (client wants to clone/fetch).
        """
        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        # Parse wants and haves from client
        input_stream = BytesIO(input_data)
        output_stream = BytesIO()

        handler = UploadPackHandler(
            DictBackend({b"/": repo}),
            [b"/"],
            input_stream.read,
            output_stream.write,
        )
        handler.handle()

        return output_stream.getvalue()

    def handle_receive_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-receive-pack (client wants to push).
        """
        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        input_stream = BytesIO(input_data)
        output_stream = BytesIO()

        handler = ReceivePackHandler(
            DictBackend({b"/": repo}),
            [b"/"],
            input_stream.read,
            output_stream.write,
        )
        handler.handle()

        return output_stream.getvalue()


def pkt_line(data: bytes) -> bytes:
    """Encode data as a git pkt-line."""
    length = len(data) + 4  # +4 for the length prefix itself
    return f"{length:04x}".encode() + data


# Singleton instances
git_repo_manager = GitRepoManager()
git_backend = HTTPGitBackend(git_repo_manager)
