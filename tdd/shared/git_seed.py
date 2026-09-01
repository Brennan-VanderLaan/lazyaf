"""Put a real commit on a branch in the internal git server.

ONE definition, imported by every suite that needs it. It existed as a
local helper in test_cards_api.py, and the moment two more suites needed
it the choice was one shared function or a third copy - and this repo has
already paid for the copy-the-helper habit once, when a single new keyword
argument broke four hand-rolled duplicates of the same test double.

Why suites need it: POST /api/cards/{id}/start refuses a repo whose default
branch does not exist, because agent work branches FROM it and the
workspace clones it. An ingested-but-never-pushed repo is a legitimate
state - it is just not one a card can start on - so any fixture for
STARTING cards has to look like a repo somebody actually pushed to.
"""


def seed_branch(repo_id, branch, *, parent=None, path="work.txt", content=b"agent\n"):
    """Put a REAL commit on `branch` in the internal git server.

    Real dulwich objects, so `approve` runs the real merge instead of a
    mocked one: a card that reaches `done` in these tests reached it by
    merging something that existed.
    """
    from dulwich.objects import Blob, Commit, Tree

    from app.services.git_server import git_repo_manager

    repo = git_repo_manager.get_repo(repo_id)
    assert repo is not None, f"repo {repo_id} is not on the internal git server"

    blob = Blob.from_string(content)
    tree = Tree()
    tree.add(path.encode(), 0o100644, blob.id)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"LazyAF QA <qa@lazyaf.test>"
    commit.commit_time = commit.author_time = 1756000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = f"work on {branch}".encode()
    if parent:
        commit.parents = [parent.encode("ascii")]

    repo.object_store.add_object(blob)
    repo.object_store.add_object(tree)
    repo.object_store.add_object(commit)
    repo.refs[f"refs/heads/{branch}".encode()] = commit.id
    return commit.id.decode("ascii")
