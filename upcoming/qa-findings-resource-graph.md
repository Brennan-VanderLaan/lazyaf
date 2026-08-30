# QA-4 findings — resource abuse & pipeline-graph pathology

**Lane:** QA-4 (graph definition layer, resource limits, `.lazyaf/pipelines/*.yaml`)
**Target:** isolated QA stack `http://localhost:8790` (compose project `lazyaf-qa`)
**Date:** 2026-08-30
**Regression tests** (all under `C:\projects\lazyaf\tdd\qa\`):

| file | findings (xfail) | guards (pass) |
|---|---|---|
| `test_graph_definition_qa4.py` | 11 | 10 |
| `test_graph_execution_qa4.py` | 9 | 6 (+1 `heavy`, deselected by default) |
| `test_yaml_pipelines_qa4.py` | 12 | 3 |
| `test_pipeline_export_qa4.py` | 4 | 1 |
| `test_step_resource_limits_qa4.py` | 2 | 2 |
| **total** | **38** | **22** |

Helpers live in `tdd/qa/qa4_support.py` (pure HTTP, no backend imports) and
`tdd/qa/conftest.py` (fixtures). Run them with:

```
python -m pytest -c tdd/qa/pytest.ini \
    tdd/qa/test_graph_definition_qa4.py tdd/qa/test_graph_execution_qa4.py \
    tdd/qa/test_yaml_pipelines_qa4.py tdd/qa/test_pipeline_export_qa4.py \
    tdd/qa/test_step_resource_limits_qa4.py -m "not heavy"
```

Every test that encodes a finding asserts the CORRECT behaviour and carries
`@pytest.mark.xfail(strict=True, ...)`, so it xfails today and turns into a
loud strict-XPASS failure the moment the bug is fixed. The `heavy` fan-out test
is excluded by default because it deliberately loads the docker daemon.

> **Note on the shared sandbox:** other QA lanes were calling
> `POST /api/test/reset` throughout this session. Several reproductions below
> were re-run two or three times to get a clean sample; every one listed here
> was observed at least twice. The lane also added `tdd/qa/__init__.py` and
> `tdd/qa/pytest.ini` is mine — if that collides with another lane's config,
> the marker list is the only thing to merge.

---

## Executive summary — ranked

| # | Severity | One line |
|---|----------|----------|
| QA4-01 | BLOCKER | A long step chain recurses once per step and blows the Python stack: `POST /run` answers a bare **500** and the run is abandoned in `status="running"` forever |
| QA4-02 | BLOCKER | A one-character typo in `on_success` silently truncates a pipeline and reports it **PASSED** (1 of 3 steps run) |
| QA4-03 | BLOCKER | A **cycle** in `steps_graph` is accepted; the run stops after the first step and reports **PASSED 1/3** |
| QA4-04 | MAJOR | An unreachable step is counted in `steps_total`, never executed, and the run still reports **PASSED 1/2** |
| QA4-05 | MAJOR | `POST /api/pipelines/{id}/run` blocks for **27–43 s** walking a large graph before answering |
| QA4-06 | MAJOR | Duplicate `entry_points` / duplicate parallel edges dispatch the same step **N times** — N StepRuns, N containers, all at the same `step_index` |
| QA4-07 | MAJOR | The run is stamped **passed** while a duplicate StepRun is still `running` — its container keeps going against an already-cleaned workspace |
| QA4-08 | MAJOR | `POST /api/repos/{id}/lazyaf/pipelines/{name}/run` skips the "pipeline has no steps" gate: a step-less YAML file reports **PASSED 0/0** |
| QA4-09 | MAJOR | A malformed `.lazyaf/pipelines/*.yaml` **silently vanishes** from the listing; the same file 500s on get-one with the raw Python exception in `detail` |
| QA4-10 | MAJOR | LazyAF's own `export/yaml` produces a document LazyAF **cannot import**; and a 393-byte YAML alias amplifier in a step config produces a **1.9 MB** response/DB row |
| QA4-11 | MAJOR | The YAML path does not validate step `type` (`type: banana` accepted); the graph API 422s the identical value |
| QA4-12 | MAJOR | No concurrency cap anywhere: a 20-way fan-out put **all 20** steps in flight simultaneously on the shared docker socket |
| QA4-21 | MAJOR | Step containers run with **no memory limit and no CPU limit** — one runaway AI-authored `script` step takes the host, and the host also runs the backend |
| QA4-13 | MAJOR | `timeout` has no bounds — `-1` spawns a container purely to kill it and reports "timed out after **-1s**"; `999999999` (≈31 years) is accepted; `0` is silently coerced to 300 |
| QA4-14 | MINOR | `PATCH` leaves `steps` and `steps_graph` both populated and disagreeing |
| QA4-15 | MINOR | `PipelineStepV2.id` is ignored; a step whose declared id disagrees with its dict key is silently renamed |
| QA4-16 | MINOR | A zero-byte pipeline YAML answers `404 Pipeline not found` although the file exists |
| QA4-17 | MINOR | `steps_completed` counts only successes, so a run shows `0/2` with two entries in `completed_step_ids` |
| QA4-18 | MINOR | `GET /api/pipeline-runs` returns every step's full log with no truncation (`limit=5` produced a **312 KB** body) |
| QA4-19 | POLISH | An empty pipeline `name` is accepted |
| QA4-20 | POLISH | Router internals leak into `StepRun.error` and into the exported YAML (`description: null`) |

---

## BLOCKER

### QA4-01 — Long chain → RecursionError → 500 → run wedged in `running` forever

**Reproduction**

```bash
REPO=$(curl -s -XPOST localhost:8790/api/repos/ingest -H 'content-type: application/json' \
       -d '{"name":"qa4","default_branch":"main"}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

python - <<'PY'
import json,urllib.request
REPO="<paste>"
N=500
steps={f"s{i}":{"id":f"s{i}","name":f"s{i}","type":"script",
                "config":{"command":"echo x","executor":"legacy"},"timeout":300} for i in range(N)}
edges=[{"id":f"e{i}","from_step":f"s{i}","to_step":f"s{i+1}","condition":"always"} for i in range(N-1)]
body={"name":"qa4-chain-500","steps_graph":{"steps":steps,"edges":edges,"entry_points":["s0"],"version":2}}
r=urllib.request.Request(f"http://localhost:8790/api/repos/{REPO}/pipelines",
                         json.dumps(body).encode(),{"Content-Type":"application/json"})
pid=json.load(urllib.request.urlopen(r))["id"]
print("pipeline", pid)
PY

curl -s -XPOST localhost:8790/api/pipelines/<pid>/run -H 'content-type: application/json' -d '{}'
curl -s localhost:8790/api/pipelines/<pid>/runs | head -c 400
```

**What happened**

```
POST /run -> 500 in 20.2s   body="Internal Server Error"
run 80d2ec61 status=running 0/500 step_runs=465 active=0 done=465 completed_at=None
```

Backend log:

```
RecursionError: maximum recursion depth exceeded
```

The threshold is **non-deterministic** — 150 and 160 steps completed, 170
completed after 27 s, 180 crashed, 200 crashed on one attempt and completed on
another. It depends on how deep the ASGI stack already is when the request
arrives.

**What SHOULD happen** — the traversal must be iterative (or must hand each
newly-ready step to `asyncio.create_task`) so chain length has no relation to
Python stack depth. Whatever the failure, `POST /run` must not answer a bare
500, and a run must never be left `running` with zero active steps and no
`completed_at`.

**Root cause** — mutual recursion between
`pipeline_executor.py:3415` (`_handle_graph_step_complete` → `for step_id in
steps_to_execute: await self._execute_graph_step(...)`) and
`pipeline_executor.py:1587` (`_execute_graph_step` → on `route_error`, `await
self._handle_graph_step_complete(...)`). Every synchronously-failing step adds
a full frame set to the caller's stack. Reached by the entirely realistic
`executor: legacy` stale-config mistake, whose own error text says the value
was removed in 12.6.

Cancelling the wedged run (`POST /api/pipeline-runs/{id}/cancel`) does rescue
it, but nothing does that automatically and nothing tells the user to.

**Test:** `test_graph_execution_qa4.py::test_long_chain_does_not_blow_the_python_stack`

---

### QA4-02 — A typo in `on_success` produces a green, truncated pipeline

**Reproduction** — create a 3-step legacy pipeline whose first step says
`"on_success": "nextt"` and run it.

**What happened**

```
status=passed steps_completed=1 steps_total=3
  one   passed
  (two and three never created)
```

Green tick. No warning surfaced anywhere in the API response.

**What SHOULD happen** — `on_success` / `on_failure` should be a closed
vocabulary rejected at 422 (the way `trigger_type` already is, `pipeline.py:299`),
or at minimum an unknown action must fail the run rather than pass it.

**Root cause** — `PipelineStepConfig.on_success` is a bare `str`
(`backend/app/schemas/pipeline.py:126`) and `PipelineStepYaml.on_success` is a
bare `str` (`backend/app/schemas/lazyaf_yaml.py:55`). At runtime
`pipeline_executor.py:3544`:

```python
else:
    logger.warning(f"Unknown action '{action}', treating as 'stop'")
    await self._complete_pipeline(db, pipeline_run, success=step_success)
```

`success=step_success` — the step passed, so the *run* passes. A backend WARNING
is the only trace.

For a CI product a false green is the worst possible defect, which is why this
is ranked above the cycle.

**Test:** `test_graph_execution_qa4.py::test_typo_in_on_success_does_not_produce_a_green_truncated_run`

---

### QA4-03 — A cycle is accepted and produces a green run that executed one third of itself

**Reproduction** — graph `a --success--> b --success--> c --success--> b`, entry `["a"]`.

```
POST /api/repos/{repo}/pipelines   -> 201 (accepted)
POST /api/pipelines/{id}/run       -> 200
final: status=passed steps_completed=1 steps_total=3 completed_step_ids=['a']
```

The executor does **not** hang or deadlock (that was the worry) — it is worse
for a demo: it finishes fast and green.

**What SHOULD happen** — `PipelineGraphModel.validate_graph_integrity`
(`backend/app/schemas/pipeline.py:56`) should reject a graph containing a cycle,
and independently the executor must never stamp a run `passed` when
`completed_step_ids` does not cover the graph.

**Root cause** — two independent gaps.

1. `validate_graph_integrity` checks edge endpoints and entry points only; there
   is no acyclicity check.
2. `_all_upstream_satisfied` (`pipeline_executor.py:3446`) requires **all**
   incoming edges' sources to be complete. `b`'s upstream is `[a, c]` and `c`
   can never precede `b`, so `b` is never ready. Control then falls to
   `pipeline_executor.py:3440`:

   ```python
   elif not steps_to_execute:
       # No more steps can run (failed branch or dead end)
       all_passed = await self._check_all_steps_passed(db, pipeline_run)
       await self._complete_pipeline(db, pipeline_run, success=all_passed)
   ```

   `_check_all_steps_passed` only inspects StepRuns that were *created*, so
   two never-created steps do not count against the verdict.

**Test:** `test_graph_execution_qa4.py::test_cycle_reports_pass_having_run_one_step`
and `test_graph_definition_qa4.py::test_cycle_is_rejected_at_definition_time`

Also accepted at definition time, same root cause: a **self-edge** `a -> a`
(that one does terminate correctly, because `completed_ids` blocks re-entry).

---

## MAJOR

### QA4-04 — Unreachable step: counted, never run, run still green

Graph with steps `{a, orphan}`, no edges, `entry_points: ["a"]` is accepted and
runs to `status=passed steps_completed=1 steps_total=2`. Same `elif not
steps_to_execute` branch as QA4-03. A user who deletes an edge in the graph
editor gets a permanently green pipeline that quietly stopped running half its
steps.

**Test:** `test_unreachable_step_does_not_produce_a_green_run`, and
`test_graph_definition_qa4.py::test_unreachable_step_is_rejected`

### QA4-05 — `POST /run` blocks for tens of seconds on a large graph

`start_pipeline`'s docstring (`pipeline_executor.py:1134`) says it "returns as
soon as the run row exists and the entry steps are dispatched". On the
synchronous-failure path it walks the entire graph inside the request handler.
Measured on the QA stack:

| chain length | `POST /run` latency | outcome |
|---|---|---|
| 100 | 6.3 s | 200 |
| 150 | 9.1 s | 200 |
| 170 | 27.2 s | 200 |
| 180 | 43.5 s | 500 (QA4-01) |
| 500 | 18.8–20.2 s | 500 (QA4-01) |

Any reverse proxy or browser in front of this times out and the user sees a
failed request for a run that is actually proceeding.

**Test:** `test_run_endpoint_returns_promptly_for_a_large_graph`

### QA4-06 — The same step is dispatched more than once

Two independent shapes, both accepted at 201:

**(a) duplicate entry points.** `entry_points: ["a","a","a"]` produced **three**
StepRuns, all at `step_index 0`, each with its own `execution_key` and therefore
its own container. `start_pipeline` (`pipeline_executor.py:1200`) loops the raw
list; the `if step_id not in active_ids` guard at `:1572` prevents a duplicate
*id* in `active_step_ids` but does not prevent the dispatch.

**(b) two parallel edges matching the same condition.** Edges
`a --success--> b` and `a --always--> b` both match on success, so `b` runs
twice. `steps_to_execute` is a plain list built at `pipeline_executor.py:3389`,
appended at `:3408` with no de-duplication, and the "already active?" guard on
`:3402` reads a snapshot captured before any dispatch happened. Observed with
real containers:

```
status=passed steps_completed=2 steps_total=2
  [0] a passed
  [1] b passed
  [1] b passed      <-- second container for the same step
```

Duplicate edges are a natural artefact of a graph editor (drag the same
connection twice), and `success` + `always` on the same pair is a perfectly
reasonable thing for a user to draw.

**(c) same-name YAML files.** `upsert_materialized_pipeline` keys the platform
row on the YAML `name:` (`trigger_service.py:82`), so `dupname-a.yaml` and
`dupname-b.yaml` both materialize into **one** pipeline id
(`f9a03ddc-…` in both responses) and each run silently overwrites the other's
step definition.

**Tests:** `test_duplicate_entry_points_dispatch_the_step_once`,
`test_duplicate_edges_dispatch_the_target_once`,
`test_yaml_pipelines_qa4.py::test_two_files_with_the_same_name_do_not_collapse`

### QA4-07 — Run marked `passed` while one of its steps is still `running`

Same duplicate-edge shape as QA4-06(b), with a slower command:

```
run ea3c743b  status=passed steps_completed=2/2
  a passed
  b passed
  b running       <-- still going, after _complete_pipeline ran
```

`_complete_pipeline` already called `_cleanup_workspace`, so the surviving
container is executing against a workspace volume that is being torn down.
For a demo: a finished, green run card with a live spinner inside it, and a
"2/2 complete" badge over three step rows.

**Test:** `test_run_is_not_completed_while_a_step_is_still_running`

### QA4-08 — `.lazyaf` run path skips the "no steps" gate → green 0/0 run

```
POST /api/repos/{repo}/lazyaf/pipelines/nosteps/run
-> 200 {"status":"passed","message":"Started pipeline run for 'No Steps'"}
   run status=passed 0/0 step_runs=0
```

The YAML file is just:

```yaml
name: "No Steps"
description: "a pipeline with no steps at all"
```

The platform endpoint `POST /api/pipelines/{id}/run` correctly refuses this with
`400 "Pipeline has no steps defined"` (`routers/pipelines.py:305`), but
`run_repo_pipeline` (`routers/lazyaf_files.py:283`) calls
`pipeline_executor.start_pipeline` directly, which falls into
`else: await self._complete_pipeline(db, pipeline_run, success=True)`
(`pipeline_executor.py:1244`). Two entry points into the same operation, two
different answers, and one of them is a green tick for nothing.

**Test:** `test_stepless_yaml_pipeline_does_not_report_a_green_pass`

### QA4-09 — A malformed pipeline YAML vanishes from the listing and 500s on fetch

Pushed to `.lazyaf/pipelines/`: `empty.yaml` (0 bytes), `alist.yaml` (a YAML
sequence), `nullish.yaml` (`null`), `scalar.yaml` (a bare string).

```
GET .../lazyaf/pipelines            -> 200, 13 entries; all four are ABSENT
GET .../lazyaf/pipelines/alist      -> 500 {"detail":"Error parsing pipeline file:
    app.schemas.lazyaf_yaml.PipelineYaml() argument after ** must be a mapping, not list"}
GET .../lazyaf/pipelines/nullish    -> 500 (… not NoneType)
GET .../lazyaf/pipelines/scalar     -> 500 (… not str)
GET .../lazyaf/pipelines/empty      -> 404 "Pipeline not found"   (see QA4-16)
```

Three problems in one:

1. `list_repo_pipelines` swallows the failure with `print(...); continue`
   (`routers/lazyaf_files.py:181`) — not even the logger, a bare `print`. The
   user's pipeline is simply gone with no diagnostic anywhere they can see.
2. The get-one endpoint answers **500** for what is unambiguously a client-side
   content error (`:225`, `:279`; agents have the same bug at `:128`).
3. The raw Python exception, including the internal module path
   `app.schemas.lazyaf_yaml.PipelineYaml`, is pasted into `detail` and lands in
   a UI toast.

**What SHOULD happen** — the listing should return the file with a parse-error
marker (or a companion `errors` array), and the get-one endpoint should answer
400/422 with a message naming the file and the line, not the Python type.

**Tests:** `test_malformed_pipeline_file_is_reported_not_swallowed`,
`test_malformed_pipeline_file_is_a_client_error`

### QA4-10 — YAML export cannot be imported; and YAML aliases amplify without bound

**(a) Broken round-trip.** `GET /api/pipelines/{id}/export/yaml` on a v2 graph
pipeline with `timeout: 777`, `continue_in_context: true`, and a fan-out
`a -> {b, c}` emits:

```yaml
name: qa4-export-roundtrip
description: null
version: 2
entry_points:
- a
steps:
  a:
    name: a
    type: script
    config:
      command: echo x
    on_success:
    - b
    - c
  b: {…}
  c: {…}
```

Four incompatibilities with the importer:

* `steps` is a **mapping**; `PipelineYaml.steps` is `list[PipelineStepYaml]`
  (`schemas/lazyaf_yaml.py:100`).
* `on_success` holds edge **targets**, and on a fan-out a **list** of them. The
  action vocabulary is `next | stop | trigger:{id} | merge:{branch}`; a bare id
  falls into the QA4-02 "unknown action → stop, report the step's verdict" path.
* `timeout` and `continue_in_context` are dropped entirely
  (`routers/pipelines.py:516-549` never copies them).
* `entry_points` and `version` are emitted but `PipelineYaml` has no such
  fields, so they are discarded on the way back in.

End-to-end proof: exporting a graph pipeline, committing the result to
`.lazyaf/pipelines/roundtrip.yaml` and pushing it gives

```
GET .../lazyaf/pipelines            -> roundtrip.yaml NOT listed
GET .../lazyaf/pipelines/roundtrip  -> 500 "1 validation error for PipelineYaml
                                       steps  Input should be a valid list"
```

The legacy (array) export **does** round-trip; only the graph shape is broken.

**(b) Alias amplification.** `yaml.safe_load` is called with no size budget
(`routers/lazyaf_files.py:167`, `:211`, `:270`). PyYAML resolves aliases by
reference, so parsing stays cheap — but `PipelineStepYaml.config` is
`dict[str, Any]`, so an alias expansion placed **inside a step config** survives
model construction and is then serialized into the API response and written to
`Pipeline.steps` in the DB.

Measured: a **393-byte** file (6 anchor levels × 8 refs) produced a
**1,910,220-byte** API response — ~4,900× amplification. Two more levels
(≈460 bytes) gives ≈120 MB; each additional level multiplies by 8.

The same amplifier placed in a top-level key is harmless, because pydantic
ignores extra fields — confirmed by the control test. The finding is
specifically about `dict[str, Any]` fields.

While the corpus was in place, `GET /api/repos/{id}/lazyaf/pipelines` also
intermittently returned **500** after 30 s with
`QueuePool limit of size 5 overflow 10 reached` — the endpoint does blocking
git I/O and YAML parsing for every file while holding a pooled DB session, so
big files pin a connection for seconds. (Load from sibling QA lanes contributed;
I am reporting the mechanism, not a clean number.)

**Tests:** `test_pipeline_export_qa4.py` (four cases) and
`test_yaml_alias_expansion_in_step_config_is_bounded`

### QA4-11 — The YAML path does not validate step `type`

`PipelineStepYaml.type` is `str = Field("script", ...)`
(`schemas/lazyaf_yaml.py:53`), so `type: banana` materializes and runs:

```
run status=failed
  S failed  execution routing failed: Unknown step type 'banana': there is no
            execution path for it. …
```

The graph API rejects the identical value at 422 (`StepType` enum). Two
definition paths, two verdicts on the same input; the YAML path only tells you
after it has created a run, a workspace and a StepRun.

Same class, same file: `on_success`/`on_failure` free text (QA4-02),
`timeout: -5` accepted (QA4-13), `triggers[].type: totally_made_up` accepted and
silently matched by nothing, and `branches: ["no-such-branch"]` accepted with no
warning that the branch does not exist.

**Test:** `test_unknown_step_type_is_refused_by_the_yaml_path`

### QA4-12 — No concurrency cap on fan-out

There is no semaphore, no queue, and no max-parallel setting anywhere in
`pipeline_executor.py` or `execution/local_executor.py`. A 20-way fan-out was
observed going from 1 active step to **20 active steps** within one poll
interval:

```
t=20s active=7  done=1 steps=8
t=22s active=20 done=1 steps=21
```

Every one of those is a `docker run` on the socket the backend is given — which
in the shipped compose files is the **host** daemon, shared with every other
LazyAF stack on the machine. A 100- or 500-way fan-out (both accepted at 201) is
a container storm nobody asked to be able to launch.

Combine with QA4-13's unbounded `timeout` and a `sleep infinity` command and
the containers never exit either.

**Test:** `test_fanout_is_capped` (marked `heavy`; excluded from the default run)

### QA4-21 — Step containers have no memory or CPU limit

`docker inspect` of a live step container started by the QA stack:

```
/xenodochial_tesla  Memory=0  MemorySwap=0  NanoCpus=0  CpuShares=0
                    Ulimits=<no value>  ReadonlyRootfs=false  Privileged=false
```

`Memory=0` and `NanoCpus=0` are Docker's "unlimited". The only resource key
anywhere in `backend/app/services/execution/local_executor.py` is

```
774:                run_kwargs["mem_limit"] = memory_limit
```

guarded by `if memory_limit:` — i.e. only when the *step author* asked for a
cap. `grep -n 'pids_limit\|nano_cpus\|cpu_quota\|ulimits\|mem_limit'` over that
file returns that single line. There is no platform default and no setting to
supply one.

This platform's premise is executing commands an AI wrote. A `script` step that
allocates without bound, spins every core, or fills the disk has nothing between
it and the host — and in the shipped compose files that host is also running the
LazyAF backend and every other stack's containers. Combined with QA4-12 (no
fan-out cap) and QA4-13 (no timeout cap), a single accepted pipeline definition
can put N unbounded containers on the host for an unbounded time.

**What SHOULD happen** — settings-driven defaults for `mem_limit`,
`nano_cpus`/`cpu_shares` and `pids_limit`, applied to every step container, with
the per-step `memory_limit` key allowed to *lower* them rather than being the
only source.

**Verified correct alongside it:** containers are not `Privileged`, and they
carry `lazyaf.pipeline_run_id` / `lazyaf.execution_key` labels, so an operator
can find and reap orphans even though the containers are unnamed.

**Tests:** `test_step_resource_limits_qa4.py`

### QA4-13 — `timeout` has no bounds and three inconsistent readers

Accepted at 201 by both definition paths: `0`, `-1`, `-99999999999999999999`,
`999999999`. Only a float is refused (422, `int_from_float`).

Observed with a real container:

```
run 322530a8  status=failed
  a failed  "step timed out after -1s"
```

LazyAF created a container, started it, then immediately killed it, and told the
user about a negative duration. `-5` from the YAML path behaves identically
("step timed out after -5s").

Three code sites read `timeout` and two of them disagree about `0`:

| site | expression | `timeout: 0` becomes |
|---|---|---|
| `pipeline_executor.py:1635` (legacy dispatch) | `step.get("timeout", 300)` | `0` |
| `pipeline_executor.py:1799` (the one that actually runs the step) | `step.get("timeout") or default_timeout_for(step_type)` | `300` (or `1800` for agents) |
| `pipeline_executor.py:3003` (timeout error message) | `step.get("timeout", 300)` | `0` |

So `timeout: 0` is silently coerced to the default at execution but reported as
`0` in the message, and `999999999` (≈31 years) is honoured with no cap.

**What SHOULD happen** — `timeout: int = Field(300, ge=1, le=<policy max>)` in
both `PipelineStepV2` (`schemas/pipeline.py:44`), `PipelineStepConfig` (`:128`)
and `PipelineStepYaml` (`schemas/lazyaf_yaml.py:57`), and one shared reader.

**Tests:** `test_negative_timeout_is_rejected`, `test_absurd_timeout_is_rejected`,
`test_negative_timeout_never_reaches_the_container`

---

## MINOR

### QA4-14 — `PATCH` leaves two disagreeing definitions on one pipeline

```bash
# create with legacy steps
curl -XPOST .../pipelines -d '{"name":"x","steps":[{"name":"LEGACY","type":"script","config":{"command":"echo L"}}]}'
# then patch a graph on
curl -XPATCH .../pipelines/{id} -d '{"steps_graph":{...a,b...}}'
curl .../pipelines/{id}
#  steps      -> [{"name":"LEGACY", ...}]        <-- still there
#  steps_graph-> {"steps":{"a":…,"b":…}, ...}    <-- what actually runs
```

`update_pipeline` (`routers/pipelines.py:213`) sets whichever keys were sent and
never clears the other definition. The YAML materializer already knows this is a
hazard and explicitly nulls `steps_graph` (`trigger_service.py:95`); the PATCH
endpoint does not do the mirror image. Any UI reading `steps` shows a definition
that is not the one executing.

### QA4-15 — `PipelineStepV2.id` is decorative

```json
{"steps": {"KEY": {"id": "DECLARED", "name": "mismatch", ...}},
 "entry_points": ["KEY"]}
```

Accepted at 201; the StepRun records `step_id="KEY"`. The executor keys
everything off the dict key (`pipeline_executor.py:1556`, `:2138`) and the
declared `id` is never read. Either enforce `key == value.id` in
`validate_graph_integrity`, or drop the field.

### QA4-16 — A zero-byte pipeline file reads as "not found"

`.lazyaf/pipelines/empty.yaml` (0 bytes) → `404 {"detail":"Pipeline not found"}`,
because `if content:` (`routers/lazyaf_files.py:214`) is falsy for an empty blob
so the parse is never attempted. The file exists; the API says it does not.

### QA4-17 — `steps_completed` and `completed_step_ids` tell different stories

`steps_completed` only increments on success (`pipeline_executor.py:3301`) while
`completed_step_ids` records terminal steps regardless. A run where both steps
failed reports `steps_completed=0 / steps_total=2` with
`completed_step_ids=["a","b"]`. A progress bar driven by the first and a graph
driven by the second disagree on screen.

### QA4-18 — Run listings carry unbounded logs

`GET /api/pipeline-runs?limit=5` returned a **312 KB** body on a run with 465
step_runs, because `StepRunRead.logs` is serialized in full for every step in
every listed run (`routers/pipelines.py:380`). At `limit=100` with real agent
logs this is a multi-megabyte payload on every dashboard poll. The websocket
`step_run_to_ws_dict` already omits `logs`; the REST listing should too, and
leave full logs to `/api/pipeline-runs/{id}/steps/{i}/logs`.

---

## POLISH

### QA4-19 — Empty pipeline name accepted
`{"name": ""}` → 201. Renders as a blank row in any pipeline list.

### QA4-20 — Internals leaking into user-visible text

* `StepRun.error` carries the router's full internal prose, e.g.
  `execution routing failed: Unknown step type 'banana': there is no execution
  path for it. Until 12.6 this fell back to the polling runner queue with a
  WARNING; that queue is gone, …` — an excellent commit message, a poor toast.
* Exported YAML contains `description: null` rather than omitting the key.

---

## Verified NOT a bug

Things I attacked that behaved correctly. These are covered by non-xfail guard
tests in the same files so they stay correct.

**Graph definition validation that works (all 422):**
1. Edge referencing a non-existent `to_step` / `from_step` — named, precise error.
2. `entry_points` naming a step that does not exist.
3. Empty `entry_points`.
4. A graph with `steps: {}` (caught by the entry-point rule).
5. A step with no `id`.
6. An unknown step `type` on the **graph** path (`StepType` enum).
7. An unknown edge `condition`.
8. A non-integer `timeout` (`int_from_float`).

**Execution semantics that are correct:**
9. A fan-in diamond `a -> {b,c} -> d` runs every step exactly once, and `d`
   waits for both branches.
10. Two independent entry points converging on one step run that step exactly
    once.
11. A self-edge `a -> a` does **not** loop — `completed_ids` blocks re-entry.
    (The *acceptance* of the self-edge is QA4-03; the execution is safe.)
12. A 2-node cycle `a <-> b` terminates; both steps run once.
13. Cancel rescues a wedged run and is idempotent-ish (200 while active,
    400 once terminal — never a 500).
14. `trigger_type` is a properly closed vocabulary: unknown values 422, and the
    reserved ad-hoc values (`card_work`, `playground`) are refused with 400 on
    the public endpoint. This is the model the rest of the schema should follow.
15. `POST /api/pipelines/{id}/run` correctly refuses a pipeline with no steps
    (400) — it is the *repo YAML* path that skips this (QA4-08).
16. A branch-scoped repo-pipeline run is refused (400) so it cannot clobber the
    trunk-owned materialized row.
17. Image preflight resolves every distinct step image once before dispatching
    step 0, as documented.

**Resource abuse that was absorbed without incident:**
18. A **5 MB** step command: accepted, stored, returned. No crash, no truncation.
19. A **10 MB** `.lazyaf/pipelines/huge.yaml`: parsed in 2.3 s, listed correctly.
20. A **200-level deeply nested** step `config`: accepted and round-tripped.
21. A YAML alias bomb in a key `PipelineYaml` does not declare: dropped by
    pydantic's extra-ignore, zero amplification. (Only `dict[str, Any]` fields
    amplify — QA4-10b.)
22. Duplicate JSON object keys in a `steps` payload collapse to last-wins, per
    JSON semantics — not a defect.
23. Duplicate edge **ids** are harmless in themselves; the damage comes from the
    duplicate edge *endpoints* (QA4-06b).
24. A 500-step graph is created and stored without incident — it is only
    *execution* that breaks (QA4-01).
25. A 2 MB `params` value and a null-byte-containing param key were accepted by
    the run endpoint without a crash.
26. `version: 999` on a graph is accepted and ignored — harmless today, but
    worth a note if graph schema versions ever start meaning something.
27. Step containers are **not** `Privileged`, and although they are unnamed
    they carry `lazyaf.pipeline_run_id` and `lazyaf.execution_key` labels, so
    orphans are findable and reapable. (The missing *resource* limits are
    QA4-21; the labelling is fine.)
28. The QA stack's `POST /api/test/reset` genuinely tears down and rebuilds the
    schema; it was hammered by five lanes concurrently all session without
    corrupting state.

---

## Suggested fix order

1. **QA4-01** — make graph traversal iterative; never let chain length equal
   stack depth. Same change fixes QA4-05.
2. **QA4-02 / QA4-03 / QA4-04 / QA4-08** — one shared invariant:
   `_complete_pipeline(success=True)` must require that every step in the graph
   either ran or was deliberately skipped by a *taken* edge condition. Anything
   else is `failed` (or a new `incomplete`) with a reason. Close the action
   vocabulary at the schema at the same time.
3. **QA4-06 / QA4-07** — de-duplicate `entry_points` and `steps_to_execute`
   (make them sets), and re-check `active_step_ids` immediately before each
   dispatch rather than from a pre-loop snapshot.
4. **QA4-13** — `ge=1, le=<max>` on all three `timeout` fields, one reader.
5. **QA4-09 / QA4-11** — surface YAML parse failures instead of `print`ing them;
   give `PipelineStepYaml.type` the `StepType` enum; answer 4xx not 5xx.
6. **QA4-10** — either fix the graph export to emit the importer's shape, or
   stop offering export for v2 pipelines until it does. Cap `dict[str, Any]`
   payload size on the YAML path.
7. **QA4-12 / QA4-21** — a configurable max-parallel-steps semaphore, and
   settings-driven default `mem_limit` / cpu / pids on every step container.
   These two are the difference between "a bad pipeline definition fails" and
   "a bad pipeline definition takes the machine down".
