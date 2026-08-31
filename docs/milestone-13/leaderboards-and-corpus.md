# Milestone 13 — Leaderboards and Corpus (Amendment A)

**Status:** amendment to the three M13 design documents. It does not replace them.
**Amends:** `phase-specs-and-metrics.md`, `api-surface.md`, `strategy-catalog.md`.
**Answers:** the owner's five requirements, labelled R1–R5 throughout.

| | requirement |
|---|---|
| **R1** | multiple strategies tracked against the SAME repo/problem |
| **R2** | a leaderboard of SOLUTION GRAPHS ranked by how well they solve the suite |
| **R3** | PER-PROBLEM leaderboards as well as aggregate |
| **R4** | easy to add a new problem / repo at a commit |
| **R5** | easy to define the goal / fix for validation, and DURABLE IN VCS |

Where this document and an existing M13 doc disagree, **this document wins and
§8 says exactly which line to change**. Nothing here is a rewrite of a design
that is already right — most of R1–R5 is already designed, and §1 says so and
gets out of the way.

Every claim about the tree was executed or read, not remembered. Citations are
`file:line`.

---

## 1. What is already designed

### R1 — multiple strategies against the same problem: **designed, correct, no changes needed to the core.**

Strategy, case and experiment are indexed first-class columns on `Trial`
(`api-surface.md:1142`, the composite `(experiment_id, benchmark_case_id,
strategy_template_id)`), so "strategy X vs strategy Y on problem P" is an index
scan. Strategy is an axis of the experiment matrix (`api-surface.md:803`), and
the null hypothesis is *mandatory*: `one-shot` is auto-injected as the baseline
and removing it forces the board to print `NO BASELINE — comparisons are
relative only` (`phase-specs-and-metrics.md:823`). The independent variable
cannot be edited under a measurement — `is_frozen` on first `Trial` reference,
409 + fork (`api-surface.md:33`, `:279–286`), mirroring the shipped
`PromptVersion` pattern.

**What this document adds:** a variant identity so the board cannot silently
pool two different variants into one row (§5.4), one precedence rule for the
four places a loop cap can come from (§5.4), and the deletion of two of the
three competing graph dialects (§4).

### R2 — a leaderboard of solution graphs: **the metrics are designed and they are good. The wire shape is not.**

The hard part is done and done carefully: solve-rate at a shared budget as the
fairness normalizer; paired cost-to-solve over `C_shared` with survivorship
bias named and defended in three ways (`phase-specs-and-metrics.md:119–140`);
Kaplan–Meier censored cost correctly demoted below the ranked headline and
*refusing* to print a median it never reached (`:142–159`); regression rate
conditioned on `target_met` because `solved = target_met AND clean` makes the
naive version definitionally zero (`:55`, `:177–191`); speedup never sortable
alone because a fan-out of 8 that discards 7 branches has a magnificent speedup
(`:258–268`); ranking structurally disabled below 5 cases or 3 repeats (`:341`);
and ranking by *paired difference* rather than the marginal-overlap fallacy
(`:364`, pinned by `test_separability_not_overlap.py` at `:887`).

**What this document adds:** §5, which is the board *as it will actually be
rendered* — exact columns, a default **partial** order, one consolidated refusal
list, and the tie rendering. The board JSON in `api-surface.md:900–923` cannot
carry what those rules require and would force the serializer to violate them
(§5.6).

### R3 — per-problem leaderboards: **designed twice, and it is the phase exit gate.**

`case` is a `group_by` value (`api-surface.md:879`) and `GET
/api/bench/board/cases` is a dedicated endpoint that is the 13.3 exit gate
(`api-surface.md:944`, `phase-specs-and-metrics.md:861`). A third view —
`vertical | complexity | contamination_risk` — is also designed
(`phase-specs-and-metrics.md:878`).

**What this document adds:** the per-case *statistics*, which the aggregate
rules do not produce correctly by inheritance (§5.2 — at `|C|=1` the `|C_shared|
< 5` gate is unsatisfiable and the cluster bootstrap degenerates), and the fact
that the shipped starter corpus makes the vertical/complexity view dead on
arrival (§5.3).

### R4 — easy to add a problem/repo at a commit: **the authoring surface is designed at the right altitude; the mechanism underneath it has three holes.**

One YAML per case, human-typable, `bench ingest | case add | validate | lint`,
with `bench lint` container-free so it runs in T1 and joins the dogfood pipeline
— a malformed case breaks CI the day it lands (`phase-specs-and-metrics.md:674–698`).
Pinned-commit clone is real, not aspirational: `git checkout --detach <sha>` at
`backend/app/services/workspace/population.py:147–155`.

**What this document fixes:** the case file names its repo as
`repo: gh-mirror/flask-api-demo` (`phase-specs-and-metrics.md:649`) — a name
that resolves to nothing, on a `Repo.name` with no unique constraint
(`backend/app/models/repo.py:14`), while every API path demands an opaque UUID
(`api-surface.md:104`). And the oracle can only see tests carrying a
`@pytest.mark.lazyaf_test_id` marker, which no upstream repo has
(`runner-common/runner_common/pytest_lazyaf.py:161–163`). §2 fixes both.

### R5 — define the goal, durable in VCS: **the principle is right and one sentence of it is load-bearing. The implementation of "durable" is missing.**

> "the source of truth lives in-repo at `bench/suites/<suite>/cases/<slug>.yaml`;
> the DB is a projection of it" — `phase-specs-and-metrics.md:644`

That is the correct answer and this document keeps it. So is the reuse of
TestRef/TestRun as the oracle store — "The harness adds **no new result store**"
(`api-surface.md:682`) — which is most of an oracle for free, with repo-scoped
identity that cannot leak across repos
(`backend/app/models/testref.py:52–62`, unique
`(repo_id, lazyaf_test_id)`). So is closing the delete-the-guard-test cheat *in
the scoring definition* rather than in a bypassable checker
(`api-surface.md:720`).

**What this document fixes:** there is no disk→DB path anywhere in the design
(the loader appears only as a test target, `phase-specs-and-metrics.md:705`),
the suite content hash is computed over DB rows including a per-install UUID
(§3), and nothing stops a `git pull` from rewriting a case under existing
trials (§3).

---

## 2. The corpus format

### 2.1 The repo-pinning decision

Three options were live. The recommendation is **(2), with (1) as the durable
identity on disk**.

| | how | verdict |
|---|---|---|
| 1. URL + sha, fetched on demand | case records `source_url` + `base_commit_sha`, clone at run time | **the identity, not the mechanism.** A sha is content-addressed, so substitution is impossible. The threat is *availability*: the repo is deleted, made private, or the commit is GC'd after a force-push. That is not hypothetical for the long tail of GitHub. |
| 2. **Internal mirror (RECOMMENDED)** | `bench ingest` fetches the commit into LazyAF's own git server; `Repo.is_ingested` / `internal_git_url` already exist (`backend/app/models/repo.py:20–32`) | **the mechanism.** Solves availability, reuses shipped machinery, and the mirror is what the export bundles. |
| 3. Vendored tree / submodule | copy the tree into `bench/`, or a git submodule | **no.** Vendoring puts N copies of a fixture repo in your history and destroys the provenance claim (it is no longer *that* commit, it is a copy). A submodule has exactly the same upstream-availability failure as (1) with a second VCS to reason about and no mirror. |

**Stated failure mode of the recommendation.** The mirror is *your* copy. If the
mirror is lost and upstream is gone, the case is unreproducible by anyone,
including you. Mitigations, in order: the export bundles the git bundle where
the licence permits redistribution (`phase-specs-and-metrics.md:936` — this is
the right design and should be understood as the *availability* answer, not the
integrity answer, since the sha already gives integrity); and for
licence-restricted repos, `repos/fetch/<repo>.json` + tree-hash verification is
a **best-effort** path that will eventually break for some cases. Say that in
METHOD.md rather than implying permanence.

**Second stated failure mode, and a one-line fix.** `_build_clone_script` clones
`--branch <branch>` *first* and only then detaches
(`backend/app/services/workspace/population.py:147–155`), so a sha not
reachable from the cloned branch fails inside a helper container with a git
error. `bench ingest` must therefore create
`refs/heads/bench/case/<slug>` on the internal mirror pointing at
`base_commit_sha`, and the case's `base_branch` *is* that ref. Reachability
becomes true by construction, and the ref is immutable because nothing ever
pushes to it. `bench validate` refuses to mark a case valid if the ref does not
resolve.

### 2.2 The on-disk unit

```
bench/
  suites/
    core-v1/
      suite.yaml                                  # name, description, nothing derived
      cases/
        requests.leading-path-separators.yaml     # slug = filename, suite = directory
      patches/
        requests.leading-path-separators.tests.diff      # test_patch  (test files only)
        requests.leading-path-separators.reference.diff  # reference_patch (non-test only)
      checks/
        requests.leading-path-separators/         # optional; only for `checks:` cases
  strategies/
    planner-fanout.yaml
  prompts/
    planner-split.md
```

Rules that are not negotiable, because each one closes a hole:

1. **`slug` is the filename and `suite` is the directory.** They are not
   restated inside the file. `phase-specs-and-metrics.md:647–648` states both
   twice with no precedence — in the one format whose whole purpose is being the
   source of truth. `bench lint` rejects a file that carries either key.
2. **`repo_id` never appears on disk.** It is a per-install UUID; putting it in
   the durable artefact is what makes a colleague's `git clone` of `bench/`
   point at nothing.
3. **Observations never appear on disk.** `quarantined_tests`,
   `oracle_file_hashes`, `solvable_verified` are outputs of running the
   validator on *a particular machine*. They live on `BenchmarkCaseValidation`
   (`api-surface.md:198–210`), never on the case. §3 explains why this is the
   difference between a comparable corpus and a silently forked one.

### 2.3 Worked example — a real public repo at a real commit

Repo: **`github.com/psf/requests`**, Apache-2.0.
Fix commit: **`60389df6d69ce833164696dcf36cbb43336d3426`** ("Trim excess leading
path separators", closes psf/requests#6643).
Base commit (its parent): **`7a13c041dbef42f9f3feb14110f02626f6892e9a`**.

This example was chosen deliberately, because it is the shape the design's own
documents disagree about: **the fix commit creates the regression test.**
`tests/test_adapters.py` does not exist at the base commit
(verified: `git ls-tree 7a13c041 tests/` lists eleven entries and that is not
one of them); the fix commit adds it as a new 8-line file with one test, plus a
3-line change to `src/requests/adapters.py`.

`bench/suites/core-v1/cases/requests.leading-path-separators.yaml`:

```yaml
# slug and suite are the path. Do not restate them here.
schema_version: 1

repo:
  source_url:      https://github.com/psf/requests
  base_commit_sha: 7a13c041dbef42f9f3feb14110f02626f6892e9a
  license:         Apache-2.0

task_statement: |
  A URL with excess leading path separators (e.g. http://host//v:h) is passed
  through to urllib3, which re-parses the request-uri as a full URI with a host
  and port. Requests should collapse leading separators so urllib3 sees a
  normal path. See https://github.com/psf/requests/issues/6643

vertical:            web-api
complexity:          small
contamination_risk:  high        # popular repo, public issue + public fix; assume the model has seen it

# --- how the oracle is invoked -------------------------------------------
oracle:
  test_command: "pytest -q tests"        # the RAW suite invocation; lazyaf-oracle owns plugin wiring
  test_image:   lazyaf-test-runner:dev
  setup:        "pip install -e ."
  id_mode:      nodeid                   # oracle ids ARE pytest nodeids; upstream is not modified

# --- the goal ------------------------------------------------------------
fail_to_pass:
  - "tests/test_adapters.py::test_request_url_trims_leading_path_separators"

pass_to_pass:                            # DERIVED by `bench case derive`, not hand-typed (§2.5)
  - "tests/test_utils.py::TestSuperLen::test_string"
  - "tests/test_utils.py::TestSuperLen::test_super_len_with_tell"
  - "tests/test_utils.py::TestIsIPv4Address::test_valid"
  # ... 340 more, machine-derived, sorted, committed

test_patch:      patches/requests.leading-path-separators.tests.diff
reference_patch: patches/requests.leading-path-separators.reference.diff

oracle_paths:                            # the frozen-and-restored closure (§6.2)
  - "tests/"
  - "conftest.py"
  - "pytest.ini"
  - "setup.cfg"
  - "pyproject.toml"

loop_defaults: {max_iterations: 6, budget_usd: "4.00", per_step_timeout: 900}
```

`patches/requests.leading-path-separators.tests.diff` is the **test-only** hunk
of the fix commit — the new `tests/test_adapters.py`, verbatim.
`patches/requests.leading-path-separators.reference.diff` is the **code-only**
hunk — the three lines in `src/requests/adapters.py`. Neither file is written by
hand; §2.5 derives both.

### 2.4 How the goal is defined — three mechanisms, one vocabulary

**(a) `test_patch` — the single most important addition in this document.**

`api-surface.md:141–143` says the `fail_to_pass` test "may not exist yet at
`base_commit_sha`, which for `fail_to_pass` is the normal case (the fix commit
adds it)". `phase-specs-and-metrics.md:681` requires the validator to assert
that every `fail_to_pass` id **fails** at base. A test that does not exist
observes `missing`, not `failed`. **The design's declared normal case is the one
its own validator refuses**, and the worked example above is exactly it.

Worse, if the agent is the one that creates the `fail_to_pass` test, the
measured party writes the measurement. Every downstream number — solve-rate,
cost-to-solve, the whole board — is then decorative.

The resolution is SWE-bench's, and it exists for exactly this reason:

- `test_patch` touches **only** test files. `reference_patch` touches **only**
  non-test files. `bench lint` rejects either violation.
- The harness applies `test_patch` on top of `base_commit_sha` **before**
  validation, so `fail_to_pass` genuinely exists and genuinely fails at base
  (`phase-specs-and-metrics.md:681` is then satisfiable rather than
  contradictory), and **force-reapplies it in the scoring container on every
  iteration** (§6.2).
- The agent's workspace also has `test_patch` applied — it is allowed to *see*
  the failing test; it is simply not able to *change* it, because the scoring
  container restores it.
- `reference_patch` then makes the gold-patch control honest: it proves the
  **code** fix turns the oracle green against tests the agent never touched.

**(b) `id_mode: nodeid` — how an unmodified upstream repo becomes scorable.**

`runner-common/runner_common/pytest_lazyaf.py:161–163` is unambiguous:

```python
test_id = self._marker_id(item)
if test_id is None:
    return  # unannotated tests are never recorded
```

No third-party repo carries `@pytest.mark.lazyaf_test_id`. Under the design as
written, making `psf/requests` scorable means forking it and editing its test
files — which makes `base_commit_sha` a commit that exists **only** in LazyAF's
git server, which silently breaks the `fetch/` reproduction path
(`phase-specs-and-metrics.md:936`, `:946`) for precisely the licence-restricted
cases that need it. Neither document notices; the two halves are individually
correct and jointly incompatible.

The fix is to decouple the oracle id from the upstream source, in the plugin:

- `LAZYAF_TEST_ID_MODE=nodeid` — the collector records **every** collected test
  using its pytest nodeid as the `lazyaf_test_id`. Nothing else changes:
  `TestRef.lazyaf_test_id` is already an arbitrary repo-scoped string
  ("users adopt their own convention",
  `backend/app/models/testref.py:65–67`), and identity is `(repo_id,
  lazyaf_test_id)` so one repo's nodeids can never satisfy another's oracle.
- `LAZYAF_TEST_ID_MAP=<path to JSON>` — optional overlay for cases where a
  stable id must survive an upstream rename. A marker, where present, always
  wins; then the map; then the nodeid.

The upstream tree is never modified, so `source_url` + `base_commit_sha` remains
a valid reproduction recipe five years out, and the licence-gated `fetch/` path
works for the cases that need it.

**Judgement call:** nodeids are less stable than markers — an upstream rename or
a parametrize-id change silently turns an oracle id into `missing`. That is the
right trade: a `missing` id is *visible* (§6.3 makes it a hard failure), whereas
a forked fixture is invisible and poisons reproducibility permanently. Use the
overlay map only when a specific case demonstrably needs it.

**(c) `checks:` — goals that are not "make these test ids pass".**

The design admits no other goal; empty `fail_to_pass` is a 422
(`api-surface.md:1170`). A refactor with no behaviour change, a performance
target, or "the docs match the code" cannot be expressed. But the underlying
mechanism is *already* generic — anything that writes the pinned manifest shape
scores (`images/base/control/run.py:141–149`), and the scoring path never looks
at how the manifest was produced.

So: **keep `fail_to_pass`/`pass_to_pass` as the only scoring vocabulary** — do
not introduce a second oracle concept — and add a producer:

```yaml
checks:
  - {lazyaf_test_id: "perf.p99_under_200ms",       command: "python bench/perf.py --p99 200"}
  - {lazyaf_test_id: "refactor.public_api_stable", command: "python bench/apidiff.py"}
fail_to_pass: ["perf.p99_under_200ms"]
pass_to_pass: ["refactor.public_api_stable"]
```

`lazyaf-oracle` runs each check and emits exactly one manifest entry from its
exit code: `0 → passed`, non-zero → `failed`, did-not-run → absent → `missing`.
Check scripts live in `checks/<slug>/` and are inside `oracle_paths`, so they
are frozen and restored like any other oracle file. The 422 relaxes to *"empty
`fail_to_pass` **and** empty `checks` is a 422"*.

### 2.5 How an author derives the oracle ids

Nobody hand-types 343 `pass_to_pass` nodeids. Hand-typing is where authoring
gets abandoned, and R4 is "easy to add".

`api-surface.md:1078–1084` already has the right idea and the wrong wiring:
`bench case add-from-fix` "infers `fail_to_pass` from the tests that flip
red→green across the fix commit". Tests **added** by the fix commit flip
`missing → passing`, not red→green — so the flagship authoring command produces
cases the validator at `phase-specs-and-metrics.md:681` rejects.

Replace it with `lazyaf bench case derive`, which runs the suite **three times**
and is self-checking:

```
$ lazyaf bench case derive core-v1 \
      --repo https://github.com/psf/requests \
      --fix 60389df6d69ce833164696dcf36cbb43336d3426 \
      --slug requests.leading-path-separators \
      --license Apache-2.0 --contamination high

  base = 60389df6^ = 7a13c041dbef42f9f3feb14110f02626f6892e9a
  splitting fix commit ..... 1 test hunk (tests/test_adapters.py), 1 code hunk (src/requests/adapters.py)

  run 1/3  at base                          343 passed,   0 failed,  0 missing
  run 2/3  at base + test_patch             343 passed,   1 failed,  0 missing
  run 3/3  at fix                           344 passed,   0 failed,  0 missing

  fail_to_pass  (1)   failed in run 2, passes in run 3
    tests/test_adapters.py::test_request_url_trims_leading_path_separators
  pass_to_pass  (343) green in run 1 AND run 2 AND run 3
  REJECTED      (0)   candidates still missing with the test patch applied
  UNSTABLE      (0)   ids whose status differed across the 3 runs

  wrote  cases/requests.leading-path-separators.yaml
         patches/requests.leading-path-separators.tests.diff
         patches/requests.leading-path-separators.reference.diff

  running bench validate ...                                          VALID
```

Three properties that matter:

- **Run 2 is the one the current design is missing.** It is what proves the
  `fail_to_pass` id genuinely fails at base once the test exists, rather than
  merely being absent.
- **`REJECTED` is printed, not swallowed.** An id that is still `missing` with
  the test patch applied is a derivation failure and the author must see it.
- **`derive` refuses to leave a file behind if `bench validate` is not green.**
  Inference that authors a measurement silently is the failure mode; inference
  that must pass the validator before it writes is a feature.

### 2.6 What makes a case invalid

`bench validate` runs the battery and writes a `BenchmarkCaseValidation` row.
`BenchmarkCase.validation_status` mirrors the newest one, and the orchestrator
**refuses to launch a trial on a case that is not `valid`**
(`api-surface.md:213–216`). That refusal is the single best structural idea in
the corpus design: the base-state control stops being discipline and becomes
construction. Keep it exactly.

The battery, with this document's additions marked **[new]**:

| # | check | invalid when |
|---|---|---|
| 1 | **base state** — run at `base_commit_sha` + `test_patch` | any `fail_to_pass` id is not `failed`; any `pass_to_pass` id is not `passed` |
| 2 | **flake screen** — repeat check 1 with `k=3` | an id's status differs across the three runs → ejected to `quarantined_tests` *with a warning, not a silent drop* (`phase-specs-and-metrics.md:681`), and if a `fail_to_pass` id is unstable the **case** is invalid |
| 3 | **solvable** — apply `reference_patch`, re-score | not every `fail_to_pass` passes, or any `pass_to_pass` breaks. If there is no `reference_patch`, `solvable_verified: false` and the case is flagged, not rejected |
| 4 | **oracle closure** — hash the `oracle_paths` closure | closure is empty, or a declared path does not exist |
| 5 | **metadata** | `license` or `contamination_risk` absent (`phase-specs-and-metrics.md:687`) |
| 6 | **[new] patch discipline** | `test_patch` touches a non-test path; `reference_patch` touches a test path; `test_patch` does not apply cleanly at `base_commit_sha` |
| 7 | **[new] ref reachability** | `refs/heads/bench/case/<slug>` does not resolve to `base_commit_sha` on the internal mirror |
| 8 | **[new] goal non-empty** | `fail_to_pass` **and** `checks` are both empty |
| 9 | **[new] manifest floor** | the run produced no manifest at all → `error:oracle_no_manifest` (§6.3), never "solved nothing" |

`bench lint` is checks 5, 6 and 8 plus schema — no containers, so it runs in T1
and joins the dogfood pipeline (`phase-specs-and-metrics.md:698`). Keep that;
it is the cheapest quality gate in the milestone.

### 2.7 `lazyaf-oracle` — the binary every strategy invokes, currently specified nowhere

Every catalog template runs `lazyaf-oracle run`
(`strategy-catalog.md:163, 222, 298, 424, 506, 511`; `--gate` at `:562, :657`).
It appears in **no** deliverable list in any of the three documents and exists
nowhere in the tree. Meanwhile the one worked `test_command` in the whole design
— `pytest -q` at `phase-specs-and-metrics.md:656` — produces **zero** results:
the plugin deliberately has no `pytest11` entry point and must be loaded with
`-p runner_common.pytest_lazyaf`
(`runner-common/runner_common/pytest_lazyaf.py:41–46`). And
`api-surface.md:248`'s spelling (`pytest -p lazyaf --lazyaf-results ...`) is
wrong in both halves: the plugin defines no `addoption`, so `--lazyaf-results`
exits "unrecognized arguments", and `-p lazyaf` cannot resolve.

Spec `lazyaf-oracle` as a **13.1 deliverable in `runner-common`**:

```
lazyaf-oracle run [--gate]
  1. read the case spec mounted at /case/case.yaml
  2. git checkout <base_commit_sha> -- <oracle_paths>       # restore (§6.2)
  3. git apply <test_patch>                                 # force-reapply
  4. export LAZYAF_TEST_RESULTS_PATH, LAZYAF_REPO_ROOT,
            LAZYAF_TEST_ID_MODE, LAZYAF_TEST_ID_MAP
  5. exec:  <test_command>  with  -p runner_common.pytest_lazyaf  injected
  6. run each `checks:` entry, appending one manifest entry per check
  7. GUARANTEE a manifest exists: every declared oracle id absent from the
     collected results is emitted as `failed` when the suite errored during
     collection, and the run exits with error_class=oracle_collect_error.
     An empty manifest that reads as `missing` is never shipped.
  --gate: exit non-zero iff the iteration is not solved
```

Step 7 is the difference between "the suite failed to collect" and "the agent
solved nothing", and there is no other place to make that distinction (§6.3).
Step 5 means a case author writes `pytest -q tests` and never types
`-p runner_common...`.

**One blocking prerequisite, one line of code.** `lazyaf-test-runner` is built
without staging `runner-common` —
`scripts/build_images.py:83` is `("test-runner", "lazyaf-test-runner", "base", [])`
where `agent-base` at `:80` passes
`[(REPO_ROOT / "runner-common", "runner-common")]`. So `pytest_lazyaf` is not
importable in the one general-purpose test image, and `lazyaf-oracle` cannot
exist there. Stage and `pip install` it, and pin it with a test asserting the
module imports inside the built image. Without this, case #1 fails for the least
interesting possible reason.

### 2.8 Adding a problem: the whole flow

```
$ lazyaf bench ingest https://github.com/psf/requests \
      --at 7a13c041dbef42f9f3feb14110f02626f6892e9a \
      --license Apache-2.0 --contamination high
$ lazyaf bench case derive core-v1 --repo ... --fix 60389df6... --slug requests.leading-path-separators
$ git add bench/ && git commit -m "case: requests leading path separators"
$ git push                       # the push IS the authoring gesture (§3.4)
```

`bench ingest` needs a backend capability that does not exist. The shipped
ingest takes a **local path** or tells you to push by hand
(`backend/app/routers/repos.py:60–95`), and `push_from_local`
(`backend/app/services/git_server.py:95–118`) requires a local `.git` and
pushes all branches with no sha pinning. There is no clone-from-remote-URL path
anywhere in the tree. Name it as a **13.1 backend deliverable, not a CLI one**:
`POST /api/repos/ingest-remote {source_url, commit_sha, license,
contamination_risk}` — fetch the commit, verify the fetched sha matches, create
`refs/heads/bench/case/<slug>`, record `remote_url` and a **new `Repo.license`
column** (licence is a property of the repo, not the case; today two cases on
one repo can declare different licences with nothing to catch it), return
`repo_id`.

---

## 3. Immutability and provenance

The owner's risk, in his framing: *a case edited after results exist silently
invalidates those results.* The design states the principle globally —
"Provenance is worthless if the referent can change under it"
(`api-surface.md:33`) — and implements it for strategies (`is_frozen`,
`content_hash`, `forked_from_id`, `api-surface.md:279–286`). For **cases** it is
only a 409 on the API PATCH path (`api-surface.md:96`). The disk path, which is
the path that matters, is unguarded.

### 3.1 What the hash covers — and the bug that must be fixed before the first published bundle

`api-surface.md:79–82` defines the suite `content_hash` as "sha256 over
canonical JSON of the suite's **case rows**, sorted by slug, with volatile
fields (`id`, `created_at`, `updated_at`) excluded". `api-surface.md:86–87` then
says "the board refuses to pool trials whose `suite_content_hash` differs".

That hash includes `repo_id` — a **per-install UUID** — and it includes
`quarantined_tests`, which is written by the *machine-local* flake screen
(`phase-specs-and-metrics.md:683`). Two people who `git clone` the identical
`bench/` directory therefore compute different hashes and their numbers refuse
to pool. **This fires on the happy path**, with identical files, identical
commits and correct behaviour by both parties. The quarantine half is worse than
the UUID half: a slower machine quarantines a different id set, so the corpus
itself silently differs.

**The fix, and it is cheap today and unfixable after the first published
bundle:**

```
case_content_hash = sha256( canonical_json( BenchmarkCaseSpec as authored on disk ) )
```

| in the hash | out of the hash |
|---|---|
| slug (from path), suite (from path) | `repo_id` — a per-install UUID, never on disk |
| `repo.{source_url, base_commit_sha, license}` | `quarantined_tests` — a machine observation |
| `task_statement`, `vertical`, `complexity`, `contamination_risk` | `oracle_file_hashes` — a machine observation |
| `oracle.{test_command, test_image, setup, id_mode}` | `solvable_verified` — a machine observation |
| `fail_to_pass`, `pass_to_pass` (sorted), `checks` | `validation_status`, timestamps, ids |
| `sha256` of `test_patch` and `reference_patch` bytes | anything the DB adds |
| `oracle_paths` (sorted), `loop_defaults` | |

```
suite_content_hash = sha256( canonical_json( sorted list of case_content_hash ) )
```

The three ejected fields move to `BenchmarkCaseValidation`, where they already
half-live (`api-surface.md:198–210`). They are **observations, not
definitions**. A quarantine difference between two machines then shows up
honestly as a per-machine validation difference in METHOD.md §5 — which is a
finding — instead of silently forking the corpus.

### 3.2 What a Trial stamps

`Trial` gains, all frozen at trial start:

| field | why |
|---|---|
| `case_content_hash` | **per-case**, so a changed case invalidates *its own* trials, not the whole suite. Today only `suite_content_hash` exists (`api-surface.md:336`) and per §3.1 it is already unstable for unrelated reasons — the one signal that would catch a real edit is also the one that cries wolf. |
| `suite_content_hash` | already designed (`api-surface.md:336`) |
| `strategy_content_hash` | already designed (`api-surface.md:286`) |
| `prompt_content_hash` | **new.** `phase-specs-and-metrics.md:16` makes `prompt_version` half the independent variable, and prompt bodies live **only** in the database (`backend/app/models/spec.py:88–96`, `backend/app/models/experiment.py:319–359`). Neither bundle layout contains a `prompts/` directory. A stranger re-runs with a different prompt body, reports the same variant name, and `PROVENANCE DRIFT` does not fire — it compares harness version, image hashes and model version (`phase-specs-and-metrics.md:946`), not prompts. |
| `variant_hash` | sha256 over canonical `(strategy_content_hash, model_assignment, prompt_content_hash, effective_loop_policy, template_variables)`. §5.4. |

Prompts therefore also move on-disk to `bench/prompts/<name>.md`, loaded by the
same loader, and into the bundle.

### 3.3 What the system does when a hash changes

Three doors, three behaviours, no silent one:

| door | behaviour |
|---|---|
| `PATCH /api/bench/cases/{id}` | **409 once any Trial references the case** — already designed (`api-surface.md:96`). Keep. |
| `git pull` brings a changed `cases/<slug>.yaml` | the loader recomputes `case_content_hash`, finds trials that reference the old one, and **refuses to mutate**, printing the trial count and the two hashes. Three sanctioned escapes: `--fork-suite <new>` (the same escape strategies already have), a new slug (a different problem), or `--supersede` (retires the old case: existing trials keep pointing at the frozen old spec, new trials use the new one, and the board renders them as **two cases**, never pooled). |
| the board renders a mixed set | any group whose rows carry more than one distinct `case_content_hash` for the same slug renders `BLOCKED: case <slug> edited mid-experiment (2 versions)` and is not ranked. |

**The quarantine write-back is a live drift source in the design's own happy
path** and must be closed: `bench validate` **prints a suggested YAML diff for
the author to commit deliberately** and never writes to the corpus itself. A
machine observation must not silently rewrite the corpus definition.

### 3.4 Making "durable in VCS" a workflow, not a filing convention

There is **no disk→DB command anywhere in the design**. Disk is declared the
source of truth (`phase-specs-and-metrics.md:644`) but the loader appears only
as a test target (`:705`). Meanwhile `POST /api/bench/suites/{id}/cases`
(`api-surface.md:93`) writes DB rows that never appear on disk. Both directions
the owner needs dead-end: a colleague's `git pull`ed case cannot be ingested,
and a UI-authored case is invisible to VCS — the exact failure R5 exists to
prevent.

LazyAF already solved this shape once. `.lazyaf/pipelines/*.yaml` sync on push,
with a subtree-sha short-circuit at
`backend/app/services/git_server.py:1595–1605` ("two commits whose
`.lazyaf/pipelines` subtree shas match define identical pipeline files"). Reuse
it:

- `bench/` lives in a repo the platform mirrors. A `git push` touching `bench/`
  triggers the same sync path, keyed on the `bench/` subtree sha via the
  existing `get_tree_sha_at_commit`. `lazyaf bench sync <repo> [--ref]` is the
  manual equivalent.
- **Close the write direction**: the case POST/PATCH endpoints either write
  through to the working tree and return the path they wrote, or they are
  refused outside an explicitly-marked scratch suite. Pick one and state it —
  *"the DB is a projection"* is only true if nothing else can write to it. The
  recommendation is **refuse**: authoring goes through `bench case derive` and
  git, and the API is read-only for cases.
- **Do not copy the one bad habit from that path.** `sync_repo_pipelines`
  swallows parse failures into a `logger.warning` and keeps the stale
  definition. A malformed case must set a visible `definition_error` and refuse
  to run trials, not warn into a log nobody reads.

---

## 4. The strategy format

### 4.1 One dialect. The other two are deleted.

There are **three mutually incompatible strategy-graph dialects** across the
three documents, and the one in the API doc does not validate against the real
schema:

| where | shape |
|---|---|
| `strategy-catalog.md:13–24` | a real `PipelineGraphModel` — `steps` **dict** keyed by id, `edges`, `entry_points`, `version: 2`; semantics under `config.lazyaf_*` |
| `api-surface.md:236–250` | `graph.steps` as a **list** of `{id, type, role, fanout, fanout_source, needs, join, workspace}` |
| `phase-specs-and-metrics.md:747–761` | a third YAML shape with `{role, fanout, branch_per_worker, depends_on, join, calls}` |

Verified: `backend/app/schemas/pipeline.py:133` declares
`steps: dict[str, PipelineStepV2]`. The `api-surface.md:236–250` body is
**rejected** by that model. Yet `api-surface.md:223` says the endpoint validates
that body "as a v2 graph", and the entire validation table at
`api-surface.md:264–272` is written in terms of `needs`, `fanout` and `join` —
keys that do not exist in the only dialect the tree can parse.

This attacks the property R1 depends on. `strategy-catalog.md:5` argues that
comparing strategies is meaningful only if every strategy is "the same kind of
object". Two graph schemas means it is not.

**Decision: `strategy-catalog.md`'s dialect is the single wire contract.**
Delete the other two. Rewrite `api-surface.md`'s validation table in `lazyaf_*`
terms. Add `tdd/unit/benchmark/test_doc_example_bodies_validate.py`, which
extracts every JSON body in `docs/milestone-13/` tagged as a strategy graph and
asserts it constructs a `PipelineGraphModel` — a doc example that would 422 then
fails CI instead of shipping.

### 4.2 The reserved keys

`PipelineStepV2.config` is already `dict[str, Any]`
(`backend/app/schemas/pipeline.py:114`), so strategy semantics ride inside it
with no schema change. Keys per `strategy-catalog.md:16–24`, with two changes:

| key | change |
|---|---|
| `lazyaf_scorer` | **new, replaces the "exactly one terminal oracle step" rule.** See §4.5. |
| `agent` | **added to the keys a template may not author.** `backend/app/services/pipeline_executor.py:423` reads `step_config.get("agent") or step_config.get("runner_type")` — `agent` **wins** — and `agent` is absent from the reserved-key list at `strategy-catalog.md:15`. A template step carrying `"agent": "claude-code"` pins the runner and makes the model assignment's `runner_type` dead config, so a matrix cell reports a binding it did not use. That is exactly the failure `api-surface.md:840` exists to prevent, arriving through a key the strict-binding checks do not look at. Reject `agent` at save time, and have the binder write **both** `agent` and `runner_type` so the executor's precedence cannot matter. |

**A correction to one lane's finding, verified in code.** Wiring `cost_by_role`
does **not** require a `step_config` column or an `environment` injection. Both
executors already translate a step config key straight to the env var the
control runtime reads:

```python
# backend/app/services/execution/local_executor.py:704
# backend/app/services/execution/runner_protocol.py:540
("LAZYAF_ROLE", step_config.get("role")),
```

and `images/base/control/run.py:375,525` reads `LAZYAF_ROLE` into the usage
manifest, which lands on `StepUsage.role`
(`backend/app/models/usage.py:79`, indexed
`(pipeline_run_id, role)`). **So the binder writes `config.role = <role>`
alongside `config.lazyaf_role`, and cost-by-role works with zero backend
changes.** Do it in the first trial, not after: `StepUsage.role` is
unrecoverable after the fact, so every trial run before this is wired is
permanently unable to answer the owner's headline hypothesis.

### 4.3 The hard case: `planner-fanout` as a template

`strategy-catalog.md:330–440` ships S4 in its **expanded** form — four workers
written out longhand at K=4. That is the *output*. The template — the thing
that makes `planner-fanout-4` and `planner-fanout-16` one object
(`strategy-catalog.md:102`) — is what has to be stored, and it is not written
out anywhere. Here it is in full.

`bench/strategies/planner-fanout.yaml`:

```yaml
slug: planner-fanout
version: 1
description: >
  One expensive planner splits the task into K disjoint work orders; K cheap
  workers execute them on their own branches in parallel; a server-side
  sequential merge integrates; the oracle scores the merged result.

variables:
  K: {type: int, default: 4, min: 1, max: 32}

parallelism:
  max_concurrent_workers: 8
  branch_per_worker: true

loop_policy: {max_iterations: 6, budget_usd: "4.00", stop_on: [solved, budget, iterations]}

roles: [planner, worker]        # derived from the graph and cross-checked at save time

graph:
  version: 2
  entry_points: ["plan"]
  steps:
    plan:
      id: plan
      name: "Plan work split"
      type: agent
      timeout: 1200
      config:
        lazyaf_role: planner
        role: planner                      # -> LAZYAF_ROLE -> StepUsage.role (4.2)
        lazyaf_branch: {mode: read_only, base: "{{ case.base_commit_sha }}"}
        title: "Plan: {{ case.task_statement }}"
        prompt_template: "bench/planner-split"
        description: |
          Split this task into exactly {{ K }} independent work orders that touch
          DISJOINT files. Write them to .control/work_orders.json.

          {{ case.task_statement }}

          {{ iteration.previous_failures }}

    worker:
      id: worker
      name: "Worker {{ i }}/{{ K }}"
      type: agent
      timeout: 1800
      config:
        lazyaf_role: worker
        role: worker
        lazyaf_fanout: {var: "K", id_template: "worker_{i}"}
        lazyaf_branch: {mode: per_worker,
                        name: "trial/{{ trial.id }}/w{{ i }}",
                        base: "{{ case.base_commit_sha }}"}
        title: "Work order {{ i }}"
        prompt_template: "bench/worker-execute"
        description: >
          Execute work order {{ i }} from .control/work_orders.json.
          Stay inside the files it assigns you.

    integrate:
      id: integrate
      name: "Integrate {{ K }} branches"
      type: script
      timeout: 900
      config:
        command: "lazyaf-integrate --trial {{ trial.id }}"
        lazyaf_integrate:
          policy: sequential-merge
          order: worker_index
          sources: "{{ fanout.branches }}"          # expander fills the K branch names
          target: "{{ trial.branch }}"
          on_conflict: fail

    score:
      id: score
      name: "Score oracle"
      type: script
      timeout: 900
      config:
        command: "lazyaf-oracle run"
        lazyaf_scorer: true                        # THE authoritative scoring node (4.5)

  edges:
    - {id: e_plan_worker,   from_step: plan,      to_step: worker,    condition: success}
    - {id: e_worker_int,    from_step: worker,    to_step: integrate, condition: success}
    - {id: e_int_score,     from_step: integrate, to_step: score,     condition: always}
```

Three nodes and three edges expand to `K + 3` nodes and `2K + 1` edges. Pass 4
of `expand_strategy_graph` (`strategy-catalog.md:47–67`) does it: the
`lazyaf_fanout` node is replaced by K clones with ids from `id_template`, each
carrying `lazyaf_worker_index: i`; every incoming edge becomes K edges (the
fan-out) and every outgoing edge becomes K edges (the fan-in); edge ids get an
`_{i}` suffix; clones are auto-laid-out `x = base.x + (i-1)*220`. At K=4 the
output is exactly the graph already printed at `strategy-catalog.md:330–440`.

**The fan-in is genuinely free and this was verified in code, not assumed.**
`backend/app/services/pipeline_executor.py:4740–4767`:

```python
# Check if at least one edge's source is completed (OR semantic for multiple paths)
# For fan-in, we need ALL sources to be completed
for edge in incoming_edges:
    from_step = edge.get("from_step")
    if from_step not in completed_ids:
        return False
```

K workers converging on one integrate node is native executor behaviour today.
Arbitrary graphs are expressible, and the answer to "is a strategy really an
arbitrary graph" is **yes** for this dialect.

### 4.4 K is static. `fanout_source` is deleted.

`api-surface.md:243` carries `"fanout_source": "plan.instructions"` — width
derived from the planner's runtime output. The expander materialises the whole
graph on a throwaway `Pipeline` row *before* execution starts
(`strategy-catalog.md:47`), and there is no runtime node-insertion path in the
executor. `fanout_source` is unbuildable without new engine work nobody has
scoped.

Static K is the better design anyway — it is what makes the K-sweep a clean
independent variable (`strategy-catalog.md:113`). Delete `fanout_source`, and
handle the consequence explicitly rather than hiding it: the planner is
instructed to produce exactly K orders, and a `.control/work_orders.json` with
fewer than K entries records **`Trial.underfilled_fanout: int`**, surfaced on
the board next to worker overlap. "The planner could not find K disjoint slices"
is a finding about the strategy's ceiling, not a bug to suppress — and without
this, K idle workers' spend lands in `cost_by_role` with no work attached.

**One matrix gap this exposes.** `strategy-catalog.md:113` says
`Experiment.matrix` gains a `template_variables` axis so a K-sweep is one matrix
over one template. The matrix schema at `api-surface.md:802–820` has no such
axis, and `api-surface.md:803` uses `planner-fanout-8` as a *slug* — which
`strategy-catalog.md:102` forbids. So the K-sweep, billed at
`api-surface.md:947` as "the roadmap's first genuinely publishable result",
currently **has no way to be requested**. Add the axis; use `planner-fanout` as
the slug and `{"K": 8}` as the variable.

### 4.5 `lazyaf_scorer` — resolving the rule that rejects two of its own templates

`api-surface.md:271` makes "Exactly one terminal oracle step" a save-time
validation rule, rationale "scoring must read one manifest, not race two". But:

- **S5 `planner-fanout-resolver`** declares **two** oracle steps, `run_tests`
  and `run_tests_after_resolve` (`strategy-catalog.md:504–512`) — forced by the
  catalog's own no-rejoining-conditional-branch rule (`:123`), which is itself
  correct and derived from real executor behaviour.
- **S6 `gated`** declares **no** oracle step at all — only a node carrying
  `lazyaf_gate: true` (`strategy-catalog.md:540–571`).

Under the rule as written, two of the six seed templates that 13.2's Definition
of Done requires to load and validate (`phase-specs-and-metrics.md:799`) are
rejected — one for having two, one for having none.

**Resolution.** Replace the rule with:

> **Exactly one node per *path to termination* carries `lazyaf_scorer: true`,
> and every such node carries an identical `command`.** Scoring reads the
> manifest of whichever scorer node actually ran, addressed by its
> `step_run_id`. `lazyaf_gate: true` implies `lazyaf_scorer: true` (a gate is a
> scorer whose exit status also decides the iteration).

S5's post-resolve rerun is then a legal second measurement of the same
iteration, not a race. S6's gate is a scorer. And the scoring query gets the one
thing it actually needs — a specific `step_run_id` — rather than "the run's
tests", which would pool an agent-authored feedback run with the authoritative
one (§6.1).

### 4.6 The catalog has two homes and two contents; pick one of each

- **Home.** `strategy-catalog.md:131` ships templates as JSON fixtures under
  `backend/app/services/benchmark/catalog/`, seeded on migration.
  `phase-specs-and-metrics.md:740` puts them at `bench/strategies/<slug>.yaml`,
  "loaded like cases". **Take `bench/strategies/*.yaml`** — it is the only one
  that survives in VCS the way R5 requires, and the seed catalog then ships as
  files in the repo rather than rows in a migration.
- **Contents.** `phase-specs-and-metrics.md:740` names six seeds;
  `strategy-catalog.md:831` names seven, of which only two names match, and it
  **drops `null-agent`** — which `phase-specs-and-metrics.md:876` requires to
  run in every published experiment. Slugs are the join key in published results
  and a rename is a breaking change (`strategy-catalog.md:797`), so this must be
  settled once, before the first published number. **Take the catalog's seven
  plus `null-agent` and `gold-patch`** (nine), with `planner-fanout` covering
  every K.

---

## 5. The leaderboards

Three views. Every one of them ranks **variants**, never trials
(`phase-specs-and-metrics.md:16`), and every one of them would rather print
`NOT SEPARABLE` than sell you a difference it cannot defend.

### 5.0 The one rule that governs all three: the default order is *partial*

No default sort is specified anywhere. `phase-specs-and-metrics.md:268` lists
what the sort control offers and forbids, but not what the board does on first
load — and **the default sort is the claim**, because it is what gets
screenshotted. Worse: a strict total order silently destroys the separability
work. If three variants are mutually not separable, a sort still prints a 1st, a
2nd and a 3rd, and the reader reads a ranking the board's own statistics refused
to make.

> **Default: rank bands, not a sort.** Primary key is solve-rate at the shared
> budget `B`, descending. A variant that is NOT SEPARABLE from the current band
> leader **joins that band**, shares its rank number, and is not reordered
> within it. Cost-per-solve (M2b) breaks ties **inside** a band only, never
> across bands. Sorting is disabled entirely in PILOT and for UNRELIABLE
> variants.

### 5.1 View 1 — the aggregate board

`GET /api/bench/board?experiment=<id>&group_by=variant&budget=5.00`

| # | column | source | notes |
|---|---|---|---|
| 1 | rank band | §5.0 | shared number for a not-separable band |
| 2 | variant | `variant_label` | strategy + K + model assignment + prompt — **never strategy alone** (§5.4) |
| 3 | solve-rate @ B | M1 macro over cases + 95% CI | the headline; `n_cases` always shown |
| 4 | cost per solve | M2b (amortized, includes failures) | the number that does not discard failures |
| 5 | cost to solve | M2a, **pairwise** `C_shared` (§5.5) | with `|C∩|` and the excluded slugs |
| 6 | regressions | M3 with its denominator: `4% (2/47 target-met, median \|p2p\| = 341)` | plus the badge (§5.7) |
| 7 | wall-clock p50 | M5 | always with `[machine_profile]`; mixed profiles → BLOCKED |
| 8 | speedup | M6 | never sortable alone |
| 9 | merged work | `merged_work_fraction` from git lineage | the honest companion to speedup |
| 10 | conflict rate | M7 | `n/a` for serial templates, **never `0%`** |
| 11 | cost coverage | shipped today (`experiment_metrics.py:186–216`) | `< 0.9` → untrustworthy |
| 12 | error rate | | `> 10%` → UNRELIABLE, not ranked |
| 13 | n | `n_cases × R` | |

```
EFFECTIVENESS BOARD  ·  suite core-v1 @ sha256:9ab3…  ·  B = $5.00  ·  R = 5  ·  9 cases × 4 variants
harness 13.4.0 · images sha256:41c9… · machine profile ci-x86-8c · ranked by paired difference, Holm(3), α=0.05

 #   variant                          solve@$5      cost/solve  cost-to-solve   regressions          wall p50    spd  merged  confl  cov   err   n
                                      macro [95% CI]  M2b       M2a  pairwise   (of target-met)      [ci-x86-8c]
─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 1   planner-fanout K=8                 0.71         $2.74      $1.94 ±0.41      4%  (2/47, |p2p|=341)  6m12s     4.1x  0.62   11%   0.98   2%   45
     opus→haiku · bench/planner-split   [0.58, 0.82]             |C∩|=6
 1=  adversarial-review                 0.69         $2.91      $2.06 ±0.44      2%  (1/44, |p2p|=341) 14m40s     n/a   n/a    n/a   1.00   0%   45
     sonnet · bench/adversarial         [0.55, 0.80]             |C∩|=6
     └─ NOT SEPARABLE from #1 on solve@$5  (Δ = +0.02, 95% CI [-0.09, +0.13]) — shown tied, not ordered
 3   one-shot            (baseline)     0.51         $3.60      $2.88 ±0.52     14%  (6/43, |p2p|=341)  9m02s     n/a   n/a    n/a   0.99   0%   45
     sonnet · bench/one-shot            [0.38, 0.64]             |C∩|=6          ⚠ SHIPS REGRESSIONS 14%
 —   null-agent          (control)      0.00           —          —              0%  (0/45)             0m11s     n/a   n/a    n/a   1.00   0%   45
 —   gold-patch          (control)      1.00           —          —              0%  (0/45)             0m14s     n/a   n/a    n/a   1.00   0%   45

CONTROLS: null-agent 0.00 (required) · gold-patch 1.00 (required) · base-state verified for 9/9 cases
COST:     half of all runs had solved by $1.91; 29% never solved within the $5.00 cap  (right-censored)
EXCLUDED FROM C∩ (3): cli.arg-parse-regression, data-pipeline.schema-drift, web-api.n-plus-one
TRUSTWORTHY: yes.
```

Two presentation notes. **"KM" never appears.** `phase-specs-and-metrics.md:167`
renders `censored p50 $1.91 (KM, 71% solved within cap)`; the estimator is right
and correctly demoted, but "KM" reads as a typo to the audience the owner wants
to reach, and a metric a reader cannot restate is one they will misquote. Keep
the API key `censored_p50` and the maths untouched; the UI and CSV get the
sentence. Same for the unreached median: `more than $4.98 — only 38% ever
solved` (`phase-specs-and-metrics.md:142–159` already refuses to print a number
there, which is exactly right).

### 5.2 View 2 — the per-problem board (R3)

`GET /api/bench/board/cases?experiment=<id>` — rows = cases, cols = variants.
This is the 13.3 exit gate (`api-surface.md:944`).

**Per-case statistics must be written down explicitly, not inherited.** Three
concrete failures if they are inherited from the aggregate rules:

1. M2a's gate — "if `|C_shared| < 5`, the metric renders `INSUFFICIENT` and
   cannot be ranked" (`phase-specs-and-metrics.md:130`) — is **unsatisfiable at
   `|C| = 1`**. The per-case board's headline cost metric could never render.
   *Fix:* the per-case cost metric is `cost_case(v, c)`, already defined at
   `phase-specs-and-metrics.md:126`, and the `|C_shared|` gate does not apply.
   The per-case gate is instead *"both variants solved this case in ≥2
   repeats"*.
2. The cluster bootstrap resamples **cases** as the cluster
   (`phase-specs-and-metrics.md:348–358`) and degenerates at `|C| = 1`. *Fix:*
   state that the per-case interval is a **within-case bootstrap over the R
   repeats**, and that it answers *"is A better than B on this case"* — a
   different question from the aggregate.
3. Holm is corrected over `K-1` baseline comparisons
   (`phase-specs-and-metrics.md:369`). A 9-case × 3-variant board is **18**
   comparisons. *Fix:* the per-case Holm family is `|C| × (K-1)`, and it is
   printed.

**And the honest answer about resolution.** At the published default `R = 5`, a
per-case solve-rate has 20-percentage-point granularity. 3/5 vs 4/5 is not a
finding. **This cannot be measured reliably at R=5 and the board must say so
rather than design around it:** the per-case board defaults to **DESCRIPTIVE** —
values and intervals shown, every verdict rendered `NOT SEPARABLE` — unless
`R ≥ 10`.

```
PER-PROBLEM BOARD  ·  core-v1  ·  B = $5.00  ·  R = 5      DESCRIPTIVE ONLY
per-case verdicts require R>=10; this board ran R=5. Values and intervals are shown; nothing is ranked.
Holm family if verdicts were enabled: 9 cases × 2 comparisons = 18.

 case                              vert / cplx     one-shot (base)      adversarial-review    planner-fanout K=8
                                                   solve   $/solve      solve   $/solve       solve   $/solve
────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 requests.leading-path-separators  web-api/small    5/5    $0.61         5/5    $1.02          5/5    $1.44
 flask-api.missing-pagination      web-api/small    4/5    $1.12         5/5    $1.30          5/5    $1.86
 flask-api.n-plus-one              web-api/medium   1/5    $4.40         3/5    $3.10          4/5    $2.05
 pandas-etl.null-coalesce          data/trivial     5/5    $0.44         5/5    $0.79          4/5    $1.61   ↓
 pandas-etl.schema-drift           data/medium      0/5      —           1/5    $4.85          3/5    $2.90
 spark-agg.window-fn               data/small       3/5    $2.05         3/5    $2.44          4/5    $2.21
 clicli.arg-parse-regression       cli/trivial      5/5    $0.38         5/5    $0.71          3/5    $1.90   ↓
 clicli.subcommand-help            cli/small        4/5    $1.20         4/5    $1.55          4/5    $1.72
 clicli.exit-codes                 cli/medium       2/5    $3.02         3/5    $2.88          4/5    $2.34
────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 macro solve-rate                                   0.64                 0.76                  0.84
 regressions (target-met)                          14% (6/43)            2% (1/44)             4% (2/47)

 ↓ = variant did worse than baseline on this case. Descriptive: not a ranked claim at R=5.
 Read this board for SHAPE (where does fan-out lose?), not for per-case winners.
```

That `↓` column is the view the owner specifically asked for and it earns its
place immediately: fan-out loses on both `trivial` cases and wins on all three
`medium` ones — which is precisely what `strategy-catalog.md:445–446` predicts,
now visible instead of asserted.

### 5.3 View 3 — the per-vertical / per-complexity board, and why it is dead on the shipped corpus

`GET /api/bench/board?group_by=vertical` (also `complexity`,
`contamination_risk`). This answers *"strategy A wins overall but loses on a
whole class of problem"*.

**On `core-v1` as specified, every split is unrankable by construction.**
`phase-specs-and-metrics.md:697` fixes the starter suite at "9 cases, 3
verticals × 3 complexities" — so every split contains exactly **3** cases,
against a hard gate of ≥5 (`:341`, `:878`). The view is built and dead.

Worse for the owner personally: the complexity ladder tops out at `medium`,
while the catalog documents `planner-fanout` as losing on `trivial` by
construction (`strategy-catalog.md:446`) and winning on "a `medium`/`large` case
where the work genuinely partitions" (`:445`). On `core-v1` the fan-out
hypothesis is **guaranteed to lose 6 of 9 cases** and has 3 `medium` cases to
win on — below the ranking gate. *The most publishable question in the milestone
cannot be answered by the corpus the milestone ships.*

Two fixes, either helps, both are cheap relative to authoring cases:

1. Grow `core-v1` to **5 cases per vertical (15)**, *or* declare in 13.1 that
   splits are `DESCRIPTIVE — NOT RANKED BY CONSTRUCTION` until a split reaches 5
   and print **that** reason rather than a generic `INSUFFICIENT`.
2. Add a **`large`** tier to the complexity enum — the catalog already reasons
   in it at `:176`, `:445`, `:674` — and ship a second small suite `fanout-v1`
   of 5–6 `medium`/`large` multi-file cases whose work genuinely partitions.
   That is the minimum corpus on which the K-sweep can produce the "first
   genuinely publishable result" it is billed as.

```
SPLIT BOARD  ·  group_by = complexity  ·  core-v1  ·  B = $5.00  ·  R = 5

 complexity   n cases   one-shot        adversarial     planner-fanout K=8    verdict
──────────────────────────────────────────────────────────────────────────────────────────────────────────
 trivial          3      0.93            0.93            0.67                 DESCRIPTIVE — 3 cases < 5
 small            3      0.73            0.80            0.87                 DESCRIPTIVE — 3 cases < 5
 medium           3      0.20            0.47            0.73                 DESCRIPTIVE — 3 cases < 5
 large            0        —               —               —                  NO CASES — tier not in core-v1
──────────────────────────────────────────────────────────────────────────────────────────────────────────
 NOT RANKED BY CONSTRUCTION: every complexity split in core-v1 holds 3 cases; ranking requires 5.
 The visible pattern (fan-out trails on trivial, leads on medium) is a HYPOTHESIS this corpus generated
 and CANNOT test. Suite `fanout-v1` (5-6 medium/large partitionable cases) is the corpus that tests it.
```

That last block is doing real work: it is the board declining to sell the
owner's own favourite finding, and telling him precisely what corpus would let
him earn it.

### 5.4 What the board refuses to rank — consolidated, in one place

| refusal | source |
|---|---|
| null-agent scored above 0% on any case → **the board refuses to render at all** and names the case | `phase-specs-and-metrics.md:876` |
| any case in scope is not `valid` | `api-surface.md:933` |
| mixed `suite_content_hash`, `harness_version`, or `image_hashes` | `api-surface.md:933–936` |
| **mixed `case_content_hash` for one slug** | **new (§3.3)** |
| mixed `machine_profile` for any latency metric | `phase-specs-and-metrics.md:240` |
| `< 5` cases or `< 3` repeats | `phase-specs-and-metrics.md:341` |
| error rate `> 10%` | `phase-specs-and-metrics.md:86` |
| `cost_coverage < 0.9` | `api-surface.md:933–936` |
| `\|C_shared\| < 5` for M2a (aggregate only; §5.2 for per-case) | `phase-specs-and-metrics.md:130` |
| paired-difference 95% CI contains zero | `phase-specs-and-metrics.md:364` |
| **pooled variants** — see below | **new** |
| **mixed iteration caps or wall-clock ceilings** — see below | **new** |

**Pooled variants.** `phase-specs-and-metrics.md:16` defines a variant as
`(strategy_template, model_assignment, prompt_version, loop_policy)` and says
"The board ranks variants, never trials". But the board's own example groups by
`{"strategy": "planner-fanout-8"}` (`api-surface.md:900`) while the matrix in
scope carries **two** model assignments (`api-surface.md:804–812`), and no
column, hash or index represents the variant tuple anywhere. `group_by=strategy`
over that matrix pools opus-plans-haiku-works trials with all-sonnet trials into
one ranked row, and the reader cannot tell.

*Fix:* `Trial.variant_hash` + denormalized `Trial.variant_label`, indexed
`(experiment_id, variant_hash)`. **Any group whose rows carry more than one
distinct `variant_hash` renders `POOLED: N variants in this row — not ranked`.**
Also denormalize `Trial.fanout_k` and `Trial.model_assignment_name` — the board
advertises `group_by=k` and `group_by=model_assignment`
(`api-surface.md:879`) with nothing queryable behind either (K lives in a JSON
dict, and the index list at `api-surface.md:1141–1148` has neither), which makes
the k-sweep a JSON scan on the board's hottest path *and* silently pools two
widths under one strategy name.

**Mixed caps.** A cap can come from four places with no stated precedence:
`BenchmarkCase.loop_defaults` (`api-surface.md:113`),
`StrategyTemplate.loop_policy` (`:252`), `matrix.shared_budget_usd` (`:816`),
and `Trial.loop_policy_override` (`:314`). The response returns
`effective_loop_policy` (`:329`) with no rule saying how it was computed. Since
`loop_policy` is part of the variant definition, two templates declaring
different `max_iterations` are *different variants* and the board will rank them
side by side as if the only difference were graph shape. M1's downward budget
re-sweep (`phase-specs-and-metrics.md:107`) rescues the **budget** axis only;
nothing covers a differing `max_iterations`, and M4 explicitly warns the
iteration CDF "stops dead at the cap" (`:220`).

*Fix:* one precedence, stated once in 13.0 —
`matrix.shared_* > trial.loop_policy_override > template.loop_policy >
case.loop_defaults` — persisted as `Trial.effective_loop_policy` and folded into
`variant_hash`. The board prints `BLOCKED: mixed iteration caps (6, 4)`, naming
the values. Budget stays re-sweepable downward as specified.

### 5.5 Cost-to-solve must be pairwise, or adding a strategy changes everyone's number

`phase-specs-and-metrics.md:124` defines
`C_shared = { c : every variant under comparison solved c in at least one
repeat }`, and M2a — the ranked cost number — is the median over it. The design
names and defends *naive* survivorship (`:119`) but not this second-order
version: **add a weak fourth strategy and `C_shared` shrinks to the cases even
the weak one solved — by construction, the easy ones — so every other
strategy's published cost-to-solve moves, with nothing telling the reader why.**
The headline number becomes non-reproducible across boards drawn from the *same
trials*, which is exactly the property this milestone exists to establish. It
also creates a perverse incentive: dropping your worst strategy improves
everyone else's number.

*Fix:* the **ranked** cost-to-solve is computed pairwise — `C_shared` over
exactly the two variants in each comparison — so a comparison never depends on
who else is on the board. Keep the global-`C_shared` value as a display column
explicitly labelled with `|C∩|` and its excluded slugs (already required at
`:130`, `:320`). Pin it:
`test_cost_to_solve_invariant_to_adding_a_third_variant`.

### 5.6 The wire shape has to carry what the rules require

`phase-specs-and-metrics.md:829` promises presentation rules are "enforced in
code … in the board serializer, not in the template, so the CSV cannot present
something the UI would refuse". The board JSON as drawn cannot carry them:

- `"cost_to_solve_usd": {median, p25, p75, min, max}` (`api-surface.md:903`) is
  a trial distribution, not the mandated M2a+M2b+M2c block —
  `phase-specs-and-metrics.md:169`: *"All three, always, in that block.
  Cost-to-solve is never printed as a lone scalar."*
- `"regression_rate": 0.089` (`api-surface.md:909`) is a bare float carrying
  neither its `target_met` denominator nor `median |p2p|` —
  `phase-specs-and-metrics.md:191`: *"Always with the conditioning denominator
  spelled out."* A CSV consumer gets `0.089` and quotes it.
- No cell carries `|C_shared|` or the excluded-case list, and there is no
  `n_cases` anywhere — even though M1's headline is a **macro average over
  cases**.
- `"separable": true|false` (`api-surface.md:919, :923`) cannot express the five
  verdicts 13.4 defines (`phase-specs-and-metrics.md:874`:
  `SEPARABLE | NOT_SEPARABLE | WITHIN_NOISE_FLOOR | BLOCKED | INSUFFICIENT`).
  **A `WITHIN_NOISE_FLOOR` result — "Reported, not ranked" (`:398`) —
  serialized as `separable: true` WILL be ranked by every consumer**, and
  `BLOCKED` collapsing to `false` reads as "tied". The board would be selling
  the two things 13.4 exists to refuse: noise dressed as a win, and an invalid
  comparison dressed as a tie.

*Fix:* one envelope for every board metric, which is what `bench_metrics.py` is
already specified to return (`phase-specs-and-metrics.md:824`: "Every function
returns a value **plus** its provenance: `n`, denominator, caps, exclusions") —
the serializer simply stops discarding it:

```json
{"value": "1.94", "ci": ["1.53", "2.35"], "n_trials": 45, "n_cases": 9,
 "denominator": "6 cases in pairwise C_shared",
 "caps": {"budget_usd": "5.00", "max_iterations": 6, "wall_clock_ms": 3600000},
 "exclusions": {"errors": 1, "c_shared_excluded_slugs": ["cli.arg-parse-regression", "..."]},
 "presentation": "$1.94 ±0.41 (|C∩|=6)"}
```

and per comparison: `verdict` (the five-state enum) + `presentation` carrying
the verbatim string already pinned at `phase-specs-and-metrics.md:386–405`, plus
`pilot: bool`, `error_rate: float`, `ranked: bool`. Retire `separable`.

### 5.7 Regressions are a badge, not a column

Nothing prevents a variant with a high `pass_to_pass` regression rate from
leading the board. The broken trial is correctly scored unsolved
(`phase-specs-and-metrics.md:55`) and M3 is a column
(`api-surface.md:909`), so it is *visible* — but for a CI/CD owner, "this
strategy ships regressions 14% of the time it thinks it succeeded" is a
**disqualifying property**, not a tiebreaker, and one column right of the sort
key it will lose to a 3-cent cost advantage.

*Fix:* a variant whose regression-rate bootstrap CI lower bound exceeds the
baseline's rate plus the 3pp ROPE band (`phase-specs-and-metrics.md:377`)
renders `⚠ SHIPS REGRESSIONS 14% (6/43 target-met, median |p2p| = 341)` beside
its rank on **every** view and **every** split, regardless of sort key. Test:
`test_regression_badge_survives_every_group_by`.

---

## 6. Anti-cheating and validity

The design's instinct here is right in one important way and should be stated as
a principle: **close a cheat by making it score as failure, not by adding a
checker that can be bypassed.** `api-surface.md:720` does exactly that for the
delete-the-guard-test cheat — a `pass_to_pass` id observed `missing` counts as
broken. Everything below extends that instinct.

### 6.1 Two scoring channels, both declared exclusive — and the one that ships is forgeable

- `strategy-catalog.md:131`: `run_tests`/`gate` steps "run the case's test
  command and emit the 12.2.6 manifest at `/workspace/.control/test_results.json`;
  the oracle scores `fail_to_pass`/`pass_to_pass` from the resulting `TestRun`
  rows. **There is no second scoring channel.**"
- `phase-specs-and-metrics.md:771`: "per iteration: score the oracle in a
  **clean container from a fresh clone** of the iteration commit".

These cannot both be true, and the choice decides whether an agent can forge its
own grade. In the catalog version the manifest is produced **inside the agent's
own container**, and the control runtime already documents that shape as
untrusted — `images/base/control/run.py:155`: *"A step's manifest is UNTRUSTED
input (any command can write that path)."* An agent never needs to touch a test
file. It writes

```json
{"version":1,"results":[{"lazyaf_test_id":"tests/test_adapters.py::test_request_url_trims_leading_path_separators","status":"passed"}]}
```

to that path and `oracle_file_hashes` never fires. Every catalog template wires
the in-graph channel (`strategy-catalog.md:158–167, 217–228, 293–306, 419–436,
504–522`), so **as specified today the shipped strategies are scored by a file
the strategy itself can write.** This is cheaper and quieter than editing tests,
and no control in the design detects it.

**Resolution — enforce structurally, not by convention:**

1. The **clean-container re-score is authoritative**, stated in exactly one
   place. The in-graph `run_tests` is demoted to agent-facing feedback: it tells
   the agent what is red, and it never scores.
2. `TestRun` gains `origin`. Only a run whose PipelineRun carries the
   orchestrator's `trial_iteration_id` ingests with `origin="trial"`; manifests
   from agent-graph steps ingest with `origin="agent_feedback"` and are excluded
   from `fail_to_pass_passed` / `pass_to_pass_broken` — by the *same mechanism*
   that already keeps trial rows out of the story-done gate
   (`api-surface.md:735–770`), so this is a second consumer of a shipped idea,
   not a new one.
3. Scoring addresses the scorer node's **`step_run_id`**, not the run's — which
   is what §4.5's `lazyaf_scorer` designation exists to provide.
4. Fix `strategy-catalog.md:131`: *"no second scoring channel"* becomes *"the
   graph's `run_tests` never scores; it only tells the agent what is red."*

### 6.2 Oracle tampering: widen the closure, and restore rather than merely detect

The mechanism exists (`oracle_file_hashes`, `phase-specs-and-metrics.md:38`;
verified per iteration, `:771`; `error:oracle_tampered` as a first-class error
class, `:50`) and it is reported as a control in METHOD.md (`:518`). Most
benchmark harnesses have nothing here. Two problems:

**Scope.** It covers only "every file carrying an oracle id" (`:38`, `:685`).
It does not cover `conftest.py` — *including a newly added one* —
`pytest.ini` / `pyproject.toml` / `setup.cfg` / `tox.ini`, `sitecustomize.py`,
or installed-package shims. The cheapest real cheat is an autouse fixture in a
new `conftest.py` that stubs the behaviour under test: no oracle-bearing file
changes, every hash matches, `fail_to_pass` turns green. **Publishing a tamper
control that a one-file `conftest.py` walks around is worse than publishing
none, because readers will trust it.**

*Fix:* hash a declared **`oracle_paths` closure** — the test directories, every
pytest/config file at repo root, and every `conftest.py` on the path from repo
root down to each oracle file, plus `checks/`. `bench validate` prints the
closure so an author can see exactly what is frozen.

**Posture.** Detection is not enough, and exclusion is currently an *incentive*:
`error` trials are dropped from every metric (`phase-specs-and-metrics.md:84`),
so tampering is a free retry rather than a penalty.

*Fix:* in the scoring container, after checking out the iteration commit,
`git checkout <base_commit_sha> -- <oracle_paths>` and re-apply `test_patch`,
**then** run. A tampered trial is scored against the real oracle rather than
excluded. Detection stays as the honesty signal — `error:oracle_tampered` is
recorded when the restore was non-empty, and reported — but the trial still gets
a real grade.

**One cheat that is already closed, and should be pinned so nobody "optimises"
it away.** An agent that adds a *new* passing test carrying an oracle id in a
different file leaves the hashed file unchanged — but
`backend/app/services/test_ingestion.py:136–172` aggregates duplicate ids
**worst-status-wins**, so a duplicate marker can never turn a red into a green.
That existing rule is the real defence. Add a named test asserting it.

### 6.3 The suite failed to run

This is the case that must never score as "solved nothing", and the current path
produces exactly that. Verified end to end:

- `runner-common/runner_common/pytest_lazyaf.py:210–214` writes **no manifest**
  when no annotated test ran.
- `images/base/control/run.py` only POSTs when the file exists.
- `backend/app/services/test_ingestion.py:337–339` returns early on empty
  results.

Net effect: **zero `TestRun` rows**, and the vocabulary has no way to say so —
`ingest_manifest` iterates the *manifest*, never an expected-id set, so an id
the suite did not report simply produces no row. There is no column whose value
is `missing`, yet `api-surface.md:189–193` requires exactly that value.

*Fix, in two places:*

1. **`lazyaf-oracle` guarantees a manifest** (§2.7 step 7). A collection error
   emits every declared oracle id as `failed`, not an empty manifest that reads
   as `missing`.
2. **`bench_oracle.score` derives `missing`** rather than needing a column. One
   pure function, no DB:

```python
score(fail_to_pass: list[str],
      pass_to_pass: list[str],
      checks:       list[str],
      observed:     dict[str, str]) -> OracleVerdict
```

where the caller builds `observed` from one indexed query keyed on the **scorer
step's** `step_run_id`:

```sql
select TestRun.status, TestRef.lazyaf_test_id
  from test_runs join test_refs on ...
 where test_runs.step_run_id = :scoring_step_run_id
```

Every declared id absent from that dict is `missing`.

| rule | value |
|---|---|
| `target_met` | every `fail_to_pass` id observed `passed` |
| `clean` | no `pass_to_pass` id observed `failed` **or `missing`** (`api-surface.md:720`) |
| `solved` | `target_met and clean` — stored as two halves, which is the only way regression rate is not definitionally zero (`phase-specs-and-metrics.md:55`) |
| **INVALID** (never `solved=false`) | `observed` is empty → `error:oracle_no_manifest`; any `pass_to_pass` is `missing` or `skipped`; any `fail_to_pass` is `skipped` |
| **not invalid** | a `fail_to_pass` that is `missing` — with `test_patch` applied it should exist, so `missing` means the agent deleted it: an honest `target_met = false` |

**Do not reuse `experiment_service.classify_cell` for this.** Its zero-rows →
ERROR branch is right, but its `if success: return PASSED` branch returns PASSED
whenever the *run* succeeded, without reading a single `TestRun` row. For a
benchmark that is a **false solve**: a strategy whose agent step exits zero while
the fix does not work would score as a solve. A trial's outcome is
`bench_oracle`'s verdict, stored on the Trial as `target_met` / `clean` /
`error_class`. The cell status stays what it is — a statement about the run.
Do not overload `passed`.

**One honest limitation to disclose rather than paper over.** `error` is in the
oracle vocabulary (`api-surface.md:187–188`) and counted in the scoring rule
(`:720`), but it **cannot be produced**: the manifest contract admits three
statuses (`backend/app/models/testref.py:41–46`;
`images/base/control/run.py:148`), and the plugin maps setup/teardown errors to
`"failed"` explicitly. So a broken environment on a `pass_to_pass` test reads as
a regression the agent caused. State this in METHOD.md's threats section rather
than pretending. `test_ingestion.py` already ranks a future `error` value
defensively, so widening later costs nothing.

### 6.4 Contamination: disclosed, not controlled — say so plainly

The design records `contamination_risk` per case, lets the board filter on it
(`api-surface.md:881`), and requires disclosure in METHOD.md
(`phase-specs-and-metrics.md:476–492`). That is the right set of moves and it is
all of them.

**It is a disclosure, not a control.** There is no held-out corpus and no
detection mechanism. The worked example in §2.3 is `contamination_risk: high`
for exactly the honest reason: `psf/requests` is one of the most-read Python
repos on GitHub, issue #6643 and its fix are public, and the model almost
certainly saw them. A `low`-risk split with fewer than 5 cases renders
`INSUFFICIENT` and never a number (`:878`) — good — but the *existence* of the
filter will imply a control to a reader unless METHOD.md says otherwise.

Write it in the owner's own voice: **"We cannot measure contamination. We record
our belief about it per case, we publish the split, and a gap that survives on
the low-risk subset is worth more than the headline. If the low-risk subset is
too small to rank, we say that instead of quoting the headline."**

### 6.5 The risk register, ranked by "how likely is this to produce a number that looks rigorous and is not"

| # | risk | design | code today |
|---|---|---|---|
| 1 | **case already green at base** — every strategy scores 100%, suite looks like a triumph | defended well (`api-surface.md:148–217`, launch refusal at `:214–216`, `phase-specs-and-metrics.md:639`) | **unimplemented** — this is why base-state validation is not deferrable |
| 2 | **flake read as variance** — a nondeterministic guard makes two strategies differ by noise; *symmetric*, so it does not show up as an error rate | `k=3` flake screen (`:681`) | unimplemented. If deferred, the cheap substitute is: run base-state validation 3× and refuse any case whose observed set is not identical |
| 3 | **contamination** | disclosure only (§6.4) | n/a — cannot be measured |
| 4 | **oracle tampering** | `oracle_file_hashes` + `error:oracle_tampered` | partially defended, and better than the design realises (worst-status-wins, §6.2), but the closure is too narrow (§6.2) |
| 5 | **budget cap not binding across a fan-out** | fleet-wide `remaining = budget - spent - reserved_in_flight` (`phase-specs-and-metrics.md:773`) | `_pump_once` recomputes observed spend before every dispatch but **reserves nothing in flight** — with K workers dispatched in one wave the overshoot is K × step cost. `budget_overrun_usd` exists on the Experiment and nothing writes it per trial. Unfair caps make cost comparisons meaningless |
| 6 | **comparing at different budgets** | M1 requires `B` named and identical (`:107–111`) | the 12.6.5 board has **no `B`** — it reports whatever was spent. Reusing it unchanged silently compares a $10 strategy to a $1 one |
| 7 | **survivorship bias in cost-to-solve** | named, three defences (`:119–125`), plus §5.5 | designed, unimplemented |
| 8 | **unknown cost reading as cheap** | — | **already implemented**: unknown counts as zero and lowers `cost_coverage`, with a warning naming the variant (`backend/app/services/experiment_metrics.py:186–216`). The best-handled risk in the stack |
| 9 | **n < 3** | — | **already implemented** as `insufficient_repeats` |

---

## 7. The build path

The design has five phases and **no "first defensible number" milestone**. 13.1
alone gates on 9 cases, a 5-check validator, 4 CLI subcommands and 8 named test
files (`phase-specs-and-metrics.md:697–723`) before any trial runs; 13.2 adds an
orchestrator, fleet-wide budget admission, integration policies, a resolver
agent and lineage capture. Nothing produces a number until both land — so the
instrument can be wrong for two phases before anyone notices.

**Target: one problem, two strategies, a real oracle verdict, a cost figure, a
two-row board.** Ordered; each step independently green.

| # | step | effort | why it cannot move later |
|---|---|---|---|
| 1 | **Make an unmodified repo scorable.** Stage + `pip install runner-common` in `lazyaf-test-runner` (`scripts/build_images.py:83`). Add `LAZYAF_TEST_ID_MODE=nodeid` to the plugin. Ship `lazyaf-oracle` (§2.7). | small | nothing downstream works without it |
| 2 | **One case file, not nine.** `BenchmarkCaseSpec` + loader + the §2.3 worked case. Fields: repo triple, `task_statement`, `oracle.*`, `fail_to_pass`, `pass_to_pass`, `test_patch`, `reference_patch`, `oracle_paths`, `license`, `contamination_risk`. **Defer:** `vertical`, `complexity`, `checks`, suite versioning, `quarantined_tests`, `machine_profile_required`, `user_story_id`. | small | — |
| 3 | **`backend/app/services/bench_oracle.py`** — the pure scoring function of §6.3. ~60 lines, no DB. | small | every honest verdict turns on it |
| 4 | **Base-state validation as an ordinary run**, scored by the same `bench_oracle.score`: apply `test_patch` at base, assert every `fail_to_pass` fails and every `pass_to_pass` passes. | small | **NOT DEFERRABLE** — risk #1. Nearly free once (3) exists |
| 5 | **`ingest-remote` + the `bench/case/<slug>` ref** (§2.1, §2.8) and `commit_sha` threaded into `trigger_context` (the executor already reads it at `pipeline_executor.py:2798`). | small | R4 is meaningless without it |
| 6 | **Two strategy graphs as `bench/strategies/*.yaml` fixtures**: `one-shot` (agent → score) and `null-agent` (commits nothing → score). Bind role → model by writing `config.model`, `config.runner_type`, `config.agent` and **`config.role`** (§4.2). **Defer:** the expander, K, `lazyaf_fanout`, integration policies, resolver, gates, fork/freeze. | small | `null-agent` is **not deferrable** — an oracle defect must be caught by the control, not by a reviewer |
| 7 | **A trial is one row and one pipeline run**, `max_iterations = 1`. Reuse `Experiment`/`ExperimentRun` as the matrix rather than writing an orchestrator: a cell becomes (strategy × case); `start_cell_run` writes **`steps_graph=`**, never `steps=`. Columns: `case_id`, `strategy_slug`, `variant_hash`, `pipeline_run_id`, `status`, `target_met`, `clean`, `error_class`, `base_commit_sha`, `final_commit_sha`, `case_content_hash`, `strategy_content_hash`. | medium | see the 12.8 note below |
| 8 | **The two-row board.** Add `strategy_slug` and `k` to `CellRow`/`VariantRow`/`_variant_label` (`backend/app/services/experiment_metrics.py:89–101, 373–406`) — purely additive, existing board keeps working with NULLs. Rename `OutcomeRow.criterion_id` to a generic `group_id` when it moves to `bench_metrics.py` and supply `case_id`. **Defer:** bootstrap CI, separability, KM, speedup, k-sweep, CSV. | small | — |

**Why the deferrals are safe.**

- A 1-iteration trial is a legitimate point, not a degraded one — the catalog
  makes exactly this argument for K=1 (`strategy-catalog.md:114`). It cannot
  measure iterations-to-solve; that metric is then **absent, not wrong**.
- Fan-out defers with no engine risk: the fan-in is already native
  (`pipeline_executor.py:4740–4767`, verified). Adding it later is graph
  authoring, not engine work.
- Deferring ranking is safe because `experiment_metrics.py` already ships
  `ranked: false` with the reason printed. Report-do-not-rank is honest;
  rank-without-separability is not.
- Bundles defer because R5's durability is satisfied by the YAML living in a
  repo. The `.tar.zst` is *redistribution* — a later problem.

**What is NOT safe to defer:** step 4 (base-state), the `null-agent` control in
step 6, `config.role` in step 6 (`StepUsage.role` is unrecoverable after the
fact), and writing `steps_graph=` in step 7.

**The 12.8 collision, and how to sidestep it.** `upcoming/wave10-v1-retirement.md:391`
lists the `experiment_service.py` cell launcher (`steps=json.dumps(steps)`) as
writer #7, owned by agent A5 in P3; `:250` marks the file CONTESTED. P1/P2 have
landed; P3–P6 have not. If M13 builds a launcher that emits `steps=`, it is
rewritten at P3 and its executor branch deleted at P5 — two rewrites of the
newest code in the tree for nothing. **Emitting `steps_graph=` from day one puts
M13 ahead of 12.8 rather than behind it**, and it is what M13 wants anyway
(`strategy-catalog.md:13`). Everything in steps 1–6 and 8 touches the pipeline
definition surface not at all and can start today with no coordination.

**Migration numbering.** `0012_workspaces_per_worker.py` already exists in the
tree, and `upcoming/wave10-v1-retirement.md:445` allocates `0012`/`0013` to 12.8
P4/P6. M13 takes **0014/0015** and states its `down_revision` explicitly. This is
an unowned coordination point, and the kind that produces two alembic heads on
the day two waves merge.

**One test converts most of this document from "someone should check" into a
build failure.** The specified round-trip (`phase-specs-and-metrics.md:958`)
runs on the machine that produced the bundle, where the mirror, the images and
the prompts all still exist — a **clean database is not a clean machine**, and
the exit gate is explicitly about a stranger (`:917`). Add
`tdd/integration/benchmark/test_bundle_import_on_virgin_tree.py`: import into a
container whose git-repos volume is empty and whose `prompt_templates` /
`prompt_versions` tables are scrubbed, then `bench validate` to green; serve the
licence-restricted `fetch/` case from a local `file://` origin so the real clone
+ tree-hash path executes rather than only its failure branch; assert no case
resolves through a pre-existing `repo_id`.

---

## 8. Amendments to the existing docs

Precise list. **These are not made here.** Grouped by file, in document order.

### `docs/milestone-13/phase-specs-and-metrics.md`

| line(s) | change |
|---|---|
| 37 | `test_command: "pytest -q"` → make explicit that this is the **raw suite invocation** and `lazyaf-oracle` owns plugin wiring. A bare `pytest -q` emits no manifest (`runner-common/runner_common/pytest_lazyaf.py:41–46`). |
| 38 | `oracle_file_hashes` → hash the **`oracle_paths` closure**, not "files carrying an oracle id" (§6.2). |
| 40 | `reference_patch` becomes **code-only**; add `test_patch` (test-only) as a sibling field (§2.4a). |
| 542 | dangling reference: sends the reader to `upcoming/m13-phase-specs.md` Part 1, which does not exist; the formulas are Part 1 of this very document. Cited **from METHOD.md**, which ships to strangers. |
| 596–618 | METHOD.md template: add a **prompts** row to the provenance table; add the contamination sentence from §6.4; add the `error`-cannot-be-produced limitation from §6.3. |
| 604 | re-run command spelling — reconcile with `api-surface.md:997` and `:1104–1107`. Three spellings exist (`--repeats` vs `--repeat`; `bench run` vs `bench experiment run`) for the one string a test asserts "executes verbatim" (`:962`). |
| 644 | keep verbatim. Add: **the DB is a projection and nothing else may write to it** — the case API becomes read-only (§3.4). |
| 647–648 | delete `slug:` and `suite:` from the case body; they are the path (§2.2). |
| 649 | `repo: gh-mirror/flask-api-demo` → the durable triple `repo: {source_url, base_commit_sha, license}`; drop top-level `source_url`/`license` (§2.2, §2.3). |
| 656 | replace the worked `test_command` with the §2.3 form under an `oracle:` block including `id_mode`. |
| 674–678 | CLI: add `lazyaf bench sync`; replace `case add-from-fix` with **`case derive`** (§2.5); name `lazyaf-oracle` as a 13.1 `runner-common` deliverable (§2.7). |
| 676 | reconcile the `bench case add` signature with `api-surface.md:1069–1076` — writes-a-file vs POSTs-a-row is the source-of-truth question the milestone claims to have settled. Recommendation: **writes a file**. |
| 678–698 | 13.1 deliverables: add `POST /api/repos/ingest-remote` and a `Repo.license` column as **backend** deliverables (§2.8); add the `lazyaf-test-runner` runner-common staging fix (`scripts/build_images.py:83`). |
| 681 | validator: base state now runs **at base + `test_patch`**; add checks 6–9 from §2.6. |
| 683 | flake screen must **print a suggested YAML diff**, never write back to the corpus (§3.3). |
| 697 | starter suite: either 15 cases (5/vertical) or an explicit `DESCRIPTIVE — NOT RANKED BY CONSTRUCTION` declaration; add a `large` complexity tier and the `fanout-v1` suite (§5.3). |
| 705 | the loader is a **deliverable**, not only a test target; add the push-triggered sync (§3.4). |
| 740 | strategy home: `bench/strategies/<slug>.yaml` **wins** over `strategy-catalog.md:131`'s JSON-fixture-seeded-on-migration. Seed list: reconcile with `strategy-catalog.md:831` — nine slugs, `null-agent` and `gold-patch` restored (§4.6). |
| 747–761 | **delete the YAML graph dialect entirely.** One dialect (§4.1). |
| 771 | the clean-container re-score is **authoritative and normative**; add the restore step (`git checkout <base> -- <oracle_paths>` + reapply `test_patch`) (§6.1, §6.2). |
| 773 | keep; note that `_pump_once` reserves nothing in flight today (risk #5). |
| 799 | 13.2 DoD: the seed templates must validate under the §4.5 `lazyaf_scorer` rule, not "exactly one terminal oracle step". |
| 824 | `bench_metrics.py`: state that the envelope of §5.6 is what the serializer emits, unchanged. |
| 925–940 | bundle layout: add `prompts/`; declare this layout **canonical** over `api-surface.md:970–980` (§8, api-surface row). |
| 946 vs 976 | **import behaviour contradiction**: `:946`/`:976` say the board refuses to pool and renders separate labelled tables; `api-surface.md:996–1000` says import succeeds with `provenance_mismatch: true` in one pooled table. The 13.5 exit gate tests the first, the board spec implements the second. **Pick refuse-to-pool.** |
| 948, 958–962 | add `test_bundle_import_on_virgin_tree.py` (§7); make `test_fetch_repo_verification.py` exercise the success path via a `file://` origin, not only the failure branch. |
| new §13.0 | **precedence rule** for `loop_policy` across its four sources, and `Trial.effective_loop_policy` (§5.4). |

### `docs/milestone-13/api-surface.md`

| line(s) | change |
|---|---|
| 79–82 | `content_hash` is computed over the **canonical on-disk `BenchmarkCaseSpec`**, not DB rows. Exclude `repo_id`, `quarantined_tests`, `oracle_file_hashes`, `solvable_verified`; `suite_content_hash` = hash over sorted `case_content_hash` (§3.1). **Fix this before the first published bundle.** |
| 93–96 | case POST/PATCH become read-only (or write-through) per §3.4. |
| 104–113 | case JSON: `repo` triple instead of `repo_id`; add `test_patch`, `oracle_paths`, `oracle.id_mode`, `checks`. |
| 141–145 | **the biggest single fix.** Delete "the test may not exist yet at `base_commit_sha`, which for `fail_to_pass` is the normal case (the fix commit adds it)". Replace with the `test_patch` rule (§2.4a). As written, the design's declared normal case is the one its validator at `phase-specs-and-metrics.md:681` refuses. |
| 187–193 | keep the vocabulary; add a note that **`error` cannot currently be produced** (§6.3) so the branch is not mistaken for live. |
| 198–217 | move `quarantined_tests` / `oracle_file_hashes` / `solvable_verified` here as validation **outputs**. Keep the launch refusal at `:214–216` exactly as written. |
| 223, 236–250, 264–272 | **delete the list-of-steps graph dialect.** It does not validate against `backend/app/schemas/pipeline.py:133`. Rewrite the validation table in `lazyaf_*` terms; delete `fanout_source` (§4.4); replace "exactly one terminal oracle step" with the `lazyaf_scorer` rule (§4.5). |
| 248 | the oracle command `pytest -p lazyaf --lazyaf-results $LAZYAF_TEST_RESULTS_PATH` is wrong in both halves → `lazyaf-oracle run` (§2.7). |
| 286 | add `case_content_hash`, `prompt_content_hash`, `variant_hash`, `variant_label`, `fanout_k`, `model_assignment_name`, `effective_loop_policy`, `underfilled_fanout` to `Trial` (§3.2, §4.4, §5.4). |
| 312–314, 329 | state the `loop_policy` precedence; state whether the graph's `prompt_template` or the trial's `prompt_template_id` reaches the agent — today both exist with no precedence, so a "prompt" axis may be varying nothing. |
| 720 | keep verbatim. This is the model for how to close a cheat. |
| 735–770 | add `origin` on `TestRun` (`trial` / `agent_feedback`) as a second consumer of this walk (§6.1). |
| 791–793 | "**no migration beyond the target type**" is **false**. `ExperimentRun` stores matrix coordinates as columns (`backend/app/models/experiment.py:285–299` — agent, model, prompt, repeat_index; no strategy, no case, no K) and `expand_matrix` enumerates models × prompts × repeats with a `cell_index` formula its own docstring calls "part of the API contract" (`backend/app/services/experiment_service.py:216–262`). Decide and write down whether a bench cell **is** a Trial. Recommendation: yes — extend `CellCoordinates` to `(strategy_i, assignment_i, case_i, repeat_i)` so the shipped pump/CAS-claim/budget/abort machinery is reused rather than reimplemented while spending real money on a 270-trial matrix. |
| 802–820 | add the `template_variables` axis so the K-sweep can be requested at all (§4.4). |
| 828 | "cells" means strategies here and "one agent run" in `backend/app/models/experiment.py:246–247`. Two units, one word. Rename. |
| 870–938 | rewrite §4.3 against §5: the metric envelope, `verdict` (five states) replacing `separable`, `n_cases`, `|C∩|` + exclusions, the regression denominator, `pilot`/`error_rate`/`ranked`, the default partial order, and the consolidated refusal list. |
| 879 | `group_by=k` and `group_by=model_assignment` need `Trial.fanout_k` and `Trial.model_assignment_name` + indexes to be real (§5.4). |
| 944 | per-case board: add the §5.2 statistics rules and the `R≥10` descriptive default. |
| 970–980 | **delete this bundle layout** in favour of `phase-specs-and-metrics.md:925–940`. The api-surface layout ships one repo copy **per case** (N copies of one fixture tree) and omits `sha256_of_tree`, which the import-verification test depends on. |
| 996–1000 | reconcile with `:946` — refuse-to-pool, not flag-and-pool. |
| 1069–1084 | `case add` signature (see phase-specs row); replace `add-from-fix` with `case derive` (§2.5). |
| 1141–1148 | add indexes: `(experiment_id, variant_hash)`, `(strategy_template_id, fanout_k)`. |
| 1170 | relax "empty `fail_to_pass` is a 422" to "empty `fail_to_pass` **and** empty `checks` is a 422" (§2.4c). |

### `docs/milestone-13/strategy-catalog.md`

| line(s) | change |
|---|---|
| 13–24 | declare this the **single** graph dialect (both other doc dialects deleted). Add `lazyaf_scorer` to the reserved keys; add **`agent`** to the keys a template may not author (`backend/app/services/pipeline_executor.py:423` — `agent` beats `runner_type`) (§4.2). |
| 81 | keep. Add: the binder also writes **`config.role`**, which is what actually reaches `LAZYAF_ROLE` (`local_executor.py:704`, `runner_protocol.py:540`) and therefore `StepUsage.role` — no new column or env plumbing needed (§4.2). |
| 102–113 | add the missing matrix axis (`api-surface.md:802–820`) so `planner-fanout` at K=8 is expressible; stop using `planner-fanout-8` as a slug anywhere. |
| 123 | keep. It is correct and derived from real executor behaviour — and it is what forces §4.5's rule change. |
| 131 | two changes: catalog home is `bench/strategies/*.yaml`, not seeded JSON fixtures (§4.6); and **"There is no second scoring channel"** becomes "the graph's `run_tests` never scores; it only tells the agent what is red" (§6.1). |
| 330–440 | keep S4 as the **expanded** form and label it so; add the §4.3 **template** form (with `lazyaf_fanout`) as the stored object. |
| 445–446 | keep. Add a pointer to §5.3: on `core-v1` this prediction cannot be tested, and `fanout-v1` is the corpus that would test it. |
| 504–512, 540–571 | S5 (two oracle steps) and S6 (none) are legal under §4.5's `lazyaf_scorer` rule and are rejected under `api-surface.md:271`'s. Annotate both with the scorer designation. |
| 831 | reconcile the seed list with `phase-specs-and-metrics.md:740`: two of seven names match, and `null-agent` — required in every published experiment (`phase-specs-and-metrics.md:876`) — is missing. Slugs are the published join key (`:797`), so settle this **before** the first number. |

### New files this document implies

```
backend/app/services/bench_oracle.py                        §6.3  (pure, no DB)
backend/app/services/bench_metrics.py                       absorbs experiment_metrics.py
runner-common/runner_common/oracle.py                       §2.7  lazyaf-oracle
bench/                                                      §2.2  corpus root, in VCS
tdd/unit/benchmark/test_doc_example_bodies_validate.py      §4.1
tdd/unit/benchmark/test_bench_oracle_missing_is_broken.py   §6.3
tdd/unit/benchmark/test_worst_status_wins_is_load_bearing.py §6.2
tdd/unit/benchmark/test_cost_to_solve_invariant_to_third_variant.py §5.5
tdd/unit/benchmark/test_group_by_strategy_over_two_assignments_is_not_ranked.py §5.4
tdd/unit/benchmark/test_board_blocks_mixed_iteration_caps.py §5.4
tdd/unit/benchmark/test_regression_badge_survives_every_group_by.py §5.7
tdd/unit/benchmark/test_per_case_board_is_descriptive_below_r10.py §5.2
tdd/unit/benchmark/test_template_may_not_pin_agent_key.py   §4.2
tdd/integration/benchmark/test_bundle_import_on_virgin_tree.py §7
```

---

## Appendix — the honest list

Things this document declines to design around, because the honest answer is
that they cannot be measured reliably yet:

1. **Contamination.** Recorded, disclosed, split on. Not controlled, not
   detected. §6.4.
2. **Per-case verdicts at R=5.** 20-percentage-point resolution. Descriptive
   only until R≥10. §5.2.
3. **Vertical and complexity splits on `core-v1`.** 3 cases per split against a
   gate of 5. The view is built and dead until the corpus grows. §5.3.
4. **The fan-out hypothesis on `core-v1`.** Guaranteed to lose 6 of 9 cases by
   the catalog's own reasoning, with 3 `medium` cases to win on — below the
   ranking gate. It needs `fanout-v1`. §5.3.
5. **`error` as an oracle status.** In the vocabulary, countable in the scoring
   rule, and impossible to produce. A broken environment on a guard test reads
   as an agent-caused regression. §6.3.
6. **Iterations-to-solve, at the first number.** `max_iterations = 1` makes it
   *absent*, not wrong. §7.
7. **Long-run reproducibility of licence-restricted `fetch/` cases.** The tree
   hash catches substitution; nothing catches deletion. Best-effort, and say so.
   §2.1.
