# The Graph Test Catalogue

**What this is.** A specification of what the graph pipeline surface should be
tested for, and why — written to be executed by an implementation wave *after*
Phase 12.8 lands. It is not test code. Test code written today would land in
files the 12.8 wave currently owns (`test_pipeline_executor.py`,
`test_graph_coverage.py`, `tdd/unit/schemas/**`, `test_pipeline_local_dispatch.py`,
`tdd/e2e/test_graph_pipeline.py`, the graph editor, `graph-pipeline.spec.ts`)
and against an executor whose v1 array fork is being deleted underneath it. The
*catalogue* is stable across that refactor; the code is not.

**Every claim in here was verified against source at authoring time.** Where a
finding says VERIFIED BY EXECUTION, it was produced by importing the real module
and calling the real function, not by reading a test name. Line numbers are cited
throughout; re-check them after the retirement lands, because the v1 deletion
will move most of `pipeline_executor.py`.

**Scope note.** Nothing here proposes a test for the v1 array format. Rows that
12.8's own acceptance gate (§5.1 of `upcoming/wave10-v1-retirement.md`) already
requires are marked `COVERED_BY_12_8` and deliberately left unspecified.

---

## 1. The coverage picture, honestly

### 1.1 The headline: `success` is the only condition any gated test has ever dispatched

`EdgeCondition` has three members. Two of them have never fired in a test that
any tier runs.

Verified by grep across `tdd/`: every execution-level assertion about a
`failure` or `always` edge lives in one of two places, and neither executes:

- **`tdd/qa/`** — `scripts/run_tier.py` maps T1 to `tdd/unit` + `tdd/demos` +
  `tdd/integration` (minus `services`) + `runner-common/tests`, T2 to
  `tdd/integration/services`, T3 to `tdd/e2e -m "not slow"`. **`tdd/qa` appears
  in no tier and has no floor.** It holds 27 graph tests (12 definition, 15
  execution), most of them `xfail(strict=True)`.
- **`tdd/e2e/test_graph_pipeline.py`** — its `failure`-edge references at
  lines 118, 155, 172, 717 and 730 are **CRUD**: create a pipeline carrying a
  failure edge, read it back over the API. No run is started.

12.8 §5.1 closes the *basic* form of this hole (a failure edge fires its target
on failure; an always edge fires on both outcomes; a success edge does not fire
on failure; plus a T2 real-container twin for the failure edge). **Everything in
this catalogue is beyond that line.**

### 1.2 Every execution-level graph e2e test runs in no tier

This is the largest "looks covered, executes nowhere" surface in the repo.

| Location | What it covers | Why it never runs |
|---|---|---|
| `tdd/e2e/test_graph_pipeline.py:182` | class `TestGraphPipelineParallelExecution` — parallel steps, fan-out, **fan-in**, parallel active tracking (4 tests) | class-level `@pytest.mark.slow` |
| `tdd/e2e/test_graph_pipeline.py:774` | class `TestGraphPipelineExecutionVisualization` — per-node step status (1 test) | class-level `@pytest.mark.slow` |
| `tdd/e2e/test_graph_pipeline.py:896` | `test_partial_failure_marks_correct_steps` | method-level `@pytest.mark.slow` |

`scripts/run_tier.py:122-127` selects T3 as `../tdd/e2e -m "not slow"`. So **6 of
the file's 21 tests — every single one that RUNS a graph — are deselected.** T3's
floor of 21 (measured 22) is met entirely by CRUD, YAML export and UI-behaviour
tests. A file named `test_graph_pipeline.py` containing fan-in and diamond
coverage contributes zero executed graph runs to any gate.

### 1.3 Two gated tests that cannot fail

- **`tdd/e2e/test_graph_pipeline.py:876`** —
  `assert response.status_code in (201, 400, 422)`. That is every status the
  endpoint can produce. Its own comment says *"For now, document the behavior."*
  This is the only thing in any tier that looks like cycle rejection at the HTTP
  boundary, and `PipelineGraphModel.validate_graph_integrity`
  (`backend/app/schemas/pipeline.py:141-160`) does not check for cycles at all.
- **`tdd/e2e/test_graph_pipeline.py:352-357`** — the repo's *only* executing
  fan-in test ends with `if step_runs:` and the bare comment
  `# Join should have completed after A and B` where the ordering assertion
  should be. It asserts a count and then declines to check the ordering it is
  named for. (It is also `@slow`, so per §1.2 it does not execute either.)
- **`tdd/e2e/test_graph_pipeline.py:938`** — `test_partial_failure_marks_correct_steps`
  guards both of its assertions behind `if pass_step and fail_step:`. A step
  rename turns it green.

### 1.4 Where the coverage actually is

| File | `def test_` | What it really proves |
|---|---|---|
| `tdd/unit/schemas/test_graph_pipeline_schemas.py` | 122 | Schema construction and `array_to_graph` fidelity. **Zero execution.** |
| `tdd/unit/services/test_pipeline_executor.py` | 58 | Actions, merge resolution, edge conditions — mostly entered at `_handle_graph_step_complete` with dispatch patched out. |
| `tdd/unit/services/test_pipeline_local_dispatch.py` | 57 | The real-dispatch seam against `FakeLocalExecutor`. The best harness in the tree; only two of its tests use a graph shape an array could not express. |
| `tdd/unit/services/test_graph_coverage.py` | 39 | `graph_definition_errors` and `unreached_graph_steps` as **pure functions**. Strong. |
| `tdd/e2e/test_graph_pipeline.py` | 21 | 15 CRUD (gated) + 6 execution (**ungated**, §1.2). |
| `tdd/qa/test_graph_execution_qa4.py` | 15 | **Ungated.** |
| `tdd/qa/test_graph_definition_qa4.py` | 12 | **Ungated.** |

**T2 has essentially no graph coverage.** `grep -l entry_points` across
`tdd/integration/services/**` returns exactly one file with a multi-node graph
shape: `test_parallel_control_steps.py:220` (two entry points, no edges). There
is **no fan-out, no fan-in, no diamond and no conditional edge against a real
container anywhere.** Every concurrency property of the graph — the shared
workspace volume, cleanup ordering, container lifetime — is proven only against
`FakeLocalExecutor`.

### 1.5 The three-state summary

- **COVERED** — schema construction; `array_to_graph`; `graph_definition_errors`
  and `unreached_graph_steps` as pure functions; a single node's actions firing
  once; routing decisions per step; graph CRUD over HTTP.
- **COVERED-BUT-UNGATED** (the trap) — fan-out, fan-in, diamond, parallel active
  tracking, partial failure, duplicate edges, duplicate entry points, cycle and
  self-edge refusal, timeout bounds, key/id mismatch, dispatch latency, fan-out
  cap. **All of it in `tdd/qa` or behind `@slow`. None of it executes in a gate.**
- **NOT COVERED** — the fan-in condition gate (`_all_upstream_satisfied` has
  **zero tests of any kind**; the only mention of the symbol anywhere under
  `tdd/` is a prose comment at `tdd/qa/test_graph_execution_qa4.py:127`); actions
  × edges interaction; action *effect* doubling; run-level invariants
  (terminality, `active_step_ids`, `steps_completed <= steps_total`); every
  hostile-input shape on a node or edge; and the entire negative surface of the
  HTTP boundary — `tdd/integration/api/test_pipelines_api.py` contains **not one
  422 assertion for a malformed graph** (its only negative test is a missing
  name, at :265).

---

## 2. The catalogue

### How to read a row

- **Guarantee** — the test name, written as the sentence the test proves. House
  style: a statement of what holds, not a description of what the test does.
- **FAILS TODAY** — 🔴 means this is a **defect**, not a gap: the test would fail
  against current `main`. 🟡 means the behaviour is correct today and the test is
  a **pin** against regression. 🔧 next to 🔴 means **a product fix must land
  before the test can pass** — see §5; write the test in the same commit as its fix.

Ordering within each group puts 🔴 first. That is deliberate: those rows are the
argument for doing this work at all.

---

### 2A · SEMANTICS — graph shapes, fan-in/fan-out, actions × edges

#### 🔴 Would fail today

| ID | Guarantee | Shape | Assertion | Tier |
|---|---|---|---|---|
| **S-01** 🔧 | *a join placed after a conditional branch can actually run* | `a -success-> ok`, `a -failure-> recover`, `ok -always-> report`, `recover -always-> report`, entry `[a]`. Two runs: `a` passes; `a` fails. | Both runs reach `report` **exactly once**; the passing run ends `PASSED`. Pure-function companion: `unreached_graph_steps(g, completed_ids={"a","ok"}, active_ids=set(), outcomes={"a":True,"ok":True}) == {}`. | T1 |
| **S-02** 🔧 | *a fan-in does not run when one of its upstream branches failed* | Diamond `a -> {b, c}`, `b -> d`, `c -> d`, all success edges. `b` FAILS, `c` passes, **`c` completes last**. | `d` is NOT dispatched. `d` has no `StepRun`. | T1 |
| **S-03** 🔧 | *a diamond with one failed arm reaches the same verdict whichever arm finishes last* | S-02's diamond, run twice: once with `c` completing last, once with `b` completing last. | The two runs agree on whether `d` ran, on the final status, and on the failure reason. Assert by comparing the two collected run summaries, not by two independent literals. | T1 |
| **S-04** 🔧 | *a join fed by two sources on different conditions reaches the same outcome whichever source finishes first* | `a -success-> b`, `c -failure-> b`, entry `[a, c]`. Both `a` and `c` PASS. Force both completion orders. | The two orders agree. (Today: `a`-then-`c` leaves `b` unrun and fails the run for coverage; `c`-then-`a` **dispatches `b` on a failure edge that never fired**.) | T1 |
| **S-05** 🔧 | *`_all_upstream_satisfied` answers for every condition × outcome combination* | Direct unit table on the method, no DB. One incoming edge × `{success, failure, always}` × `{source passed, source failed, source unfinished}`; then two incoming edges over the same matrix; then an incoming edge from an ACTIVE step; then no incoming edges. | The documented rule row by row: **a step is ready when every incoming edge whose condition can still fire has fired.** A success edge from a FAILED source does not satisfy; a failure edge from a PASSING source does not block; an always edge from a finished source satisfies. Requires the method to take `outcomes`, which it does not today — that signature change *is* the fix. | T1 |
| **S-06** 🔧 | *a "clean up either way" join is refused at save time rather than failing every run* | `deploy -success-> cleanup`, `deploy -failure-> rollback`, `rollback -failure-> cleanup`, entry `[deploy]`. | `POST /api/repos/{id}/pipelines` returns 422 naming `cleanup` and the unsatisfiable join **or** (once S-05 lands) the happy-path run reaches `cleanup` and ends `PASSED`. What must not hold is today's behaviour: 201, then a red run blaming `cleanup`. | T1 |
| **S-07** 🔧 | *a node reached by both a success edge and an always edge from the same source runs once* | `a -success-> b` AND `a -always-> b`, entry `[a]`, `a` passes. | Exactly one `StepRun` for `b`; exactly one entry in `env.local.calls` for `b`. | T1 |
| **S-08** 🔧 | *a node reached twice fires its actions once* | S-07's shape, with `b` carrying `actions.success = ["merge:main"]` and `actions.failure = ["trigger:card-1"]`. | `len(fake_git.merges) == 1`; exactly one Card/Job pair. **This asserts a different observable from S-07** (effects, not rows) and must be its own test — see D2. | T1 |
| **S-09** 🔧 | *two identical `a->b` edges dispatch `b` once* | `a -success-> b` twice with distinct edge ids, entry `[a]`. | One `StepRun` for `b`; one dispatch. Kept separate from S-07 because the *fix* differs: a literal duplicate can be refused at definition time, a success/always pair cannot without changing edge semantics. | T1 |
| **S-10** 🔧 | *a step named twice in `entry_points` is dispatched once* | `entry_points: ["a", "a"]`, single node `a`, no edges. | One `StepRun`; one dispatch; the run goes terminal only after that one container finishes; **no `StepRun` left `status == "running"`**. | T1 |
| **S-11** 🔧 | *`steps_completed` never exceeds `steps_total`* | Any double-dispatching graph — S-07's success/always pair, or S-10's duplicate entry point. | `0 <= run.steps_completed <= run.steps_total` on the terminal run. The cheapest possible detector of duplicate dispatch, reading the field the UI renders as a progress bar. | T1 |
| **S-12** 🔧 | *every `StepRun` on a terminal run is itself terminal* | An invariant helper asserted by the shared graph harness after `start_and_wait` returns, applied to a matrix of runs: the duplicate-dispatch shapes, a run failed by a bad terminal action mid fan-out, and a cancelled fan-out. | For a run whose status is `passed`/`failed`/`cancelled`: every `StepRun` has `status in (passed, failed, cancelled)` and `completed_at is not None`. | T1 |
| **S-13** 🔧 | *`active_step_ids` is empty on every terminal run, cancellation included* | Three runs: a passing chain; a run failed by an unresolvable `merge:` action mid fan-out; a fan-out cancelled while both siblings are live. | `parse_json_list(run.active_step_ids) == []` on all three. | T1 |
| **S-14** 🔧 | *a still-running sibling is not abandoned when another sibling's action fails the run* | `a -> {b, c}`; `b` carries `actions.success = ["merge:does-not-exist"]`; `c` is a slow step still RUNNING when `b` finishes. | `c`'s `StepRun` is not left `status == "running"` with `completed_at IS NULL`, and the terminal run does not still name `c` in `active_step_ids`. | T1 |
| **S-15** 🔧 | *every node of a dead component is named when the run fails for not covering its graph* | Component 1: `{a}`, entry `[a]`. Component 2: `x -success-> y`, no entry point. `entry_points == ["a"]`. | The run FAILS **and a `StepRun` names both `x` and `y`**. Verified today: `graph_definition_errors` names only `x`; `unreached_graph_steps` also names only `x`. | T1 |
| **S-16** 🔧 | *a graph run's failure is attributed to a step that actually ran* | Any 3-node linear graph where step 2 fails. | `machine.mark_step_failed` is not handed index 0 for a failure in step 1. Either `current_step` tracks the last dispatched node, or the field leaves the graph wire contract. See D8 — after the v1 deletion this column has **no writer at all**. | T1 |
| **S-17** | *a cycle is refused at definition time* | `a -success-> b -success-> a`, entry `[a]`, posted to `POST /api/repos/{id}/pipelines`. | 422 naming the cycle path, in the words `graph_definition_errors` already produces. Parametrize over cycle length 2, 3, 50. | T1 + T3 |
| **S-18** | *a self-edge is refused at definition time* | `a -success-> a`, entry `[a]`. | 422 naming the edge id and the step. Today it is accepted, **silently discarded** by the traversal, and then fails the run through the "structurally broken but nothing unrun" branch — the author's intent is thrown away twice before anyone tells them. | T1 + T3 |

#### 🟡 Pins against regression

| ID | Guarantee | Shape | Assertion | Tier |
|---|---|---|---|---|
| **S-19** | *a linear chain dispatches every node exactly once, in edge order* | `a -> b -> c`, entry `[a]`, all pass. Real dispatch via `start_pipeline` + `FakeLocalExecutor`. | `env.local.calls` records the three commands **in edge order**; exactly 3 `StepRun`s, one per `step_id`; `completed_step_ids` covers all three; run `PASSED`; `active_step_ids == []`; `steps_completed == steps_total == 3`. | T1 |
| **S-20** | *a chain that fails mid-way leaves the tail with no `StepRun` at all* | `a -> b -> c`, entry `[a]`; `b` fails. | Run `FAILED`; exactly 2 `StepRun`s (`a` PASSED, `b` FAILED); **no row of any kind for `c`** — not a FAILED one, not a synthetic "never ran" one; `steps_completed == 1`, `steps_total == 3`. Guards `unreached_graph_steps` against over-firing and trading a false green for a false red. | T1 |
| **S-21** | *a fan-out dispatches every target exactly once and runs them concurrently* | `source -> {t1, t2, t3}`, entry `[source]`, all pass. | 4 `StepRun`s; `len(env.local.calls) == 4`; the three targets' execution windows **overlap** (two in flight before either finishes); run `PASSED`. | T1 |
| **S-22** | *a failing fan-out sibling does not stop its siblings* | `start -> {will_pass, will_fail}`, entry `[start]`; `will_fail` exits non-zero. | All 3 `StepRun`s exist; `will_pass` PASSED and `will_fail` FAILED; run FAILED; **both siblings' containers were launched** (`len(env.local.calls) == 3`). | T1 |
| **S-23** | *a join waits for every declared upstream, not just the one that selected it* | `a -> join`, `b -> join`, entry `[a, b]`; `b` is slow so `a` finishes far earlier. | `join` dispatches exactly once; `join.started_at >= max(a.completed_at, b.completed_at)`; 3 `StepRun`s; run `PASSED`. **This is the assertion `tdd/e2e/test_graph_pipeline.py:352` declines to make.** | T1 |
| **S-24** | *a diamond runs the join exactly once when every arm passes* | `a -> {b, c}`, `b -> d`, `c -> d`, entry `[a]`, all pass. | Exactly one `StepRun` for `d`; `len(env.local.calls) == 4`; `d` started after both `b` and `c` completed; run `PASSED`; `steps_completed == 4`. | T1 |
| **S-25** | *a node reached by a success edge and a failure edge from the same source runs once, whichever way the source goes* | `a -success-> b` AND `a -failure-> b`, entry `[a]`. Two runs: `a` passes; `a` fails. | Exactly one `StepRun` for `b` in each run; `b` runs in both. **The control row for S-07/S-09**: it is correct today, and a naive de-dup by `(from_step, to_step)` would collapse it and lose the failure path. | T1 |
| **S-26** | *a doubly-dispatched node does not cascade its duplication downstream* | `a -success-> b` AND `a -always-> b`; `b -success-> c`; entry `[a]`. | However many times `b` runs, `c` runs **exactly once**. Pins that the defect is contained; a de-dup fix that moves the already-active guard could turn a contained defect into a cascading one. | T1 |
| **S-27** | *two independent entry points both start, and the run status is the AND of both* | `{a}`, `{b}`, no edges, entry `[a, b]`. Three runs: both pass; `a` passes and `b` fails; both fail. | 2 `StepRun`s in every case; `PASSED` only when both pass; `steps_completed` counts only the passing ones while `steps_total` stays 2; `active_step_ids == []`. Also pins that `steps_completed != steps_total` on a FAILED run is deliberate. | T1 |
| **S-28** | *two disconnected components that each have an entry point both run to completion* | `{a -> b}` entry `a`, and `{x -> y}` entry `x`, no edges between. All pass. | 4 `StepRun`s; `graph_definition_errors(graph) == []`; run `PASSED`; `steps_completed == 4`. **The negative control for S-15** — the coverage guard must not fire on a disconnected graph that is fully covered, or every multi-lane pipeline turns red. | T1 |
| **S-29** | *a single-node graph with no edges runs it once and takes its verdict* | `{only}`, `edges: []`, entry `["only"]`. Two runs: it passes; it fails. | One `StepRun`; run status matches the node; **no synthetic "never ran" or "pipeline graph" `StepRun` invented by the coverage gate**; `steps_total == 1`. **The most executed shape in the product** after the retirement: `start_adhoc_agent_run`, `start_endpoint_probe_run`, the experiment cell launcher and `test_api.seed_state` all emit exactly this (plan §4.4 rows 4-7), so a coverage-gate regression here reddens every card. | T1 |
| **S-30** | *a recovery branch cannot turn a failed run green* | `a` FAILS, `a -failure-> fix`, `fix` passes, no other steps. | Run `FAILED`, and the reason names `a`. `_check_all_steps_passed` is what makes this correct; nothing pins it, and the recovery branch is precisely where a false green is most tempting. The archetypal day-one graph. | T1 |
| **S-31** | *an action on a fan-in node fires after the join, once, not once per upstream* | `a -> join`, `b -> join`, entry `[a, b]`; `join` has `actions.success = ["merge:main"]`. | Exactly one `merge_branch` call, after both `a` and `b` completed; run `PASSED`. "Merge only when everything is green" is the single most important thing a CI product does with a graph, and every existing action test fires on a node with one upstream or none. | T1 |
| **S-32** | *a chain of acting nodes fires its effects in graph order, each exactly once* | `a` (`actions.success = ["merge:main"]`) `-success-> b` (`actions.success = ["trigger:card-x"]`), entry `[a]`. | Recorded event order: merge → dispatch `b` → `b`'s container → trigger card. One merge, one card, run `PASSED`. 12.8 pins that *one* node's merge precedes *its* fan-out; nothing pins that the effect chain stays ordered across two nodes. | T1 |
| **S-33** | *a failing node fires its failure actions before its always actions, and both before the failure edge fans out* | `a` with `actions.failure = ["trigger:card-x"]` and `actions.always = ["merge:release"]`, plus `a -failure-> recover`. `a` fails. | Order is trigger → merge → dispatch `recover`; run `FAILED`; `recover`'s `StepRun` exists. 12.8 pins this ordering on the **success** side only; the failure side is where fix cards live, and it is the only place an action has ever been combined with a failure edge. | T1 |
| **S-34** | *an always edge and an always action on the same node both fire, on both outcomes, without one consuming the other* | `a` with `actions.always = ["merge:main"]` and `a -always-> cleanup`. Two runs: `a` passes; `a` fails. | In both: exactly one merge, `cleanup` dispatched exactly once, merge before dispatch. The only place the two `always` vocabularies meet on one node. | T1 |
| **S-35** | *an action on a step the run never reaches never fires* | `a -success-> ok`, `a -failure-> recover`; `recover` carries `actions.always = ["merge:main"]`. `a` passes. | `fake_git.merges == []` **and** the run ends `PASSED` (the untaken branch is not a coverage defect). Guards a future de-dup or eager-action fix: an action firing for a node that never ran would merge a branch no step validated and look identical to a correct run. | T1 |
| **S-36** | *the ad-hoc single-node run reaches its verdict through `start_pipeline`* | Single node with `actions.failure = ["trigger:card-x"]`, driven from `start_pipeline` through real dispatch to completion. The node fails. | The fix card is spawned via `agent_run.start_card_work`; the marker `StepRun` exists with `step_id is None`; run `FAILED`; `_notify_agent_run_complete` fires **exactly once**. 12.8's fix-card coverage enters at `_handle_graph_step_complete` with dispatch patched; on a `card_work` run the action, `_complete_pipeline`'s triggers and the notification all fire from the same method. | T1 |

---

### 2B · PITFALLS — what a human authoring a graph will build that does not do what they meant

The organising question: **does the product tell them at the earliest possible
moment (R1), or does it accept the graph and fail the run?**

#### 🔴 Would fail today

| ID | Guarantee | Shape | Assertion | Tier |
|---|---|---|---|---|
| **P-01** 🔧 | *the two authoring boundaries refuse the same graphs* | One shared corpus of structurally-broken graphs (cycle, self-edge, unreachable step, orphaned tail, duplicate entry points, duplicate parallel edges). For each: (a) the equivalent YAML through `array_to_graph`, assert `ArrayConversionError`; (b) the same graph as `steps_graph` over HTTP, assert 422. | For every graph in the corpus, both boundaries reach the **same verdict with the same defect strings** — not merely "both raise something". `graph_definition_errors` is the single authority (its own docstring says so) and both callers must reach it. | T1 |
| **P-02** | *a step no entry point names and no edge reaches is refused at save time* | `steps {a, orphan}`, `edges []`, entry `["a"]`. | 422 naming `orphan` and why. Today: 201, the run executes `a`, then `_verify_graph_coverage` fails the whole run at the end — a red run for work that was never going to happen, discovered after the containers ran. One forgotten wire in a node editor. | T1 |
| **P-03** | *a step that continues on neither outcome is refused, naming the tail it orphans* | `a -> b`, plus a node `c` with no incoming edge — the author wired `a->b`, stopped, and left `c` dangling. | 422 naming the step that stops **and counting the steps left unreachable behind it** — the same message shape `array_to_graph` already produces for a mid-array stop (`schemas/pipeline.py:820-833`, 12 tests behind it at `test_graph_pipeline_schemas.py:1413-1616`). The array path refuses this; the graph path — **the only path after the retirement** — accepts it silently. That message quality is the bar. | T1 |
| **P-04** | *an entry point that is also the target of an edge is refused* | `entry_points ["a","b"]` with an edge `a -> b`. | 422: `b` cannot be both an entry point and an edge target — it will run before its own predecessor. **VERIFIED: `graph_definition_errors` returns `[]`. Silent at every layer.** At run time both dispatch immediately; when `a` completes, the `a->b` edge is skipped by the already-completed guard, so **the edge the author drew never fires** and the run goes green. In the editor this is what you get by wiring Start to a node already in a chain. | T1 |
| **P-05** | *connecting Start to the same step twice yields one entry point* | Playwright, or a vitest unit test over the editor's `createEdge`/`deriveEntryPoints` pair: connect `Start->a`, connect `Start->a` again, read the emitted graph. | `entry_points == ["a"]` exactly once; one `Start->a` edge. **VERIFIED: `addStartEdge` (`PipelineGraphEditor.svelte:161`) filters an existing `Start->target` edge before appending; `createEdge` (:397-417), which is where the handle drag lands via `onConnect` (:421), does not — and `deriveEntryPoints` (:370-374) is a bare `.filter().map()` with no de-dup.** Two authoring gestures in one editor, opposite behaviour. A second drag — the thing every user does when the first line does not visibly land — authors S-10, which since P1 is a double merge. | playwright / vitest |
| **P-06** | *reconnecting two already-connected steps does not silently become a failure edge* | Editor: connect `a->b` (gets `success`). Connect `a->b` again. Then a third time. | Either the second connect is refused/ignored **with a visible message**, or it is created and labelled as what it is — never a silent condition flip. A third connect must not produce two identical failure edges. **VERIFIED: `defaultConditionFor` (:386-394) chooses by "does this SOURCE already have a success edge", not "does this PAIR already have an edge".** So a repeat drag silently becomes a FAILURE edge — the author's conditional `a->b` quietly became "b runs whatever a does" — and a third drag emits a *second* identical failure edge, i.e. S-09 reached purely by clicking. Nothing in the UI says any of this happened. | playwright / vitest |
| **P-07** | *deleting a step in the middle of a chain reports the steps it orphans* | Editor: build `a->b->c`, delete `b`, read the emitted graph. | The author is told at **delete time** that `c` is now unreachable. `deleteStep` correctly removes both edges touching `b` — which is exactly what leaves `c` with no incoming edge and no entry point. `graph_definition_errors` *would* name it; the editor never asks and the save is accepted (P-02). Deleting a step from the middle of a pipeline is an ordinary edit, and the consequence lands three layers away in a failed run. | playwright / vitest |
| **P-08** | *an agent step that names no timeout gets the agent default* | Materialize an agent step with no timeout through the real authoring path (`PipelineStepYaml` → `PipelineStepConfig` → `PipelineStepV2` → `model_dump`), then resolve it the way the executor does. | The resolved timeout is `DEFAULT_AGENT_STEP_TIMEOUT` (1800), not 300. **VERIFIED: `PipelineStepV2.timeout` defaults to 300 (`schemas/pipeline.py:119`) and `PipelineStepYaml.timeout` defaults to 300 (`lazyaf_yaml.py:72`), so `step.get("timeout")` is always truthy and `pipeline_executor.py:2777`'s `or default_timeout_for(step_type)` never reaches its fallback.** The comment three lines above says *"300 is a rounding error for an agent"*. The user's agent step is killed at five minutes; the product's stated intent is thirty. `default_timeout_for` has **zero tests in the entire tree**. | T1 |
| **P-09** | *a zero timeout is refused rather than silently becoming the type default* | A step carrying `timeout: 0`, posted and separately resolved the executor's way. | 422 at save time. Characterizing sub-assert if the refusal is deferred: `timeout=0` currently resolves to **1800** for an agent and 300 for a script, because `0` is falsy at `:2777`. A user writing `0` means "no limit" or "fail instantly" and gets thirty minutes. This and P-08 are the same one-character bug seen from two sides. | T1 |
| **P-10** | *a run refuses to start when its stored graph is structurally broken* | A pipeline row whose `steps_graph` carries a cycle, reaching the row past validation the way a pre-existing row would (direct write, the YAML sync, or the `0012` backfill). `POST /api/pipelines/{id}/run`. | 400 naming the defect; **zero `PipelineRun` rows and zero `StepRun`s created**. `run_pipeline` (`routers/pipelines.py:437-446`) checks only "has entry_points" and "has steps". `Pipeline.definition_error` is guarded at :457-466 but is only ever *set* by the YAML materializer (`trigger_service.py:131`), so that guard is **structurally unreachable for API- and editor-authored graphs**. At run start `graph_definition_errors` is called and only LOGGED (`pipeline_executor.py:2003`, *"the run is NOT aborted here on purpose"*). This is the safety net for every row that predates P-01's refusal. | T1 |
| **P-11** | *`continue_in_context` is honoured or refused, never silently ignored* | A graph node setting `continue_in_context: true`, both posted and run. | Either the flag changes observable behaviour, or the save is refused naming the field. What must NOT hold is today's outcome: accepted, stored, exported, and inert. The field is advertised in three places a user reads (`schemas/pipeline.py:119` *"next step runs with preserved workspace"*, `lazyaf_yaml.py:73`, and the step editor) and `_execute_graph_step`'s only response is a one-time INFO log nobody sees (`pipeline_executor.py:2494` → `:1406-1414`). The `is_continuation` machinery that gives it meaning lives in `_execute_step` — **the array path being deleted.** After the retirement it is documented, settable, exported and dead. | T1 |
| **P-12** | *a fan-out tells the author its branches share one working tree* | `root -> {w1, w2}`, neither naming `config.lazyaf_workspace`. Resolve each step's workspace the way the executor does. | Both branches resolve to the **same volume** (characterizing), **and** the product says so — a save-time warning, or a refusal when two fan-out siblings both set `continue_in_context`. The lane is `worker_key_for_step(step_config)` (`pipeline_executor.py:2807`) reading `config["lazyaf_workspace"]`, defaulting to `DEFAULT_WORKER_KEY`, and `workspace_service.acquire` is a refcount (`use_count += 1`), not an exclusive lock. So N parallel branches run in ONE checkout concurrently. The trap is that **the isolation mechanism exists but is opt-in through an undocumented free-form config key**, so the default fan-out is the unsafe one and nothing says so. | T2 |

#### 🟡 Pins against regression

| ID | Guarantee | Shape | Assertion | Tier |
|---|---|---|---|---|
| **P-13** | *`graphStepOrder` survives a hostile graph* | Vitest, new `frontend/src/lib/components/graph/order.test.ts`. `graphStepOrder`/`graphStepList` over: a 2-cycle, a self-edge, duplicate entry points, an edge to a nonexistent step, a nonexistent entry point, empty steps, a null graph, a 5000-node chain. | Terminates; returns every declared step **exactly once**; returns no id that is not a declared step; is stable across repeated calls. `order.ts:22` is the ONE definition of step order for the pipeline card's "N steps" and the debug re-run modal's breakpoint checkboxes (its own docstring says so) and it has **zero tests** — no test file exists for anything under `components/graph/`. It is safe today; delete the `seen` guard and it hangs the browser on a cyclic graph the backend accepts (S-17). Pure function, plain object, no fixture cost. | vitest |
| **P-14** | *the join step starts only after both upstream steps finish* | Repair of `tdd/e2e/test_graph_pipeline.py:318`: replace the `if step_runs:` block with the ordering assertion it is named for. | `join.started_at >= max(a.completed_at, b.completed_at)`. Pairs with S-23 (the T1 twin) and with X-05 (removing the `@slow` marker so it executes at all). | T3 |

---

### 2C · NEGATIVE — hostile and malformed input at the boundary

Every row in this group was **verified accepted today** by constructing the model.
`PipelineGraphModel.validate_graph_integrity` (`schemas/pipeline.py:141-160`)
checks only that edge endpoints and entry points resolve. It never calls
`graph_definition_errors`, despite that function's own docstring saying the
schema is where these belong — and `array_to_graph` **does** call it
(`schemas/pipeline.py:817-819`). Same defect, two opposite answers, decided by
which surface the user happened to open.

#### 🔴 Would fail today — all of them

| ID | Guarantee | Shape | Assertion | Tier |
|---|---|---|---|---|
| **N-01** | *an unknown condition key on `actions` is refused rather than dropped* | `actions = {"sucess": ["merge:main"]}` (one-character typo). Also `{"succeeded": …}`, `{"on_success": …}`, `{"SUCCESS": …}`. | 422 naming the unknown key and listing `success\|failure\|always`. **VERIFIED: `StepActions` (`schemas/pipeline.py:75`) has no `extra="forbid"`; the model dumps as `{"success": [], "failure": [], "always": []}`. The merge is gone.** 201, a pipeline that renders correctly, a green run, and no merge. `"on_success"` is the sharpest row: it is the key `export_pipeline_yaml` writes for graph steps, so **a user re-importing their own export loses every action** — the exact collision §1.3 chose the `actions.` namespace to avoid. | T1 |
| **N-02** | *an unknown field on a node or an edge is refused rather than dropped* | Node with `"timout": 5`, `"contnue_in_context": true`, `"on_success": "next"`, `"actions": []` (list not object). Edge with `"conditon": "failure"`. | 422 naming the unknown field. **VERIFIED: `"contnue_in_context": true` is accepted and `continue_in_context` stays `False`; `"timout": 5` leaves `timeout` at 300.** A typo'd condition silently leaves the edge on its `success` default, routing a failure branch onto the success path. `tdd/shared/factories/pipelines.py` made exactly this argument to justify refusing unknown keys **in the fixture helper** — production accepts what the fixtures refuse. | T1 |
| **N-03** | *a step id is bounded, stripped, and free of control characters* | Parametrized ids, each used as both the `steps` key and an `entry_point`: `""`, `"   "`, `"\t\n"`, `"Z"*10000`, `"a\x00b"`, `"a\nb"`, `"a\x1bb"`, `"../../etc/passwd"`, `"a/b"`, a lone combining mark, an RTL override. | 422 on every row, naming `steps` and the offending id. **VERIFIED accepted today.** `PipelineStepV2.id` is a bare `str` (`schemas/pipeline.py:114`) — the `Name` alias from `_strings.py` is applied only to the *pipeline* name (:264, :284), so the bound the QA sweep won never reached the graph. Three distinct readers make this concrete harm: the id is the **breakpoint address**; it is exported into container env as `LAZYAF_STEP_ID` (`local_executor.py:718`), where a newline is an env-injection primitive; and it is rendered in the node graph and run list, where 10 000 characters is the same oversized card `_strings.py` already documents for names. | T1 |
| **N-04** | *a step name is bounded and a NUL byte in it is refused* | `name = "N"*10000`, then `"x\x00y"`, then `""` and `"   "`. | 422 on each. **VERIFIED accepted.** `PipelineStepV2.name` is a bare `str` (:115) while every other user-supplied name in the package carries the `Name` alias. Split from N-03 because the **fix differs**: `name` wants the existing `Name` alias plus a control-character rule; `id` wants a slug pattern. | T1 |
| **N-05** | *a step timeout is bounded at both ends* | `timeout = -1, -999999999, 0, 999_999_999, 2**63, 1.5, "300", None`. | 422 for every row except a plausible in-range integer; the message names a floor **and** a ceiling. **VERIFIED: `timeout: int = 300` (:119) has no `ge`/`le`.** `-1` survives to the executor's deadline arithmetic, which is already in the past when the container starts — LazyAF creates a container purely to kill it and reports *"timed out after -1s"*. `999999999` is 31 years and **nothing else caps a step's lifetime**. Note `1.5` IS already refused by int coercion: keep that row, it is the guard against an over-broad fix. | T1 |
| **N-06** | *a step whose declared id disagrees with its dict key is refused* | `steps = {"KEY": {"id": "DECLARED", …}}`, `entry_points ["KEY"]`. | 422 naming **both** spellings. **VERIFIED accepted**: the model builds with `node.id == "DECLARED"` while the executor keys everything off the dict KEY. Since 12.8 that id also travels into the container as `LAZYAF_STEP_ID` and is the breakpoint address (`debug_state.py:89`), so the author's declared id appears in one place and is ignored in the others. Two identities behind one node is the R3 violation the `actions`/`on_success` naming decision was made to avoid. `graph_definition_errors` lists "step key/id agreement" under NOT checked (`:889`). | T1 |
| **N-07** | *two edges may not share an id, and an edge id may not collide with a step id* | (a) two edges both `id: "E"`; (b) an edge whose id equals a declared step id; (c) an edge id that is empty or whitespace-only. | 422 in every case, naming the collision. **VERIFIED all three accepted, and this is on nobody's list** — not in `graph_definition_errors`' NOT-checked docstring, not in QA4. It matters because **every structural diagnostic the product emits is keyed by edge id** (`"edge 'e1' is a self-edge"`, `"edge 'e1' from finished step 'c' selected it"`), so a duplicate makes the one error channel ambiguous exactly when it is needed — and `graph_definition_errors` falls back to `#{index}` for a missing id, producing two defect lines naming the same edge. Edge ids are also the address the editor deletes and re-conditions by, so two edges sharing an id means deleting one deletes an arbitrary one. | T1 |
| **N-08** | *duplicate entry points are refused at save time* | `entry_points: ["a","a","a"]`. | 422 naming the repeated id. The definition-time twin of S-10; both are needed, because rows already in the database predate any new refusal and the executor is what runs them. | T1 |
| **N-09** | *two edges between the same pair that both fire on one outcome are refused at save time* | Edges `e1: a->b success` and `e2: a->b success`; plus the variant `e2: a->b always`. | 422 naming both edge ids and the pair. `graph_definition_errors` **deliberately declines this** (`:886-890` lists "duplicate parallel edges" under NOT checked), so somebody has to decide it here. Must not refuse the legitimate `(success, failure)` pair — see S-25. | T1 |
| **N-10** | *a merge target with surrounding whitespace is refused at the boundary* | `actions.success = ["merge: main"]`, then `["merge:main "]`, `["merge:\tmain"]`, `["merge:main\n"]`. | 422 naming the whitespace. Executor companion: `_run_terminal_action` never calls `merge_branch` with a target that differs from its own `.strip()`. **VERIFIED: `describe_terminal_action` tests `action[len(prefix):].strip()` for TRUTHINESS (`schemas/pipeline.py:63`) and returns `None` for `"merge: main"`; `_run_terminal_action` then slices the UNSTRIPPED remainder (`pipeline_executor.py:5020`), so `" main"` reaches git verbatim.** It does not mis-merge — dulwich has no ref `refs/heads/ main`, so it fails loudly — but it fails at **run** time, after every container has burned, for a defect visible in the request body. And `source == target` no longer matches, so the deliberate no-op path is skipped too. | T1 |
| **N-11** | *a merge target that is not a valid git ref name is refused at the boundary* | `"../../evil"`, `"a/../b"`, `"main..dev"`, `"main~1"`, `"HEAD^"`, `"a:b"`, `"a?b"`, `"a[b"`, `"@"`, `".hidden"`, `"trailing.lock"`, `"a\\b"`, `"a\x00b"`, `"B"*10000`. | 422 naming the invalid ref. Executor companion: whatever survives the boundary is a **named run failure**, never an unhandled exception. The target is interpolated straight into `f"refs/heads/{target_branch}"` (`git_server.py:682,745`). This is **not** a traversal today — dulwich raises `RefFormatError` and `merge_branch`'s `except Exception` (`:756`) catches it — and that is precisely why it needs a test: nothing in LazyAF stops it, and the only thing between a user-authored action string and a ref write is a third-party library's validation. The bound belongs where every other action rule lives, in `describe_terminal_action`. | T1 |
| **N-12** | *a trigger target with surrounding whitespace is refused at the boundary* | `actions.failure = ["trigger: card-1"]`, `["trigger:card-1 "]`. | 422. Executor companion: the failure text names the target **the author wrote**. Same unstripped slice (`pipeline_executor.py:5026`). `" card-1"` misses the Card lookup and produces `"template card  card-1 not found"` — two spaces, which reads as a bug in LazyAF rather than a typo in the pipeline. The fix-card loop is the one action a user reaches for when a step fails, so a silent whitespace miss means **nobody is fixing what failed** and the run's only clue is a double space. | T1 |
| **N-13** | *a graph declaring a version other than 2 is refused* | `version = 1, 0, 3, 99, -1, "2", None, absent`. | 422 for every value except `2` and absent (absent defaults to 2). **VERIFIED: version 1 and version 99 both build and store.** `version: int = 2` (:136) is documented as *"Schema version for migration"* and is read by nothing. The one field whose job is to let a future migration recognise an old graph accepts any integer and is never checked — so the first real v3 will have no way to tell a v3 graph from a v2 one somebody typed `3` into. **Costs nothing now** (every graph in the tree is version 2) and is worthless to add later. | T1 |
| **N-14** | *a deeply nested or oversized step config is a 4xx and never a 500* | `config` nested 100/1500/3000 deep; `config` carrying a 5 MB string; `config` as a list; as `null`; with 10 000 keys. Posted as **raw bytes** so the nesting reaches the parser. | `status < 500` for every row, and `GET /health` still answers 200 afterwards. `config: dict[str, Any]` (:117) is the one field in the graph with no shape at all, and it is stored, re-parsed on every run, and handed to the executor as `step_config`. The identical fuzz table already exists for agent-files (`tdd/qa/test_api_fuzz_findings.py:671`) and found real 500s there; the graph has no equivalent. | T1 |
| **N-15** | *every graph refusal the boundary makes on POST it also makes on PATCH* | One parametrized table of ~15 malformed graphs (the P-01 corpus plus N-01..N-14's rows), each driven twice: `POST /api/repos/{id}/pipelines` and `PATCH /api/pipelines/{id}`. | Identical status **and identical detail text** from both verbs. Also: a graph created by POST and the same graph applied by PATCH produce **byte-identical** stored `steps_graph` JSON. POST serializes with `pipeline.steps_graph.model_dump_json()` (`routers/pipelines.py:287`) and PATCH with `json.dumps(update.model_dump()[...])` (`:333-335`) — two different serializations of one model, and nothing asserts they agree. This is the cheap way to keep N-01..N-14 from drifting apart as they land. | T1 |
| **N-16** | *a step id that cannot be breakpointed is refused rather than silently undebuggable* | A graph with a step whose id is `""` (accepted today, N-03). Create a debug session naming that step; then run. | Either the id is refused at 422 (preferred — then this is N-03's companion), or `resolve_step_keys` refuses the breakpoint by name. What must NOT happen is a stored breakpoint that never fires. **VERIFIED: `debug_step_key` branches on `if step_id:` (`debug_state.py:131`), so a step whose author id is `""` is keyed with `NON_STEP_KEY_PREFIX`** — the namespace reserved for the coverage-defect row and the fix-card marker. That step is outside the breakpoint vocabulary **by construction**: the checkbox renders, the key never matches, the breakpoint never fires, and the run completes normally. Plan §6 item 2 names *"a breakpoint that never fires is silent"* as the second-highest-risk silent break of the whole wave; this is the input that produces it with no migration involved. | T1 |

---

### 2D · SCALE, RECURSION AND GATING

| ID | Guarantee | Shape | Assertion | Tier | Fails today |
|---|---|---|---|---|---|
| **X-01** | *a 500-step chain of failing steps does not raise `RecursionError`* | A 500-node always-edge chain, every step failing to ROUTE, driven through `start_pipeline` against a real session with `_dispatch_step_run` stubbed to return a route error. | `start_pipeline` returns normally; the run reaches a terminal status; **no run left `status="running"` with `completed_at IS NULL`**. `_handle_graph_step_complete` and `_execute_graph_step` recurse into each other once per step (`pipeline_executor.py:2483`), so chain length **is** Python stack depth. Around 170-500 it raises inside the request handler: `POST /run` answers a bare 500 and the `PipelineRun` is abandoned in `running` forever, recoverable only by `/cancel`. The gated test that *sounds* like this (`test_graph_coverage.py:189`) exercises `graph_definition_errors`, whose cycle walk is already iterative — it proves nothing about the executor. | T1 | 🔴 |
| **X-02** | *`POST /run` answers before it has walked the whole graph* | A 150-step chain of always-edges whose every step fails during routing (`config executor: "legacy"`). Start the run, read the run row immediately, **no sleep**. | At the moment `start_pipeline` returns, `steps_completed < steps_total` (ideally `<= len(entry_points)`). `start_pipeline`'s own docstring (`:1968`) promises it *"returns as soon as the run row exists and the entry steps are dispatched"*. **This is the structural replacement for QA4-05 and it has no clock in it**, so unlike a 5.0s wall-clock bound it cannot XPASS on a quiet machine and fail minutes later on the same code. It is gateable today: `tdd/unit/services/test_graph_coverage.py:619` already owns `_synchronously_failing_dispatch`, which is exactly this harness. **Distinct from X-01**: making the walk iterative fixes X-01 and does not fix X-02. | T1 | 🔴 |
| **X-03** | *1000 parallel edges between two nodes dispatch the target once* | 1000 always-edges all running `a -> b`. Recording dispatch. | Exactly one dispatch of `b`; one `StepRun`; the run is not stamped terminal while any `b` is still running. The scale form of S-09, and the one that turns a definition typo into an outage: `steps_to_execute` gets 1000 entries and the loop at `:4696` dispatches all of them, so **1000 containers hit the docker socket at once** — which in the shipped compose files is the HOST daemon shared with every other stack. | T1 | 🔴 |
| **X-04** | *something caps how many graph steps run at once* | A 20-way fan-out of sleeping steps against a fake executor; poll `active_step_ids` and record the peak. | `peak < width`. **VERIFIED: there is no `Semaphore`, no `max_parallel`, no `max_concurrent` anywhere in `pipeline_executor.py` or `local_executor.py`.** QA4-12 already states this correctly but is `@heavy @containers` in `tdd/qa`, so it runs in no tier **twice over**. Listed here for a **gated home** — a T1 twin counting dispatches against a fake executor needs no Docker at all — so that a cap, once added, cannot regress silently. | T1 | 🔴 |
| **X-05** | *the graph execution e2e suite runs in T3* | Not a new test: remove `@pytest.mark.slow` from `tdd/e2e/test_graph_pipeline.py:182`, `:774` and `:896`, or add non-slow twins, so T3 actually selects the fan-out, fan-in, diamond and parallel-tracking specs. | T3's collected node ids include `TestGraphPipelineParallelExecution`'s four tests, `TestGraphPipelineExecutionVisualization`'s one, and `test_partial_failure_marks_correct_steps`. **+6 executed T3 tests for zero new test code.** | T3 | 🔴 |
| **X-06** | *the graph failure-mode tests assert a single expected status* | Rewrite of `TestGraphPipelineFailureModes`. | Replace `assert status_code in (201, 400, 422)` (`:876`) with the one status S-17's contract names. Unwrap `test_partial_failure_marks_correct_steps`'s `if pass_step and fail_step:` (`:941`) so a missing `StepRun` fails loudly. | T3 | 🟡 |
| **X-07** | *dispatch cost does not grow with the number of steps behind the entry point* | Four measurements in one test on one machine: `t_fail(50)`, `t_fail(150)`, `t_pass(50)`, `t_pass(150)`. Assert on **ratios only**. | `t_fail(150)/t_fail(50) < 2.0` (the contract is O(1) in graph size; a linear walk gives ~3, today's superlinear walk far more), and `t_fail(N)/t_pass(N)` does not grow between N=50 and N=150. **If `t_pass(150)` is not measurably above clock resolution, FAIL with "the control measurement is unusable" — never skip.** The succeeding chain is the load control, isolating the synchronous-failure recursion from graph size and from whatever else the machine is doing. The explicit no-skip clause matters: a skip on a fast machine is the "passes by not stopping" failure mode this catalogue is not allowed to reintroduce. | qa (or T1 if the ratio proves stable in CI) | 🔴 |
| **X-08** | *a graph larger than the declared node cap is refused, or every operation on it is bounded* | A 10 000-node graph on POST; the same on PATCH; a GET of a stored one; the YAML export. | 422 naming the cap on POST and PATCH alike. **If the decision is "no cap"**, then instead: create, read, export and run-refusal each complete within a stated budget and none of them 500s. **Measured: a 10 000-node `PipelineGraphModel` builds in 0.09s and serializes to 2.66 MB of JSON**, stored in one TEXT column, re-parsed on every GET, re-rendered by SvelteFlow, and walked by `graph_definition_errors` on every run start. Nothing bounds node or edge count anywhere. This is the one row where "refuse" may not be the right answer — the test must force the decision to be **written** rather than left to whatever the request timeout happens to be. | T1 | 🔴 |
| **X-09** | *the QA4 graph lane still collects after the array is retired* | Run `tdd/qa/test_graph_definition_qa4.py` and `tdd/qa/test_graph_execution_qa4.py` against the post-retirement backend and reconcile each xfail. | Every file imports and collects; every strict xfail is either **still XFAIL** (open) or **flipped to a positive assertion**; nothing reads a wire field that no longer exists. Two will break silently, and `tdd/qa` has no tier and no floor to notice: `test_graph_definition_qa4.py:196` reads `after["steps"]` from `GET /api/pipelines/{id}` and `PipelineRead.steps` is deleted at P3 — and the behaviour it tests is now covered by `_refuse_both_dialects`, so it should be **deleted, not repaired**. `test_graph_execution_qa4.py:175` posts a v1 `steps:` array to exercise a typo'd `on_success`; that defect has moved to `describe_terminal_action` and the test needs re-pointing. Everything else in both files was checked against current source and is **still open and still correct**: definition `:39 :55 :70 :86 :103 :123 :138 :154`; execution `:62 :106 :218 :238 :261 :297 :341`. | qa | 🔴 |

---

### 2E · Already covered by 12.8 — do not re-specify

These are in `upcoming/wave10-v1-retirement.md` §5.1 and are the 12.8 wave's to
deliver. Listed only so an implementer does not write them twice.

| Guarantee | Where 12.8 owns it |
|---|---|
| A failure edge fires its target when the source fails, and the success edge does not | §5.1 Capability |
| An always edge fires on both outcomes | §5.1 Capability |
| A success edge does not fire on failure | §5.1 Capability |
| The T2 real-container twin for the failure edge | §5.1 Capability |
| `actions.failure = ["trigger:card-x"]` on a failing step spawns the fix card, leaves the marker StepRun, and the run ends FAILED | §5.1 Capability |
| `actions.success = ["merge:main"]` reaches `merge_branch`; unresolvable branch fails the run; source == target skips | §5.1 Capability |
| A graph step's `step_index` equals its position in the graph's `steps` dict | §5.1 Capability |
| `array_to_graph` round-trips and refuses (unknown action, mid-array `stop`, colliding ids, empty list) | §5.1 Capability / Boundary |
| `POST`/`PATCH` with both `steps` and `steps_graph` ⇒ 422 | §5.1 Boundary |
| A YAML whose conversion refuses ⇒ `definition_error` set, `POST /run` refuses | §5.1 Boundary |
| Export of a linear graph parses; export of a fan-out graph refuses naming the construct | §5.1 Boundary |
| A breakpoint keyed by a real graph step id actually fires | §5.1 Debug |
| A pipeline card renders its real step count from `steps_graph` | §5.1 Frontend |
| Playwright: create a pipeline **and update it**, asserting `steps_graph.edges` and `entry_points` from `GET` | §5.1 Frontend (R8) |

**Two lane findings were already fixed by 12.8 mid-audit and are dropped from
this catalogue**, recorded here so nobody re-opens them: the editor's
`PUT` → `PATCH` save (`PipelineEditorPage.svelte:150`, fixed with a comment
explaining it 405'd every time), and the nine `test.skip`s in
`frontend/e2e/graph-pipeline.spec.ts` (now real tests, including
`refuses to connect a step to itself, naming it`).

---

## 3. The shared graph fixture builder

### 3.1 What already exists, and why this is not a competitor

12.8 lane B2 owns `tdd/shared/factories/pipelines.py`. It has landed and it is
good: `linear_graph(steps, *, ids=None)` takes the **same `list[dict]` the ten
old array helpers took**, so every converted fixture keeps its literal step list
and only the persist line moves. Its siblings are `graph_json`,
`graph_pipeline_payload` and `make_repo_and_graph_pipeline`. Its own docstring
explains, correctly, why it is not built on `array_to_graph`.

**It is linear-only, by design.** It renders one chain of `on_success`/
`on_failure` semantics. It cannot express a fan-out, a fan-in, a diamond, a
disconnected component, a duplicate edge, or a node with hand-authored
`actions` — it refuses `actions` outright, deliberately, saying *"a fixture that
wants to hand-author `actions` is authoring a graph and should write the dict
itself"*.

Roughly forty rows in this catalogue need exactly that.

**R3: add one function to that module. Do not create a second module.** Two
graph builders in two files is the drift this repo keeps writing notes about.

### 3.2 The addition

```python
# tdd/shared/factories/pipelines.py — alongside linear_graph, same module,
# same GraphFixtureError, exported from tdd.shared.factories.

def graph(
    nodes: dict[str, dict] | list[str],
    *,
    edges: Sequence[tuple[str, str] | tuple[str, str, str]] = (),
    entry: Sequence[str] | None = None,
    edge_ids: Sequence[str] | None = None,
    version: int = 2,
) -> dict[str, Any]:
    """An ARBITRARY-SHAPE v2 graph dict. The fan-out/fan-in sibling of
    `linear_graph`.

    `linear_graph` renders a chain from v1 step dicts and is the right tool
    for every fixture that used to hold a `steps` array. This is the tool for
    a test that is ABOUT the shape: a diamond, a duplicate edge, a
    disconnected component, an unsatisfiable join.

    nodes:
        `{"a": {...}, "b": {...}}` — step-id -> partial node dict. Every key
        is optional; `id` defaults to the mapping key, `name` to the key,
        `type` to "script", `config` to `{"command": f"echo {key}"}`.
        `actions` IS accepted here (unlike `linear_graph`) and is validated
        through `describe_terminal_action` so a typo'd action is a loud
        fixture error, never a silently dropped one.
        A bare `list[str]` is sugar for all-default nodes: `graph(["a","b"])`.

    edges:
        `("from", "to")` for a success edge, or `("from", "to", condition)`
        where condition is `success` | `failure` | `always`. Ids are
        auto-assigned `e1..eN` in order unless `edge_ids` overrides, matching
        `linear_graph`'s `ids=` idiom.
        DUPLICATE TUPLES ARE PERMITTED. That is not an oversight - it is how
        S-07/S-09/X-03 are authored.

    entry:
        Defaults to every node with no incoming edge, in `nodes` order, which
        is what a reader means by "the entry points" for almost every shape.
        Pass it explicitly whenever the test is ABOUT the entry points
        (S-10, N-08, P-04) - and duplicates in it are PERMITTED, same reason.

    REFUSES (GraphFixtureError): an empty `nodes`; an unknown node key; an
    edge naming a step that is not in `nodes`; an `entry` naming a step that
    is not in `nodes`; an unknown action string; a condition outside the
    EdgeCondition vocabulary; `edge_ids` of the wrong length.

    DOES NOT REFUSE, deliberately: a cycle, a self-edge, an unreachable step,
    a duplicate edge, a duplicate entry point, a key/id mismatch, an
    unsatisfiable join. Those are GRAPH-AUTHORING defects and they are the
    thing under test - a fixture that refused them would make this whole
    catalogue unwritable. The line is: refuse the TEST AUTHOR's typos, permit
    the PIPELINE AUTHOR's defects.
    """
```

Plus one persist sibling, mirroring the existing pair exactly:

```python
async def make_repo_and_shaped_pipeline(
    factory, graph_dict: dict, *, name: str = "test-pipeline",
    repo_name: str = "test-repo", default_branch: str = "main",
) -> tuple[Repo, Pipeline]: ...
```

This replaces `make_graph_pipeline` at
`tdd/unit/services/test_pipeline_local_dispatch.py:280` (which the 12.8 wave kept
alongside `make_linear_pipeline` "for the shapes an array can never describe" —
exactly this need, solved locally in one file).

### 3.3 The run-level invariant helper

S-12, S-13 and S-11 are assertions **every** graph execution test should make, not
three separate tests that happen to be listed here. Put them in one helper in
the same module and call it from the shared `start_and_wait` used by the graph
suites:

```python
def assert_run_is_coherent(run: PipelineRun) -> None:
    """Invariants that must hold on ANY terminal run, whatever its shape.

      * every StepRun is terminal and has completed_at        (S-12)
      * active_step_ids == []                                 (S-13)
      * 0 <= steps_completed <= steps_total                   (S-11)

    Called by the shared harness rather than written per-test, so a new graph
    test gets the invariants for free and a regression is caught by whichever
    test runs first rather than only by the three that name it.
    """
```

Cost of a shape after this lands: one `graph(...)` call and one `assert`. That
is what makes forty rows a mechanical implementation rather than forty hand-built
dicts.

### 3.4 Sequencing against 12.8

B2 converts `test_pipeline_local_dispatch.py` first (44 call sites are the best
measure of whether `linear_graph`'s signature is right) and T2 **last, one file
at a time**, because T2's floor margin is two and one uncollectable file fails
the tier. **`graph()` lands after B2's rollout is complete**, as a pure addition
to a stable module. It touches nothing B2 wrote.

---

## 4. What this adds, per tier

Counted from the catalogue, excluding §2E (12.8's) and excluding X-05/X-06/X-09,
which are edits to existing tests rather than new ones.

| Tier | New tests | Notes |
|---|---|---|
| **T1** | **~62** | 36 semantics + 10 pitfalls + 16 negative. Several are parametrized tables (N-03, N-05, N-11, N-15) whose *collected* count is much higher — N-15 alone is ~30 collected. Expect **+120 to +180 collected**, not +62. |
| **T2** | **2** | P-12 (fan-out shares one working tree) and one real-container conditional-join twin of S-01. T2's margin is **two** against floor 75 / measured 77 — these must land only when the tier is green, and the floor raised deliberately afterwards. |
| **T3** | **+6 executed, 0 new** | X-05 unmarks the existing `@slow` classes. X-06 and P-14 repair three tests already collected. T3 goes 22 → 28 with no new test code. |
| **vitest / playwright** | **3** | P-05, P-06, P-07 (editor authoring gestures) + P-13 (`order.ts`, a pure-function file with no test file today). |
| **qa** | **2** | X-07 (the ratio replacement for QA4-05) and X-09 (reconcile the QA4 lane). Note `tdd/qa` runs in **no tier** — putting anything there means it does not execute. Prefer T1 wherever a fake executor suffices. |

**Raise the floors deliberately.** Current: T1 floor 4432 / measured 4523; T2
floor 75 / measured 77; T3 floor 21 / measured 22
(`tdd/tier_floors.json`, measured 2026-08-30). The rule this repo already
follows: measure on a **green** run, set floor to measured minus ~2%, and never
lower one. A floor set from a red run is the fake green the file exists to
prevent.

**One structural recommendation, cheaper than any test here:** ten of these
pitfalls already have a written, strict, correct test in `tdd/qa` that executes
nowhere. **Moving `tdd/qa/test_graph_definition_qa4.py` and
`test_graph_execution_qa4.py` into a tier is a smaller change than writing them
again**, and `xfail(strict=True)` means each one flips to a hard failure the
moment its defect is fixed — which is exactly the ratchet behaviour wanted. Do
this in Stage 0.

---

## 5. Defects this surfaced, ranked

Live bugs, not missing tests. `🔧` marks the ones where **a product fix must land
before the test can pass** — write each fix and its test in the same commit.

### D1 🔧 — The fan-in gate ignores edge conditions. **Highest severity.**

`backend/app/services/pipeline_executor.py:4757-4785`.

The docstring promises *"ALL its incoming edges come from completed steps AND the
edge conditions match (success edge requires success, etc)"*. **The body never
reads `edge.condition`.** It tests membership in `completed_ids` only — and
`completed_ids` contains FAILED steps (populated at `:4600`).

Three consequences, all verified by execution:

1. **A join after a conditional branch can never go green.** For
   `a -success-> ok` / `a -failure-> recover` / `{ok, recover} -always-> report`
   with a passing `a`:
   `unreached_graph_steps` returns
   `{'report': "edge 'e3' from finished step 'ok' selected it, but it never ran (still waiting on upstream ['recover'] which never completed)"}`
   so `_verify_graph_coverage` **fails the run and blames the wrong node**.
   Branch → recover → rejoin to notify/cleanup is the single most natural use of
   a failure edge, and retiring v1 makes edges the **only** way to express
   failure routing. This lands on every user who follows the new model.
2. **The diamond is order-dependent.** `a -> {b, c} -> d`, `b` fails:
   `_all_upstream_satisfied(d, {"a","b","c"})` returns **True**, so if `c`
   completes last, `d` **runs on a branch whose upstream failed**. If `b`
   completes last, `d` never dispatches and gets a synthetic "never ran" row.
   One graph, one set of step outcomes, two executions and two error messages,
   decided by container scheduling. In CI terms: `a -> {test, lint} -> deploy`
   gives you a deploy that sometimes happens after a failing test.
3. **A failure edge from a passing step still counts toward a join.**
   `a -success-> b`, `c -failure-> b`, entry `[a, c]`, both pass: `c`-then-`a`
   dispatches `b` **on an edge condition that did not fire**; `a`-then-`c` leaves
   `b` unrun and fails the run for coverage.

**The fix**, and it closes all three: give `_all_upstream_satisfied` the
`outcomes` map and treat an incoming edge whose condition is **already ruled out
by its source's real outcome** as not blocking. `unreached_graph_steps` already
computes exactly this predicate correctly at `:1035-1044` — **share it (R3)**,
do not write it twice. S-05 is where that gets pinned; S-01/S-02/S-03/S-04 are
the orchestration-level guards that keep the fix honest.

### D2 🔧 — Duplicate dispatch is now duplicate **effect**

`pipeline_executor.py:4660` / `:4679` / `:4696`, with `:4670` and `:2428`.

`steps_to_execute` is a plain list appended once per matching edge and never
de-duplicated; the already-active guard reads the tracking sets computed at the
top of the call, before this batch is reserved; `_reserve_active_steps` (`:4788`)
de-duplicates the **active column** but the dispatch loop iterates the raw list;
and `_execute_graph_step` has **no already-dispatched guard of its own** — it
mints a fresh `StepRun` on every call.

**Three forms, and the third has not been written down anywhere:**

1. Duplicate `a->b` edges (QA4-07, ungated).
2. Duplicate `entry_points` — `start_pipeline` reserves the deduped batch at
   `:2074` and then iterates the **raw** list at `:2080` (QA4-06, ungated).
3. **`a -success-> b` PLUS `a -always-> b`.** `get_downstream_edges` (`:748`)
   matches an always edge for **every** condition, so on success both edges
   match. **Verified: `len(get_downstream_edges(g, "a", "success")) == 2`.**
   Unlike a literal duplicate this is two *legally distinct* edges, and it is
   what the editor's own "second edge from the same source defaults to failure"
   logic invites you to build. Neither `graph_definition_errors` (which declines
   at `:889`) nor `validate_graph_integrity` rejects it: a 201 at the API and a
   double execution at run time.

**Since 12.8 P1 this is no longer a wasted container.** Actions fire from
`_handle_graph_step_complete` keyed on the completing node, so a doubly-dispatched
node is **a double git merge, or two fix cards**. A merge is not idempotent and
not undoable from inside the product. Every existing action test completes each
node exactly once, so the effect-doubling is uncovered in every tier including
`tdd/qa`. S-08 is the test; S-07/S-09/S-10 are the row-count twins; X-03 is the
scale form.

### D3 🔧 — `validate_graph_integrity` never calls `graph_definition_errors`

`backend/app/schemas/pipeline.py:141-160` vs `pipeline_executor.py:875-967`.

`graph_definition_errors`' own docstring says the schema is where these belong,
and `array_to_graph` **does** call it (`schemas/pipeline.py:817-819`). The
hand-authored graph boundary does not.

**Verified accepted at the schema, i.e. 201 at the API:** a 2-cycle, a
self-edge, an unreachable step, duplicate parallel edges, duplicate
`entry_points`, duplicate edge ids, an edge id equal to a step id, a key/id
mismatch, `timeout=-1`, `timeout=0`, a 31-year timeout, `version=99`, an empty
step id and name, `merge: main` with a leading space, and an entry point that is
also an edge target. **Only empty `entry_points` and an empty `steps` dict are
refused.**

So a YAML author gets `definition_error` and a refused run; the editor author
gets 201 and a red run. Same defect, two opposite answers, decided by which
surface they opened. P-01 is the test that cannot be satisfied without wiring
the shared check.

### D4 🔧 — An unsatisfiable join is accepted and fails every run

`_all_upstream_satisfied` is an unconditional AND over every incoming edge
regardless of condition, so `deploy -success-> cleanup` + `rollback -failure->
cleanup` ("clean up either way") demands **both** `deploy` and `rollback`
complete. On the happy path `rollback` never runs, `cleanup` never runs, and
`unreached_graph_steps` then blames `cleanup` on `deploy`'s success edge and
fails the run (**verified**). The graph passes `validate_graph_integrity`,
renders fine in the editor, and **fails 100% of its runs** with a red step the
user never wrote. There is no way in the format to express an OR-join and nothing
refuses the attempt. Fixed by D1; refused at the boundary by D3. S-06.

### D5 🔧 — `DEFAULT_AGENT_STEP_TIMEOUT` is unreachable

`pipeline_executor.py:2777` — `step.get("timeout") or default_timeout_for(step_type)`.

`PipelineStepV2.timeout` defaults to 300 (`schemas/pipeline.py:119`) and
`PipelineStepYaml.timeout` defaults to 300 (`lazyaf_yaml.py:72`), so the left
operand is **always truthy** and the fallback never runs. `default_timeout_for`
(`:292`) has exactly one caller and is dead for every authored pipeline. Its own
neighbouring comment says *"300s is a rounding error for an agent"*. **Agent steps
are killed at five minutes; the product's stated intent is thirty.**
`default_timeout_for` has **zero tests in the entire tree**.

The mirror: `timeout: 0` is accepted and resolves to the **type default** (1800
for an agent) because `0` is falsy — it is the only input that reaches
`default_timeout_for` at all. One character, two user-visible failures. P-08,
P-09, N-05.

### D6 🔧 — Whitespace in an action target reaches git and the card lookup verbatim

`schemas/pipeline.py:63` vs `pipeline_executor.py:5020` / `:5026`.

`describe_terminal_action` tests `action[len(prefix):].strip()` for
**truthiness**; `_run_terminal_action` then slices the **unstripped** remainder.
**Verified: `describe_terminal_action("merge: main")` returns `None`.** So
`" main"` reaches `merge_branch` verbatim. It does not mis-merge — dulwich has no
`refs/heads/ main` and the run fails loudly — but it fails at **run** time, after
every container has burned, for a defect that was visible in the request body.
And `source == target` no longer matches, so the deliberate no-op path is skipped
too. On the trigger side, `" card-1"` produces `"template card  card-1 not
found"` — two spaces, which reads as a LazyAF bug rather than a user typo, on the
one action a user reaches for when a step fails. N-10, N-12.

### D7 🔧 — Silent drops: `StepActions`, `PipelineStepV2` and `PipelineEdge` accept unknown keys

None declares `extra="forbid"`. **Verified:** `actions={"sucess": ["merge:main"]}`
dumps as `{"success": [], "failure": [], "always": []}` — the merge is gone, and
the user gets a 201, a correctly-rendered pipeline, a green run and no merge.
`"contnue_in_context": true` is accepted and the flag stays `False`. A typo'd
`"timout"` becomes the 300s default; a typo'd `"conditon"` leaves the edge on its
`success` default, **routing a failure branch onto the success path**.

`"on_success"` is the sharpest row twice over: it is the v1 spelling, it is the
key the YAML export writes for graph steps, and §1.3 explicitly chose the
`actions.` namespace to avoid that collision — a silent drop re-opens it, so **a
user re-importing their own export loses every action.**

`tdd/shared/factories/pipelines.py` made exactly this argument to justify
refusing unknown keys **in the fixture helper**, and `tdd/unit/shared/
test_graph_fixture.py:158` pins it there. **Production accepts what the fixtures
refuse.** N-01, N-02.

### D8 🔧 — `current_step` will have no writer at all

`pipeline_executor.py:2561` is the **only** assignment, and it is inside
`_execute_step` — the v1 dispatcher being deleted. The graph path never writes
it, so it is permanently 0. It is on the wire (`schemas/pipeline.py:491`) and it
is the index `_complete_pipeline` hands to `machine.mark_step_failed` (`:1671`),
so **every graph run failure is attributed to step 0**. After the retirement this
is a permanently-zero column driving a state machine and a UI field. Decide:
track the last dispatched node, or remove it from the graph wire contract. S-16.

### D9 🔧 — A failed terminal action abandons live siblings

`pipeline_executor.py:5051` — `_fail_run_on_terminal_action` calls
`_complete_pipeline` **unconditionally**, with no check for still-active
siblings. `_complete_pipeline` runs `_cleanup_workspace`, removing the shared
named volume out from under a live sibling container; that sibling's completion
is then swallowed by the terminal guard at `_finish_local_step_locked:4313`,
leaving its `StepRun` `status="running"` with `completed_at IS NULL` on a
finished run. The user sees a failed run with a step **spinning forever** and a
container writing into a deleted volume. S-14, S-12.

### D10 🔧 — `cancel_run` never clears `active_step_ids`

`pipeline_executor.py:5733`. It drives the step rows and the state machine
terminal and never touches `active_step_ids` or `completed_step_ids`.
`_fail_run_on_terminal_action` and `_verify_graph_coverage` both reach
`_complete_pipeline` without clearing them either. The column is on the wire
(`schemas/pipeline.py:411`) and it is what the graph editor highlights as
running, so **a cancelled fan-out renders as permanently in-flight.** S-13.

### D11 — `steps_completed` can exceed `steps_total`

`pipeline_executor.py:4373` and `:4509` increment per successful **completion**,
not per node, while `steps_total` is `count_total_steps` (the node count). A
doubly-dispatched node reports 3 of 2 steps done on the wire and in the progress
bar. Closes with D2; S-11 is the cheapest possible detector of D2.

### D12 — Editor authoring gestures produce the D2 shapes in three clicks

`frontend/src/lib/components/graph/PipelineGraphEditor.svelte`:

- `deriveEntryPoints` (`:370-374`) is a bare `.filter().map()` with **no
  de-duplication**, and `createEdge` (`:397-417`) — where the handle drag lands
  via `onConnect` (`:421`) — has **no `(from_step, to_step)` guard**, while
  `addStartEdge` (`:161`) **does** filter an existing `Start->target` edge. Two
  gestures in one editor, opposite behaviour. Two drags `Start->a` give
  `entry_points == ["a","a"]`.
- `defaultConditionFor` (`:386-394`) picks the condition from *"does this SOURCE
  already have a success edge"*, not from the **pair**. A repeat drag on an
  already-connected pair silently becomes a **failure** edge — flipping the
  author's conditional into unconditional — and a third drag emits a second
  identical failure edge, i.e. D2 form 1 reached purely by clicking.

P-05, P-06.

### D13 — A disconnected multi-node component is under-reported

**Verified**: for component `{x -> y}` with no entry point,
`graph_definition_errors` returns `["step 'x' is unreachable: …"]` and
`unreached_graph_steps` returns `{'x': …}` — **neither names `y`**. `y` is in
`reached_by_edge` because `x` points at it (`:965`), and `y`'s source never
appears in `outcomes`, so it falls through the "branch legitimately not taken"
return at `:1039`. The operator gets a red run naming one of two dead steps and
no explanation for the other. The existing orphan coverage
(`test_graph_coverage.py:175`, `:487`) is a **single isolated node** — the one
shape that does not exercise this. S-15, with S-28 as the negative control.

### D14 — Unbounded and unvalidated fields on every graph node

`PipelineStepV2.id` and `.name` are bare `str` (`schemas/pipeline.py:114-115`) —
the `Name` alias from `_strings.py` is applied only to the **pipeline** name at
`:264`/`:284`. `timeout` is a bare `int` with no `ge`/`le` (`:119`). `config` is
`dict[str, Any]` with no shape (`:117`). `version` is unchecked (`:136`). Edge
ids are unconstrained and may collide with each other or with step ids.

Accepted and stored today: a 10 000-character id, a 10 000-character name,
`"a\x00b"`, `"a\nb"`, `""`, `"   "`, `"../../etc/passwd"`. The id is exported
into container env as `LAZYAF_STEP_ID` (`local_executor.py:718`), is the
breakpoint address, and is rendered in the node graph and run list. **An empty
id falls out of the breakpoint vocabulary entirely** — `debug_step_key` branches
on `if step_id:` (`debug_state.py:131`), so it is keyed with
`NON_STEP_KEY_PREFIX`, the namespace reserved for the coverage-defect row and the
fix-card marker, and its breakpoint can never fire. N-03, N-04, N-05, N-07,
N-13, N-14, N-16.

### D15 — Recursion depth is graph depth

`pipeline_executor.py:2483` — `_execute_graph_step` re-enters
`_handle_graph_step_complete` in the caller's stack on the synchronous-failure
path, and they recurse into each other once per step. Two separate consequences,
**and fixing one does not fix the other**: around 170-500 steps it raises
`RecursionError` **inside the request handler**, so `POST /run` answers a bare
500 and the `PipelineRun` is abandoned in `running` with `completed_at NULL`
forever (X-01); and independently, `POST /run` walks the whole graph before
answering, contradicting `start_pipeline`'s own docstring at `:1968` (X-02).

### D16 — There is no fan-out cap

**Verified: no `Semaphore`, no `max_parallel`, no `max_concurrent` anywhere in
`pipeline_executor.py` or `local_executor.py`.** A fan-out of N puts N containers
on the docker socket simultaneously — the HOST daemon, in the shipped compose
files, shared with every other stack. Combined with D2 form 1 at scale (X-03),
one duplicated edge authored 1000 times is an outage. QA4-12 states this
correctly and is `@heavy @containers` in `tdd/qa`, so it runs in **no tier
twice over**. X-04.

### D17 — Pitfall by design, and unsurfaced: a merge lands on a run that then reports FAILED

Not a bug — a consequence of the P1 model that nobody has written down. Actions
fire at **node completion** (`:4610-4650`) while the verdict is computed later by
`_check_all_steps_passed`. So on `root -> {passer, failer}` where `passer`
carries `actions.success = ["merge:main"]`, **the merge lands and the run then
reports FAILED.** `tdd/unit/services/test_pipeline_executor.py:910` pins that the
merge does not *rescue* the run; nobody has asked the mirror question — that the
merge **happened anyway**, and that the operator can find out. A red run whose
code is already on main, with nothing saying so, is the same class of lie as a
false green. **Recommendation: leave the behaviour, surface it** — a `StepRun`
record on the acting node naming the landed merge — and pin both halves.

### D18 — Dead code carrying fake coverage that encodes the wrong semantics

`PipelineGraphModel.get_successors` / `get_predecessors` / `get_all_successors`
(`schemas/pipeline.py:162-178`) have **zero production callers** (grep across
`backend/`, `frontend/src/`, `tdd/`) yet carry 6 tests at
`tdd/unit/schemas/test_graph_pipeline_schemas.py:302-350`. Worse:
`get_successors` matches condition by **exact equality**, while the live
`get_downstream_edges` (`pipeline_executor.py:748`) treats `always` as matching
every condition — **the tested helper and the executed helper disagree about the
always edge.** Delete the methods and their tests, or make them call the live
helper. Do not leave a second, wrong definition of edge matching in the schema
module (R3).

---

## 6. Build order

### Dependency: 12.8 must land first

Non-negotiable. This catalogue's tests land in files 12.8 owns, against a
`_handle_graph_step_complete` whose v1 sibling is being deleted, and roughly
forty of them need `graph()` — which extends a module 12.8 lane B2 is still
rolling out. **Wait for the §5.1 acceptance gate to pass**, including the
dogfood ratchet.

### Stage 0 — free coverage, no new test code *(do first, independently)*

1. **X-05** — unmark `@pytest.mark.slow` at `tdd/e2e/test_graph_pipeline.py:182`,
   `:774`, `:896`. **+6 executed T3 tests.** Verify T3 stays green, then raise the
   T3 floor.
2. **X-06 / P-14** — repair the three tests that cannot fail: the
   `in (201, 400, 422)` at `:876`, the `if step_runs:` at `:352`, the
   `if pass_step and fail_step:` at `:941`.
3. **Move `tdd/qa/test_graph_definition_qa4.py` and
   `test_graph_execution_qa4.py` into T1** (they are pure-Python; the
   container-marked ones stay behind a marker). Ten catalogue pitfalls already
   have a correct strict-xfail test there. **This is smaller than writing them
   again**, and strict xfail means each flips to a hard failure the moment its
   defect is fixed. Do X-09's reconciliation as part of the move.

Stage 0 alone converts a large share of §1.2 and §1.5's "COVERED-BUT-UNGATED"
into gated coverage.

### Stage 1 — the fixture *(blocks almost everything else)*

4. `graph()` + `make_repo_and_shaped_pipeline` + `assert_run_is_coherent` in
   `tdd/shared/factories/pipelines.py` (§3). Land after B2's rollout completes.
   Extend `tdd/unit/shared/test_graph_fixture.py` with `graph()`'s refusals —
   the same file that already pins `linear_graph`'s.

### Stage 2 — the fan-in gate *(D1 + D4; the highest-severity fix)*

5. **S-05 first** — the direct condition × outcome table on
   `_all_upstream_satisfied`. It is at the altitude the bug lives, takes plain
   dicts and a set, and is what makes the fix safe rather than a guess.
6. Ship the fix: `outcomes` into the signature, an edge whose condition is ruled
   out does not block, **sharing the predicate `unreached_graph_steps` already
   computes at `:1035-1044`** rather than writing it twice (R3).
7. **S-01, S-02, S-03, S-04, S-06** — the orchestration-level guards.
8. **S-23, S-24, S-31** — the all-passing fan-in and diamond, which must stay
   green through the fix. Write these *before* the fix if you want the safety
   net first; they pass today.

### Stage 3 — duplicate dispatch *(D2, D11)*

9. **S-07, S-08, S-09, S-10, S-11** — all forms plus the effect-doubling. Note
   the three forms have **three different fixes**; S-25 and S-26 are the controls
   that stop an over-broad de-dup.
10. **N-08, N-09** — the definition-time twins.
11. **X-03** — the scale form.

### Stage 4 — run-level invariants *(D9, D10)*

12. **S-12, S-13, S-14** via `assert_run_is_coherent`, wired into the shared
    harness so every later graph test inherits them.

### Stage 5 — the boundary *(D3, D7, D14, D6)*

13. **P-01** — wire `graph_definition_errors` into `validate_graph_integrity`.
    This one change closes S-17, S-18, P-02, P-03, N-08 and N-09 at once.
14. **N-01, N-02** — `extra="forbid"` on `StepActions`, `PipelineStepV2`,
    `PipelineEdge`. Expect fallout: check the editor and `array_to_graph` for
    keys they currently pass and the models currently drop.
15. **N-03, N-04, N-05, N-07, N-13** — bounds and patterns. **N-16 rides on
    N-03**: refusing an empty step id is what closes the silent-breakpoint hole.
16. **N-10, N-11, N-12** — strip and validate in `describe_terminal_action`, so
    the boundary and `_run_terminal_action` cannot disagree.
17. **N-14, N-15** — the fuzz table and the POST/PATCH parity table. Land N-15
    **last in this stage**, since it is the table that keeps all of the above
    from drifting apart.

### Stage 6 — semantics pins *(no product change)*

18. **S-19 through S-22, S-25 through S-30, S-32 through S-36.** These pass
    today and are pure regression protection. Cheap once `graph()` exists.

### Stage 7 — pitfalls, frontend and scale

19. **P-04, P-10, P-11, P-08, P-09** — each needs a product decision as much as a
    test. P-11 in particular: **honour `continue_in_context` on the graph path,
    or refuse it and remove it from the three places it is advertised.**
20. **P-05, P-06, P-07, P-13** — the editor and `order.ts`. P-13 creates the
    first test file under `frontend/src/lib/components/graph/`.
21. **X-01, X-02, X-04, X-07, X-08** — recursion, dispatch latency, the fan-out
    cap, and the node-count decision. X-08 exists to force a written decision;
    do not let it default to whatever the request timeout happens to be.
22. **T2 last, one file at a time** — P-12 and the real-container conditional-join
    twin of S-01. T2's margin is **two**; a single uncollectable file fails the
    tier.

### Stage 8 — close out

23. **D18** — delete `get_successors`/`get_predecessors`/`get_all_successors`
    and their 6 tests, or make them call `get_downstream_edges`. Do not leave a
    second, wrong definition of edge matching in the schema module.
24. **D8** — decide `current_step`: track it on the graph path, or take it off
    the wire. **S-16 cannot be written until that decision is made.**
25. Re-measure every tier on a green run; raise the floors to measured minus ~2%
    with a note saying what grew, in the register `tdd/tier_floors.json` already
    uses.

---

## 7. Two things an implementer must not do

**Do not write a test that passes by not crashing.** Three already exist in this
area (§1.3) and they are the reason the graph looks covered. Concretely: no
`assert status in (201, 400, 422)`; no assertion guarded by `if rows:`; no
`skip` when a timing control is unusable — **fail** with "the control measurement
is unusable" (X-07 says this explicitly for that reason).

**Do not put a new test in `tdd/qa`.** It runs in no tier and has no floor. If a
test needs Docker it goes in T2; if a fake executor suffices — and for almost
everything here one does — it goes in T1. The single largest finding in this
audit is that the graph's best negative coverage was already written, correctly,
in a directory nothing executes.
