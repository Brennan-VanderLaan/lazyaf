"""The two generated Go tables are current with the Python tables that own them.

`scripts/gen_cli_contracts.py` renders

    cli/internal/doctor/step_images_gen.go      from scripts/build_images.py IMAGES
    cli/internal/envfile/placeholders_gen.go    from backend/app/config.py's
                                                _PLACEHOLDER_SECRETS and
                                                RETIRED_PUBLIC_SECRETS

so that `lazyaf doctor` and `lazyaf init` never carry a hand-copied third
version of either list (go-cli plan 8.3; the Python scripts they replace kept
theirs "in sync by eye", and the drift that invites is the reason this file
exists). The generator is the ONLY writer. This test renders both files
in-process and byte-compares them with the committed ones: a table change
without `python scripts/gen_cli_contracts.py` goes red here, naming that
command, in T1 - before any Go compiles.

It runs in the backend environment (every tier runs pytest from backend/),
so importing `app.config` for the second half is the real import the backend
performs, not a re-parse of the source.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "scripts" / "gen_cli_contracts.py"
REMEDY = "run python scripts/gen_cli_contracts.py and commit the result"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("lazyaf_gen_cli_contracts", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("lazyaf_gen_cli_contracts", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rendered(gen):
    """path -> fresh text, rendered once for the module."""
    return gen.render_all()


class TestCommittedFilesAreCurrent:
    def test_the_generator_names_two_outputs(self, rendered, gen):
        assert set(rendered) == {gen.STEP_IMAGES_GO, gen.PLACEHOLDERS_GO}

    def test_step_images_gen_go_is_current(self, rendered, gen):
        path = gen.STEP_IMAGES_GO
        assert path.exists(), f"{path} is missing - {REMEDY}"
        assert path.read_text(encoding="utf-8") == rendered[path], (
            f"{path.relative_to(REPO_ROOT).as_posix()} is stale against "
            f"scripts/build_images.py IMAGES - {REMEDY}"
        )

    def test_placeholders_gen_go_is_current(self, rendered, gen):
        path = gen.PLACEHOLDERS_GO
        assert path.exists(), f"{path} is missing - {REMEDY}"
        assert path.read_text(encoding="utf-8") == rendered[path], (
            f"{path.relative_to(REPO_ROOT).as_posix()} is stale against "
            f"backend/app/config.py - {REMEDY}"
        )

    def test_check_mode_agrees_with_this_test(self, gen, capsys):
        """`--check` is what the acceptance gate runs; it must see the same
        currency this test sees and say so in the words the plan names."""
        assert gen.main(["--check"]) == 0
        assert "cli contracts current" in capsys.readouterr().out

    def test_check_mode_refuses_a_stale_file_with_a_diff(self, gen, tmp_path, monkeypatch, capsys):
        """The negative: an edited output is reported as stale, with a
        unified diff and the remedy, and NOTHING is written in --check."""
        stale = tmp_path / "step_images_gen.go"
        stale.write_text("package doctor\n\nvar StepImages = []string{}\n", encoding="utf-8")
        monkeypatch.setattr(gen, "STEP_IMAGES_GO", stale)
        before = stale.read_bytes()
        assert gen.main(["--check"]) == 1
        captured = capsys.readouterr()
        assert "stale: " in captured.err
        assert "--- committed/" in captured.err and "+++ fresh render" in captured.err
        assert "gen_cli_contracts.py" in captured.err
        assert stale.read_bytes() == before, "--check must never write"

    def test_write_mode_repairs_a_stale_file(self, gen, tmp_path, monkeypatch, rendered):
        expected = rendered[gen.STEP_IMAGES_GO]  # keyed by the real path, read before patching
        stale = tmp_path / "step_images_gen.go"
        stale.write_text("package doctor\n", encoding="utf-8")
        monkeypatch.setattr(gen, "STEP_IMAGES_GO", stale)
        assert gen.main([]) == 0
        assert stale.read_bytes() == expected.encode("utf-8")
        assert b"\r\n" not in stale.read_bytes(), "LF only, on every platform"


class TestTheRenderedTablesAreTheSources:
    """The rendered VALUES are the Python tables, not a snapshot of them."""

    def test_step_images_are_the_images_table_in_order(self, rendered, gen):
        spec = importlib.util.spec_from_file_location(
            "lazyaf_build_images_for_test", REPO_ROOT / "scripts" / "build_images.py"
        )
        build_images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_images)
        expected = [f"{row[1]}:{build_images.TAG}" for row in build_images.IMAGES]
        assert gen.load_step_images() == expected
        listed = re.findall(r'^\t"([^"]+)",$', rendered[gen.STEP_IMAGES_GO], re.MULTILINE)
        assert listed == expected

    def test_placeholders_are_the_backends_own_sets(self, rendered, gen):
        from app import config  # the backend env is on sys.path under pytest

        placeholders, retired = gen.load_placeholders()
        assert placeholders == sorted(config._PLACEHOLDER_SECRETS)
        assert retired == sorted(config.RETIRED_PUBLIC_SECRETS)
        text = rendered[gen.PLACEHOLDERS_GO]
        for value in placeholders + retired:
            assert f'\t"{value}",' in text
        # Two disjoint tables: a retired constant is refused exactly, a
        # placeholder case-insensitively, and nothing is in both.
        assert not set(placeholders) & set(retired)

    def test_the_backend_refuses_every_rendered_value(self, gen):
        """What the Go binary refuses is what the backend refuses: each
        rendered value is 'not configured' to `config.py` itself."""
        from app import config

        placeholders, retired = gen.load_placeholders()
        for value in placeholders + retired:
            assert config.is_placeholder_secret(value), f"backend accepts {value!r}"


class TestGeneratedFileShape:
    @pytest.mark.parametrize("which", ["STEP_IMAGES_GO", "PLACEHOLDERS_GO"])
    def test_go_marks_the_file_as_generated(self, rendered, gen, which):
        """Go's own convention: `^// Code generated .* DO NOT EDIT\\.$` on a
        line of its own, so tooling and reviewers treat it as output."""
        text = rendered[getattr(gen, which)]
        first = text.split("\n", 1)[0]
        assert re.fullmatch(r"// Code generated .* DO NOT EDIT\.", first), first
        assert "scripts/gen_cli_contracts.py" in first

    def test_packages_match_their_directories(self, rendered, gen):
        assert "\npackage doctor\n" in rendered[gen.STEP_IMAGES_GO]
        assert gen.STEP_IMAGES_GO.parent.name == "doctor"
        assert "\npackage envfile\n" in rendered[gen.PLACEHOLDERS_GO]
        assert gen.PLACEHOLDERS_GO.parent.name == "envfile"

    def test_go_string_literals_are_escaped(self, gen):
        assert gen._go_string('a"b\\c') == '"a\\"b\\\\c"'
        assert gen._go_string("<generate>") == '"<generate>"'

    @pytest.mark.parametrize("which", ["STEP_IMAGES_GO", "PLACEHOLDERS_GO"])
    def test_files_are_gofmt_shaped(self, rendered, gen, which):
        """Tabs for indentation, a trailing comma on every element, one
        trailing newline: what gofmt would emit, so a `gofmt -l` gate in TG
        never lists a generated file."""
        text = rendered[getattr(gen, which)]
        assert text.endswith("}\n") and not text.endswith("\n\n")
        for line in text.splitlines():
            if line.startswith("\t"):
                assert line.endswith(","), line
            assert not line.startswith(" "), f"space-indented: {line!r}"
