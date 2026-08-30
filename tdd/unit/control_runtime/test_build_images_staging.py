"""
Staged build context + parent-hash chain (Phase 12.5, agent A).

`lazyaf-agent-base` installs runner-common, which lives at
REPO_ROOT/runner-common and must NOT be vendored under images/ (a second copy
of the executors in git is the R3 violation 12.5 exists to remove) and must
NOT be bind-mounted (images must be self-contained for 12.6 remote nodes). So
`scripts/build_images.py` assembles a TEMPORARY build context per image: its
own images/<subdir>/ tree plus each declared extra source, minus
STAGE_EXCLUDE, and hashes THAT.

Two properties carry the whole design and both are pinned here:

1. determinism — the same content stages to the same hash, on a CRLF checkout
   too (dogfood run #8 paid for the sort-key half of this lesson: a host label
   of 723a51a4 against an in-container 115b6472 for identical content);
2. relevance — editing `runner-common/tests/**` must NOT restamp an agent
   image, while editing `runner_common/executors/claude.py` MUST. Get that
   backwards and either `--check` is permanently red or a stale agent runtime
   ships silently.

No docker daemon is touched: staging and hashing are pure filesystem work.
Placement note: this file lives beside the other control-runtime contract
tests (rather than in runner-common/tests/) so the T1 tier actually runs it —
runner-common's own suite is not in any tier selection.
"""
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_images  # noqa: E402
from build_images import (  # noqa: E402
    IMAGES,
    IMAGES_ROOT,
    STAGE_EXCLUDE,
    stage_context,
    tree_hash,
)


@pytest.fixture
def staged():
    """stage_context with guaranteed cleanup (the script does this in a
    finally; a test that leaked temp dirs would hide that it must)."""
    created = []

    def _stage(image_dir, extras=()):
        path = stage_context(image_dir, extras)
        created.append(path)
        return path

    yield _stage
    for path in created:
        shutil.rmtree(path, ignore_errors=True)


def write(root: Path, relative: str, content: bytes) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


@pytest.fixture
def fake_pair(tmp_path):
    """A synthetic (image dir, package dir) pair to mutate freely.

    Mutating the REAL runner-common to prove "an executor edit restamps the
    image" would be a test that edits the tree it is testing.
    """
    image_dir = tmp_path / "image"
    write(image_dir, "Dockerfile", b"FROM lazyaf-base:dev\nCOPY pkg/ /opt/pkg/\n")

    package = tmp_path / "pkg"
    write(package, "pyproject.toml", b"[project]\nname = 'pkg'\n")
    write(package, "pkg/__init__.py", b"")
    write(package, "pkg/executors/claude.py", b"CLI = 'claude'\n")
    write(package, "tests/test_pkg.py", b"def test_x(): assert True\n")
    return image_dir, package


class TestStagedContents:
    def test_image_dir_and_extras_land_side_by_side(self, staged, fake_pair):
        image_dir, package = fake_pair
        context = staged(image_dir, [(package, "runner-common")])

        names = sorted(
            str(p.relative_to(context)).replace("\\", "/")
            for p in context.rglob("*")
            if p.is_file()
        )
        assert names == [
            "Dockerfile",
            "runner-common/pkg/__init__.py",
            "runner-common/pkg/executors/claude.py",
            "runner-common/pyproject.toml",
        ]

    def test_the_real_agent_base_context_carries_the_executors(self, staged):
        context = staged(
            IMAGES_ROOT / "agent-base",
            [(REPO_ROOT / "runner-common", "runner-common")],
        )
        for expected in (
            "Dockerfile",
            "runner-common/pyproject.toml",
            "runner-common/runner_common/agent_wrapper.py",
            "runner-common/runner_common/agent_config.py",
            "runner-common/runner_common/usage.py",
            "runner-common/runner_common/executors/claude.py",
        ):
            assert (context / expected).exists(), expected

    @pytest.mark.parametrize(
        "relative",
        [
            "runner-common/tests/test_pkg.py",
            "runner-common/.venv/pyvenv.cfg",
            "runner-common/runner_common/__pycache__/x.cpython-312.pyc",
            "runner-common/runner_common.egg-info/PKG-INFO",
            "runner-common/dist/pkg-0.1.0.tar.gz",
            "runner-common/uv.lock",
            "runner-common/.pytest_cache/v/cache/lastfailed",
        ],
    )
    def test_excluded_paths_never_reach_the_context(
        self, staged, tmp_path, relative
    ):
        image_dir = tmp_path / "image"
        write(image_dir, "Dockerfile", b"FROM lazyaf-base:dev\n")
        package = tmp_path / "pkg"
        write(package, "pyproject.toml", b"[project]\n")
        write(package, relative.split("/", 1)[1], b"junk")

        context = staged(image_dir, [(package, "runner-common")])
        assert not (context / relative).exists()
        assert (context / "runner-common" / "pyproject.toml").exists()

    def test_missing_extra_source_fails_loudly(self, tmp_path):
        image_dir = tmp_path / "image"
        write(image_dir, "Dockerfile", b"FROM lazyaf-base:dev\n")
        with pytest.raises(FileNotFoundError, match="staged context source missing"):
            stage_context(image_dir, [(tmp_path / "nope", "runner-common")])

    def test_stage_exclude_still_names_the_designed_tokens(self):
        """A future cleanup may ADD tokens; dropping one of these silently
        reintroduces the churn the staging design exists to prevent."""
        for token in ("__pycache__", ".egg-info", "dist", ".venv", "uv.lock", "tests"):
            assert token in STAGE_EXCLUDE


class TestDeterminism:
    def test_staging_the_same_tree_twice_yields_the_same_hash(
        self, staged, fake_pair
    ):
        image_dir, package = fake_pair
        first = tree_hash(staged(image_dir, [(package, "runner-common")]))
        second = tree_hash(staged(image_dir, [(package, "runner-common")]))
        assert first == second

    def test_the_real_agent_base_hash_is_stable_across_stagings(self, staged):
        extras = [(REPO_ROOT / "runner-common", "runner-common")]
        first = tree_hash(staged(IMAGES_ROOT / "agent-base", extras))
        second = tree_hash(staged(IMAGES_ROOT / "agent-base", extras))
        assert first == second

    def test_a_crlf_checkout_hashes_identically(self, staged, tmp_path):
        """tree_hash normalizes line endings; the staged copy must not
        reintroduce the difference (shutil.copyfile is binary, so this pins
        that staging did not grow a text-mode copy)."""
        lf_image = tmp_path / "lf"
        write(lf_image, "Dockerfile", b"FROM lazyaf-base:dev\nRUN true\n")
        lf_pkg = tmp_path / "lf-pkg"
        write(lf_pkg, "runner_common/usage.py", b"A = 1\nB = 2\n")

        crlf_image = tmp_path / "crlf"
        write(crlf_image, "Dockerfile", b"FROM lazyaf-base:dev\r\nRUN true\r\n")
        crlf_pkg = tmp_path / "crlf-pkg"
        write(crlf_pkg, "runner_common/usage.py", b"A = 1\r\nB = 2\r\n")

        assert tree_hash(staged(lf_image, [(lf_pkg, "runner-common")])) == tree_hash(
            staged(crlf_image, [(crlf_pkg, "runner-common")])
        )

    def test_windows_path_separators_do_not_change_the_hash(self, staged, fake_pair):
        """The hash key is the POSIX-normalized relative path (dogfood #8)."""
        image_dir, package = fake_pair
        context = staged(image_dir, [(package, "runner-common")])
        keys = [
            str(p.relative_to(context)).replace("\\", "/")
            for p in context.rglob("*")
            if p.is_file()
        ]
        assert all("\\" not in key for key in keys)


class TestRelevance:
    """Only build-relevant edits may restamp an image."""

    def test_editing_the_packages_tests_does_not_change_the_hash(
        self, staged, fake_pair
    ):
        image_dir, package = fake_pair
        before = tree_hash(staged(image_dir, [(package, "runner-common")]))

        write(package, "tests/test_pkg.py", b"def test_x(): assert 1 == 1\n")
        write(package, "tests/test_new_suite.py", b"def test_y(): pass\n")

        assert tree_hash(staged(image_dir, [(package, "runner-common")])) == before

    def test_editing_an_executor_does_change_the_hash(self, staged, fake_pair):
        image_dir, package = fake_pair
        before = tree_hash(staged(image_dir, [(package, "runner-common")]))

        write(package, "pkg/executors/claude.py", b"CLI = 'claude'\nFLAG = '-p'\n")

        assert tree_hash(staged(image_dir, [(package, "runner-common")])) != before

    def test_editing_the_dockerfile_changes_the_hash(self, staged, fake_pair):
        image_dir, package = fake_pair
        before = tree_hash(staged(image_dir, [(package, "runner-common")]))
        write(image_dir, "Dockerfile", b"FROM lazyaf-base:dev\nRUN echo new\n")
        assert tree_hash(staged(image_dir, [(package, "runner-common")])) != before

    def test_the_real_agent_base_hash_ignores_runner_common_test_edits(
        self, staged, tmp_path
    ):
        """The property that matters in this repo, on the REAL trees: a copy
        of runner-common with an extra test file stages to the same hash."""
        extras_real = [(REPO_ROOT / "runner-common", "runner-common")]
        real = tree_hash(staged(IMAGES_ROOT / "agent-base", extras_real))

        clone = tmp_path / "runner-common"
        shutil.copytree(
            REPO_ROOT / "runner-common",
            clone,
            ignore=shutil.ignore_patterns(
                ".venv", "__pycache__", "*.egg-info", ".pytest_cache"
            ),
        )
        (clone / "tests").mkdir(exist_ok=True)
        (clone / "tests" / "test_brand_new.py").write_text("def test_z(): pass\n")

        assert tree_hash(staged(IMAGES_ROOT / "agent-base", [(clone, "runner-common")])) == real


class TestParentHashChain:
    def test_images_table_declares_the_three_level_tree(self):
        parents = {subdir: parent for subdir, _n, parent, _e in IMAGES}
        assert parents == {
            "base": None,
            "agent-base": "base",
            "claude": "agent-base",
            "gemini": "agent-base",
            "test-runner": "base",
        }

    def test_parents_precede_their_children(self):
        """main() reads hashes[parent]; dependency order is what makes that
        a lookup rather than a KeyError."""
        seen = set()
        for subdir, _name, parent, _extras in IMAGES:
            if parent is not None:
                assert parent in seen, f"{subdir} is built before {parent}"
            seen.add(subdir)

    def test_extra_context_is_declared_only_for_agent_base(self):
        extras = {subdir: extra for subdir, _n, _p, extra in IMAGES}
        assert [name for _src, name in extras["agent-base"]] == ["runner-common"]
        for subdir in ("base", "claude", "gemini", "test-runner"):
            assert extras[subdir] == []

    def test_a_base_change_restamps_the_grandchild(self, staged, tmp_path):
        """base -> agent-base -> claude: the whole point of chaining."""
        base_dir = tmp_path / "base"
        write(base_dir, "Dockerfile", b"FROM python:3.12-slim\n")
        agent_dir = tmp_path / "agent-base"
        write(agent_dir, "Dockerfile", b"FROM lazyaf-base:dev\n")
        claude_dir = tmp_path / "claude"
        write(claude_dir, "Dockerfile", b"FROM lazyaf-agent-base:dev\n")

        def chain():
            base = tree_hash(staged(base_dir))
            agent = tree_hash(staged(agent_dir), extra=base)
            claude = tree_hash(staged(claude_dir), extra=agent)
            return base, agent, claude

        before = chain()
        write(base_dir, "Dockerfile", b"FROM python:3.12-slim\nRUN apt-get update\n")
        after = chain()

        assert after[0] != before[0], "base itself changed"
        assert after[1] != before[1], "agent-base must chain base"
        assert after[2] != before[2], "claude must chain agent-base which chains base"

    def test_sibling_children_do_not_collide(self, staged, tmp_path):
        """claude and gemini share a parent; identical parent hash plus
        different own content must still yield different hashes."""
        claude_dir = tmp_path / "claude"
        write(claude_dir, "Dockerfile", b"FROM lazyaf-agent-base:dev\nRUN claude\n")
        gemini_dir = tmp_path / "gemini"
        write(gemini_dir, "Dockerfile", b"FROM lazyaf-agent-base:dev\nRUN gemini\n")

        parent = "deadbeef1234"
        assert tree_hash(staged(claude_dir), extra=parent) != tree_hash(
            staged(gemini_dir), extra=parent
        )


class TestBaseHashDidNotMove:
    def test_staging_base_reproduces_the_pre_12_5_direct_hash(self, staged):
        """Before 12.5 the base hash was tree_hash(images/base) directly.
        Staging must not have moved it, or every existing lazyaf-base:dev on
        every developer machine goes stale for no reason."""
        direct = tree_hash(IMAGES_ROOT / "base")
        assert tree_hash(staged(IMAGES_ROOT / "base")) == direct


class TestPublicSignatures:
    def test_build_image_and_tree_hash_keep_their_signatures(self):
        """tdd/integration/services/test_control_roundtrip.py imports both
        and calls build_image(client, dir, ref, hash) / tree_hash(dir)."""
        import inspect

        assert list(inspect.signature(build_images.build_image).parameters) == [
            "client",
            "directory",
            "image_ref",
            "content_hash",
        ]
        assert list(inspect.signature(tree_hash).parameters) == ["directory", "extra"]
