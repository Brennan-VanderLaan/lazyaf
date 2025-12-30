"""
Git server service - manages bare repos and HTTP smart protocol.
"""

import os
import shutil
from pathlib import Path
from io import BytesIO

from dulwich.repo import Repo as DulwichRepo


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

    def list_branches(self, repo_id: str) -> list[str]:
        """List all branches in a repo."""
        refs = self.get_refs(repo_id)
        branches = []
        for ref_name in refs.keys():
            if ref_name.startswith(b"refs/heads/"):
                branch_name = ref_name[11:].decode("utf-8")  # Strip "refs/heads/"
                branches.append(branch_name)
        return sorted(branches)

    def get_branch_commit(self, repo_id: str, branch_name: str) -> str | None:
        """Get the commit SHA for a branch."""
        refs = self.get_refs(repo_id)
        ref_key = f"refs/heads/{branch_name}".encode()
        if ref_key in refs:
            # refs values are already 40-byte hex strings in dulwich
            return refs[ref_key].decode("ascii")
        return None

    def get_commit_log(self, repo_id: str, branch_name: str = None, max_count: int = 20) -> list[dict]:
        """Get commit history for a branch."""
        from dulwich.walk import Walker

        repo = self.get_repo(repo_id)
        if not repo:
            return []

        # Determine the starting commit
        if branch_name:
            commit_sha = self.get_branch_commit(repo_id, branch_name)
            if not commit_sha:
                return []
            head = commit_sha.encode('ascii')
        else:
            # Use HEAD
            try:
                head = repo.refs[b'HEAD']
            except KeyError:
                return []

        commits = []
        try:
            walker = Walker(repo.object_store, [head], max_entries=max_count)
            for entry in walker:
                commit = entry.commit
                commits.append({
                    "sha": commit.id.decode('ascii'),
                    "short_sha": commit.id.decode('ascii')[:8],
                    "message": commit.message.decode('utf-8', errors='replace').strip(),
                    "author": commit.author.decode('utf-8', errors='replace'),
                    "timestamp": commit.commit_time,
                })
        except Exception as e:
            print(f"[git_server] Error walking commits: {e}")

        return commits

    def merge_branch(self, repo_id: str, source_branch: str, target_branch: str,
                     author: str = "LazyAF <lazyaf@localhost>") -> dict:
        """
        Merge source_branch into target_branch.

        Returns dict with:
            - success: bool
            - merge_type: 'fast-forward' | 'merge' | None
            - message: str
            - new_sha: str | None (commit sha after merge)
            - error: str | None
        """
        from dulwich.objects import Commit
        import time

        repo = self.get_repo(repo_id)
        if not repo:
            return {"success": False, "error": "Repo not found", "merge_type": None, "new_sha": None, "message": ""}

        source_sha = self.get_branch_commit(repo_id, source_branch)
        target_sha = self.get_branch_commit(repo_id, target_branch)

        if not source_sha:
            return {"success": False, "error": f"Branch '{source_branch}' not found", "merge_type": None, "new_sha": None, "message": ""}
        if not target_sha:
            return {"success": False, "error": f"Branch '{target_branch}' not found", "merge_type": None, "new_sha": None, "message": ""}

        if source_sha == target_sha:
            return {"success": True, "merge_type": None, "new_sha": target_sha, "message": "Already up to date", "error": None}

        try:
            source_commit = repo.object_store[source_sha.encode('ascii')]
            target_commit = repo.object_store[target_sha.encode('ascii')]

            # Check if fast-forward is possible (target is ancestor of source)
            if self._is_ancestor(repo, target_sha, source_sha):
                # Fast-forward: just update the target ref to point to source
                target_ref = f"refs/heads/{target_branch}".encode()
                repo.refs[target_ref] = source_sha.encode('ascii')
                print(f"[git_server] fast-forward merge: {target_branch} -> {source_sha[:8]}")
                return {
                    "success": True,
                    "merge_type": "fast-forward",
                    "new_sha": source_sha,
                    "message": f"Fast-forward merge of {source_branch} into {target_branch}",
                    "error": None
                }

            # Check for merge conflicts by comparing trees
            source_tree = repo.object_store[source_commit.tree]
            target_tree = repo.object_store[target_commit.tree]

            # Find merge base
            merge_base_sha = self._find_merge_base(repo, source_sha, target_sha)
            if not merge_base_sha:
                return {
                    "success": False,
                    "error": "Cannot find common ancestor for merge",
                    "merge_type": None,
                    "new_sha": None,
                    "message": ""
                }

            merge_base_commit = repo.object_store[merge_base_sha.encode('ascii')]
            merge_base_tree = repo.object_store[merge_base_commit.tree]

            # Attempt three-way merge of trees
            merged_tree_sha, conflicts = self._merge_trees(
                repo, merge_base_tree.id, target_tree.id, source_tree.id
            )

            if conflicts:
                # Get detailed conflict information for each file
                conflict_details = self._get_conflict_details(
                    repo, merge_base_tree.id, target_tree.id, source_tree.id, conflicts
                )
                return {
                    "success": False,
                    "error": f"Merge conflicts in: {', '.join(conflicts)}",
                    "merge_type": None,
                    "new_sha": None,
                    "message": "",
                    "conflicts": conflict_details
                }

            # Create merge commit
            commit = Commit()
            commit.tree = merged_tree_sha
            commit.parents = [target_sha.encode('ascii'), source_sha.encode('ascii')]
            commit.author = author.encode('utf-8')
            commit.committer = author.encode('utf-8')
            commit.commit_time = commit.author_time = int(time.time())
            commit.commit_timezone = commit.author_timezone = 0
            commit.encoding = b'UTF-8'
            commit.message = f"Merge branch '{source_branch}' into {target_branch}\n".encode('utf-8')

            # Add commit to object store
            repo.object_store.add_object(commit)

            # Update target branch ref
            target_ref = f"refs/heads/{target_branch}".encode()
            repo.refs[target_ref] = commit.id

            print(f"[git_server] merge commit created: {commit.id.decode('ascii')[:8]}")
            return {
                "success": True,
                "merge_type": "merge",
                "new_sha": commit.id.decode('ascii'),
                "message": f"Merged {source_branch} into {target_branch}",
                "error": None
            }

        except Exception as e:
            import traceback
            print(f"[git_server] merge error: {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e), "merge_type": None, "new_sha": None, "message": ""}

    def _is_ancestor(self, repo, ancestor_sha: str, descendant_sha: str) -> bool:
        """Check if ancestor_sha is an ancestor of descendant_sha."""
        from dulwich.walk import Walker

        try:
            walker = Walker(repo.object_store, [descendant_sha.encode('ascii')], max_entries=1000)
            for entry in walker:
                if entry.commit.id.decode('ascii') == ancestor_sha:
                    return True
        except Exception:
            pass
        return False

    def _find_merge_base(self, repo, sha1: str, sha2: str) -> str | None:
        """Find the common ancestor (merge base) of two commits."""
        from dulwich.walk import Walker

        try:
            # Get all ancestors of sha1
            ancestors1 = set()
            walker = Walker(repo.object_store, [sha1.encode('ascii')], max_entries=1000)
            for entry in walker:
                ancestors1.add(entry.commit.id.decode('ascii'))

            # Find first ancestor of sha2 that's also in ancestors1
            walker = Walker(repo.object_store, [sha2.encode('ascii')], max_entries=1000)
            for entry in walker:
                commit_sha = entry.commit.id.decode('ascii')
                if commit_sha in ancestors1:
                    return commit_sha
        except Exception as e:
            print(f"[git_server] merge base error: {e}")
        return None

    def _merge_trees(self, repo, base_tree_sha, ours_tree_sha, theirs_tree_sha, path_prefix: str = "") -> tuple[bytes, list[str]]:
        """
        Perform a three-way merge of trees (recursively for subdirectories).

        Returns (merged_tree_sha, list_of_conflicts)
        - If entry unchanged in ours, take theirs
        - If entry unchanged in theirs, take ours
        - If both changed the same way, take either
        - If both changed a subdirectory, recursively merge
        - If both changed a file differently, conflict
        """
        from dulwich.objects import Tree
        import stat

        base_tree = repo.object_store[base_tree_sha]
        ours_tree = repo.object_store[ours_tree_sha]
        theirs_tree = repo.object_store[theirs_tree_sha]

        # Get all entries from both sides
        base_entries = {e.path: (e.mode, e.sha) for e in base_tree.items()}
        ours_entries = {e.path: (e.mode, e.sha) for e in ours_tree.items()}
        theirs_entries = {e.path: (e.mode, e.sha) for e in theirs_tree.items()}

        all_paths = set(base_entries.keys()) | set(ours_entries.keys()) | set(theirs_entries.keys())

        merged_entries = {}
        conflicts = []

        for path in all_paths:
            base = base_entries.get(path)
            ours = ours_entries.get(path)
            theirs = theirs_entries.get(path)

            full_path = f"{path_prefix}{path.decode('utf-8', errors='replace')}"

            if ours == theirs:
                # Both sides same - use either (or None if both deleted)
                if ours:
                    merged_entries[path] = ours
            elif ours == base:
                # Ours unchanged from base, take theirs
                if theirs:
                    merged_entries[path] = theirs
                # else: theirs deleted it, so don't include
            elif theirs == base:
                # Theirs unchanged from base, take ours
                if ours:
                    merged_entries[path] = ours
                # else: ours deleted it, so don't include
            else:
                # Both changed - check if it's a directory we can recursively merge
                ours_is_tree = ours and stat.S_ISDIR(ours[0])
                theirs_is_tree = theirs and stat.S_ISDIR(theirs[0])
                base_is_tree = base and stat.S_ISDIR(base[0])

                if ours_is_tree and theirs_is_tree and base_is_tree:
                    # Both sides modified a directory - recursively merge
                    merged_subtree_sha, sub_conflicts = self._merge_trees(
                        repo, base[1], ours[1], theirs[1],
                        path_prefix=f"{full_path}/"
                    )
                    merged_entries[path] = (ours[0], merged_subtree_sha)  # Keep mode from ours
                    conflicts.extend(sub_conflicts)
                elif ours_is_tree and theirs_is_tree and not base_is_tree:
                    # New directory on both sides - try to merge
                    # Use an empty tree as base if directory didn't exist
                    empty_tree = Tree()
                    repo.object_store.add_object(empty_tree)
                    merged_subtree_sha, sub_conflicts = self._merge_trees(
                        repo, empty_tree.id, ours[1], theirs[1],
                        path_prefix=f"{full_path}/"
                    )
                    merged_entries[path] = (ours[0], merged_subtree_sha)
                    conflicts.extend(sub_conflicts)
                else:
                    # File conflict (or type changed file<->dir)
                    conflicts.append(full_path)
                    # Take theirs (source branch) on conflict
                    if theirs:
                        merged_entries[path] = theirs
                    elif ours:
                        merged_entries[path] = ours

        # Build merged tree
        merged_tree = Tree()
        for path, (mode, sha) in sorted(merged_entries.items()):
            merged_tree.add(path, mode, sha)

        repo.object_store.add_object(merged_tree)
        return merged_tree.id, conflicts

    def _get_blob_at_path(self, repo, tree_sha, path: str) -> bytes | None:
        """
        Get blob content at a nested path in a tree.
        Returns None if path doesn't exist or isn't a blob.
        """
        from dulwich.objects import Blob, Tree
        import stat

        parts = path.split('/')
        current_sha = tree_sha

        for i, part in enumerate(parts):
            try:
                obj = repo.object_store[current_sha]
                if not isinstance(obj, Tree):
                    return None

                # Find the entry matching this part
                part_bytes = part.encode('utf-8')
                found = False
                for entry in obj.items():
                    if entry.path == part_bytes:
                        current_sha = entry.sha
                        found = True
                        break

                if not found:
                    return None

            except KeyError:
                return None

        # current_sha should now point to the blob
        try:
            obj = repo.object_store[current_sha]
            if isinstance(obj, Blob):
                return obj.data
        except KeyError:
            pass

        return None

    def _get_conflict_details(self, repo, base_tree_sha, ours_tree_sha, theirs_tree_sha, conflict_paths: list[str]) -> list[dict]:
        """
        Get detailed conflict information including file contents from all three versions.
        Returns a list of conflict dicts with file path and content from base, ours, and theirs.
        Handles nested paths like 'backend/app/main.py'.
        """
        conflict_details = []
        for path_str in conflict_paths:
            # Get content from each version using nested path lookup
            base_data = self._get_blob_at_path(repo, base_tree_sha, path_str)
            ours_data = self._get_blob_at_path(repo, ours_tree_sha, path_str)
            theirs_data = self._get_blob_at_path(repo, theirs_tree_sha, path_str)

            conflict_details.append({
                "path": path_str,
                "base_content": base_data.decode('utf-8', errors='replace') if base_data else None,
                "ours_content": ours_data.decode('utf-8', errors='replace') if ours_data else None,
                "theirs_content": theirs_data.decode('utf-8', errors='replace') if theirs_data else None
            })

        return conflict_details

    def _set_blob_at_path(self, repo, tree_sha, path: str, blob_sha, mode: int = 0o100644):
        """
        Create a new tree with a blob set at the given nested path.
        Returns the new root tree SHA.
        """
        from dulwich.objects import Tree

        parts = path.split('/')
        if len(parts) == 1:
            # Base case: update this tree directly
            tree = repo.object_store[tree_sha]
            new_tree = Tree()
            found = False
            for entry in tree.items():
                if entry.path == parts[0].encode('utf-8'):
                    new_tree.add(entry.path, mode, blob_sha)
                    found = True
                else:
                    new_tree.add(entry.path, entry.mode, entry.sha)
            if not found:
                new_tree.add(parts[0].encode('utf-8'), mode, blob_sha)
            repo.object_store.add_object(new_tree)
            return new_tree.id

        # Recursive case: update subtree and rebuild parent
        tree = repo.object_store[tree_sha]
        new_tree = Tree()
        subdir = parts[0].encode('utf-8')
        rest_path = '/'.join(parts[1:])

        for entry in tree.items():
            if entry.path == subdir:
                # Recurse into this subtree
                new_subtree_sha = self._set_blob_at_path(repo, entry.sha, rest_path, blob_sha, mode)
                new_tree.add(entry.path, entry.mode, new_subtree_sha)
            else:
                new_tree.add(entry.path, entry.mode, entry.sha)

        repo.object_store.add_object(new_tree)
        return new_tree.id

    def resolve_and_merge(self, repo_id: str, source_branch: str, target_branch: str,
                          resolutions: list[dict], author: str = "LazyAF <lazyaf@localhost>") -> dict:
        """
        Resolve merge conflicts and create a merge commit.

        Args:
            repo_id: Repository ID
            source_branch: Source branch to merge from
            target_branch: Target branch to merge into
            resolutions: List of dicts with 'path' and 'content' keys (supports nested paths)
            author: Author string for the commit

        Returns dict with success, merge_type, message, new_sha, and error
        """
        from dulwich.objects import Commit, Tree, Blob
        import time

        repo = self.get_repo(repo_id)
        if not repo:
            return {"success": False, "error": "Repo not found", "merge_type": None, "new_sha": None, "message": ""}

        source_sha = self.get_branch_commit(repo_id, source_branch)
        target_sha = self.get_branch_commit(repo_id, target_branch)

        if not source_sha or not target_sha:
            return {"success": False, "error": "Branch not found", "merge_type": None, "new_sha": None, "message": ""}

        try:
            source_commit = repo.object_store[source_sha.encode('ascii')]
            target_commit = repo.object_store[target_sha.encode('ascii')]

            # Find merge base
            merge_base_sha = self._find_merge_base(repo, source_sha, target_sha)
            if not merge_base_sha:
                return {"success": False, "error": "Cannot find common ancestor", "merge_type": None, "new_sha": None, "message": ""}

            merge_base_commit = repo.object_store[merge_base_sha.encode('ascii')]

            # Perform recursive merge (handles subdirectories)
            merged_tree_sha, conflicts = self._merge_trees(
                repo,
                merge_base_commit.tree,
                target_commit.tree,
                source_commit.tree
            )

            # Check we have resolutions for all conflicts
            resolution_map = {r["path"]: r["content"] for r in resolutions}
            missing = [c for c in conflicts if c not in resolution_map]
            if missing:
                return {
                    "success": False,
                    "error": f"Missing resolution for conflicted files: {', '.join(missing)}",
                    "merge_type": None,
                    "new_sha": None,
                    "message": ""
                }

            # Apply resolutions to the merged tree
            current_tree_sha = merged_tree_sha
            for path, content in resolution_map.items():
                # Create blob with resolved content
                blob = Blob()
                blob.data = content.encode('utf-8')
                repo.object_store.add_object(blob)

                # Update tree with resolved blob
                current_tree_sha = self._set_blob_at_path(repo, current_tree_sha, path, blob.id)

            # Create merge commit
            commit = Commit()
            commit.tree = current_tree_sha
            commit.parents = [target_sha.encode('ascii'), source_sha.encode('ascii')]
            commit.author = author.encode('utf-8')
            commit.committer = author.encode('utf-8')
            commit.commit_time = commit.author_time = int(time.time())
            commit.commit_timezone = commit.author_timezone = 0
            commit.encoding = b'UTF-8'
            commit.message = f"Merge branch '{source_branch}' into {target_branch} (conflicts resolved)\n".encode('utf-8')

            repo.object_store.add_object(commit)

            # Update target branch ref
            target_ref = f"refs/heads/{target_branch}".encode()
            repo.refs[target_ref] = commit.id

            print(f"[git_server] conflict resolution merge commit created: {commit.id.decode('ascii')[:8]}")
            return {
                "success": True,
                "merge_type": "merge",
                "new_sha": commit.id.decode('ascii'),
                "message": f"Merged {source_branch} into {target_branch} with conflict resolution",
                "error": None
            }

        except Exception as e:
            print(f"[git_server] resolve_and_merge error: {e}")
            return {
                "success": False,
                "error": f"Merge failed: {str(e)}",
                "merge_type": None,
                "new_sha": None,
                "message": ""
            }

    def rebase_branch(self, repo_id: str, branch_name: str, onto_branch: str) -> dict:
        """
        Rebase branch_name onto onto_branch (pull in latest changes from onto_branch).

        This is essentially a fast-forward or merge operation where we update branch_name
        to include the latest commits from onto_branch.

        Returns dict with:
            - success: bool
            - message: str
            - new_sha: str | None (commit sha after rebase)
            - error: str | None
        """
        repo = self.get_repo(repo_id)
        if not repo:
            return {"success": False, "error": "Repo not found", "new_sha": None, "message": ""}

        branch_sha = self.get_branch_commit(repo_id, branch_name)
        onto_sha = self.get_branch_commit(repo_id, onto_branch)

        if not branch_sha:
            return {"success": False, "error": f"Branch '{branch_name}' not found", "new_sha": None, "message": ""}
        if not onto_sha:
            return {"success": False, "error": f"Branch '{onto_branch}' not found", "new_sha": None, "message": ""}

        if branch_sha == onto_sha:
            return {"success": True, "new_sha": branch_sha, "message": "Already up to date", "error": None}

        try:
            # Check if onto_branch is ancestor of branch (branch is ahead, no update needed)
            if self._is_ancestor(repo, onto_sha, branch_sha):
                return {"success": True, "new_sha": branch_sha, "message": "Branch is already up to date with target", "error": None}

            # Check if branch is ancestor of onto (can fast-forward)
            if self._is_ancestor(repo, branch_sha, onto_sha):
                # Fast-forward: just update the branch ref to point to onto
                branch_ref = f"refs/heads/{branch_name}".encode()
                repo.refs[branch_ref] = onto_sha.encode('ascii')
                print(f"[git_server] fast-forward rebase: {branch_name} -> {onto_sha[:8]}")
                return {
                    "success": True,
                    "new_sha": onto_sha,
                    "message": f"Fast-forwarded {branch_name} to {onto_branch}",
                    "error": None
                }

            # Branches have diverged - need to do a merge-style rebase
            # For simplicity, we'll create a merge commit rather than replaying commits
            from dulwich.objects import Commit
            import time

            branch_commit = repo.object_store[branch_sha.encode('ascii')]
            onto_commit = repo.object_store[onto_sha.encode('ascii')]

            # Find merge base
            merge_base_sha = self._find_merge_base(repo, branch_sha, onto_sha)
            if not merge_base_sha:
                return {
                    "success": False,
                    "error": "Cannot find common ancestor for rebase",
                    "new_sha": None,
                    "message": ""
                }

            merge_base_commit = repo.object_store[merge_base_sha.encode('ascii')]
            merge_base_tree = repo.object_store[merge_base_commit.tree]
            branch_tree = repo.object_store[branch_commit.tree]
            onto_tree = repo.object_store[onto_commit.tree]

            # Attempt three-way merge of trees
            # In this case: base=merge_base, ours=onto (what we're rebasing onto), theirs=branch (our changes)
            merged_tree_sha, conflicts = self._merge_trees(
                repo, merge_base_tree.id, onto_tree.id, branch_tree.id
            )

            if conflicts:
                # Get detailed conflict information
                conflict_details = self._get_conflict_details(
                    repo, merge_base_tree.id, onto_tree.id, branch_tree.id, conflicts
                )
                return {
                    "success": False,
                    "error": f"Rebase conflicts in: {', '.join(conflicts)}",
                    "new_sha": None,
                    "message": "",
                    "conflicts": conflict_details
                }

            # Create merge commit
            commit = Commit()
            commit.tree = merged_tree_sha
            commit.parents = [onto_sha.encode('ascii'), branch_sha.encode('ascii')]
            commit.author = b"LazyAF <lazyaf@localhost>"
            commit.committer = b"LazyAF <lazyaf@localhost>"
            commit.commit_time = commit.author_time = int(time.time())
            commit.commit_timezone = commit.author_timezone = 0
            commit.encoding = b'UTF-8'
            commit.message = f"Rebase: merge {onto_branch} into {branch_name}\n".encode('utf-8')

            # Add commit to object store
            repo.object_store.add_object(commit)

            # Update branch ref
            branch_ref = f"refs/heads/{branch_name}".encode()
            repo.refs[branch_ref] = commit.id

            print(f"[git_server] rebase commit created: {commit.id.decode('ascii')[:8]}")
            return {
                "success": True,
                "new_sha": commit.id.decode('ascii'),
                "message": f"Rebased {branch_name} onto {onto_branch}",
                "error": None
            }

        except Exception as e:
            import traceback
            print(f"[git_server] rebase error: {e}")
            traceback.print_exc()
            return {"success": False, "error": str(e), "new_sha": None, "message": ""}

    def resolve_rebase_conflicts(self, repo_id: str, branch_name: str, onto_branch: str,
                                  resolutions: list[dict], author: str = "LazyAF <lazyaf@localhost>") -> dict:
        """
        Resolve rebase conflicts and create a merge commit on the feature branch.

        Args:
            repo_id: Repository ID
            branch_name: Branch being rebased (feature branch)
            onto_branch: Branch being rebased onto (e.g. main)
            resolutions: List of dicts with 'path' and 'content' keys
            author: Author string for the commit

        Returns dict with success, new_sha, message, and error
        """
        from dulwich.objects import Commit, Blob
        import time

        repo = self.get_repo(repo_id)
        if not repo:
            return {"success": False, "error": "Repo not found", "new_sha": None, "message": ""}

        branch_sha = self.get_branch_commit(repo_id, branch_name)
        onto_sha = self.get_branch_commit(repo_id, onto_branch)

        if not branch_sha:
            return {"success": False, "error": f"Branch '{branch_name}' not found", "new_sha": None, "message": ""}
        if not onto_sha:
            return {"success": False, "error": f"Branch '{onto_branch}' not found", "new_sha": None, "message": ""}

        try:
            branch_commit = repo.object_store[branch_sha.encode('ascii')]
            onto_commit = repo.object_store[onto_sha.encode('ascii')]

            # Find merge base
            merge_base_sha = self._find_merge_base(repo, branch_sha, onto_sha)
            if not merge_base_sha:
                return {"success": False, "error": "Cannot find common ancestor", "new_sha": None, "message": ""}

            merge_base_commit = repo.object_store[merge_base_sha.encode('ascii')]

            # Perform recursive merge: base=merge_base, ours=onto, theirs=branch
            merged_tree_sha, conflicts = self._merge_trees(
                repo,
                merge_base_commit.tree,
                onto_commit.tree,
                branch_commit.tree
            )

            # Check we have resolutions for all conflicts
            resolution_map = {r["path"]: r["content"] for r in resolutions}
            missing = [c for c in conflicts if c not in resolution_map]
            if missing:
                return {
                    "success": False,
                    "error": f"Missing resolution for conflicted files: {', '.join(missing)}",
                    "new_sha": None,
                    "message": ""
                }

            # Apply resolutions to the merged tree
            current_tree_sha = merged_tree_sha
            for path, content in resolution_map.items():
                # Create blob with resolved content
                blob = Blob()
                blob.data = content.encode('utf-8')
                repo.object_store.add_object(blob)

                # Update tree with resolved blob
                current_tree_sha = self._set_blob_at_path(repo, current_tree_sha, path, blob.id)

            # Create merge commit on the feature branch
            commit = Commit()
            commit.tree = current_tree_sha
            commit.parents = [onto_sha.encode('ascii'), branch_sha.encode('ascii')]
            commit.author = author.encode('utf-8')
            commit.committer = author.encode('utf-8')
            commit.commit_time = commit.author_time = int(time.time())
            commit.commit_timezone = commit.author_timezone = 0
            commit.encoding = b'UTF-8'
            commit.message = f"Rebase: merge {onto_branch} into {branch_name} (conflicts resolved)\n".encode('utf-8')

            repo.object_store.add_object(commit)

            # Update feature branch ref (not the target branch)
            branch_ref = f"refs/heads/{branch_name}".encode()
            repo.refs[branch_ref] = commit.id

            print(f"[git_server] rebase conflict resolution commit created: {commit.id.decode('ascii')[:8]}")
            return {
                "success": True,
                "new_sha": commit.id.decode('ascii'),
                "message": f"Rebased {branch_name} onto {onto_branch} with conflict resolution",
                "error": None
            }

        except Exception as e:
            import traceback
            print(f"[git_server] resolve_rebase_conflicts error: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "error": f"Rebase failed: {str(e)}",
                "new_sha": None,
                "message": ""
            }

    def get_diff(self, repo_id: str, base_branch: str, head_branch: str) -> dict:
        """Get diff between two branches.

        Shows changes unique to head_branch by diffing from merge base to head.
        This prevents showing parallel work on base_branch as "deletions".
        """
        from dulwich.diff_tree import tree_changes
        from dulwich.objects import Blob

        repo = self.get_repo(repo_id)
        if not repo:
            return {"error": "Repo not found", "files": []}

        base_sha = self.get_branch_commit(repo_id, base_branch)
        head_sha = self.get_branch_commit(repo_id, head_branch)

        if not base_sha or not head_sha:
            return {"error": "Branch not found", "files": []}

        try:
            # Find merge base to show only changes unique to head_branch
            # Without this, parallel work on base_branch appears as "deletions"
            merge_base_sha = self._find_merge_base(repo, base_sha, head_sha)

            # Use merge base for diff if found, otherwise fall back to base
            diff_base_sha = merge_base_sha if merge_base_sha else base_sha

            diff_base_commit = repo.object_store[diff_base_sha.encode('ascii')]
            head_commit = repo.object_store[head_sha.encode('ascii')]

            diff_base_tree = repo.object_store[diff_base_commit.tree]
            head_tree = repo.object_store[head_commit.tree]

            changes = tree_changes(repo.object_store, diff_base_tree.id, head_tree.id)

            files = []
            for change in changes:
                # change.old and change.new can be None entirely for adds/deletes
                old_path = change.old.path if change.old else None
                new_path = change.new.path if change.new else None
                old_sha = change.old.sha if change.old else None
                new_sha = change.new.sha if change.new else None

                file_info = {
                    "path": (new_path or old_path).decode('utf-8', errors='replace'),
                    "status": "modified",
                    "additions": 0,
                    "deletions": 0,
                    "diff": "",
                }

                # Determine change type
                if old_sha is None:
                    file_info["status"] = "added"
                elif new_sha is None:
                    file_info["status"] = "deleted"

                # Get file contents for diff
                old_content = ""
                new_content = ""

                if old_sha:
                    try:
                        old_blob = repo.object_store[old_sha]
                        if isinstance(old_blob, Blob):
                            old_content = old_blob.data.decode('utf-8', errors='replace')
                    except Exception:
                        pass

                if new_sha:
                    try:
                        new_blob = repo.object_store[new_sha]
                        if isinstance(new_blob, Blob):
                            new_content = new_blob.data.decode('utf-8', errors='replace')
                    except Exception:
                        pass

                # Generate unified diff
                import difflib
                old_lines = old_content.splitlines(keepends=True)
                new_lines = new_content.splitlines(keepends=True)

                diff_lines = list(difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"a/{file_info['path']}",
                    tofile=f"b/{file_info['path']}",
                ))

                file_info["diff"] = "".join(diff_lines)
                file_info["additions"] = sum(1 for line in diff_lines if line.startswith('+') and not line.startswith('+++'))
                file_info["deletions"] = sum(1 for line in diff_lines if line.startswith('-') and not line.startswith('---'))

                files.append(file_info)

            # Count commits unique to head_branch (in head but not in base)
            # This handles diverged branches correctly by using set difference
            commit_count = 0
            try:
                from dulwich.walk import Walker

                # Get commits reachable from base
                base_commits = set()
                base_walker = Walker(repo.object_store, [base_sha.encode('ascii')], max_entries=1000)
                for entry in base_walker:
                    base_commits.add(entry.commit.id.decode('ascii'))

                # Count commits reachable from head that aren't in base
                head_walker = Walker(repo.object_store, [head_sha.encode('ascii')], max_entries=1000)
                for entry in head_walker:
                    commit_sha = entry.commit.id.decode('ascii')
                    if commit_sha not in base_commits:
                        commit_count += 1
            except Exception as e:
                print(f"[git_server] Error counting commits: {e}")

            return {
                "base_branch": base_branch,
                "head_branch": head_branch,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "merge_base_sha": merge_base_sha,  # Where feature branched off
                "diff_base_sha": diff_base_sha,    # What we're actually diffing from
                "commit_count": commit_count,
                "files": files,
                "total_additions": sum(f["additions"] for f in files),
                "total_deletions": sum(f["deletions"] for f in files),
            }

        except Exception as e:
            print(f"[git_server] Error getting diff: {e}")
            import traceback
            traceback.print_exc()
            return {"error": str(e), "files": []}


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
                # no-done tells client to send everything in one request (no multi-round negotiation)
                caps = b"thin-pack side-band side-band-64k ofs-delta shallow no-progress no-done"
            else:
                # No side-band for receive-pack - simpler response handling
                caps = b"report-status delete-refs ofs-delta"
            zero_id = b"0" * 40
            output.write(pkt_line(zero_id + b" capabilities^{}\x00" + caps + b"\n"))
        else:
            # Send refs with capabilities on first line
            first = True
            if service == "git-upload-pack":
                # no-done tells client to send everything in one request (no multi-round negotiation)
                caps = b"thin-pack side-band side-band-64k ofs-delta shallow no-progress include-tag allow-tip-sha1-in-want allow-reachable-sha1-in-want no-done"
            else:
                # No side-band for receive-pack - simpler response handling
                caps = b"report-status delete-refs ofs-delta"

            # HEAD first if it exists
            if b"HEAD" in refs:
                # refs values are already 40-byte hex strings in dulwich
                ref_line = refs[b"HEAD"] + b" HEAD"
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

            # Then all other refs
            for ref_name, sha in sorted(refs.items()):
                if ref_name == b"HEAD":
                    continue
                # refs values are already 40-byte hex strings in dulwich
                ref_line = sha + b" " + ref_name
                if first:
                    ref_line += b"\x00" + caps
                    first = False
                output.write(pkt_line(ref_line + b"\n"))

        output.write(b"0000")  # Final flush
        return output.getvalue(), content_type

    def handle_upload_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-upload-pack (client wants to clone/fetch).

        Uses dulwich's pack generation to create pack data for requested objects.
        """
        import struct

        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        print(f"[git_server] upload-pack: got {len(input_data)} bytes")

        try:
            input_stream = BytesIO(input_data)
            wants = []
            haves = []
            capabilities = []
            got_done = False

            # Parse pkt-lines for wants and haves
            while True:
                len_bytes = input_stream.read(4)
                if len(len_bytes) < 4:
                    break

                pkt_len = int(len_bytes, 16)
                if pkt_len == 0:
                    # Flush packet - check if there's more (haves section)
                    continue

                if pkt_len == 1:
                    # Delimiter packet
                    continue

                line = input_stream.read(pkt_len - 4)
                if line.endswith(b'\n'):
                    line = line[:-1]

                print(f"[git_server] pkt-line: {line}")

                if line.startswith(b'want '):
                    # Parse: want <sha> [capabilities]
                    parts = line[5:].split(b' ')
                    sha = parts[0]
                    wants.append(sha)
                    if len(parts) > 1:
                        # First want line has capabilities
                        caps = b' '.join(parts[1:]).split(b' ')
                        capabilities.extend(c for c in caps if c)
                elif line.startswith(b'have '):
                    sha = line[5:].strip()
                    haves.append(sha)
                elif line == b'done':
                    got_done = True
                    break

            print(f"[git_server] wants: {[w[:8].decode() if isinstance(w, bytes) else w[:8] for w in wants]}")
            print(f"[git_server] haves: {len(haves)} objects")
            print(f"[git_server] caps: {capabilities}")
            print(f"[git_server] got_done: {got_done}, no-done in caps: {b'no-done' in capabilities}")

            # Debug: check pack directory
            pack_dir = Path(repo.object_store.path) / "pack"
            if pack_dir.exists():
                pack_files = list(pack_dir.glob("*.pack"))
                idx_files = list(pack_dir.glob("*.idx"))
                print(f"[git_server] pack directory has {len(pack_files)} pack files, {len(idx_files)} idx files")
                for pf in pack_files:
                    print(f"[git_server]   - {pf.name} ({pf.stat().st_size} bytes)")
            else:
                print(f"[git_server] no pack directory yet")

            # Force fresh repo to ensure packs are loaded
            repo = self.repo_manager.get_repo(repo_id)
            packs = list(repo.object_store.packs)
            print(f"[git_server] object store has {len(packs)} loaded packs")

            use_sideband = b'side-band-64k' in capabilities or b'side-band' in capabilities

            # Build pack with requested objects
            output = BytesIO()

            # Check if client is using no-done capability (sends everything in one request)
            client_uses_no_done = b'no-done' in capabilities

            # If we haven't received "done" and client isn't using no-done, this is just negotiation
            # Send NAK and wait for the next request with "done"
            if not got_done and not client_uses_no_done and haves:
                print(f"[git_server] No 'done' received - sending NAK for negotiation")
                output.write(pkt_line(b"NAK\n"))
                return output.getvalue()

            if wants:
                # dulwich 0.25+ expects hex SHA for object_store lookups
                def normalize_sha(sha):
                    """Ensure SHA is hex bytes format for object_store lookup."""
                    if isinstance(sha, str):
                        return sha.encode('ascii')
                    if isinstance(sha, bytes) and len(sha) == 20:
                        # Binary SHA - convert to hex
                        return sha.hex().encode('ascii')
                    return sha  # Already hex bytes

                # Collect all objects needed (use hex SHAs for dulwich 0.25+)
                object_ids = set()
                pending = list(wants)  # wants are already hex bytes
                have_set = set(haves)

                while pending:
                    sha_hex = normalize_sha(pending.pop())
                    if sha_hex in object_ids or sha_hex in have_set:
                        continue
                    object_ids.add(sha_hex)

                    try:
                        obj = repo.object_store[sha_hex]
                        # Add parent objects for commits/trees
                        if obj.type_name == b'commit':
                            pending.append(normalize_sha(obj.tree))
                            pending.extend(normalize_sha(p) for p in obj.parents)
                        elif obj.type_name == b'tree':
                            for entry in obj.items():
                                pending.append(normalize_sha(entry.sha))
                    except KeyError as e:
                        print(f"[git_server] object not found: {sha_hex[:16].decode()}")

                print(f"[git_server] packing {len(object_ids)} objects")

                import hashlib

                # Get objects for packing
                def get_objects():
                    for sha_hex in object_ids:
                        try:
                            obj = repo.object_store[sha_hex]
                            yield obj.type_num, obj.as_raw_string()
                        except KeyError as e:
                            print(f"[git_server] skip object {sha_hex[:16].decode()}: {e}")

                # Generate pack
                pack_data = BytesIO()

                # Use dulwich's write_pack_data for correct format
                entries = list(get_objects())
                print(f"[git_server] writing {len(entries)} objects to pack")

                # Write pack header manually for control
                pack_data.write(b'PACK')
                pack_data.write(struct.pack('>I', 2))  # Version 2
                pack_data.write(struct.pack('>I', len(entries)))  # Object count

                # Write objects using proper pack object format
                for type_num, data in entries:
                    self._write_pack_object_v2(pack_data, type_num, data)

                # Get pack content and calculate checksum
                pack_content = pack_data.getvalue()
                checksum = hashlib.sha1(pack_content).digest()

                pack_bytes = pack_content + checksum
                print(f"[git_server] pack size: {len(pack_bytes)} bytes")

                # Send NAK before pack data (simple protocol without multi_ack)
                nak_pkt = pkt_line(b"NAK\n")
                print(f"[git_server] NAK pkt: {nak_pkt!r}")
                output.write(nak_pkt)
                print(f"[git_server] sent NAK, output so far: {output.getvalue()[:50]!r}")

                if use_sideband:
                    # Sideband: band 1 = pack data, band 2 = progress
                    # Send pack data through sideband
                    # Max pkt-line = 65520 (0xfff0), minus 4 for length, minus 1 for band = 65515
                    chunk_size = 65515
                    offset = 0
                    while offset < len(pack_bytes):
                        chunk = pack_bytes[offset:offset + chunk_size]
                        # Band 1 prefix (pack data)
                        sideband_pkt = bytes([1]) + chunk
                        output.write(pkt_line(sideband_pkt))
                        offset += chunk_size

                    output.write(b"0000")  # Flush
                else:
                    # No sideband - just send pack data directly
                    output.write(pack_bytes)
                    output.write(b"0000")

            result = output.getvalue()
            print(f"[git_server] final response: first 100 bytes = {result[:100]!r}")
            print(f"[git_server] final response length: {len(result)} bytes")
            return result

        except Exception as e:
            import traceback
            print(f"[git_server] upload-pack error: {e}")
            traceback.print_exc()
            raise

    def _write_pack_object_v2(self, output: BytesIO, type_num: int, data: bytes) -> None:
        """Write a single object to pack format.

        Pack object format:
        - Header: variable-length encoded type (3 bits) + size
        - Data: zlib compressed object data
        """
        import zlib

        size = len(data)

        # First byte: type (bits 4-6) + size bits 0-3
        c = (type_num << 4) | (size & 0x0f)
        size >>= 4

        # Continue with size if more bits needed
        while size:
            output.write(bytes([c | 0x80]))
            c = size & 0x7f
            size >>= 7
        output.write(bytes([c]))

        # Write zlib compressed data (git uses default compression)
        compressed = zlib.compress(data)
        output.write(compressed)

    def handle_receive_pack(self, repo_id: str, input_data: bytes) -> bytes:
        """
        Handle POST git-receive-pack (client wants to push).

        Stores the pack file directly in objects/pack/ with an index file.
        This is more robust than unpacking to loose objects, especially
        for packs with delta objects.
        """
        import struct

        repo = self.repo_manager.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repository {repo_id} not found")

        print(f"[git_server] receive-pack: got {len(input_data)} bytes")

        try:
            # Parse the incoming data
            # Format: [ref-update-lines] [PACK data]
            # Each ref update: old-sha new-sha ref-name\n
            # Ends with flush (0000) then PACK

            input_stream = BytesIO(input_data)
            output_lines = []

            # Read pkt-lines for ref updates
            ref_updates = []
            while True:
                # Read 4-byte length
                len_bytes = input_stream.read(4)
                if len(len_bytes) < 4:
                    break

                pkt_len = int(len_bytes, 16)
                if pkt_len == 0:
                    # Flush packet - end of ref updates
                    break

                # Read the line (pkt_len includes the 4 length bytes)
                line = input_stream.read(pkt_len - 4)
                if line.endswith(b'\n'):
                    line = line[:-1]

                # Parse: old-sha new-sha ref-name[\0capabilities]
                parts = line.split(b'\x00')[0].split(b' ')
                if len(parts) >= 3:
                    old_sha, new_sha, ref_name = parts[0], parts[1], parts[2]
                    ref_updates.append((old_sha, new_sha, ref_name))
                    print(f"[git_server] ref update: {ref_name.decode()} {old_sha[:8].decode()}..{new_sha[:8].decode()}")

            # Rest is PACK data
            pack_data = input_stream.read()
            print(f"[git_server] pack data: {len(pack_data)} bytes")

            # Track if pack import succeeded - only update refs if it did
            pack_import_success = False
            pack_import_error = None

            if pack_data and pack_data.startswith(b'PACK'):
                print(f"[git_server] importing pack ({len(pack_data)} bytes)")

                # Parse pack header for logging
                assert pack_data[:4] == b'PACK'
                version = struct.unpack('>I', pack_data[4:8])[0]
                num_objects = struct.unpack('>I', pack_data[8:12])[0]
                print(f"[git_server] pack version {version}, {num_objects} objects")

                try:
                    # Use dulwich's add_thin_pack which handles delta resolution
                    # and stores the pack properly in the object store
                    pack_stream = BytesIO(pack_data)

                    def read_all(size):
                        return pack_stream.read(size)

                    def read_some(size):
                        return pack_stream.read(size)

                    print(f"[git_server] importing thin pack via object store...")
                    pack = repo.object_store.add_thin_pack(read_all, read_some)
                    print(f"[git_server] pack imported successfully")

                    # Verify first ref update object is accessible
                    test_sha = ref_updates[0][1] if ref_updates else None
                    if test_sha and test_sha != b'0' * 40:
                        try:
                            obj = repo.object_store[test_sha]
                            print(f"[git_server] verified: {test_sha[:8].decode()} is {obj.type_name.decode()}")
                            pack_import_success = True
                        except KeyError:
                            pack_import_error = f"Object {test_sha[:8].decode()} not accessible after import"
                            print(f"[git_server] ERROR: {pack_import_error}")
                    else:
                        # No objects to verify (delete-only push or empty)
                        pack_import_success = True

                except Exception as e:
                    import traceback
                    pack_import_error = str(e)
                    print(f"[git_server] pack import error: {e}")
                    traceback.print_exc()
            else:
                # No pack data (delete-only push)
                pack_import_success = True

            # Only apply ref updates if pack import succeeded
            if not pack_import_success:
                # Return error response
                output = BytesIO()
                output.write(pkt_line(f"unpack error: {pack_import_error or 'unknown error'}\n".encode()))
                output.write(b"0000")
                return output.getvalue()

            # Apply ref updates
            first_branch_pushed = None
            for old_sha, new_sha, ref_name in ref_updates:
                try:
                    # Convert hex to bytes if needed
                    if new_sha != b'0' * 40:
                        repo.refs[ref_name] = new_sha
                        print(f"[git_server] updated ref {ref_name.decode()}")
                        output_lines.append(f"ok {ref_name.decode()}")
                        # Track first non-lazyaf branch for HEAD
                        if ref_name.startswith(b'refs/heads/') and not first_branch_pushed:
                            branch_name = ref_name[11:].decode()
                            if not branch_name.startswith('lazyaf/'):
                                first_branch_pushed = branch_name
                    else:
                        # Delete ref
                        del repo.refs[ref_name]
                        output_lines.append(f"ok {ref_name.decode()}")
                except Exception as e:
                    print(f"[git_server] ref update error: {e}")
                    output_lines.append(f"ng {ref_name.decode()} {e}")

            # Set HEAD if it doesn't point to a valid branch yet
            if first_branch_pushed:
                try:
                    current_head = repo.refs.read_ref(b"HEAD")
                    # Check if HEAD points to a valid branch
                    if current_head and current_head.startswith(b"ref: refs/heads/"):
                        head_branch = current_head[16:].decode()
                        head_ref = b"refs/heads/" + head_branch.encode()
                        if head_ref not in repo.refs:
                            # HEAD points to non-existent branch, update it
                            repo.refs.set_symbolic_ref(b"HEAD", f"refs/heads/{first_branch_pushed}".encode())
                            print(f"[git_server] updated HEAD to {first_branch_pushed}")
                    elif not current_head or current_head == b"ref: refs/heads/main":
                        # No HEAD or default HEAD, set to first branch
                        repo.refs.set_symbolic_ref(b"HEAD", f"refs/heads/{first_branch_pushed}".encode())
                        print(f"[git_server] set HEAD to {first_branch_pushed}")
                except Exception as e:
                    print(f"[git_server] HEAD update skipped: {e}")

            # Build response in pkt-line format
            output = BytesIO()
            output.write(pkt_line(b"unpack ok\n"))
            for line in output_lines:
                output.write(pkt_line(f"{line}\n".encode()))
            output.write(b"0000")  # Flush packet

            return output.getvalue()

        except Exception as e:
            import traceback
            print(f"[git_server] receive-pack error: {e}")
            traceback.print_exc()
            raise


def pkt_line(data: bytes) -> bytes:
    """Encode data as a git pkt-line."""
    length = len(data) + 4  # +4 for the length prefix itself
    return f"{length:04x}".encode() + data


# Singleton instances
git_repo_manager = GitRepoManager()
git_backend = HTTPGitBackend(git_repo_manager)
