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
| `test` | Tests only | hidden | none |
| `chore` | Housekeeping, deps, tooling | hidden | none |
| `build` | Build system, packaging | hidden | none |
| `ci` | `.github/` and pipeline plumbing | hidden | none |
| `style` | Formatting only | hidden | none |

> **Hidden types do not move the version - observed, not assumed.** On
> 2026-09-16 three `chore:`/`ci:` commits landed on `main` after `v0.2.0` and
> release-please's run completed without opening a release PR. Earlier
> revisions of this table said "patch" for every hidden type; that was wrong
> for this repository's configuration, and it matters: a wave that must not
> ship a release mid-flight (the Go CLI's P0-P3, `upcoming/go-cli.md` section
> 4.5) relies on hidden types to keep the standing release PR unchanged.

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
| `fix:` / `perf:` / `refactor:` / `docs:` | `0.1.1` |
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

**The git tag**, which release-please derives from
`.github/.release-please-manifest.json`. Nothing in the tree carries a version
to bump by hand: the container images take the number from the tag, and the
`lazyaf` binary learns it at build time — `scripts/build_cli.sh` stamps
`-ldflags -X .../internal/version.Version=<tag>` on a tag build, and every
other build reports `dev+<sha>` (never a bare semver), so a source build past
the release PR cannot claim to be the release.

`.github/scripts/check_binary_version.py` runs `go version -m` over all six
release binaries and fails the release if any of them recorded a version other
than the tag; the release job also executes the host binary's
`--version --short` and compares it to the tag. Both are hard stops before
`gh release`.

**Until the 0.3.0 cutover** the Python CLI is still in the tree, and its
`cli/lazyaf/__init__.py` still carries a `__version__` that release-please's
`extra-files` block rewrites for the wheel. That is the copy being replaced:
the cutover commit deletes the file and the `extra-files` block together, and
`.github/scripts/check_release_version.py` (the wheel-vs-tag check) goes with
them. Do not edit either version line by hand in the meantime; release-please
rewrites it inside the release PR, and a manual bump only conflicts with the
next one.

**Do not merge the standing release PR before the cutover commit lands.**
release-please refreshes it on every push, so it is mergeable long before the
Go binary is what a release would ship; the pre-merge checklist in
`.github/WORKFLOWS.md` spells out what its diff must contain (`CHANGELOG.md`
and the manifest, nothing else).

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
