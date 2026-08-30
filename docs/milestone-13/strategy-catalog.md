### Phase 13.2a: The Strategy Catalog — A Strategy Is Data

**Goal**: Make "a strategy is an arbitrary graph of activity, expressed as data" mechanically true. A `StrategyTemplate` is a v2 pipeline graph with role placeholders and one fan-out variable; a Trial expands it into a plain `PipelineGraphModel` and runs it through the executor that already exists. Adding a strategy to the catalog is authoring JSON, not shipping code.

> **Why now (with 13.2, before 13.3):** Phase 13.3's board compares strategies against each other, which is only meaningful if every strategy is the *same kind of object* differing only in graph shape and role binding. If the catalog is hand-written pipelines, "one-shot vs planner-fanout" is a comparison of two codepaths and the independent variable is contaminated. The expansion contract has to be pinned before the first matrix runs, or the first published number is unfalsifiable.

---

#### 1. The StrategyTemplate Graph Contract

##### 1.1 The template IS a v2 graph

`StrategyTemplate.graph` is a `PipelineGraphModel` (`backend/app/schemas/pipeline.py`) — `steps` keyed by id, `edges`, `entry_points`, `version: 2`. No new graph schema, no parallel dialect. A template validates against the *same* pydantic model as any pipeline a user draws in the UI, which is the property that makes R3 (one source of truth per wire contract) hold: there is exactly one graph schema in the system.

Strategy semantics ride in `PipelineStepV2.config`, which is already `dict[str, Any]`. All reserved keys are prefixed `lazyaf_` so they can never collide with the config keys the executor already reads (`command`, `image`, `title`, `description`, `runner_type`, `prompt_template`, `agent_file_ids`).

| Reserved config key | On step types | Meaning |
|---|---|---|
| `lazyaf_role` | agent | Role placeholder. Bound to a concrete model at trial time. |
| `lazyaf_fanout` | any | Marks the node replicable: `{"var": "K", "id_template": "worker_{i}"}`. |
| `lazyaf_branch` | agent, script | Branch + workspace allocation for this node. |
| `lazyaf_integrate` | script, agent | This node is the join; carries the integration policy block. |
| `lazyaf_gate` | script | This node's exit status is the CI gate for `gated` strategies. |
| `lazyaf_determinism` | agent | Written by the binder, never authored: `{temperature, seed, top_p}`. |

Two fields are added to the `StrategyTemplate` / `Trial` model blocks in the Specification Layer section:

```python
class StrategyTemplate:
    # ... as specified ...
    variables: dict              # {"K": {"type": "int", "default": 4, "min": 1, "max": 32}}

class Trial:
    # ... as specified ...
    template_variables: dict     # {"K": 16} - the bound values for THIS trial
```

`variables` is what makes `planner-fanout-4` and `planner-fanout-16` the same template. `template_variables` is provenance: a trial that does not record the K it ran at cannot be reproduced.

##### 1.2 Expansion: template graph -> executable graph

```
expand_strategy_graph(template, case, trial, model_assignment, variables)
    -> PipelineGraphModel
```

Six ordered passes. The output is an ordinary graph stored on a throwaway `Pipeline` row and executed as an ordinary run — which is what makes "each iteration IS a visible pipeline run" true rather than aspirational.

```python
def expand_strategy_graph(template, case, trial, model_assignment, variables):
    graph = PipelineGraphModel(**template.graph)      # 1. parse + structural validate
    require_dag(graph)                                # 2. reject cycles (see 1.5)
    vals = resolve_variables(template.variables, variables)   # 3. defaults <- overrides, bounds-checked
    graph = replicate_fanout_nodes(graph, vals)       # 4. K clones + edge rewrite
    graph = bind_roles(graph, model_assignment)       # 5. role -> {runner_type, model, determinism}
    graph = render_placeholders(graph, case, trial, vals)     # 6. {{ ... }} substitution
    return PipelineGraphModel(**graph.model_dump())   # re-validate; must still parse
```

**Pass 4 — replication.** A node carrying `lazyaf_fanout: {"var": "K", "id_template": "worker_{i}"}` is removed and replaced by K clones with ids from `id_template` (`worker_1 .. worker_K`), each carrying `lazyaf_worker_index: i`. Edge rewrite:

- every incoming edge `X -> node` becomes K edges `X -> worker_i` (the fan-out)
- every outgoing edge `node -> Y` becomes K edges `worker_i -> Y` (the fan-in)
- edge ids get an `_{i}` suffix; if the node was an entry point, all K clones become entry points
- clone `position` is auto-laid-out `x = base.x + (i - 1) * 220` (same precedent as `array_to_graph`), so the fan renders as a fan in the graph UI

The fan-in is free: `PipelineExecutor._all_upstream_satisfied` already requires **all** incoming edges' sources to be in `completed_step_ids` before a node runs. K workers converging on one integrate node is native executor behavior, not new engine work.

**Pass 5 — role binding.** For each step with `lazyaf_role: R`, merge `model_assignment[R]` into the step config:

```json
{
  "planner":    {"runner_type": "claude-code", "model": "claude-opus-5",
                 "determinism": {"temperature": 0.0}},
  "worker":     {"runner_type": "claude-code", "model": "claude-haiku-4-5",
                 "determinism": {"temperature": 0.0}},
  "integrator": {"runner_type": "claude-code", "model": "claude-sonnet-5"}
}
```

becomes, on each bound step, `config.runner_type`, `config.model` (already read by `runner-claude/entrypoint.py` as `job.get("model")` and passed as `--model`), and `config.lazyaf_determinism`. `lazyaf_role` is retained after binding — it is the key `StepUsage` rows are attributed by, and `Trial.cost_by_role` is a group-by on it. Drop the role at bind time and cost-by-role, the number that actually tests the "expensive planner, cheap workers" hypothesis, becomes unrecoverable.

**Pass 6 — placeholders.** `{{ case.base_commit_sha }}`, `{{ case.task_statement }}`, `{{ case.fail_to_pass }}`, `{{ trial.id }}`, `{{ trial.branch }}`, `{{ iteration.index }}`, `{{ iteration.previous_failures }}`, `{{ i }}` (worker index), and any declared variable (`{{ K }}`). Rendering runs over string leaves of `config` only. A rendered graph containing a residual `{{` is a hard error — a silently unrendered placeholder would be shipped to the agent as literal text and quietly poison a trial.

##### 1.3 Role binding completeness

Roles are **derived from the graph**, not trusted from the `roles` column: `roles == {step.config.lazyaf_role for step in graph.steps.values() if "lazyaf_role" in step.config}`. A template whose stored `roles` disagrees with its graph is rejected at save time.

At trial start the binding is checked **strictly in both directions**:

| Condition | Result |
|---|---|
| Role in graph, absent from `model_assignment` | Trial refuses to start: `unbound_role` |
| Key in `model_assignment`, no such role in graph | Trial refuses to start: `unknown_role` |
| Assignment entry missing `runner_type` | Trial refuses to start: `incomplete_binding` |
| Assignment names a model with no configured provider credentials | Trial refuses to start: `unavailable_model` |

The extra-key case is refused rather than ignored on purpose. The common failure is a typo (`"reviewers"` for `"reviewer"`), and the silent-ignore version of that bug runs a whole matrix cell against the default model and reports it as a measured binding.

##### 1.4 K-parameterization

`planner-fanout-4` and `planner-fanout-16` are **one template, two trials**:

```json
{
  "slug": "planner-fanout",
  "variables": {"K": {"type": "int", "default": 4, "min": 1, "max": 32}},
  "parallelism": {"max_concurrent_workers": 8, "branch_per_worker": true}
}
```

- `Trial.template_variables = {"K": 16}` selects the width.
- `Experiment.matrix` gains a `template_variables` axis, so a K-sweep (`K in [1, 2, 4, 8, 16]`) is one matrix over one template — the cheapest genuinely publishable experiment this harness can run, and the direct answer to the fan-out-ceiling open question.
- `K = 1` is not a special case: replication with K=1 yields a single `worker_1` node, so "planner + one worker" is a real, comparable point on the same curve rather than a different strategy.
- `max_concurrent_workers` caps *simultaneous* execution without changing the graph — K is the width of the work split, concurrency is a resource limit. A trial at K=16 with `max_concurrent_workers: 4` is a legitimate configuration and its `serial_equivalent_ms / wall_clock_ms` speedup reflects that, which is exactly what the board should show.

##### 1.5 The DAG constraint (and the one authoring rule it implies)

The executor traverses a DAG. Two consequences, both enforced at template-save time:

1. **A template with a cycle is rejected.** Iteration is the Trial orchestrator running N sequential pipeline runs, feeding `{{ iteration.previous_failures }}` forward — never an edge pointing backwards. `require_dag` runs a Kahn topological sort over `edges` and reports the offending cycle by step id.

2. **A conditional branch must not rejoin.** `_all_upstream_satisfied` requires *every* incoming edge's source to have completed, regardless of that edge's `condition`. So a node with one `SUCCESS` upstream and one `FAILURE` upstream can never run — exactly one of them will ever complete, and the run stalls with the node permanently un-dispatched. Authoring rule: **a `FAILURE` edge starts a branch that terminates in its own tail**; it never merges back into the happy path. The resolver-agent strategy in the catalog below is built this way (`integrate` fails -> `resolve` -> `integrate_retry` -> `gate_retry`, a parallel tail), and `test_conditional_branch_never_rejoins_a_join_node` pins it so nobody re-learns this by staring at a hung run.

---

#### 2. The Catalog

All graphs below are literal `PipelineGraphModel` JSON. `position` is omitted (it is `Optional`); the expander auto-lays-out. Structural cost is quoted as **serial depth** (longest path, the wall-clock floor) x **agent steps** (the cost floor).

Every catalog template ships as a JSON fixture under `backend/app/services/benchmark/catalog/` and is seeded on migration. `run_tests` / `gate` steps run the case's test command and emit the 12.2.6 manifest at `/workspace/.control/test_results.json`; the oracle scores `fail_to_pass` / `pass_to_pass` from the resulting `TestRun` rows. There is no second scoring channel.

---

##### S1. `one-shot` — the control

**Hypothesis:** none. This is the null hypothesis made runnable. Every claim in the write-up is "better than one-shot by X at cost Y", so one-shot appears in every comparison in Phase 13.3's board. It is also the cheapest place to detect a broken case: if one-shot with a frontier model never solves *any* case in a suite, the suite is miscalibrated, not the strategies.

```json
{
  "version": 2,
  "entry_points": ["implement"],
  "steps": {
    "implement": {
      "id": "implement",
      "name": "Implement",
      "type": "agent",
      "timeout": 1800,
      "config": {
        "lazyaf_role": "solver",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "{{ case.task_statement }}",
        "description": "{{ case.task_statement }}\n\nFailing tests to fix: {{ case.fail_to_pass }}\n\n{{ iteration.previous_failures }}",
        "prompt_template": "bench/one-shot-implement"
      }
    },
    "run_tests": {
      "id": "run_tests",
      "name": "Run oracle",
      "type": "script",
      "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    }
  },
  "edges": [
    {"id": "e_impl_tests", "from_step": "implement", "to_step": "run_tests",
     "condition": "always"}
  ]
}
```

**Roles:** `solver`.
**Structural cost:** serial depth 2, agent steps 1. No parallelism, no integration.
**Good at:** trivial and small cases where the whole change fits one context window; the lowest possible cost-to-solve when it works; near-zero variance in shape.
**Bad at:** anything needing more than one coherent pass. Expect it to lead on `trivial`, and to be the strategy with the highest `pass_to_pass_broken` on `large` cases — nothing in this graph ever looks at its own work.

---

##### S2. `test-first`

**Hypothesis:** an agent that writes the oracle before the implementation produces a sharper target and catches its own regressions, buying solve-rate for one extra agent step. The obvious threat to validity is that the agent writes tests that pass trivially — which is why the case's own `fail_to_pass` ids, not the agent's tests, remain the oracle. The agent's tests are scaffolding; they never score.

```json
{
  "version": 2,
  "entry_points": ["write_tests"],
  "steps": {
    "write_tests": {
      "id": "write_tests",
      "name": "Write tests",
      "type": "agent",
      "timeout": 1200,
      "config": {
        "lazyaf_role": "test_author",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "Write tests for: {{ case.task_statement }}",
        "description": "Write failing tests that characterize the desired behavior. Do NOT implement it.\n\n{{ case.task_statement }}",
        "prompt_template": "bench/test-first-author"
      }
    },
    "implement": {
      "id": "implement",
      "name": "Implement against tests",
      "type": "agent",
      "timeout": 1800,
      "config": {
        "lazyaf_role": "solver",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}"},
        "title": "{{ case.task_statement }}",
        "description": "Make the tests written in the previous step pass, without weakening them.\n\n{{ iteration.previous_failures }}",
        "prompt_template": "bench/test-first-implement"
      },
      "continue_in_context": false
    },
    "run_tests": {
      "id": "run_tests",
      "name": "Run oracle",
      "type": "script",
      "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    }
  },
  "edges": [
    {"id": "e_tests_impl", "from_step": "write_tests", "to_step": "implement",
     "condition": "success"},
    {"id": "e_impl_oracle", "from_step": "implement", "to_step": "run_tests",
     "condition": "always"}
  ]
}
```

**Roles:** `test_author`, `solver`.
**Structural cost:** serial depth 3, agent steps 2. No parallelism.
**Good at:** cases with under-specified `task_statement`s, where the act of writing tests forces the agent to resolve ambiguity; suppressing `pass_to_pass` regressions, because the agent has a habit of running tests.
**Bad at:** cases where the repo's test harness is expensive or awkward to extend — the test-authoring step burns budget on scaffolding that never scores. Also the strategy most likely to "solve" by weakening tests, which is precisely why the platform oracle is external to it.

---

##### S3. `adversarial-review` — implement -> fan out N reviewers -> join -> fix

**Hypothesis:** review is cheaper than re-implementation, and independent reviewers find disjoint defects. N reviewers with **no shared context** is the point: N sequential review passes converge on one opinion, N parallel ones do not. This is a fan-out of *attention*, not of *work* — the reviewers do not branch, they read one diff and write findings.

Shown in **template form**, with `lazyaf_fanout` un-expanded.

```json
{
  "version": 2,
  "entry_points": ["implement"],
  "steps": {
    "implement": {
      "id": "implement",
      "name": "Implement",
      "type": "agent",
      "timeout": 1800,
      "config": {
        "lazyaf_role": "solver",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "{{ case.task_statement }}",
        "description": "{{ case.task_statement }}\n\n{{ iteration.previous_failures }}",
        "prompt_template": "bench/implement"
      }
    },
    "review": {
      "id": "review",
      "name": "Review {{ i }}/{{ N }}",
      "type": "agent",
      "timeout": 900,
      "config": {
        "lazyaf_role": "reviewer",
        "lazyaf_fanout": {"var": "N", "id_template": "review_{i}"},
        "lazyaf_branch": {"mode": "read_only", "name": "{{ trial.branch }}"},
        "title": "Adversarial review pass {{ i }}",
        "description": "Review the diff on {{ trial.branch }} against {{ case.base_commit_sha }}. Report defects only. Do not edit files.",
        "prompt_template": "bench/adversarial-review"
      }
    },
    "fix": {
      "id": "fix",
      "name": "Apply review findings",
      "type": "agent",
      "timeout": 1800,
      "config": {
        "lazyaf_role": "fixer",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}"},
        "title": "Fix findings from {{ N }} reviewers",
        "description": "Apply the findings from all reviewers. Reject findings you judge incorrect, and say why.",
        "prompt_template": "bench/apply-review"
      }
    },
    "run_tests": {
      "id": "run_tests",
      "name": "Run oracle",
      "type": "script",
      "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    }
  },
  "edges": [
    {"id": "e_impl_review", "from_step": "implement", "to_step": "review",
     "condition": "success"},
    {"id": "e_review_fix", "from_step": "review", "to_step": "fix",
     "condition": "success"},
    {"id": "e_fix_oracle", "from_step": "fix", "to_step": "run_tests",
     "condition": "always"}
  ]
}
```

At N=3 the expander produces `review_1..review_3`, edges `implement -> review_i` and `review_i -> fix`, and the executor's all-upstream-satisfied rule makes `fix` a true join.

**Roles:** `solver`, `reviewer`, `fixer`.
**Structural cost:** serial depth 4, agent steps `2 + N` (5 at N=3), of which N run in parallel. Wall-clock floor is 4 steps regardless of N.
**Good at:** `pass_to_pass` regression rate — this is the strategy designed to catch the collateral damage that one-shot ships. Also good at `medium` cases where the first attempt is nearly right.
**Bad at:** cost efficiency on `trivial` cases (N+2 agent steps to fix a typo). Degenerate failure to watch for: N reviewers all reporting the same obvious defect, making N>2 pure waste — measurable as finding-overlap across reviewers, and the reason N is a swept variable rather than a constant.

---

##### S4. `planner-fanout` at K=4 — the owner's hypothesis

**Hypothesis:** buy latency and cost efficiency by spending a little intelligence up front. A high-end model reads the case once and writes K disjoint work orders; K cheap models execute them in parallel on their own branches off the case's base commit; an integrator merges. If the hypothesis holds, `cost_by_role` shows a small planner bill and a large-but-cheap worker bill totaling less than one frontier model doing everything, at a fraction of the wall-clock.

This is the strategy that is **impossible on a single-sandbox harness** and trivial here: `lazyaf_branch.mode: "per_worker"` allocates a branch and a workspace per worker off `{{ case.base_commit_sha }}` using the same `populate_workspace(..., commit_sha)` path cards have used since 12.2-INT.

Shown **expanded at K=4** — this is the exact graph handed to the executor.

```json
{
  "version": 2,
  "entry_points": ["plan"],
  "steps": {
    "plan": {
      "id": "plan",
      "name": "Plan work split",
      "type": "agent",
      "timeout": 1200,
      "config": {
        "lazyaf_role": "planner",
        "runner_type": "claude-code",
        "model": "claude-opus-5",
        "lazyaf_determinism": {"temperature": 0.0},
        "lazyaf_branch": {"mode": "read_only", "base": "{{ case.base_commit_sha }}"},
        "title": "Plan: {{ case.task_statement }}",
        "description": "Split this task into exactly 4 independent work orders that touch DISJOINT files. Write them to .control/work_orders.json.\n\n{{ case.task_statement }}\n\n{{ iteration.previous_failures }}",
        "prompt_template": "bench/planner-split"
      }
    },
    "worker_1": {
      "id": "worker_1", "name": "Worker 1/4", "type": "agent", "timeout": 1800,
      "config": {
        "lazyaf_role": "worker", "lazyaf_worker_index": 1,
        "runner_type": "claude-code", "model": "claude-haiku-4-5",
        "lazyaf_determinism": {"temperature": 0.0},
        "lazyaf_branch": {"mode": "per_worker", "name": "trial/{{ trial.id }}/w1",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "Work order 1",
        "description": "Execute work order 1 from .control/work_orders.json. Stay inside the files it assigns you.",
        "prompt_template": "bench/worker-execute"
      }
    },
    "worker_2": {
      "id": "worker_2", "name": "Worker 2/4", "type": "agent", "timeout": 1800,
      "config": {
        "lazyaf_role": "worker", "lazyaf_worker_index": 2,
        "runner_type": "claude-code", "model": "claude-haiku-4-5",
        "lazyaf_determinism": {"temperature": 0.0},
        "lazyaf_branch": {"mode": "per_worker", "name": "trial/{{ trial.id }}/w2",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "Work order 2",
        "description": "Execute work order 2 from .control/work_orders.json. Stay inside the files it assigns you.",
        "prompt_template": "bench/worker-execute"
      }
    },
    "worker_3": {
      "id": "worker_3", "name": "Worker 3/4", "type": "agent", "timeout": 1800,
      "config": {
        "lazyaf_role": "worker", "lazyaf_worker_index": 3,
        "runner_type": "claude-code", "model": "claude-haiku-4-5",
        "lazyaf_determinism": {"temperature": 0.0},
        "lazyaf_branch": {"mode": "per_worker", "name": "trial/{{ trial.id }}/w3",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "Work order 3",
        "description": "Execute work order 3 from .control/work_orders.json. Stay inside the files it assigns you.",
        "prompt_template": "bench/worker-execute"
      }
    },
    "worker_4": {
      "id": "worker_4", "name": "Worker 4/4", "type": "agent", "timeout": 1800,
      "config": {
        "lazyaf_role": "worker", "lazyaf_worker_index": 4,
        "runner_type": "claude-code", "model": "claude-haiku-4-5",
        "lazyaf_determinism": {"temperature": 0.0},
        "lazyaf_branch": {"mode": "per_worker", "name": "trial/{{ trial.id }}/w4",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "Work order 4",
        "description": "Execute work order 4 from .control/work_orders.json. Stay inside the files it assigns you.",
        "prompt_template": "bench/worker-execute"
      }
    },
    "integrate": {
      "id": "integrate",
      "name": "Integrate 4 branches",
      "type": "script",
      "timeout": 900,
      "config": {
        "command": "lazyaf-integrate --trial {{ trial.id }}",
        "lazyaf_integrate": {
          "policy": "sequential-merge",
          "order": "worker_index",
          "sources": ["trial/{{ trial.id }}/w1", "trial/{{ trial.id }}/w2",
                      "trial/{{ trial.id }}/w3", "trial/{{ trial.id }}/w4"],
          "target": "{{ trial.branch }}",
          "on_conflict": "fail"
        }
      }
    },
    "run_tests": {
      "id": "run_tests",
      "name": "Run oracle",
      "type": "script",
      "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    }
  },
  "edges": [
    {"id": "e_plan_w1", "from_step": "plan", "to_step": "worker_1", "condition": "success"},
    {"id": "e_plan_w2", "from_step": "plan", "to_step": "worker_2", "condition": "success"},
    {"id": "e_plan_w3", "from_step": "plan", "to_step": "worker_3", "condition": "success"},
    {"id": "e_plan_w4", "from_step": "plan", "to_step": "worker_4", "condition": "success"},
    {"id": "e_w1_int", "from_step": "worker_1", "to_step": "integrate", "condition": "success"},
    {"id": "e_w2_int", "from_step": "worker_2", "to_step": "integrate", "condition": "success"},
    {"id": "e_w3_int", "from_step": "worker_3", "to_step": "integrate", "condition": "success"},
    {"id": "e_w4_int", "from_step": "worker_4", "to_step": "integrate", "condition": "success"},
    {"id": "e_int_oracle", "from_step": "integrate", "to_step": "run_tests", "condition": "always"}
  ]
}
```

`lazyaf-integrate` ships in the base image alongside `/control/run.py` and calls `POST /api/trials/{trial_id}/integrate`, which drives `git_server.merge_branch` / `rebase_branch`. It is a thin client on purpose — the merge happens server-side against the bare repo, so integration cost and conflict counts land on the `Trial` row rather than in a step's stdout.

**Roles:** `planner`, `worker`, `integrator` (the integrator role is unbound in this variant — see the strict-binding rule; `sequential-merge` uses no model, so `integrator` is simply not a role in this graph. The agent-composed variant introduces it).
**Structural cost:** serial depth 4, agent steps `1 + K` (5 at K=4), of which K run in parallel. Serial-equivalent time is ~K worker steps; wall-clock is ~1, so speedup approaches K bounded by `max_concurrent_workers`.
**Good at:** wall-clock-to-solve, and cost-to-solve on `large` cases where the work genuinely partitions (multi-file, multi-module changes). Best case for the thesis: a `medium`/`large` case where planner + 4 cheap workers beats one frontier one-shot on both axes.
**Bad at:** `trivial` cases — the planner tax is unamortizable and it will lose to one-shot on cost, which is a real and reportable result, not a bug. Also bad when the work does not partition: a bad split produces K agents editing the same file, which shows up as `integration_conflicts` and as file-overlap across workers. That overlap metric is the direct test of "does the planner's quality drive the conflict rate", which is the most publishable question in the whole fan-out story.

---

##### S5. `planner-fanout-resolver` — conflict resolution as an agent task

**Hypothesis:** the tax on parallelism is integration, and that tax is payable by a model rather than by a human or by failing the iteration. LazyAF returns conflicts as **structured data** — `_get_conflict_details` yields `{path, base_content, ours_content, theirs_content}` per file, not a wall of `<<<<<<<` markers — so a resolver agent gets a clean three-way input and returns resolved contents to `resolve_and_merge`. This strategy variant is not expressible on a harness that lacks a git server.

Delta from S4 — `plan` and `worker_1..4` are byte-identical to S4 and elided, so this fragment declares `integrate` as its entry point; in the full template `entry_points` is `["plan"]`. The failure branch is a **parallel tail** — it never rejoins `run_tests`, per the authoring rule in 1.5.

```json
{
  "version": 2,
  "entry_points": ["integrate"],
  "steps": {
    "integrate": {
      "id": "integrate",
      "name": "Integrate 4 branches",
      "type": "script",
      "timeout": 900,
      "config": {
        "command": "lazyaf-integrate --trial {{ trial.id }}",
        "lazyaf_integrate": {
          "policy": "sequential-merge",
          "order": "worker_index",
          "sources": ["trial/{{ trial.id }}/w1", "trial/{{ trial.id }}/w2",
                      "trial/{{ trial.id }}/w3", "trial/{{ trial.id }}/w4"],
          "target": "{{ trial.branch }}",
          "on_conflict": "resolver-agent",
          "emit_conflicts_to": ".control/conflicts.json"
        }
      }
    },
    "resolve": {
      "id": "resolve",
      "name": "Resolve conflicts",
      "type": "agent",
      "timeout": 1200,
      "config": {
        "lazyaf_role": "resolver",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}"},
        "title": "Resolve merge conflicts",
        "description": "Structured conflicts are in .control/conflicts.json as {path, base_content, ours_content, theirs_content}. Write resolved contents to .control/resolutions.json. Preserve BOTH sides' intent; dropping a worker's change is a failure.",
        "prompt_template": "bench/resolve-conflicts"
      }
    },
    "integrate_retry": {
      "id": "integrate_retry",
      "name": "Apply resolutions",
      "type": "script",
      "timeout": 600,
      "config": {
        "command": "lazyaf-integrate --trial {{ trial.id }} --apply-resolutions .control/resolutions.json",
        "lazyaf_integrate": {"policy": "sequential-merge",
                             "target": "{{ trial.branch }}",
                             "on_conflict": "fail"}
      }
    },
    "run_tests": {
      "id": "run_tests", "name": "Run oracle", "type": "script", "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    },
    "run_tests_after_resolve": {
      "id": "run_tests_after_resolve", "name": "Run oracle (post-resolve)",
      "type": "script", "timeout": 900,
      "config": {"command": "lazyaf-oracle run", "lazyaf_gate": false}
    }
  },
  "edges": [
    {"id": "e_int_oracle", "from_step": "integrate", "to_step": "run_tests",
     "condition": "success"},
    {"id": "e_int_resolve", "from_step": "integrate", "to_step": "resolve",
     "condition": "failure"},
    {"id": "e_resolve_retry", "from_step": "resolve", "to_step": "integrate_retry",
     "condition": "success"},
    {"id": "e_retry_oracle", "from_step": "integrate_retry",
     "to_step": "run_tests_after_resolve", "condition": "always"}
  ]
}
```

**Roles:** `planner`, `worker`, `resolver`.
**Structural cost:** serial depth 4 clean / 6 on conflict; agent steps `1 + K` clean, `2 + K` on conflict. The conflict path's cost lands in `Trial.integration_cost_usd` and its wall-clock in the gap between `wall_clock_ms` and the clean path — the tax on parallelism, itemized.
**Good at:** rescuing high-K trials that `on_conflict: fail` would throw away, which is what makes a K-sweep meaningful past the point where conflicts start. It is also the only variant that produces a `conflicts_resolved / integration_conflicts` ratio, i.e. "can a model actually do this job".
**Bad at:** honesty about silent loss. A resolver that picks `ours` for every file produces a clean merge and a *wrong* result; only `pass_to_pass` catches it. Report `pass_to_pass_broken` for resolver trials separately — if resolver-agent trials regress more than fail-fast trials at the same K, the resolver is laundering conflicts, not resolving them.

---

##### S6. `gated` — any strategy plus a real CI gate

**Hypothesis:** "done" should mean "CI is green", not "the agent stopped". A gate is a graph *suffix*, which is the cleanest demonstration that these compose: gating is not a mode flag on a strategy, it is one more node.

Shown as the suffix applied to S1, yielding `one-shot-gated`:

```json
{
  "version": 2,
  "entry_points": ["implement"],
  "steps": {
    "implement": {
      "id": "implement", "name": "Implement", "type": "agent", "timeout": 1800,
      "config": {
        "lazyaf_role": "solver",
        "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}",
                          "base": "{{ case.base_commit_sha }}"},
        "title": "{{ case.task_statement }}",
        "description": "{{ case.task_statement }}\n\n{{ iteration.previous_failures }}",
        "prompt_template": "bench/one-shot-implement"
      }
    },
    "gate": {
      "id": "gate",
      "name": "CI gate",
      "type": "script",
      "timeout": 1800,
      "config": {
        "command": "lazyaf-oracle run --gate",
        "lazyaf_gate": true
      }
    }
  },
  "edges": [
    {"id": "e_impl_gate", "from_step": "implement", "to_step": "gate",
     "condition": "success"}
  ]
}
```

`lazyaf_gate: true` means the step's exit status decides the iteration outcome: a red gate is a failed iteration, the orchestrator feeds its output forward as `{{ iteration.previous_failures }}`, and the next iteration starts. Exactly one node per graph may carry `lazyaf_gate: true`; more than one is a validation error (two gates means two definitions of done).

**Roles:** whatever the gated strategy uses; the gate adds none.
**Structural cost:** +1 serial script step, +0 agent steps. The cheapest suffix in the catalog.
**Good at:** making `iterations-to-solve` a real distribution rather than a constant — an ungated strategy has no signal to iterate on. Also the closest analogue to how the platform is actually used (R7's dogfood pipeline is a gated loop).
**Bad at:** cases whose test suite is slow — the gate dominates wall-clock and makes speedup comparisons between strategies converge toward the suite's runtime rather than the strategy's shape. Report gate duration separately from agent duration so this confound is visible instead of buried.

---

##### S7. `composed-full` — the proof that graphs compose and modes would not

**Hypothesis:** none in particular. This entry exists to prove the architectural claim. If strategies were an enum of blessed loops, "test-first AND planner fan-out AND adversarial review AND a gate" would require a new enum member and new code. As graphs, it is concatenation.

```
write_tests (test_author)
   -> plan (planner)
      -> worker_1..K (worker, branch per worker)
         -> integrate (sequential-merge, on_conflict: resolver-agent)
            -> review_1..N (reviewer, read-only)
               -> fix (fixer)
                  -> gate (lazyaf_gate: true)
```

```json
{
  "version": 2,
  "entry_points": ["write_tests"],
  "steps": {
    "write_tests": {
      "id": "write_tests", "name": "Write tests", "type": "agent", "timeout": 1200,
      "config": {"lazyaf_role": "test_author",
                 "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}",
                                   "base": "{{ case.base_commit_sha }}"},
                 "title": "Write tests for: {{ case.task_statement }}",
                 "description": "Write failing tests. Do not implement.",
                 "prompt_template": "bench/test-first-author"}
    },
    "plan": {
      "id": "plan", "name": "Plan work split", "type": "agent", "timeout": 1200,
      "config": {"lazyaf_role": "planner",
                 "lazyaf_branch": {"mode": "read_only", "name": "{{ trial.branch }}"},
                 "title": "Plan: {{ case.task_statement }}",
                 "description": "Split into {{ K }} disjoint work orders against the tests just written. Write .control/work_orders.json.",
                 "prompt_template": "bench/planner-split"}
    },
    "worker": {
      "id": "worker", "name": "Worker {{ i }}/{{ K }}", "type": "agent", "timeout": 1800,
      "config": {"lazyaf_role": "worker",
                 "lazyaf_fanout": {"var": "K", "id_template": "worker_{i}"},
                 "lazyaf_branch": {"mode": "per_worker",
                                   "name": "trial/{{ trial.id }}/w{{ i }}",
                                   "base": "{{ trial.branch }}"},
                 "title": "Work order {{ i }}",
                 "description": "Execute work order {{ i }} from .control/work_orders.json.",
                 "prompt_template": "bench/worker-execute"}
    },
    "integrate": {
      "id": "integrate", "name": "Integrate", "type": "script", "timeout": 900,
      "config": {"command": "lazyaf-integrate --trial {{ trial.id }}",
                 "lazyaf_integrate": {"policy": "sequential-merge",
                                      "order": "worker_index",
                                      "target": "{{ trial.branch }}",
                                      "on_conflict": "resolver-agent"}}
    },
    "review": {
      "id": "review", "name": "Review {{ i }}/{{ N }}", "type": "agent", "timeout": 900,
      "config": {"lazyaf_role": "reviewer",
                 "lazyaf_fanout": {"var": "N", "id_template": "review_{i}"},
                 "lazyaf_branch": {"mode": "read_only", "name": "{{ trial.branch }}"},
                 "title": "Adversarial review pass {{ i }}",
                 "description": "Review the integrated diff. Report defects only.",
                 "prompt_template": "bench/adversarial-review"}
    },
    "fix": {
      "id": "fix", "name": "Apply findings", "type": "agent", "timeout": 1800,
      "config": {"lazyaf_role": "fixer",
                 "lazyaf_branch": {"mode": "trunk", "name": "{{ trial.branch }}"},
                 "title": "Fix findings",
                 "description": "Apply reviewer findings. {{ iteration.previous_failures }}",
                 "prompt_template": "bench/apply-review"}
    },
    "gate": {
      "id": "gate", "name": "CI gate", "type": "script", "timeout": 1800,
      "config": {"command": "lazyaf-oracle run --gate", "lazyaf_gate": true}
    }
  },
  "edges": [
    {"id": "e1", "from_step": "write_tests", "to_step": "plan", "condition": "success"},
    {"id": "e2", "from_step": "plan", "to_step": "worker", "condition": "success"},
    {"id": "e3", "from_step": "worker", "to_step": "integrate", "condition": "success"},
    {"id": "e4", "from_step": "integrate", "to_step": "review", "condition": "success"},
    {"id": "e5", "from_step": "review", "to_step": "fix", "condition": "success"},
    {"id": "e6", "from_step": "fix", "to_step": "gate", "condition": "success"}
  ]
}
```

**Roles:** `test_author`, `planner`, `worker`, `reviewer`, `fixer` (+ `resolver` when the conflict tail is expanded in).
**Structural cost:** serial depth 7, agent steps `4 + K + N` (11 at K=4, N=3). Two independent fan-out variables, so `Experiment.matrix` can sweep the (K, N) plane on one template.
**Good at:** it should not be *good*, it should be *expensive* — its job is to be the ceiling case, showing whether stacking every technique compounds or just compounds cost. If `composed-full` does not beat `planner-fanout` on solve-rate, that is a headline result about diminishing returns.
**Bad at:** everything cheap. Expect it to lose cost-to-solve to one-shot on `trivial`/`small` by a wide margin, and to be the strategy most likely to hit `budget_exhausted` — which is exactly why fixed-budget solve-rate stays on the board next to cost-to-solve.

---

#### 3. Integration Policies

`StrategyTemplate.integration` (and per-node `lazyaf_integrate`) is a `(policy, on_conflict)` pair. Both halves are measured variables, not fixed details — "which integration policy wins" is a question the harness answers rather than one the architecture assumes.

##### 3.1 Policies

| Policy | Mechanism | Tradeoff | Failure mode |
|---|---|---|---|
| `sequential-merge` | `git_server.merge_branch(w_i -> target)` for i in a fixed order | Simplest; one merge commit per worker; K merges total | **Order-dependent conflict count.** The last worker eats every accumulated conflict, so the same K branches merged in a different order yield a different `integration_conflicts`. Mitigation: `order` is pinned (`worker_index`) and recorded; a policy that did not pin it would not be measurable. |
| `rebase-onto-trunk` | `git_server.rebase_branch(w_i, onto=target)` then fast-forward | Linear, bisectable history; each worker's commits are attributable | **Conflicts are re-litigated per worker.** A worker whose base moved may need its entire branch replayed, and the cost is O(K) in the worst case rather than O(1). A rebase that fails mid-way leaves the worker branch in a state the next policy step must reason about. |
| `cherry-pick` | Pick only the commits the planner's work order claims | Precise; drops incidental churn (stray formatting, debug prints) that would inflate diff-churn metrics | **Silent context loss.** A picked commit can depend on an unpicked one from the same worker: the result compiles in the worker's workspace and not on trunk. Caught only by the oracle, and it looks like a bad agent rather than a bad policy. |
| `agent-composed` | An `integrator` agent reads all K branch diffs and writes the union onto trunk | Cannot produce a git conflict at all; can reconcile *semantic* overlap that git cannot see (two workers adding the same helper under different names) | **Plausible synthesis.** The integrator can silently drop a worker's change and produce a clean, wrong result. Costs integrator tokens on every run, not just on conflict. `pass_to_pass_broken` is the only detector. |

##### 3.2 `on_conflict`

| on_conflict | Behavior | Tradeoff | Failure mode |
|---|---|---|---|
| `fail` | Integration step fails; iteration ends; conflict recorded | Cheapest, and gives the **cleanest conflict-rate signal** — nothing masks it | Confounds conflict rate with solve rate: a strategy that would have solved is scored unsolved. The two must be reported as separate columns, never collapsed into one "worse". |
| `resolver-agent` | Structured conflicts -> resolver agent -> `resolve_and_merge` / `resolve_rebase_conflicts` | Rescues trials that `fail` would discard; makes high-K viable; cost is itemized into `integration_cost_usd` | Resolver picks a side and drops the other's work. Detected as elevated `pass_to_pass_broken` versus `fail` trials at the same K. |
| `human` | Trial pauses; `ConflictResolver.svelte` is the path | The realistic upper bound on resolution quality; useful as a control for "could a *good* resolver save this" | Unbounded wall-clock. Human trials are **excluded from published wall-clock and speedup figures** and flagged in the bundle, because a number that includes a human coffee break is not a measurement. |

##### 3.3 The matrix, and the two invalid cells

|  | `fail` | `resolver-agent` | `human` |
|---|---|---|---|
| `sequential-merge` | valid | valid | valid |
| `rebase-onto-trunk` | valid | valid | valid |
| `cherry-pick` | valid | valid | valid |
| `agent-composed` | valid (no-op) | **invalid** | **invalid** |

`agent-composed` never produces a git conflict, so pairing it with a conflict handler declares a resolver role that can never be exercised — and a role bound to a model that never runs shows up in `cost_by_role` as a suspicious zero. Template validation rejects these two combinations at save time (`test_agent_composed_rejects_conflict_handler`), rather than letting them into a matrix where they look like a legitimate cell that always costs nothing.

##### 3.4 Dispatch

The policy string is dispatched through one registry, not an if-chain scattered across the orchestrator:

```python
INTEGRATION_POLICIES = {
    "sequential-merge":  SequentialMergePolicy,
    "rebase-onto-trunk": RebaseOntoTrunkPolicy,
    "cherry-pick":       CherryPickPolicy,
    "agent-composed":    AgentComposedPolicy,
}

class IntegrationResult:
    merged_branches: list[str]
    final_sha: str | None
    conflicts: list[dict]        # {path, base_content, ours_content, theirs_content}
    conflicts_resolved: int
    cost_usd: Decimal            # summed from StepUsage rows with lazyaf_role="resolver"
```

An unknown policy string is a hard failure at template save time, listing the valid keys. A policy that silently fell back to `sequential-merge` would make every "which policy wins" comparison a lie, so there is no default.

---

#### 4. Tests

New tiers: `tdd/unit/benchmark/` and `tdd/integration/benchmark/`. Per R4 no test in this phase is a `pass # architecture ensures this`; anything targeting unbuilt machinery is `xfail(strict=True)`.

**tdd/unit/benchmark/test_strategy_graph_validation.py** — the executor is a DAG, and the template must respect it.

| Test | Defines Contract |
|------|------------------|
| `test_template_graph_parses_as_pipeline_graph_model` | Every catalog template validates against the real `PipelineGraphModel` — one graph schema, not two |
| `test_cyclic_template_rejected` | A template with `a -> b -> a` is rejected at save with the cycle's step ids named |
| `test_self_loop_rejected` | `a -> a` (the naive "iterate" mistake) is rejected |
| `test_conditional_branch_never_rejoins_a_join_node` | A node with both a `success` and a `failure` upstream is rejected — `_all_upstream_satisfied` would deadlock it |
| `test_edge_to_unknown_step_rejected` | Inherited from `PipelineGraphModel`, asserted here so catalog authoring gets the error |
| `test_at_most_one_gate_node` | Two `lazyaf_gate: true` steps is a validation error (two definitions of done) |
| `test_declared_roles_match_graph_roles` | Stored `roles` disagreeing with the graph's `lazyaf_role` set is rejected |
| `test_unknown_lazyaf_reserved_key_rejected` | A typo'd `lazyaf_fanount` fails loudly instead of being ignored as free-form config |

**tdd/unit/benchmark/test_role_binding.py** — a trial with an unbound role must not start.

| Test | Defines Contract |
|------|------------------|
| `test_binding_writes_runner_type_and_model_into_config` | `model_assignment["planner"]` lands as `config.model` / `config.runner_type` on every planner step |
| `test_missing_role_refuses_trial_start` | Graph has `reviewer`, assignment does not -> `unbound_role`, trial never enters `running` |
| `test_extra_role_refuses_trial_start` | Assignment has `reviewers` (typo) -> `unknown_role`, not silently ignored |
| `test_incomplete_binding_refuses_trial_start` | Assignment entry without `runner_type` -> `incomplete_binding` |
| `test_role_survives_binding_for_cost_attribution` | `lazyaf_role` is still present post-bind, so `StepUsage` -> `cost_by_role` group-by works |
| `test_determinism_recorded_on_trial` | `lazyaf_determinism` from the assignment is copied to `Trial.determinism` (provenance, not just config) |
| `test_same_model_two_roles_costs_separately` | Binding planner and worker to the same model still yields two `cost_by_role` entries |

**tdd/unit/benchmark/test_k_parameterization.py** — one template, many widths.

| Test | Defines Contract |
|------|------------------|
| `test_fanout_expands_to_k_clone_steps` | K=4 yields `worker_1..worker_4` with `lazyaf_worker_index` 1..4 |
| `test_fanout_rewrites_incoming_and_outgoing_edges` | `plan -> worker` becomes 4 edges; `worker -> integrate` becomes 4 edges; edge ids unique |
| `test_k_equals_one_is_not_special_cased` | K=1 yields exactly one `worker_1` node — a real point on the sweep |
| `test_k_out_of_declared_bounds_rejected` | K=64 against `{"max": 32}` refuses before any container starts |
| `test_two_independent_fanout_vars_expand_together` | `composed-full` at K=4, N=3 yields 4 workers and 3 reviewers, edges intact |
| `test_expanded_graph_is_valid_pipeline_graph_model` | The expansion output re-parses; property-checked over the whole catalog x K in [1,2,4,8] |
| `test_expanded_graph_is_still_a_dag` | Replication never introduces a cycle |
| `test_fanout_entry_point_expands` | A fan-out node that is an entry point yields K entry points |
| `test_residual_placeholder_is_hard_error` | An unrendered `{{ ... }}` in any config string fails expansion rather than reaching an agent as literal text |
| `test_template_variables_recorded_on_trial` | `Trial.template_variables` holds the bound K — a trial that cannot state its K is not reproducible |
| `test_clone_positions_auto_laid_out` | Clones get distinct `position.x` so the fan renders as a fan in the graph UI (R8 surface) |

**tdd/unit/benchmark/test_integration_policy_dispatch.py** — the policy is data and it dispatches exactly once.

| Test | Defines Contract |
|------|------------------|
| `test_each_policy_resolves_to_its_handler` | All four keys in `INTEGRATION_POLICIES` resolve; parametrized so a new policy cannot be added without a test |
| `test_unknown_policy_rejected_at_save` | `"policy": "yolo-merge"` fails template save listing valid keys — no silent default |
| `test_agent_composed_rejects_conflict_handler` | The two invalid matrix cells (3.3) are refused |
| `test_agent_composed_requires_integrator_role` | `agent-composed` without an `integrator` in the graph is a validation error |
| `test_sequential_merge_order_is_pinned` | Merge order is `worker_index`, deterministic across runs — an unpinned order makes conflict rate unmeasurable |
| `test_integration_result_carries_structured_conflicts` | Conflicts surface as `{path, base_content, ours_content, theirs_content}`, matching `_get_conflict_details` |
| `test_integration_cost_attributed_to_integration_cost_usd` | Resolver `StepUsage` lands in `Trial.integration_cost_usd`, not in the general pool |

**tdd/unit/benchmark/test_strategy_catalog_fixtures.py** — the shipped catalog is not allowed to rot.

| Test | Defines Contract |
|------|------------------|
| `test_every_catalog_template_expands_and_validates` | Parametrized over every JSON in `catalog/`; a broken fixture fails CI, not a trial at 2am |
| `test_one_shot_is_present_and_single_agent_step` | The control exists and is actually a control (exactly one agent step) |
| `test_catalog_slugs_unique_and_stable` | Slugs are the join key in published results; a rename is a breaking change and must be deliberate |

**tdd/integration/benchmark/test_fanout_branch_allocation.py** — real git server, real workspaces (R6: named volumes, not tmp_path).

| Test | Defines Contract |
|------|------------------|
| `test_each_worker_gets_own_branch_at_base_commit` | K branches all point at `case.base_commit_sha` before any worker runs |
| `test_workers_do_not_share_a_workspace` | K distinct workspace volumes; a file written by worker 1 is invisible to worker 2 |
| `test_join_step_waits_for_all_workers` | `integrate` does not dispatch until all K are in `completed_step_ids` |
| `test_sequential_merge_produces_single_final_sha` | Clean merge of K disjoint branches yields one `final_commit_sha` on the trial branch |
| `test_overlapping_workers_report_conflict_not_crash` | Two workers editing the same file yields `integration_conflicts >= 1` and a structured conflict list |

**tdd/integration/benchmark/test_resolver_agent_on_conflict.py** — the agent-addressable-conflict claim, end to end with the mock runner.

| Test | Defines Contract |
|------|------------------|
| `test_conflict_routes_to_resolver_via_failure_edge` | `integrate` fails -> `resolve` dispatches; `run_tests` (success tail) never runs |
| `test_resolver_receives_three_way_contents` | The resolver's `.control/conflicts.json` carries base/ours/theirs, not merge markers |
| `test_resolutions_applied_yield_clean_merge` | `resolve_and_merge` with the returned contents produces a final sha; `conflicts_resolved` increments |
| `test_resolver_failure_ends_iteration_cleanly` | A resolver that returns nothing fails the iteration without corrupting the trial branch |
| `test_resolver_cost_lands_in_integration_cost_usd` | Round-trip: resolver `StepUsage` -> `Trial.integration_cost_usd` (R3 contract, container -> API -> DB row) |

---

#### Definition of Done

- [ ] `StrategyTemplate.variables` and `Trial.template_variables` added to the models + migration; both round-trip through the API
- [ ] `StrategyTemplate.graph` validates against the real `PipelineGraphModel` on save — no second graph schema anywhere in the tree (R3)
- [ ] Reserved `lazyaf_*` config keys documented in-repo and enforced: an unknown reserved key is a save-time error
- [ ] `expand_strategy_graph()` implemented as the six documented passes; output re-parses as `PipelineGraphModel` and is DAG-checked
- [ ] Cycle rejection, self-loop rejection, and the no-rejoining-conditional-branch rule all enforced at save time with step ids in the message
- [ ] Role binding is strict both ways; a trial with an unbound or unknown role never reaches `running`
- [ ] `lazyaf_role` survives binding and `Trial.cost_by_role` is a group-by on it, verified against real `StepUsage` rows
- [ ] K-parameterization works: one `planner-fanout` template runs at K in [1, 2, 4, 8, 16] with no template edits, and each trial records its K
- [ ] All seven catalog templates (`one-shot`, `test-first`, `adversarial-review`, `planner-fanout`, `planner-fanout-resolver`, `one-shot-gated`, `composed-full`) ship as seeded JSON fixtures and pass `test_every_catalog_template_expands_and_validates`
- [ ] `lazyaf-integrate` ships in the base image; `POST /api/trials/{id}/integrate` drives `git_server.merge_branch` / `rebase_branch` server-side
- [ ] `INTEGRATION_POLICIES` registry dispatches all four policies; unknown policy is a save-time error with no fallback default
- [ ] The two invalid `(agent-composed, conflict-handler)` matrix cells are rejected at save time
- [ ] Sequential-merge order is pinned to `worker_index` and recorded, so `integration_conflicts` is reproducible
- [ ] Fan-out allocates a branch and a workspace per worker at `case.base_commit_sha`, proven on named volumes (R6)
- [ ] Conflict -> resolver-agent -> `resolve_and_merge` round-trips with structured three-way contents; resolver cost lands in `integration_cost_usd`
- [ ] Every test file named in section 4 exists and is green (or `xfail(strict=True)` against a named, unbuilt target); `tdd/skip_baseline.json` updated in the same commit (R4)
- [ ] Expanded strategy graphs render in the existing graph UI with the fan laid out as a fan, covered by a Playwright spec (R8)
- [ ] The dogfood pipeline runs one catalog template (`one-shot-gated`) against a real case, so the catalog is not dark code (R1, R7)
