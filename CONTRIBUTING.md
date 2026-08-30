# Contributing to LazyAF

## The one rule that is new

**Every commit message on `main` must start with a conventional-commit prefix.**

This is not style policing. Since `.github/workflows/release-please.yml` landed,
the commit log *is* the version number and *is* the changelog. A commit without
a prefix is invisible to both: it ships, but it will not appear in
`CHANGELOG.md` and it will not move the version.

The history before this file predates the convention — that is expected and
fine. `bootstrap-sha` in `.github/release-please-config.json` pins the boundary
(`8b567e5`, "release CI: publish the wheel and images"), and release-please
never looks further back than that. Nothing needs rewriting. The convention
applies **from now on**.

---

## Commit format

```
<type>(<optional scope>): <description>

<optional body>

<optional footer>
```

### Types

| Type | Use it for | Shows in CHANGELOG | Version effect |
|---|---|---|---|
| `feat` | A new capability | **Features** | minor |
| `fix` | A bug fix | **Bug Fixes** | patch |
| `perf` | Faster/leaner, same behaviour | **Performance** | patch |
| `refactor` | Restructuring, no behaviour change | **Refactoring** | patch |
| `docs` | Documentation only | **Documentation** | patch |
| `test` | Tests only | hidden | patch |
| `chore` | Housekeeping, deps, tooling | hidden | patch |
| `build` | Build system, packaging | hidden | patch |
| `ci` | `.github/` and pipeline plumbing | hidden | patch |
| `style` | Formatting only | hidden | patch |

"Hidden" means the commit still counts toward *whether* there is a release; it
just does not get its own changelog line. The sections are defined explicitly in
`.github/release-please-config.json` — change them there, not here.

### Breaking changes

Either append `!` to the type, or add a `BREAKING CHANGE:` footer:

```
feat!: runners register over the websocket protocol only

BREAKING CHANGE: the polling endpoints are gone. Runners older than
0.2.0 cannot connect.
```

Both forms produce a `⚠ BREAKING CHANGES` section in the changelog.

### Scopes

Optional and free-form. Useful ones here: `backend`, `cli`, `frontend`,
`runner`, `images`, `control-layer`, `spec`. They appear in the changelog line
and cost nothing.

### Examples

```
feat(cli): lazyaf tests tie junit results back to spec blocks
fix(control-layer): do not report OK when the suite is red
perf(images): reuse the base layer instead of rebuilding it per step
docs: explain the release flow end to end
chore(deps): bump httpx to 0.28
refactor(backend)!: pipeline_executor takes a graph, not a strategy enum
```

---

## Pre-1.0 semantics — what a breaking change actually does

LazyAF is `0.x`. Under **release-please's defaults**
(`bump-minor-pre-major: false`) a breaking change below 1.0 bumps the **major**:
`0.1.0` → `1.0.0`. That is defensible semver, and it is wrong for this project —
the first `feat!` would silently declare 1.0 and the promise that comes with it.

So the config sets **`bump-minor-pre-major: true`**, and leaves
`bump-patch-for-minor-pre-major` at its default (`false`). While the version is
below `1.0.0`:

| Commit | `0.1.0` becomes |
|---|---|
| `fix:` / `perf:` / `refactor:` / `docs:` / `chore:` | `0.1.1` |
| `feat:` | `0.2.0` |
| `feat!:` or `BREAKING CHANGE:` | `0.2.0` |

Read that last row carefully: **below 1.0, a breaking change is
indistinguishable from a feature in the version number.** That is the deal
`0.x` makes — `0.x` is *documented* as unstable, so the minor bump is the
warning. The distinction survives where it matters: the `⚠ BREAKING CHANGES`
section in `CHANGELOG.md` and the GitHub Release notes. Anyone pinning LazyAF
below 1.0 should pin an exact version and read the changelog before moving.

Once the version reaches `1.0.0`, `bump-minor-pre-major` stops applying and
normal semver resumes: `feat!` → `2.0.0`.

### Going to 1.0.0 on purpose

It is a decision, not an accident. Put a `Release-As:` footer in any commit that
lands on `main`:

```
chore: declare the API stable

Release-As: 1.0.0
```

The next release PR will be for exactly that version. The same trick forces any
other version (a hotfix line, a `1.0.0-rc1` prerelease).

---

## The version number lives in exactly one place

`cli/lazyaf/__init__.py`:

```python
# x-release-please-start-version
__version__ = "0.1.0"
# x-release-please-end
```

`cli/pyproject.toml` reads it via `[tool.setuptools.dynamic]`, and the container
images take the same number from the git tag. The bracketing comments are what
release-please's generic updater keys on; it rewrites the version *between*
them. The markers bracket the assignment rather than sitting on the end of it
because `tdd/unit/packaging` reads that line as text, and a trailing comment
would end up inside the version string it parses out.

**Do not edit that line by hand.** release-please rewrites it inside the release
PR. A manual bump only conflicts with the next one, and
`.github/scripts/check_release_version.py` will fail the release if the wheel
and the tag ever disagree.

---

## Pull requests

* Branch off `main`. PRs into `main`.
* `pr-build.yml` builds every release artifact and runs the leak scan. It
  pushes nothing.
* **GitHub does not decide whether your change is correct.** LazyAF's own
  dogfood pipeline does — see `.github/WORKFLOWS.md`. Do not add test jobs to
  the GitHub workflows; that would reverse a standing project decision by
  accident.
* Squash-merge is the assumption. The **squash commit title** is what
  release-please reads, so it is the one that needs the conventional prefix —
  not necessarily every commit on the branch. GitHub defaults that title to the
  PR title, so **name the PR the way you want the changelog to read.**
* A PR that should not appear in the changelog at all: use a hidden type
  (`chore`, `ci`, `test`, `build`, `style`).

---

## Releasing

You do not tag. `release-please` opens a standing release PR; merging it is the
release. The full flow, including how to cut the very first one, is in
[`.github/WORKFLOWS.md`](.github/WORKFLOWS.md#release-please-owns-the-version-number).

---

## Running the tests locally

```bash
cd backend
uv run pytest ../tdd -m "not slow"
```
