"""Tests for .github/scripts/check_binary_version.py (upcoming/go-cli.md §4.4).

The release workflow runs this checker on all six cross-compiled binaries
before `gh release`; it is the one place that proves "the binary IS the tag"
without executing five binaries this host cannot run. It reads `go version
-m` transcripts, so the tests feed it CANNED transcripts through the
checker's `read` seam - the exact shapes go1.26.8 prints, captured on the
dev box on 2026-09-16 (see the module docstring for the two proofs and why
both exist: -trimpath makes Go drop the -ldflags line).

Canned transcripts can only pin the PARSER. The P3 verifiers found the
gap that leaves: build_cli.sh's tag flags (-trimpath -buildvcs=false at the
time) produced binaries with NEITHER proof, so every real tag build was
refused and no canned test could see it. `TestAgainstARealTagBuild` closes
that gap: it copies the Go tree into a scratch git repo, tags it, runs the
REAL scripts/build_cli.sh in tag mode and the REAL checker over its six
binaries - exit 0 at the tag, exit 1 for the mismatch reason (not "no
proof") when built under another tag name. It needs go, git and bash;
a missing tool is a failure naming it, never a skip (R4).

The empty-dist test also drives the real CLI by subprocess, because that is
how release.yml invokes it; everything else goes through `main()` in-process.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_binary_version.py"
BUILD_SH = REPO_ROOT / "scripts" / "build_cli.sh"

MODULE = "github.com/Brennan-VanderLaan/lazyaf"
VERSION_PKG = f"{MODULE}/cli/internal/version"

SIX = [
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("windows", "amd64"),
    ("windows", "arm64"),
]


def load_checker():
    # `.github` is not an importable package name, so load the file directly.
    spec = importlib.util.spec_from_file_location("check_binary_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def asset(version, goos, goarch):
    return checker.asset_name(version, goos, goarch)


def transcript(goos, goarch, *, ldflags_version=None, mod_version="(devel)", trimpath=True):
    """A `go version -m` transcript as go1.26.8 prints it.

    With ldflags_version set, a `build -ldflags=` line is present (the
    no-trimpath shape); otherwise the line is absent, exactly as it is for a
    -trimpath build.
    """
    lines = [
        f"cli/dist/{asset('x', goos, goarch)}: go1.26.8",
        f"\tpath\t{MODULE}/cli/cmd/lazyaf",
        f"\tmod\t{MODULE}\t{mod_version}\t",
        "\tdep\tgithub.com/spf13/cobra\tv1.10.2\th1:DMTTonx5m65Ic0GOoRY2c16WCbHxOOw6xxezuLaBpcU=",
        "\tbuild\t-buildmode=exe",
        "\tbuild\t-compiler=gc",
    ]
    if ldflags_version is not None:
        lines.append(
            f'\tbuild\t-ldflags="-s -w -X {VERSION_PKG}.Version={ldflags_version} '
            f'-X {VERSION_PKG}.Commit=5a91734ed5eabe2ee90e244a3e13f63b88efdcd3"'
        )
    if trimpath:
        lines.append("\tbuild\t-trimpath=true")
    lines += [
        "\tbuild\tCGO_ENABLED=0",
        f"\tbuild\tGOARCH={goarch}",
        f"\tbuild\tGOOS={goos}",
    ]
    return "\n".join(lines) + "\n"


def make_dist(tmp_path, version, transcripts):
    """Write one placeholder file per transcript key and return (dist, reader)."""
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in transcripts:
        (dist / name).write_bytes(b"\x7fELF not really\n")
    # checksums.txt sits beside the binaries in a real dist; it must be ignored.
    (dist / "checksums.txt").write_text("deadbeef  " + next(iter(transcripts)) + "\n")

    def read(path):
        text = transcripts[path.name]
        if isinstance(text, Exception):
            raise text
        return text

    return dist, read


def six_transcripts(version, **kw):
    return {asset(version, o, a): transcript(o, a, ldflags_version=version, **kw) for o, a in SIX}


def run_main(dist, tag, read, capsys):
    code = checker.main(["--tag", tag, "--dist", str(dist)], read=read)
    out = capsys.readouterr()
    return code, out.out, out.err


class TestSixMatching:
    def test_ldflags_proof_exits_0_and_names_every_binary(self, tmp_path, capsys):
        dist, read = make_dist(tmp_path, "0.3.0", six_transcripts("0.3.0", trimpath=False))
        code, out, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 0, err
        for goos, goarch in SIX:
            assert f"OK  {asset('0.3.0', goos, goarch)}" in out
        assert err == ""

    def test_vcs_stamp_proof_exits_0_under_trimpath(self, tmp_path, capsys):
        # build_cli.sh passes -trimpath, so the -ldflags line is gone; the
        # module version Go stamped from the tag is the surviving proof.
        transcripts = {
            asset("0.3.0", o, a): transcript(o, a, mod_version="v0.3.0") for o, a in SIX
        }
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, out, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 0, err
        assert out.count("VCS stamp records module version v0.3.0") == 6

    def test_tag_without_v_is_accepted(self, tmp_path, capsys):
        dist, read = make_dist(tmp_path, "0.3.0", six_transcripts("0.3.0", trimpath=False))
        code, _, _ = run_main(dist, "0.3.0", read, capsys)
        assert code == 0


class TestOneMismatched:
    def test_exits_1_naming_the_binary_and_only_it(self, tmp_path, capsys):
        transcripts = six_transcripts("0.3.0", trimpath=False)
        bad = asset("0.3.0", "darwin", "arm64")
        transcripts[bad] = transcript("darwin", "arm64", ldflags_version="0.2.0", trimpath=False)
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, out, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "RELEASE VERSION MISMATCH" in err
        assert f"{bad}: -ldflags records version.Version=0.2.0" in err
        assert "tagged v0.3.0" in err
        # The five good ones are not blamed: they appear only as OK lines,
        # never inside the mismatch block.
        blamed = err.split("RELEASE VERSION MISMATCH", 1)[1]
        for goos, goarch in SIX:
            name = asset("0.3.0", goos, goarch)
            if name != bad:
                assert name not in blamed
                assert f"OK  {name}" in err

    def test_file_name_version_must_match_the_tag(self, tmp_path, capsys):
        # A dist built at the wrong ref names its files after that ref.
        dist, read = make_dist(tmp_path, "0.2.0", six_transcripts("0.2.0", trimpath=False))
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "the file name says version 0.2.0" in err

    def test_goos_goarch_must_match_the_name(self, tmp_path, capsys):
        transcripts = six_transcripts("0.3.0", trimpath=False)
        mislabelled = asset("0.3.0", "linux", "arm64")
        transcripts[mislabelled] = transcript("linux", "amd64", ldflags_version="0.3.0", trimpath=False)
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert f"{mislabelled}: the binary records GOOS=linux GOARCH=amd64" in err


class TestNoProof:
    def test_no_ldflags_line_and_devel_mod_is_refused_naming_both(self, tmp_path, capsys):
        # -trimpath AND -buildvcs=false: the flags §2.5 first prescribed for
        # a tag, which is why the P3 verifiers saw every real tag build
        # refused. Neither proof exists, and the refusal says why and how -
        # naming the flag build_cli.sh must not pass.
        transcripts = {asset("0.3.0", o, a): transcript(o, a) for o, a in SIX}
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "no `build -ldflags=` line" in err
        assert "go omits it under -trimpath" in err
        assert "`mod` line reads (devel), not v0.3.0" in err
        assert "-buildvcs on at the tagged commit" in err
        assert "scripts/build_cli.sh - it must not pass -buildvcs=false on a tag" in err

    def test_another_release_version_in_the_vcs_stamp_is_a_mismatch_not_no_proof(self, tmp_path, capsys):
        # The stamp IS a proof - of the wrong version: the dist was named
        # after GITHUB_REF_NAME=v0.3.0 but built at the v0.2.0 commit. That
        # is the §13.3 negative run's shape and it must read as a MISMATCH.
        transcripts = {asset("0.3.0", o, a): transcript(o, a, mod_version="v0.2.0") for o, a in SIX}
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "RELEASE VERSION MISMATCH" in err
        assert f"{asset('0.3.0', 'linux', 'amd64')}: VCS stamp records module version v0.2.0, but this release is tagged v0.3.0" in err
        assert "no proof of version" not in err

    def test_dirty_vcs_stamp_is_refused(self, tmp_path, capsys):
        transcripts = {asset("0.3.0", o, a): transcript(o, a, mod_version="v0.3.0") for o, a in SIX}
        dirty = asset("0.3.0", "windows", "amd64")
        transcripts[dirty] = transcript("windows", "amd64", mod_version="v0.3.0+dirty")
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert f"{dirty}: built from a DIRTY checkout of v0.3.0" in err

    def test_pseudo_version_is_not_the_tag(self, tmp_path, capsys):
        transcripts = {
            asset("0.3.0", o, a): transcript(o, a, mod_version="v0.2.1-0.20260916114907-5a91734ed5ea")
            for o, a in SIX
        }
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "VCS stamp records module version v0.2.1-0.20260916114907-5a91734ed5ea, but this release is tagged v0.3.0" in err
        assert "HEAD carried no v0/v1 tag" in err

    def test_unreadable_binary_is_a_refusal_not_a_traceback(self, tmp_path, capsys):
        transcripts = six_transcripts("0.3.0", trimpath=False)
        broken = asset("0.3.0", "linux", "amd64")
        transcripts[broken] = RuntimeError("`go version -m x` exited 1: not a Go binary")
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert f"{broken}: could not read build info: `go version -m x` exited 1" in err


class TestTheMatrixIsWhole:
    def test_five_binaries_exit_1_naming_the_missing_target(self, tmp_path, capsys):
        transcripts = six_transcripts("0.3.0", trimpath=False)
        del transcripts[asset("0.3.0", "windows", "arm64")]
        dist, read = make_dist(tmp_path, "0.3.0", transcripts)
        code, _, err = run_main(dist, "v0.3.0", read, capsys)
        assert code == 1
        assert "missing lazyaf_0.3.0_windows_arm64.exe" in err
        assert "do not publish a partial matrix" in err

    def test_checksums_txt_is_not_a_binary(self, tmp_path, capsys):
        dist, read = make_dist(tmp_path, "0.3.0", six_transcripts("0.3.0", trimpath=False))
        assert (dist / "checksums.txt").exists()
        code, out, _ = run_main(dist, "v0.3.0", read, capsys)
        assert code == 0
        assert "checksums.txt" not in out


class TestEmptyDist:
    def test_exits_2_through_the_real_cli(self, tmp_path):
        empty = tmp_path / "dist"
        empty.mkdir()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--tag", "v0.3.0", "--dist", str(empty)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2, proc.stderr
        assert "no lazyaf_* binary found" in proc.stderr
        assert "bash scripts/build_cli.sh" in proc.stderr

    def test_missing_directory_is_also_exit_2(self, tmp_path, capsys):
        code, _, err = run_main(tmp_path / "nope", "v0.3.0", lambda p: "", capsys)
        assert code == 2
        assert "nothing to verify" in err


class TestTranscriptParsing:
    """The parser against the literal shapes captured from go1.26.8."""

    def test_real_trimpath_transcript_has_no_ldflags(self):
        text = (
            "cli/dist/lazyaf_dev_linux_arm64: go1.26.8\n"
            f"\tpath\t{MODULE}/cli/cmd/lazyaf\n"
            f"\tmod\t{MODULE}\tv0.2.1-0.20260916114907-5a91734ed5ea+dirty\t\n"
            "\tbuild\t-buildmode=exe\n"
            "\tbuild\t-trimpath=true\n"
            "\tbuild\tGOARCH=arm64\n"
            "\tbuild\tGOOS=linux\n"
            "\tbuild\tvcs.modified=true\n"
        )
        facts = checker.parse_transcript(text)
        assert facts["ldflags_version"] is None
        assert facts["mod_version"] == "v0.2.1-0.20260916114907-5a91734ed5ea+dirty"
        assert facts["settings"]["GOOS"] == "linux"
        assert facts["settings"]["-trimpath"] == "true"

    def test_real_ldflags_transcript(self):
        text = (
            f'\tbuild\t-ldflags="-s -w -X {VERSION_PKG}.Version=9.9.9 '
            f'-X {VERSION_PKG}.Commit=abc"\n'
        )
        assert checker.parse_transcript(text)["ldflags_version"] == "9.9.9"

    @pytest.mark.parametrize(
        "name,ok",
        [
            ("lazyaf_0.3.0_linux_amd64", True),
            ("lazyaf_0.3.0_windows_arm64.exe", True),
            ("lazyaf_0.3.0-rc1_darwin_arm64", True),
            ("lazyaf_0.3.0_linux_amd64.tar.gz", False),
            ("checksums.txt", False),
        ],
    )
    def test_asset_name_grammar(self, name, ok):
        assert bool(checker.ASSET_NAME.match(name)) is ok


# --- the real thing: build_cli.sh in tag mode, the checker over its output ---

# Not the tag under test in any workflow, and a v0 tag on purpose: Go derives
# the module version from the tag only for v0/v1 on a /v-less module path
# (a v9.9.9 tag would stamp a pseudo-version - the verifiers hit that).
REAL_TAG = "v0.9.9"
OTHER_TAG = "v0.9.8"

# What a tag build needs from the tree: the module, the Go sources, THE build
# script, and the repo's own .gitignore - cli/dist/ must be ignored or the
# first binary written dirties the tree and Go stamps `+dirty` on the rest.
CLONE_PATHS = ("go.mod", "go.sum", ".gitignore", "cli/cmd", "cli/internal", "scripts/build_cli.sh")


def need(name, remedy):
    """Absolute path of a tool, or a FAILURE naming it (never a skip, R4)."""
    found = shutil.which(name)
    if not found:
        pytest.fail(f"{name} is not on PATH and TestAgainstARealTagBuild needs it: {remedy}")
    return found


def find_bash():
    """The bash that runs build_cli.sh - the same preference as
    cli/internal/installsh/install_test.go: on Windows the first `bash` on
    PATH may be the WSL launcher in System32, which cannot run a Git Bash
    script against Windows paths; Git for Windows' own bash is wanted."""
    forced = os.environ.get("LAZYAF_TEST_BASH")
    if forced:
        return forced
    found = shutil.which("bash")
    if sys.platform != "win32":
        return found or need("bash", "install bash; build_cli.sh is a bash script")
    if found and "\\windows\\" not in found.lower():
        return found
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <root>\cmd\git.exe -> <root>
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.exists():
                return str(candidate)
    pytest.fail("no Git Bash found (only the WSL launcher, or none); install Git for Windows or set LAZYAF_TEST_BASH")


def clean_env(**overrides):
    """The ambient environment minus anything that would steer build_cli.sh
    (GITHUB_* from a CI runner), plus the given values."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    env.update(overrides)
    return env


def run(argv, cwd, env, what):
    proc = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"{what} exited {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}")
    return proc


@pytest.fixture(scope="module")
def tagged_clone(tmp_path_factory):
    """A scratch git repo holding this tree's Go module, one commit, tagged
    REAL_TAG - the state a release runner's checkout is in at a tag push."""
    git = need("git", "install git; Go stamps the module version from the checkout")
    need("go", "install Go >= 1.21 (it bootstraps the go.mod toolchain); https://go.dev/dl/")
    clone = tmp_path_factory.mktemp("tag-build") / "repo"
    for rel in CLONE_PATHS:
        src, dst = REPO_ROOT / rel, clone / rel
        if not src.exists():
            pytest.fail(f"{src} is missing; the release build needs it (run from a LazyAF checkout)")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    env = clean_env(
        GIT_AUTHOR_NAME="lazyaf-test", GIT_AUTHOR_EMAIL="test@lazyaf.invalid",
        GIT_COMMITTER_NAME="lazyaf-test", GIT_COMMITTER_EMAIL="test@lazyaf.invalid",
    )
    run([git, "init", "-q"], clone, env, "git init")
    run([git, "add", "-A"], clone, env, "git add")
    run([git, "commit", "-q", "-m", "tree at the tag"], clone, env, "git commit")
    run([git, "tag", REAL_TAG], clone, env, "git tag")
    return clone


def build_at(clone, ref_name):
    """Run THE build script exactly as release.yml does at a tag push."""
    git = need("git", "install git")
    sha = run([git, "rev-parse", "HEAD"], clone, clean_env(), "git rev-parse").stdout.strip()
    env = clean_env(GITHUB_REF_TYPE="tag", GITHUB_REF_NAME=ref_name, GITHUB_SHA=sha)
    run([find_bash(), "scripts/build_cli.sh"], clone, env, f"bash scripts/build_cli.sh (tag {ref_name})")
    return clone / "cli" / "dist"


def check(dist, tag):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--tag", tag, "--dist", str(dist)],
        capture_output=True, text=True, env=clean_env(),
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestAgainstARealTagBuild:
    """build_cli.sh's flags and the checker's proofs, pinned to each other on
    the real toolchain - the thing the canned transcripts cannot see."""

    def test_a_clean_tagged_checkout_passes_on_all_six(self, tagged_clone):
        dist = build_at(tagged_clone, REAL_TAG)
        code, out, err = check(dist, REAL_TAG)
        assert code == 0, f"stdout:\n{out}\nstderr:\n{err}"
        assert out.count(f"VCS stamp records module version {REAL_TAG}") == 6, out
        for goos, goarch in SIX:
            assert f"OK  {asset(REAL_TAG[1:], goos, goarch)}" in out

    def test_go_version_m_shape_matches_the_docstring(self, tagged_clone):
        # The facts the checker's docstring asserts about go1.26.8, read off
        # a real binary: -trimpath recorded, NO -ldflags line, a clean VCS
        # stamp. A toolchain that started recording ldflags would not break
        # the check (proof 1 is accepted first) but this test would say so.
        dist = build_at(tagged_clone, REAL_TAG)
        text = checker.go_version_m(dist / asset(REAL_TAG[1:], "linux", "amd64"), need("go", "install Go"))
        facts = checker.parse_transcript(text)
        assert facts["settings"].get("-trimpath") == "true", text
        assert facts["ldflags_version"] is None, "go now records -ldflags under -trimpath; update the docstring"
        assert facts["mod_version"] == REAL_TAG, text
        assert facts["settings"].get("vcs.modified") == "false", text

    def test_built_under_another_tag_name_is_refused_as_a_mismatch(self, tagged_clone):
        # The §13.3 negative run, for the RIGHT reason: the dist is named
        # v0.9.8 (GITHUB_REF_NAME) but the commit is tagged v0.9.9, so the
        # VCS stamp disagrees with the file name and the tag. A refusal
        # that said "no proof of version" here would prove nothing about
        # version comparison - the verifiers' objection.
        dist = build_at(tagged_clone, OTHER_TAG)
        code, out, err = check(dist, OTHER_TAG)
        assert code == 1, f"stdout:\n{out}\nstderr:\n{err}"
        assert "RELEASE VERSION MISMATCH" in err
        assert "no proof of version" not in err
        for goos, goarch in SIX:
            name = asset(OTHER_TAG[1:], goos, goarch)
            assert f"{name}: VCS stamp records module version {REAL_TAG}, but this release is tagged {OTHER_TAG}" in err

    def test_without_trimpath_the_ldflags_line_is_recorded(self, tagged_clone, tmp_path):
        # Proof 1 on the real toolchain (one lane's return value claimed go
        # "never records ldflags in build info"; it does, without -trimpath).
        go = need("go", "install Go")
        out = tmp_path / "lazyaf_9.9.9_linux_amd64"
        env = clean_env(CGO_ENABLED="0", GOOS="linux", GOARCH="amd64")
        run(
            [go, "build", "-buildvcs=false",
             "-ldflags", f"-s -w -X {VERSION_PKG}.Version=9.9.9", "-o", str(out), "./cli/cmd/lazyaf"],
            tagged_clone, env, "go build without -trimpath",
        )
        facts = checker.parse_transcript(checker.go_version_m(out, go))
        assert facts["ldflags_version"] == "9.9.9"
        assert facts["mod_version"] == "(devel)"
