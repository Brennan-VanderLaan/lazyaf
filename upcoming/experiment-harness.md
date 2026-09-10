# The Experiment Harness — how long does *this setup* take to solve *that problem*

**Status**: design, ready to brief · **Owner ask recorded**: verbatim in §0.1 · **Standing rules**: R1 nothing dark · R2 delete only after acceptance · R3 one source of truth per wire contract · R4 no fake green · R5 async-first · R8 any UI ships a Playwright spec

**Reconciles with**: `docs/milestone-13/leaderboards-and-corpus.md` (Amendment A) and the M13 body in `PLAN.md:1320-1590`. Where I differ from those, §0.4 lists every difference with the reason. Most of M13's *thinking* survives intact; a large part of its *machinery* is deferred, and §0.4 says which and why.

**A correction before anything else.** The brief that commissioned this document cites `upcoming/experiments-redesign.md`. **That file does not exist in this tree** — verified by `find`, by `git log --all --name-only`, and by a repo-wide grep for its quoted phrase "Massive forms". Its three quoted constraints are treated here as authoritative because the owner supplied them directly: (a) experiments must be able to take graphs from one repo and apply them to another; (b) "N=" for jitter; (c) *"Massive forms are not popular with **anyone**"*. If that file exists somewhere outside the tree, this document should be re-checked against it.

Every claim about the tree below was read, not remembered. Citations are `file:line` against `wip/p6-and-qol` at `84ea472`.

---

## 0. The shape of the thing

### 0.1 The ask, as given

> "the agent wants to be able to take a set of agents and lazyaf graphs for what should happen when commits land and how the agents trigger each other, and be able to point at a github repo / starting point for a problem, and measure how long it takes for that setup to 'solve' the problem. This needs to cover things from straight forward asks like 'Write a hello world application in python that I can call from bash' with a matching check, to creating full applications that satisfy a set of requirements. The researcher wants to be able to run experiments in parallel, wants to be able to check back on old results, to queue up ideas if needed, and to be able to easily share the results and how to reproduce them with others."

### 0.2 Three nouns, named once

| noun | definition | lives in |
|---|---|---|
| **ARM** | agent definitions **+** a pipeline graph **+** trigger wiring. The whole bundle is one experimental arm — the independent variable. | **git** |
| **PROBLEM** | a starting point (a github repo at a sha, or empty) **+** a statement **+** an objective definition of *solved*. | **git** |
| **TRIAL** | one arm × one problem × one repeat. The unit that produces a measured number. | **SQLite** |

Two more nouns fall out of those three, and the full mapping to the vocabulary in the ask is:

| in the ask | here | what it is |
|---|---|---|
| setup | **arm** | git |
| problem | **problem** | git |
| — | **study** | SQLite — the researcher's *question*: which arms × which problems × N |
| — | **trial** | SQLite — one arm × one problem × one repeat. The measured unit. |
| run | **attempt** | SQLite — one `PipelineRun`: either the arm's graph running, or the harness grading |
| result | *not a table* | `Trial.solved` + the trial's own columns + `StepUsage` + `TestRun`. **There is no result table**, because a materialized copy of money or outcomes is a second writer and a second writer drifts (R3, and the shipped experiment layer's decision at `backend/app/models/experiment.py:16-20`) |

Why "arm" and not the owner's word "setup": *setup* is too generic to be a column name and collides with `oracle.setup` in the M13 corpus format. *Arm* is the standard term in experimental design, this is a science platform, and it is exactly what he described — "one experimental arm". M13's `StrategyTemplate` is an arm **minus the agent files and minus the trigger wiring**; §0.4 covers the widening.

### 0.3 The one-sentence thesis

A trial is: **put the problem's start state on a branch, let the arm's graph run, let the arm's own commits re-trigger it, and stop when a harness-owned grader — which the arm cannot see, edit, or run — says solved, or when a cap trips.** Everything else in this document is the honest bookkeeping around that sentence.

### 0.4 Where this differs from M13, and why

M13 is a good design that answers a *narrower* question than the one now on the table. It assumes every problem is SWE-bench-shaped: an existing repo, a real fix commit, `fail_to_pass`/`pass_to_pass` test ids derived by splitting that commit. **Neither of the owner's two named examples is that shape.** "Write a hello world application in python" has no repo, no fix commit and no upstream suite; "a full application satisfying a set of requirements" likewise.

| # | M13 says | This document says | why |
|---|---|---|---|
| 1 | The scoring vocabulary is `fail_to_pass`/`pass_to_pass` test ids; `checks:` is an escape hatch for "goals that are not tests" (`leaderboards-and-corpus.md:327-351`). | **`checks:` is the trunk.** Every check is *a command in a container that exits 0 or non-zero*. `fail_to_pass`/`pass_to_pass` becomes a check **generator** (`from_test_ids:`), not a parallel vocabulary. | It is the only shape that spans the whole ladder. M13 invented the right primitive and filed it as the exception. §3, §4. |
| 2 | The independent variable is the **strategy** = a pipeline graph + role→model binding + loop policy. | The independent variable is the **arm** = graph + **agent definition files** + **trigger wiring**. | The owner's ask names all three ("a set of agents and lazyaf graphs for *what should happen when commits land and how the agents trigger each other*"). A graph alone cannot express "on a red CI run, the fixer agent wakes up". §2.2. |
| 3 | A trial is N *sequential pipeline runs* driven by a **Trial orchestrator** that feeds failures forward (`PLAN.md:1406-1412`). | A trial is N *attempts*, and an attempt is started by **a commit landing** on the trial branch (`trial_commit`), not by an orchestrator loop. The orchestrator only enforces caps and grades. | This is the owner's stated subject matter. It also makes the arm's trigger wiring a real variable instead of a fixed harness policy. §2.4, §5.3. |
| 4 | `Experiment`/`ExperimentRun` are reused as the matrix; a bench cell **is** a Trial (`leaderboards-and-corpus.md:1420`, `:1523`). | **New tables.** `studies` / `trials` / `trial_attempts`, additive; the shipped 12.6.5 experiment stack is untouched. | A cell is one pipeline run (`experiment_service.py:918-1044`); a trial is *many*. And M13 itself already concedes the outcome semantics cannot be reused: *"Do not reuse `experiment_service.classify_cell` for this"* (`leaderboards-and-corpus.md:1345`). Reusing the table while replacing its status rule and its cardinality is not reuse. §2.6. |
| 5 | Headline metric is solve-rate at a shared budget `B`; wall-clock is demoted and blocked on mixed machine profiles. | The owner named **wall clock** as the headline. I keep it, but split it: `wall_clock_ms` is a ranked metric **only in serial studies**; `agent_seconds` and `cost_usd` are the parallelism-robust headline. | §5.4 — this is the sharpest tension in the ask and it gets its own section. |
| 6 | Corpus format: `bench/suites/<suite>/cases/<slug>.yaml` + `patches/<slug>.*.diff` + `checks/<slug>/` — one problem spread across three directories, slug repeated in three filenames. | **One directory per problem.** `bench/problems/<set>/<slug>/{problem.yaml, checks/, requirements.md, patches/}`. | A full-application problem carries prose and a check tree. Keep M13's rule that the *path* is the identity and nothing inside restates it (`:171-184`). |
| 7 | 13.1 ships `bench case derive` (3 suite runs, fix-commit splitting), `lazyaf-oracle`, `ingest-remote`, 9 cases, a 5-check validator, before the first number. | **All of that is deferred past the first number.** The first slice is one greenfield problem, one arm, one check, one real measurement. §10. | M13's own §7 makes this argument (*"no 'first defensible number' milestone"*, `:1402`) and then still gates on the SWE-bench machinery. Greenfield problems need none of it. |
| 8 | M13 takes alembic revisions `0014`/`0015` (`:1453`). | **`0016` onward.** | Head is `0015` (`backend/alembic/versions/0015_drop_pipeline_steps.py`); `0014`/`0015` went to 12.8 P4/P6. M13's numbering note is now stale. |

**What I take from M13 unchanged, and would not want re-litigated:** the disk-is-truth / DB-is-a-projection rule and its content-hash discipline (`:495-556`); `error` ≠ `failed` in a denominator; the null-agent and gold-patch controls; refuse-to-rank over rank-the-noise; the metric envelope carrying its own provenance (`:1175-1181`); rank *bands* rather than a total order (`:899-904`); close a cheat by making it **score as failure**, never by adding a bypassable checker (`:1209`); and contamination is a disclosure, not a control (`:1364-1382`).

---

## 1. The researcher's day

Five scenarios. Everything after this section exists to make one of them work.

### S1 — 11pm, an idea, and bed

He has been reading about reviewer agents. The question: *does adding a reviewer agent to the loop make hello-world-class tasks faster, or just more expensive?*

He already has two arms committed in his `lazyaf-lab` repo — `solo` and `coder-reviewer` — and a problem set `warmup` with six small problems. He opens **Experiments → New study**, and the whole interaction is:

```
arms      [x] solo   [x] coder-reviewer          (from lazyaf-lab @ HEAD)
problems  [x] warmup (6)
N         5
run       ( ) now   (o) queued        slots: 3
                                            → 60 trials · est. $18.40 · queue position 2
[ Queue ]
```

Four controls. Everything else — budget per trial, wall-clock cap, attempt cap, which model each agent uses, what "solved" means — is already in the problem files and the arm files, in git. He clicks **Queue** and closes the laptop. The study sits at `queued` behind one already running; nothing is dispatched until a slot frees.

### S2 — morning, and one arm did not actually run

The board shows the study `complete`. `coder-reviewer` solved 28/30. `solo` solved 24/30 — but two of its trials read **ERROR — no grade manifest**, not "unsolved", and the board's headline row carries:

```
error rate 6.7% (2/30) — values shown, comparison DISABLED (> 10% is unrankable; this is under it, but n is small)
```

He clicks one errored trial: the grade step's container failed to pull `python:3.12` because his laptop was offline at 03:14. That trial is **excluded from the solve-rate denominator** and counted in the error rate — it is not a zero. This is `MEASURED_CELL_STATUSES` discipline (`backend/app/models/experiment.py:144-147`) applied to trials. He re-queues those two trials from the trial detail view; the rest are not re-run.

### S3 — the A/B, taken seriously

The gap looks real but N=5 per problem is 20-percentage-point granularity. He re-queues the same two arms on the same six problems at **N=15, run: serial**, and goes to work. Serial costs him nine hours of wall clock and buys him the only wall-clock number he is allowed to rank on (§5.4). The board now prints a paired difference with a bootstrap interval, and — because both arms landed in the same band — the verdict is `NOT SEPARABLE on wall-clock (Δ = -38s, 95% CI [-2m10s, +54s])` with both arms sharing rank 1. It *does* separate on cost: the reviewer arm is 2.1× the dollars. That is the finding.

### S4 — the benchmark run

Two weeks later he has twelve problems, four of them full-application. He runs one arm across all twelve at N=3 to get a coverage picture. This is not an A/B and the board says so: the default view is the **per-problem grid**, not an aggregate, and the aggregate row carries `DESCRIPTIVE — 12 problems, N=3; ranking requires N>=10`. He is reading it for *shape* — where does this arm fall over — not for a winner.

### S5 — "how did you get 4m12s?"

A colleague asks. He sends one URL: `http://lazyaf.local/#/experiments/studies/9f2c1a`. The colleague can read every trial, every attempt, every step's logs, and the exact grade manifest. Then he runs:

```
lazyaf exp bundle 9f2c1a -o reviewer-vs-solo.tar.zst
```

and sends the file. It contains `METHOD.md`, the arm tree at its sha, the problem tree at its sha, `results.jsonl` (one line per trial, full envelope), the pinned image digests and model identifiers, **and a `NOT-PINNED.md`** listing what could not be pinned and therefore what "reproduce" honestly means here. The stated re-run command is one line and it is tested to execute verbatim.

---

## 2. The object model

### 2.1 The split: what lives in git, what lives in SQLite

**Rule.** *Definitions* live in git. *Observations* live in SQLite. A row in SQLite that is not derivable from git is an observation; a value in git that a machine wrote is a bug.

This is M13's rule (`leaderboards-and-corpus.md:95-114`, `:583-586`) and it is the right one. It is also the direct answer to the owner's "durable and survive in VCS": an arm and a problem are files a colleague can `git clone`, and the DB tables that mirror them are a **projection with a content hash**, so a projection that has drifted is detectable rather than authoritative.

### 2.2 In git: the arm

An arm repo is any repo the researcher controls. It is **never** the problem repo (§4.3 explains why that is a security boundary, not a preference).

```
lazyaf-lab/                              # the arm repo — mirrored into LazyAF
  arms/
    solo/
      arm.yaml
      agents/
        coder.md
    coder-reviewer/
      arm.yaml
      agents/
        coder.yaml
        reviewer.yaml
      graph.yaml
  problems/                              # optional; problems may live in their own repo
    ...
```

`arms/coder-reviewer/arm.yaml`:

```yaml
schema_version: 1
description: >
  A coder agent implements; on a green build a reviewer agent reviews and may
  push a follow-up commit; the loop re-enters on every commit the arm lands.

agents:                                  # files, not DB rows
  coder:    {file: agents/coder.yaml,    model: claude-sonnet-4-6}
  reviewer: {file: agents/reviewer.yaml, model: claude-haiku-4-5}

graph: graph.yaml                        # a PipelineGraphModel, version 2

triggers:                                # THE WIRING — what happens when commits land
  - on: trial_commit
    branches: ["trial/*"]
    entry: implement                     # which graph entry point this trigger enters at
  - on: trial_commit
    branches: ["trial/*"]
    when: {author: agent, agent: coder}  # only commits the coder landed
    entry: review

caps:                                    # arm-side caps; the problem's caps also apply, lowest wins
  attempts: 8
```

`arms/coder-reviewer/graph.yaml` is a `PipelineGraphModel` **verbatim** — `steps` as a dict keyed by id, `edges` with `EdgeCondition` ∈ {success, failure, always}, `entry_points`, `version: 2` (`backend/app/schemas/pipeline.py:133`; the model at `backend/app/models/pipeline.py:52` stores exactly this JSON in `steps_graph`). This is M13's §4.1 decision — one dialect, the `strategy-catalog.md` one — and I keep it without change.

**It is deliberately NOT `.lazyaf/pipelines/*.yaml`.** That file format is the *ordered array* (`PipelineYaml.steps: list[PipelineStepYaml]`, `backend/app/schemas/lazyaf_yaml.py:115`), converted at materialization by `pipeline_yaml_to_graph` → `array_to_graph` (`:118-168`, `backend/app/schemas/pipeline.py:681`), and `array_to_graph` emits a **linear chain**: step *i* gets at most one edge to step *i+1*. **The repo-authored YAML format cannot express a fan-out, a fan-in, or a conditional branch.** Since graph shape is the owner's independent variable, an arm that could only be authored as `.lazyaf/pipelines/*.yaml` could not vary the thing under study. `graph.yaml` is loaded straight into a `PipelineGraphModel` and written to `Pipeline.steps_graph`, which is now the only definition a pipeline has (`backend/app/models/pipeline.py:43-52`).

**Agent files are `.yaml`, and the resolver will silently substitute one if you let it.** A repo-defined agent is `.lazyaf/agents/{name}.yaml` (or `.yml`), read through dulwich, and it **overrides** a platform `AgentFile` row of the same name — resolution order is documented at `backend/app/services/agent_resolver.py:5-9` and implemented at `:28-68`. Two things follow for the harness, and both are refusals:

- `_get_repo_agent` swallows a parse failure into a bare `except Exception: pass` (`agent_resolver.py:149-150`). **A malformed arm agent file therefore falls through silently to a *platform* agent of the same name** — an arm quietly substituting a different arm, which is the R1 failure mode in its purest form. The arm loader must parse and validate every agent file at load time and set `definition_error`, because the resolver will not.
- `AgentFile.name` is **globally unique, not repo-scoped** (`backend/app/models/agent_file.py:14`). Two arms that both define `coder` would collide if arms were projected into that table. **They are not.** An arm's agent files are materialized into the trial's work repo at `.lazyaf/agents/*.yaml` during trial setup, on a commit the harness makes, and **re-materialized at the start of every attempt** — the same restore discipline grading uses (§4.2 rule 1), so an agent that rewrites its own definition mid-trial does not get to keep the edit.

### 2.2.1 How an arm's graph runs against a *different* repo

This is the owner's quoted constraint — *"take graphs from one repo and apply them to another"* — and today it is **structurally impossible**, for one line:

```python
# backend/app/services/pipeline_executor.py:3089  (inside the step task)
select(Repo).where(Repo.id == pipeline.repo_id)
```

`start_pipeline` accepts a `repo` argument (`pipeline_executor.py:1910`), so its *signature* would admit repo B — but the step task re-loads the repo from `pipeline.repo_id` and clones that one anyway. `Pipeline.repo_id` is a non-nullable FK (`backend/app/models/pipeline.py:40`), trigger matching is repo-scoped (`trigger_service.py:547-549`), and workspace population is keyed on that same id (`workspace/population.py:182`). `Pipeline.is_template` exists (`models/pipeline.py:70`) but is **inert** — no apply-to-repo endpoint, no clone endpoint, no `repo_id` reassignment path anywhere. The only way to move a graph between repos today is a human copying a YAML file.

**The harness does not fix that line, and does not need to.** It uses the pattern already shipped for ad-hoc runs: **each trial creates an ephemeral `Pipeline` row owned by the trial's work repo, whose `steps_graph` is a copy of the arm's graph.** That is exactly what `agent_run.start_adhoc_agent_run` does (`backend/app/services/agent_run.py:415-534`) and what an experiment cell already does (`experiment_service.py:1009-1024`) — a hidden row named `__lazyaf_adhoc__:...`, `triggers="[]"`, cascade-deleted with its runs, filtered out of the pipelines list (`routers/pipelines.py:246-247`).

So "apply this graph to that repo" is implemented as **copy the graph onto a pipeline row that belongs to that repo**, with the `arm_content_hash` recording which graph it was a copy of. No executor change, no new cross-repo concept, and the run's repo stays exactly as unambiguous as it is today. The harness's rows use their own prefix — `__lazyaf_trial__:<trial_id[:8]>:<attempt>` — so they are distinguishable from card and playground ad-hoc rows in the run list.

### 2.3 In git: the problem

See §3 for the full format and two worked examples. Structurally:

```
lazyaf-problems/                         # may be the same repo as arms, or its own
  problems/
    warmup/
      hello-world-bash/
        problem.yaml
        checks/
          callable-from-bash.sh
    apps/
      url-shortener/
        problem.yaml
        requirements.md
        checks/
          acceptance/test_shortener.py
          smoke.sh
```

**Path is identity.** `set` is the directory under `problems/`, `slug` is the directory under that; neither is restated inside `problem.yaml`, and the loader rejects a file that carries either key. This is M13 `:171-177`, kept.

### 2.4 In SQLite: five new tables

Additive. Nothing existing changes shape. Migration `0016_experiment_harness.py`, `down_revision = '0015'`.

#### `arms` — a projection of git, never authoritative

| column | type | note |
|---|---|---|
| `id` | String(36) PK | |
| `repo_id` | String(36) FK repos.id | the **arm repo** mirror |
| `slug` | String(128) | directory name under `arms/` |
| `source_ref` | String(255) | the ref synced from |
| `source_commit_sha` | String(40) | the commit this projection was read at |
| `content_hash` | String(64) | sha256 over canonical `arm.yaml` + each agent file's bytes + `graph.yaml`. **This is the arm's identity in every result.** |
| `description` | Text | |
| `spec_json` | Text | the resolved `arm.yaml` |
| `graph_json` | Text | the resolved `PipelineGraphModel` |
| `agents_json` | Text | `{role: {file, sha256, model}}` |
| `definition_error` | Text NULL | why this arm could not be materialized; a trial on an arm carrying one is **refused**, never run against a stale definition |
| `created_at` | DateTime | |

Unique index `(repo_id, slug, content_hash)`. Editing an arm and re-syncing creates a **new row**, exactly as `PromptVersion` does today (`backend/app/models/experiment.py:319-345`); nothing is ever updated in place, so a trial's `arm_id` always resolves to the bytes that ran.

`definition_error` copies the pattern and the reasoning already on `Pipeline.definition_error` (`backend/app/models/pipeline.py:53-68`) — and deliberately does **not** copy `sync_repo_pipelines`' habit of swallowing a parse failure into a `logger.warning` and keeping the stale definition (`backend/app/services/trigger_service.py:452-460`). In CI that is annoying. In a measurement it silently substitutes the arm.

#### `problems` — a projection of git, same discipline

| column | type | note |
|---|---|---|
| `id` | String(36) PK | |
| `repo_id` | String(36) FK repos.id | the **problem-definition repo** mirror |
| `set_name`, `slug` | String(128) | from the path |
| `source_commit_sha` | String(40) | |
| `content_hash` | String(64) | sha256 over canonical `problem.yaml` + every byte under `checks/` + `requirements.md`. Excludes anything a machine observed (M13 §3.1's fix, taken whole). |
| `spec_json` | Text | |
| `start_repo_id` | String(36) NULL | the **mirror of the github repo under test**, if any. NULL for greenfield. |
| `start_commit_sha` | String(40) NULL | |
| `start_ref` | String(255) NULL | `refs/heads/bench/problem/<slug>` on the start mirror — see §3.4 |
| `validation_status` | String(16) | `unvalidated` / `valid` / `invalid` |
| `definition_error` | Text NULL | |

#### `studies` — the researcher's question

| column | type | note |
|---|---|---|
| `id` | String(36) PK | |
| `name`, `description` | String/Text | |
| `kind` | String(16) | `jitter` \| `compare` \| `benchmark` \| `matrix` — **derived and validated**, see §5.3 |
| `arm_ids` | Text (JSON list) | frozen at queue time |
| `problem_ids` | Text (JSON list) | frozen at queue time |
| `repeats` | Integer | the "N=" |
| `run_mode` | String(8) | `serial` \| `parallel` — governs whether wall-clock is rankable (§5.4) |
| `status` | String(24) | `draft` \| `queued` \| `running` \| `complete` \| `aborted` \| `budget_exhausted` |
| `queued_at`, `started_at`, `completed_at` | DateTime NULL | |
| `budget_usd` | Numeric(18,6) | hard cap for the whole study, required, > 0 |
| `budget_overrun_usd` | Numeric(18,6) | what was in flight when the cap tripped, **recorded, never absorbed** |
| `estimated_cost_usd`, `estimate_basis` | Numeric / String(24) | reuse `EstimateBasis` verbatim (`backend/app/models/experiment.py:150-163`) |
| `machine_profile` | String(64) | free-form host label; a study whose trials carry two of these cannot rank latency |
| `created_by`, `created_at` | | |

Indexes: `(status, queued_at)` — the queue scan; `(status, created_at)` — the list.

#### `trials` — one arm × one problem × one repeat. **This is the measured unit.**

| column | type | note |
|---|---|---|
| `id` | String(36) PK | |
| `study_id` | FK studies.id ON DELETE CASCADE | |
| `arm_id`, `problem_id` | String(36) | |
| `arm_content_hash`, `problem_content_hash` | String(64) | **frozen copies**, so a result survives its projection row being replaced |
| `repeat_index` | Integer | |
| `cell_index` | Integer | deterministic: `((arm_i * n_problems) + problem_i) * repeats + repeat_i` — the same formula shape the shipped matrix uses (`experiment_service.py:216-260`), so the grid renders without a four-column tuple match |
| `work_repo_id` | String(36) | the mirror this trial's branch lives in |
| `work_branch` | String(255) | `trial/<trial_id[:8]>` |
| `base_commit_sha` | String(40) | what the branch started from — the pin |
| `status` | String(24) | `queued`→`dispatching`→`running`→ one of `solved` / `unsolved` / `error` / `cancelled` / `skipped_budget` |
| `solved` | Boolean NULL | NULL until graded. **NULL is not false.** |
| `error_class` | String(48) NULL | `grade_no_manifest`, `grade_container_failed`, `arm_definition_error`, `problem_invalid`, `cap_wall_clock`, `cap_attempts`, `cap_budget`, `infra` |
| `attempts_used` | Integer | |
| `first_solved_attempt` | Integer NULL | |
| `started_at`, `solved_at`, `completed_at` | DateTime NULL | |
| `wall_clock_ms` | Integer NULL | `solved_at - started_at`; NULL when unsolved |
| `contention_peak` | Integer | max concurrent running trials observed during this trial's life. §5.4. |
| `final_commit_sha` | String(40) NULL | what was graded |

Indexes: `(study_id, cell_index)` **unique**; `(study_id, status)` — the pump's live count and the queue scan; `(problem_content_hash, arm_content_hash)` — the cross-study leaderboard (§6.4).

**Deliberately absent:** `cost_usd`, `tokens`, `tests_passed`. `StepUsage` and `TestRun` remain the only sources of truth for money and outcomes (R3). This is the shipped experiment layer's decision (`backend/app/models/experiment.py:16-20`) and it was right.

#### `trial_attempts` — one "commits landed, the graph ran" cycle

| column | type | note |
|---|---|---|
| `id` | String(36) PK | |
| `trial_id` | FK trials.id ON DELETE CASCADE | |
| `attempt_index` | Integer | 0-based |
| `kind` | String(12) | `arm` (the graph ran) \| `grade` (the harness graded) |
| `pipeline_run_id` | String(36) NULL | convenience mirror; the **durable link is `PipelineRun.trigger_ref == attempts.id`**, written at run creation |
| `trigger_commit_sha` | String(40) | the commit that caused this attempt |
| `head_commit_sha` | String(40) NULL | branch tip when the attempt ended |
| `status`, `started_at`, `completed_at` | | |

Unique `(trial_id, attempt_index)`. Index on `pipeline_run_id`.

The `trigger_ref` choice is not cosmetic. `pipeline_executor.start_pipeline` can complete a run **synchronously** (image preflight failure, empty step list), so a `pipeline_runs.trial_attempt_id` column set *after* the call could not beat that race. `trigger_type`/`trigger_ref` are written at creation. This is exactly the reasoning already recorded at `backend/app/models/experiment.py:22-29`, and it applies here unchanged.

### 2.5 What is reused wholesale, with nothing new written

| need | already shipped |
|---|---|
| run a graph, fan out, fan in | `pipeline_executor` — fan-in is native (`leaderboards-and-corpus.md:791-805`, verified against `pipeline_executor.py`) |
| a workspace per run, cloned at a pinned sha | `trigger_context.commit_sha` → `pipeline_executor.py:2725`, pinned at `:2790` and `:3832` |
| per-worker branch + workspace | migration `0012_workspaces_per_worker.py`, composite `(pipeline_run_id, worker_key)` |
| money | `StepUsage`, `experiment_metrics.observed_spend` (`:186-202`) — unknown cost contributes **zero** and lowers coverage rather than reading as cheap |
| check outcomes | `TestRef`/`TestRun` + `test_ingestion.ingest_manifest`, with worst-status-wins on duplicate ids (`backend/app/services/test_ingestion.py:136-172`) — this is a real anti-cheat and §4.4 leans on it |
| live logs, cancellation, watchdog, step JWTs | the control layer, unchanged |
| CAS-claimed dispatch under a concurrency cap, budget recomputed from observed spend before every dispatch, CAS finalization, restart resume | the shipped experiment pump — `experiment_service.py:716` (pump), `:789-792` (cap), `:798-802` (budget), `:844-858` (claim), `:1081-1161` (finalize), `:1410-1443` (resume, wired at `backend/app/main.py:123-127`) |

That last row is the single biggest reuse. **Copy the pump's shape; do not copy its outcome rule.**

### 2.6 The one thing that must NOT be reused: `classify_cell`

```python
# backend/app/services/experiment_service.py:1279-1294 (paraphrased shape)
if pipeline_run.status == CANCELLED: return "cancelled"
if success:                          return "passed"
measured = count(TestRun where pipeline_run_id == run.id)
return "failed" if measured else "error"
```

`if success: return "passed"` returns PASSED whenever the **run** succeeded, without reading a single `TestRun` row. `success` is `_check_all_steps_passed` (`pipeline_executor.py:4920-4931`), i.e. "no StepRun is FAILED", i.e. every step exited 0.

**For a benchmark that is a false solve, and it is the worst possible bug in a science platform (R4).** An arm whose agent step exits 0 while the code does not work scores as solved. M13 spotted this (`leaderboards-and-corpus.md:1345-1352`) and its conclusion is adopted verbatim: **`Trial.solved` is computed by a pure grading function over the grade run's `TestRun` rows, and by nothing else.** The attempt's own pass/fail stays what it is — a statement about whether the containers ran.

---

## 3. The problem definition format

### 3.1 The one primitive

> **A check is a command, run in a named image, in a clean container, against a fresh clone of the graded commit, with the problem's own `checks/` tree copied in over the top. It exits 0 (`passed`) or non-zero (`failed`). Not running at all is `missing`, and `missing` is never `passed`.**

That is the whole grading vocabulary. Everything on the difficulty ladder is a way of *producing* checks:

- hello-world → one check, three lines of shell
- a provided acceptance suite → one check that runs pytest, expanded to one manifest entry per test id
- an upstream repo's own suite red→green → generated from `fail_to_pass`/`pass_to_pass`
- a requirements checklist → one check per requirement
- a rubric → one check whose command is `lazyaf-judge`, and it carries a trust badge (§4)

M13 already built this primitive as its `checks:` escape hatch, with the exit-code mapping spelled out (`leaderboards-and-corpus.md:339-351`). Promoting it to the trunk costs nothing and buys the whole ladder.

### 3.2 Worked example A — hello world, complete and real

`problems/warmup/hello-world-bash/problem.yaml`:

```yaml
# set = "warmup" and slug = "hello-world-bash" are the PATH. Do not restate them.
schema_version: 1

title: Hello world, callable from bash

statement: |
  Add a Python program to this repository that prints exactly

      Hello, world!

  followed by a newline and nothing else, when run from bash as:

      python3 hello.py

  Commit it on the branch you were given.

start:
  kind: empty                 # a fresh repo with one empty commit on `main`
  default_branch: main

solved_when: [hello-prints]   # every id listed must be `passed`

checks:
  - id: hello-prints
    image: python:3.12
    network: none
    timeout: 60
    run: |
      out="$(python3 hello.py)"
      test "$out" = "Hello, world!"

caps:
  wall_clock: 20m
  budget_usd: "1.00"
  attempts: 5

contamination_risk: high      # this is in every training corpus; say so
```

**23 lines, and 12 of them are prose.** That is the bar: if defining a problem is a chore, the second one never gets written. `lazyaf exp problem lint` (container-free, runs in T1) checks the schema, that every `solved_when` id exists in `checks`, that `caps` are present, and that `contamination_risk` is stated.

`checks/` is empty here because the check is inline. A check may be `run:` (inline shell) **or** `script: checks/foo.sh` (a file in the problem tree). Inline is the on-ramp; files are for anything longer than five lines.

### 3.3 Worked example B — a full application

`problems/apps/url-shortener/problem.yaml`:

```yaml
schema_version: 1

title: A URL shortener service satisfying the stated requirements

statement_file: requirements.md      # 60 lines of prose the agent reads

start:
  kind: repo
  source_url:      https://github.com/lazyaf-bench/py-service-skeleton
  commit_sha:      3f9a1c2e5b7d4a8f0c1e6b2d9a4f7c3e5d8b1a06
  license:         MIT
  default_branch:  main

solved_when:
  all_of: [builds, smoke, acceptance/*]      # glob over generated ids
  # NOT included: rubric/*. See `advisory` below and §4.5.

checks:
  # --- T0: a command that exits 0 -----------------------------------------
  - id: builds
    image: python:3.12
    network: allowed                          # pip install; recorded as provenance
    timeout: 300
    run: |
      pip install -e . && python -c "import shortener"

  - id: smoke
    image: python:3.12
    network: none
    timeout: 120
    script: checks/smoke.sh                   # starts the service, curls it, diffs

  # --- T2: an author-written acceptance suite, frozen ----------------------
  - id: acceptance
    kind: pytest                              # expands to ONE ID PER TEST
    image: lazyaf-test-runner:dev
    network: none
    timeout: 900
    tests_from: checks/acceptance/            # copied in at grade time, over the top
    setup: "pip install -e . && pip install -r checks/acceptance/requirements.txt"
    command: "pytest -q checks/acceptance"
    # -> acceptance/test_shortener.py::test_shorten_returns_7_char_slug, ...

  # --- T5: a judged rubric. ADVISORY. Never in solved_when by default. -----
  - id: rubric
    kind: judge
    advisory: true                            # required; a judge check without it is a lint error
    image: lazyaf-base:dev
    model: claude-sonnet-4-6
    rubric_file: checks/rubric.md
    # -> rubric/error-handling, rubric/readme-accuracy, rubric/no-dead-code

caps:
  wall_clock: 4h
  budget_usd: "25.00"
  attempts: 20

vertical: web-api
complexity: medium
contamination_risk: low       # skeleton repo authored for this corpus; requirements are novel
```

`requirements.md` is the requirements document — the thing the agent is graded against in prose, and the thing `checks/acceptance/` is the *executable rendering of*. `checks/rubric.md` is what the judge reads.

**The honest cost, stated.** `checks/acceptance/` is roughly a day of work for a real application. There is no way around that and this document will not pretend otherwise: *an objective definition of "a full application satisfying a set of requirements" is an acceptance suite, and somebody has to write it.* What the harness can do is make it write-once (the suite is the problem, forever), make it live in git next to the requirements it renders, and make the easy end of the ladder genuinely free so the corpus can start.

### 3.4 The starting point, and the `bench/problem/<slug>` ref

For `kind: repo`, the github repo is fetched into LazyAF's internal git server at the pinned sha, and `refs/heads/bench/problem/<slug>` is created pointing at it. **Reachability then becomes true by construction**, which matters because the shipped clone helper clones `--branch <branch>` first and only then detaches — a sha not reachable from the cloned branch fails inside a helper container with a bare git error. M13 found this (`leaderboards-and-corpus.md:140-149`) and the fix is taken whole.

For `kind: empty`, LazyAF creates a repo with a single empty commit on the default branch. This is the greenfield path and it needs no ingest at all — which is why the first slice can ship before `ingest-remote` exists.

**`POST /api/repos/ingest-remote` does not exist today.** The shipped ingest takes a local path (`backend/app/routers/repos.py`), and there is no clone-from-remote-URL path anywhere. M13 already names it as a 13.1 backend deliverable (`:493-501`); this document agrees and defers it past the first number, because greenfield problems do not need it.

### 3.5 Problem validation

`lazyaf exp problem validate <set>/<slug>` writes a `problem_validations` row and mirrors the newest onto `problems.validation_status`. **The scheduler refuses to queue a trial on a problem that is not `valid`.** That refusal is the best structural idea in M13's corpus design (`:405-412`) and it is kept exactly.

The battery:

| # | check | invalid when |
|---|---|---|
| 1 | **start state** — grade the start commit with no agent involved | any `solved_when` id is `passed`. *A problem that is already solved at the start is broken, and every arm would score 100%.* |
| 2 | **flake screen** — repeat (1) three times | any id's status differs across runs → the case is invalid and the differing ids are **printed as a suggested diff for the author to commit**, never written back to the corpus by the machine (M13 `:583-586`) |
| 3 | **reference** — if `reference/` exists, apply it and re-grade | not every `solved_when` id passes |
| 4 | **check closure** — the `checks/` tree is non-empty and every `script:`/`tests_from:` path exists | a declared path does not exist |
| 5 | **metadata** | `license` (for `kind: repo`), `contamination_risk` or `caps` absent |
| 6 | **goal non-empty** | `solved_when` resolves to zero ids, or names an id that is `advisory: true` |
| 7 | **manifest floor** | a validation grade run produced **no** manifest → `error:grade_no_manifest`, never "solved nothing" |
| 8 | **ref reachability** (`kind: repo`) | `refs/heads/bench/problem/<slug>` does not resolve to `commit_sha` |

Checks 5 and 6 plus schema are `lint` — container-free, so they run in T1 and join the dogfood pipeline. A malformed problem then breaks CI the day it lands. This is the cheapest quality gate available and M13 is right to insist on it (`:428-430`).

---

## 4. Grading

### 4.1 The ladder, with a trust level for each

| tier | mechanism | authoring cost | trust | cheatable by an agent that notices it is being graded? |
|---|---|---|---|---|
| **T0** | a command that exits 0 (`run:` / `script:`) | minutes | **HIGH** | Only by making the *real thing* true. The command is restored from the problem tree at grade time. |
| **T1** | golden output (`expect_stdout:` sugar over T0) | minutes | **HIGH** | As T0. |
| **T2** | an author-written acceptance suite frozen in the problem (`tests_from:`) | hours to a day | **HIGH** | No — the suite is copied in from the problem tree over whatever is in the workspace. |
| **T3** | the upstream repo's own suite, red→green (`from_test_ids:`) | high (needs `derive`) | **HIGH for the guard half, MEDIUM for the target half** | The `pass_to_pass` guard half is robust. The target half needs the test to exist and be red at start, which is what M13's `test_patch` is for. Deferred; §10. |
| **T4** | a requirement checklist, each item a T0/T2 check | proportional to the requirements | **HIGH** | No, but partial-credit temptation is real — see §4.6. |
| **T5** | a rubric graded by a judge model | minutes | **LOW** | **Yes, and also by accident.** Prompt-injectable from the repo contents; non-deterministic; drifts with the judge model. |

**The recommendation to a problem author, in one line:** *start at T0; move to T2 the moment one command stops being enough; use T5 only to describe a result you have already established some other way, and never to establish one.*

### 4.2 The anti-cheat position: three structural rules, no checkers

M13 states the principle correctly and it is adopted: **close a cheat by making it score as failure, not by adding a checker that can be bypassed** (`leaderboards-and-corpus.md:1209`). Three rules follow. All three are structural; none is a scanner that has to stay ahead of an adversary.

#### Rule 1 — the grader never runs in the agent's workspace

Grading is its own pipeline run (`kind: grade`), on its own workspace volume, and it does this in order:

1. clone the trial's work branch at `final_commit_sha`, detached;
2. **`rm -rf` every path the problem's check closure declares, then copy the problem's `checks/` tree in from the problem repo mirror** — not from the workspace, not from the trial branch;
3. run each check, one manifest entry per id;
4. POST the manifest through the existing control-layer channel, so it lands as `TestRun` rows.

The agent cannot edit what it never had. Note that this is *stronger and simpler* than M13's `git checkout <base_commit_sha> -- <oracle_paths>` restore (`:1281-1283`): for a greenfield problem there is no base to restore from, so "restore from the base commit" has no meaning. "Materialize the check closure from the problem definition, always, for every tier" is one rule that covers the whole ladder, and M13's restore is the special case of it where the closure happens to be tracked upstream.

**The `conftest.py` hole M13 found is closed by construction here** (`:1263-1275`): an autouse fixture in a newly added `conftest.py` that stubs the behaviour under test does not survive step 2, because the closure is a *whitelist of paths the problem owns*, not a blacklist of files it noticed changing.

#### Rule 2 — the grade step is not in the arm's graph, and the arm cannot see it

The arm's graph is authored by the researcher and **is the variable**. If it could contain the scorer, the variable would include the measurement. So:

- `TestRun` gains `origin: "trial" | "agent_feedback"`. Only manifests from a `kind: grade` attempt ingest as `origin="trial"`; everything a step in the arm's graph posts ingests as `agent_feedback` and is **excluded from grading**.
- `Trial.solved` is computed by `bench_grade.score(solved_when, advisory, observed) -> Verdict`, a pure function with no DB, over `origin="trial"` rows keyed on the grade attempt's `step_run_id`.
- An arm graph is *encouraged* to run the problem's checks for feedback — that is how the agent learns what is red — and those rows show up in the UI labelled as feedback. They never score.

This is M13 §6.1 (`:1239-1254`) and it is right. The reason it matters more here than there: M13's strategy is a graph the *benchmark author* ships; an arm is a graph the *subject of the experiment* ships. The blast radius is larger.

#### Rule 3 — the problem repo can never replace the arm

**This is the finding this document would most regret omitting**, and it is the harness's version of the hazard that gates shadow CI.

`on_push` (`backend/app/services/trigger_service.py:422`) does two things in order, and the first is the problem: it calls `sync_repo_pipelines` **before** matching (`:447-460`), which reads `.lazyaf/pipelines/*.yaml` at the incoming commit and overwrites the materialized pipeline's description, steps **and triggers**. Trigger matching then runs immediately in the same call, and a sync failure is caught into `logger.error` and matching proceeds anyway (`:458-460`).

`upcoming/shadow-ci.md:145` states the security consequence for mirrored repos:

> "mirror someone else's repo, route the landed commit through `on_push`, and **any commit that repo's authors land is arbitrary code execution on the owner's machine with the owner's API key**"

The harness inherits that hazard whole — a problem's start repo is a github repo, i.e. somebody else's code — **and adds a second one that is unique to it and is a measurement bug rather than a security bug**:

> **The agent under test can write `.lazyaf/pipelines/*.yaml` and commit it.** Under `on_push` semantics, that commit rewrites the pipeline definition *before* the next attempt is matched. The arm being measured can therefore replace itself, mid-trial, with a different arm — or with one step that exits 0. Every number downstream is then decorative, and nothing in the run list looks unusual.

So, three refusals, all structural:

1. **A new trigger type `trial_commit`**, in `ADHOC_TRIGGER_TYPES` alongside the existing `"experiment"` (`backend/app/schemas/pipeline.py:566-567`) — meaning it is settable **only** by the internal harness path and refused on the public run endpoint. A new `TriggerService.on_trial_commit()` that does trigger matching **only** and never calls `sync_repo_pipelines`.
2. **A `Repo.mode` column** (`normal` | `bench`). `on_push` returns immediately for a `bench` repo, with a recorded, visible reason. Problem mirrors and trial working repos are always `bench`. A single guard at the top of one function; nothing else has to remember.
3. **Rejected, for the same reasons shadow CI rejected them** (`upcoming/shadow-ci.md:152`): a `sync_definitions=False` flag on `on_push` ("a boolean that disables the dangerous half of a function is the kind of default the next refactor flips back"), and sanitising the incoming yaml by allowlisting step types ("an allowlist that must stay ahead of every future step type is a losing race").

**Consequence the owner should hear now, and it is the same one shadow CI states** (`:154`): **an arm cannot be authored inside the problem repo.** Arms live in a repo the researcher controls. If a future change assumes otherwise, this decision gets quietly reversed and both the RCE and the self-replacing-arm bug come back.

**Why `Repo.mode` and not the gate that already exists.** `sync_repo_pipelines` does have a guard — it returns early unless the pushed branch equals `repo.default_branch` (`trigger_service.py:280-281`), on the reasonable theory that "the CI definition follows the trunk". That is not a boundary the harness can lean on, because `default_branch` is a **mutable string that two unrelated code paths write automatically**: `_adopt_pushed_default_branch` runs on every receive-pack (`backend/app/routers/git.py:199`, landed in `c0a0b87`), and the repos router writes it too. `upcoming/shadow-ci.md:37` puts it exactly right — *"A security boundary whose enforcement is 'a string field happens not to be equal to another string field' is not a boundary."* `Repo.mode` is a value nothing infers and nothing adopts.

**And one more the arm loader must catch, because nothing downstream will.** `upsert_materialized_pipeline` records a refused conversion in `definition_error` and **leaves the previous graph in place** (`trigger_service.py:130-147`), while `sync_repo_pipelines` keeps the previous definition *and* its triggers for an empty or unparseable file (`:363-390`). In CI those are humane defaults. In a measurement they mean *the arm that ran is not the arm in the file and nothing said so*. The arm loader refuses instead: a malformed `arm.yaml`, `graph.yaml` or agent file sets `definition_error`, and a trial on an arm carrying one is refused at queue time with `error_class=arm_definition_error`.

There is also a mundane version of the same hazard already documented in the shipped code and worth carrying forward: `Experiment.push_branches` defaults to **False** precisely because "a push-triggered pipeline that declares no `branches:` pattern matches EVERY branch" (`backend/app/models/experiment.py:207-213`, and the matching code at `trigger_service.py:478-479`). The harness *needs* pushes — commits landing is the whole subject — so it cannot dodge this the way the shipped experiment layer does. `on_trial_commit` matching only `trial/*` on `bench`-mode repos is what replaces that dodge.

### 4.3 Two controls, cheap, mandatory

Both are arms, not special code paths:

- **`null-arm`** — an arm whose graph is one step that commits nothing. It must score **0/N solved on every problem.** If it ever scores above zero, the grader is measuring something other than the work, and **the board refuses to render at all** and names the problem.
- **`reference-arm`** — an arm whose one step applies the problem's `reference/` patch, where one exists. It must score **N/N**. If it does not, the problem is broken, not the arm.

M13 requires both (`:940-943`, `:1073`). They cost one graph file each and they catch the class of instrument bug a reviewer will not.

### 4.4 One cheat that is already closed, and must be pinned

An agent that adds a *new* passing test carrying a check id in a different file leaves the problem's own files untouched — but `_aggregate_results` collapses duplicate ids **worst-status-wins** (`backend/app/services/test_ingestion.py:136-172`, and the docstring says why: "R4 fake green"). A duplicate id can never turn a red into a green. That existing rule is the real defence, it predates this design, and it needs a named test asserting it so nobody "optimises" it away — M13 says the same (`:1288-1293`).

### 4.5 The judge, and why it is never in `solved_when`

A judge check is a real check — a command in a container that exits 0 or non-zero — so it needs no new plumbing. What it needs is a refusal:

- `kind: judge` **requires** `advisory: true`. A judge check without it is a **lint error**, not a warning.
- `solved_when` naming an advisory id is a **validation failure** (§3.5 check 6).
- Judge results are reported in their own block, labelled `TRUST: judged — not part of solved`, with the judge model and its version stamped.

An author who genuinely wants a judged gate must edit the problem to drop `advisory`, which the lint refuses — i.e. **it is not currently possible, on purpose.** If the owner later wants it, the honest form is a fourth trial outcome (`solved` / `unsolved` / `judged-solved` / `error`) that the board never pools with `solved`. I recommend against building it until a real problem demands it.

**Why this hard a line.** A judge reads the repository. The repository is written by the agent being judged. That is prompt injection with the attacker holding the pen, and the failure mode is not a crash — it is a plausible number. R1 and R4 both point the same way.

### 4.6 Partial credit

Recorded, never headline. `Trial` stores the full observed map, so "3 of 5 acceptance ids passed" is queryable and rendered in the trial detail. `solved` stays binary because the board needs one honest number, and a partial-credit headline lets an arm that never finishes anything out-rank one that finishes most things. This matches the owner's own recorded position (`PLAN.md:1566-1568`).

### 4.7 The honest limits of this grading design

Stated rather than papered over:

1. **`network: allowed` is a hole and it is sometimes unavoidable.** A check that installs packages can be satisfied by an agent that vendors a stub, poisons a local index, or shims `sitecustomize.py`. `network` is recorded per check and surfaced in the report; a problem whose `solved_when` includes any `network: allowed` check carries `TRUST: network-dependent` on every board row. There is no cheap fix; a proxy/cache is the eventual one.
2. **`error` as a check status cannot currently be produced.** The manifest contract admits exactly three statuses (`images/base/control/run.py:141-149`) and the pytest plugin maps setup/teardown errors to `"failed"` (`runner-common/runner_common/pytest_lazyaf.py:49-50`). So a broken environment on a guard check reads as a regression the agent caused. M13 found this (`:1354-1362`); it stays a disclosed limitation, and `test_ingestion` already ranks a future `error` defensively so widening later costs nothing.
3. **The pytest plugin records nothing without a marker.** `if test_id is None: return  # unannotated tests are never recorded` (`runner-common/runner_common/pytest_lazyaf.py:163`) and `_write_manifest` returns early when nothing was recorded (`:210-213`). For **author-written** acceptance suites (T2) this is fine — the author adds markers, or the harness's `kind: pytest` expander runs pytest with `-p runner_common.pytest_lazyaf` and a nodeid id mode. For **upstream** suites (T3) it is the blocker M13 named, and `LAZYAF_TEST_ID_MODE=nodeid` is its fix (`:305-319`). T3 is deferred; when it lands, that fix lands with it.
4. **A grade run that produced no manifest is `error`, never `unsolved`.** This is the single most important line in the whole design and it needs its own named test. The current path produces exactly the wrong answer: no annotated test → no manifest → no POST → `ingest_manifest` returns early on empty results → zero `TestRun` rows → "solved nothing". The grader must therefore **guarantee a manifest**: every declared check id absent from the collected results is emitted explicitly, and a collection failure sets `error_class=grade_no_manifest` rather than emitting an empty manifest that reads as all-missing. M13 §2.7 step 7 (`:456-465`) is the same requirement and the same reasoning.

---

## 5. Scheduling and parallelism

### 5.1 The queue

One table, one pump, no new infrastructure. There is no Redis and no external queue, and this design does not want one.

- A study is created with `status: 'queued'` (or `'draft'`, if the researcher is still composing). "Queue up an idea at 11pm" is literally this row.
- **One process-global pump**, driven by exactly three events — a study being queued, a trial completing, and backend startup — with an `asyncio.Lock` per study and a re-pump flag for the caller who finds it held. This is the shipped pump's shape verbatim (`experiment_service.py:716`, `:744-757`) and it is proven; do not invent a second one.
- **Claiming is a CAS**: `UPDATE trials SET status='dispatching' WHERE id=:id AND status='queued'`, and `rowcount == 1` decides the winner (`experiment_service.py:844-858`). No in-memory claim set.
- **The live count is a `SELECT count(*)`, never a counter** (`experiment_service.py:815-827`). A counter does not survive a restart; the database does.
- **Budget is observed, recomputed before every dispatch**, from `StepUsage` (`experiment_service.py:798-802` + `experiment_metrics.observed_spend`). The cap bounds *dispatch*, so the maximum overshoot is whatever is in flight — and that overshoot is **recorded** in `budget_overrun_usd`, not absorbed.
- **Restart resume already exists in the right shape**: `resume_stalled_experiments()` (`experiment_service.py:1410-1443`) is wired into the lifespan at `backend/app/main.py:123-127`. The harness gets `resume_stalled_studies()` alongside it, and a `POST /api/exp/studies/{id}/resume` as the guaranteed manual path.
- **Skips are data, not log lines.** Terminal trial statuses include `skipped_budget` and `cancelled` as first-class values, for the reason `upcoming/shadow-ci.md:173` gives: *"Why didn't my repo build" must be answerable without reading logs.*

### 5.2 The concurrency ceiling, honestly

The ask says "run experiments in parallel". Start from the fact that shapes everything else:

> **There is no global bound on concurrent steps or containers anywhere in LazyAF.** `_dispatch_step_run` calls `_spawn_task` — an `asyncio.create_task` — per dispatched step, unconditionally (`backend/app/services/pipeline_executor.py:2424-2436`, registry at `:1066`, spawn at `:1174-1192`). There is no semaphore, no worker pool, no queue in that path, and `Settings` (`backend/app/config.py:310-364`) has no such field. **N pipeline runs × M parallel entry steps = N×M containers on the Docker socket, simultaneously, with nothing in between.**

So what actually bounds parallelism, innermost first:

| # | governor | value today | citation |
|---|---|---|---|
| 1 | **Docker host CPU/RAM** | whatever the box is | the real physical wall; **nothing in LazyAF models it, and nothing backs off** |
| 2 | **Per-model-endpoint concurrency** — the only real backpressure that exists | `max_concurrency`, **default 1** | `backend/app/models/model_endpoint.py:224-226`; DB-CAS admission gate `backend/app/services/model_endpoints/scheduler.py:181-224`, called before the container starts (`pipeline_executor.py:1503-1559`); plus an `asyncio.Semaphore` on the proxy path (`backend/app/routers/model_endpoints.py:738-754`). **Applies only to `openai-harness` steps against a `direct`/`proxy` endpoint** — it is deliberately skipped for `reach=runner-local` because two gates that can block each other is a deadlock (`scheduler.py:44`, `:247`) |
| 3 | **Per runner-agent process** | `MAX_CONCURRENT_STEPS = 1` | `runner-agent/lazyaf_runner/session.py:56` — remote path only |
| 4 | **Provider rate limits** for hosted models | not modelled | a 429 surfaces as a step failure, i.e. as `error`, i.e. it is visible — but it is not scheduled around |
| 5 | **Single-worker uvicorn** | `WEB_CONCURRENCY=1`, warned at startup if not | `backend/app/main.py:60-76` — the runner registry, the dispatcher **and the trigger deduplicator** are per-process |
| 6 | **SQLite, one writer** | | every CAS, every step callback, every usage POST serialises |
| 7 | **The study's own cap** | shipped analogue: `EXPERIMENT_MAX_CONCURRENCY = 8`, default `2`, enforced at `experiment_service.py:789-792` | `backend/app/models/experiment.py:82`, `:85` |

Row 7 is the only one the harness controls, and rows 1–4 are why it matters: **the harness's slot cap is the entire backpressure mechanism for the box.** A study that dispatches eight trials, each of which fans out to four workers, puts thirty-two containers on the socket and there is nothing underneath to catch it.

**The recommendation, and it is a change from the shipped shape.** A *per-study* cap composes badly: three queued studies at 8 is 24 trials in flight, each holding a workspace volume, one or more containers, and an endpoint slot. Introduce **one global governor**:

```
LAZYAF_TRIAL_SLOTS   default 3
```

on `Settings`, enforced in the pump as a count over `trials` in `dispatching|running` **across all studies**. A study may lower its own effective parallelism but never raise it above the global. Studies beyond the slot budget stay `queued`, which is exactly the "queue up ideas" behaviour the ask wants — the queue is not a separate feature, it is what the slot cap produces.

**A slot is a trial, and a trial is not one container.** An arm that fans out to K workers puts K containers on the socket from one slot. Until row 1 of the table above has any real answer, the honest mitigation is arithmetic made visible: the arm loader computes `max_parallel_steps` from the graph (the widest reachable fan-out) and stores it on `arms`; the queue-time estimate shows `slots × max_parallel_steps` as **peak containers**, and refuses to queue when that product exceeds `LAZYAF_MAX_TRIAL_CONTAINERS` (default 12). This is a crude bound and it is stated as one — it is a static width, not a runtime count — but "refuse with a number" beats "discover it when the Docker daemon stops responding".

**Realistic numbers to set expectations, on a single developer box** — these are estimates from the mechanism, not measurements, and should be replaced by measured ones once the first slice runs: with hosted models and serial arms, 3–6 concurrent trials before the Docker host is the binding constraint. With a **self-hosted endpoint at `max_concurrency: 1`, the ceiling is 1** regardless of `LAZYAF_TRIAL_SLOTS`; trials will queue at the admission gate and their wall clock will include that wait, up to `ENDPOINT_WAIT_TIMEOUT = 900` (`backend/app/services/model_endpoints/scheduler.py:64`) before the step fails. That case deserves a UI warning at queue time, because otherwise the researcher reads queueing delay as agent latency and publishes it.

### 5.3 The three shapes, expressed once and not conflated

All three are the same cross product — arms × problems × N. Storing them three ways would be three sources of truth for one contract (R3). Storing them one way and *rendering* them one way conflates the researcher's three different questions, which the ask explicitly separates.

The resolution: **one table, one derived-and-validated `kind`, three default views and three different statistical postures.**

| `kind` | shape | validation at queue time | default view | what the board will and will not say |
|---|---|---|---|---|
| `jitter` | 1 arm × 1 problem × N | requires N ≥ 3 | **the run strip**: N dots, each a trial, plus the spread | reports median + full spread + min/max. **Never** a comparison — there is nothing to compare to. This is the owner's "N= for jitter". |
| `compare` | ≥2 arms × 1..k problems × N | requires ≥2 arms | **the paired table**, one row per arm | paired differences with intervals; rank *bands*; `NOT SEPARABLE` where the interval contains zero; disabled entirely below N=3 |
| `benchmark` | 1 arm × ≥5 problems × N | requires ≥5 problems | **the per-problem grid** | per-problem values always; an aggregate row marked `DESCRIPTIVE` below N=10 |
| `matrix` | ≥2 arms × ≥5 problems × N | — | the grid, arms as columns | everything above, plus the split views |

`kind` is computed from the selection and **stored**, so a board rendered a year later still knows what question was asked. A selection that satisfies none of the shapes (e.g. 1 arm × 3 problems × N=1) is accepted as `matrix` with `PILOT` stamped on every number and ranking structurally disabled — it is a legitimate thing to run, it is just not a result.

### 5.4 Wall clock vs parallelism — the tension in the ask, named

The owner named **wall clock** as the headline and **parallelism** as a requirement. **On one Docker host you cannot have both.** Two trials competing for CPU, for the Docker daemon, and above all for a self-hosted endpoint at `max_concurrency: 1` do not produce comparable wall-clock numbers. A harness that reports wall clock from a parallel study and calls it a measurement is producing "a plausible-looking number", which is exactly R1's failure mode.

**Recommendation, in three parts:**

1. **`Study.run_mode`.** `serial` runs one trial at a time regardless of `LAZYAF_TRIAL_SLOTS`. **Wall-clock is a rankable metric only in `serial` studies.** In a `parallel` study it is still recorded, still shown, and carries `TRUST: contended — not comparable` on every row.
2. **`Trial.contention_peak`** — the maximum number of concurrently `running` trials observed during this trial's lifetime, sampled by the pump on every wake. The board **refuses to compare wall clock across arms whose trials ran at different contention**, and prints the two values rather than a difference. Same posture as M13's `machine_profile` block (`:1077`), applied to the thing that actually varies on one box.
3. **Make `agent_seconds` the parallelism-robust headline** — the sum of `StepRun` durations across the trial, which is invariant to how many trials shared the host. Report it *beside* wall clock, always, with cost. The three together are the honest answer to "how long did it take": *4m12s of wall clock, 6m40s of agent time across 3 attempts, $0.41.*

The UI must therefore say, at queue time, in one line: **"serial: 9h, wall clock is a result · parallel: 3h, wall clock is descriptive"**. That is the trade, made visible at the moment it is chosen.

### 5.5 The attempt loop, and why caps are structural

An arm's graph can push commits. A `trial_commit` trigger fires the graph again. **That is a loop with no natural termination**, and it is the owner's stated subject matter, so it cannot be designed away.

**It is also, precisely, the loop the platform already has a guard against — and the harness turns that guard off on purpose.** `resolve_agent_work_branch` (`backend/app/services/pipeline_executor.py:299-353`) exists for exactly this, and its docstring is the clearest statement of the hazard anywhere in the tree (`:306-315`):

> "THE LOOP THIS EXISTS TO STOP. Before this, an agent step that named no branch fell through to the run's base branch — which for a push-triggered run is the branch that was just pushed. The step then committed and PUSHED there, the push fired the same push trigger, and the pipeline re-ran itself with a real provider bill attached to every lap. Nothing in the loop was rate-limited or depth-capped: it stopped when the budget did."

The rule it enforces (`:317-318`): **only an explicit `branch:` in a step's config may resolve to the run's trigger/base branch.** An agent step that names nothing gets an isolated `lazyaf/agent-<8 hex>` branch derived from its StepRun id (`:353`) and its commits go nowhere the trigger can see.

Two consequences the arm author must know, and the arm loader should enforce:

1. **An arm step that is meant to land work on the trial branch must set `branch:` explicitly.** An arm whose graph names no branch anywhere will run, commit to a throwaway branch, be graded on an unchanged trial branch, and score `unsolved` — correctly, and confusingly. `lint` should warn when an arm's graph contains a committing agent step with no `branch:` and no `work_branch` in context.
2. **The harness supplies exactly what the guard withholds, and replaces it with something bounded.** The guard's own complaint is that "nothing in the loop was rate-limited or depth-capped: it stopped when the budget did". The harness's caps are that depth cap, and they are why re-enabling the loop is acceptable here and nowhere else.

The loop is bounded by three caps, evaluated in the orchestrator, not in the arm:

| cap | source | on trip |
|---|---|---|
| `attempts` | `min(problem.caps.attempts, arm.caps.attempts)` | `status=unsolved`, `error_class=cap_attempts` |
| `wall_clock` | `problem.caps.wall_clock` | `status=unsolved`, `error_class=cap_wall_clock`, in-flight attempt cancelled |
| `budget_usd` | `min(problem.caps.budget_usd, study remaining)` | `status=unsolved`, `error_class=cap_budget` |

**Precedence, stated once:** `study override > arm caps > problem caps`, lowest wins for each field independently, and the resolved values are persisted on the trial as `effective_caps`. M13 found four unordered cap sources and no precedence rule (`:1105-1121`); this is that rule, written down before there are results to invalidate.

**A trial stopped by a cap is censored data, not a zero.** It means "did not solve within these caps", and the board must say that rather than folding it into a solve-rate as if it were a failure to solve. M13's censoring discipline (`PLAN.md:1476-1487`) applies unchanged.

**Attempt idempotence must be durable, and today's mechanism is not.** `TriggerDeduplicator` is a process-local plain dict (`backend/app/services/workspace/trigger_dedup.py:69-71`) with a 10-second window (`trigger_service.py:40`) and 600-second eviction (`:44`). It answers "two push events arrived for one sha in two seconds" well; it cannot survive a restart, and it cannot answer "have we already run an attempt for this commit". So the harness adds an **attempt claim**: `trial_attempts` carries a unique index on `(trial_id, trigger_commit_sha, kind)` and the row is inserted **before** the run starts, using the insert / IntegrityError / rollback / re-read pattern already proven at `StepExecution.execution_key` (`backend/app/services/execution/idempotency.py`, unique index at `backend/app/models/pipeline.py:163`). **The claim is not auto-released on failure** — for the reason `upcoming/shadow-ci.md:171` gives: prefer "ran zero times, visibly stuck" over "ran a hundred times". Stuck claims are swept at startup by `resume_stalled_studies()`, the same shape as `recover_orphaned_executions` (`backend/app/main.py:94`).

**Grading cadence.** Grading after every attempt is expensive; grading only at the end loses `first_solved_attempt` and the iteration curve. Default: **grade after every attempt**, because a check that costs a container and 60 seconds is cheap next to an agent step, and because it is what lets the trial *stop early* the moment it is solved — which is the number being measured. A problem whose checks are expensive can set `grade_every: N`, and the trial then records `first_solved_attempt` as a range, and says so.

---

## 6. Results, comparison and leaderboards

### 6.1 What is recorded per trial, and why each one is needed for the headline to mean anything

| field | source | why the wall-clock number is meaningless without it |
|---|---|---|
| `solved` | `bench_grade.score` over `origin="trial"` TestRuns | it is the denominator's gate |
| `wall_clock_ms` | `solved_at - started_at` | the headline |
| `agent_seconds` | Σ StepRun durations | the parallelism-robust twin (§5.4) |
| `cost_usd`, `cost_coverage` | `StepUsage` via `observed_spend` / `cost_coverage` | a faster arm that costs 4× is a different trade, not a better result. Coverage < 0.9 makes the dollars a **lower bound** and it is labelled as such (`experiment_metrics.py:205-216`, `:367-371`) |
| `tokens_in/out` | `StepUsage` | the only thing comparable across providers |
| `attempts_used`, `first_solved_attempt` | `trial_attempts` | whether the loop converges or thrashes |
| `commits` | git lineage on the work branch | churn; also the honest denominator for "how many times did the agent trigger itself" |
| `contention_peak` | pump sampling | §5.4 |
| `error_class` | orchestrator | separates *did not solve* from *was never measured* |
| `arm_content_hash`, `problem_content_hash` | frozen at start | the result's identity |

### 6.2 What the board refuses to do

Inherited from M13's consolidated refusal list (`:1069-1084`), narrowed to what this harness can actually check today:

| refusal | trigger |
|---|---|
| **refuses to render at all** | `null-arm` scored above 0 on any problem in scope — and it names the problem |
| `BLOCKED` | any problem in scope is not `valid` |
| `BLOCKED` | more than one `problem_content_hash` for one slug — the problem was edited mid-study |
| `BLOCKED` | mixed `machine_profile`, for any latency metric |
| `BLOCKED` | mixed effective caps (prints the values) |
| `TRUST: contended` | wall clock in a `parallel` study, or across differing `contention_peak` |
| `TRUST: network-dependent` | any `solved_when` check declares `network: allowed` |
| `UNRELIABLE — not ranked` | error rate > 10% (`experiment_metrics.MAX_ERROR_RATE`, `:66-70`) |
| `lower bound` on every dollar | `cost_coverage < 0.9` |
| `PILOT — not ranked` | N < 3, or fewer problems than the `kind` requires |
| `NOT SEPARABLE` | paired-difference interval contains zero |
| `DESCRIPTIVE` | per-problem verdicts below N=10 |

And the presentation rule that makes all of this survive the trip to a CSV: **every metric is serialized as an envelope carrying its own provenance**, not as a bare float. M13 `:1175-1181`, taken verbatim in shape:

```json
{"metric": "wall_clock_ms", "value": 252000, "ci": [198000, 331000],
 "n_trials": 15, "n_problems": 1,
 "denominator": "15 solved trials of 15",
 "caps": {"wall_clock_ms": 1200000, "attempts": 5, "budget_usd": "1.00"},
 "exclusions": {"errors": 0},
 "trust": ["serial"],
 "presentation": "4m12s  [3m18s – 5m31s]  n=15"}
```

A consumer that reads only `value` still gets a number; a consumer that reads `trust` and `presentation` cannot accidentally publish `0.089` with no denominator.

### 6.3 Rank bands, not a sort

M13's `§5.0` is adopted without change (`:899-904`): the default is **rank bands, not a total order**. An arm not separable from the current band leader joins that band and shares its rank number. Sorting is disabled entirely for `PILOT` and `UNRELIABLE` rows. A strict sort would print a 1st, a 2nd and a 3rd for three arms the statistics refused to distinguish, and the reader would read a ranking that was never claimed.

The shipped board already gets the honest half of this right and it is worth not regressing: `LeaderboardResponse.ranked` is `Literal[False]` with a verbatim not-ranked note (`backend/app/routers/experiments.py:520-545`), `pass_rate` returns `None` rather than `0.0` when the denominator is empty (`experiment_metrics.py:151-161`), and the frontend renders `"N/A"` and never `"0%"` — asserted by the Playwright spec (`frontend/e2e/experiments.spec.ts:248-302`).

### 6.4 The cross-study leaderboard (M13's R2)

M13's headline requirement is *"a leaderboard of SOLUTION GRAPHS ranked by how well they solve the suite"*, per problem and aggregate. That survives here, with one substitution and one addition:

- the leaderboard entry is an **arm**, not a graph — because the agent files and trigger wiring are part of what solved it, and two arms with identical graphs and different agent prompts are different arms;
- it accrues **across studies**, keyed on `(problem_content_hash, arm_content_hash)`, which is what the index on `trials` exists for. A study is a session; the leaderboard is the accumulated record.

`GET /api/exp/board?problem=<set>/<slug>` is the per-problem board and is the primary view. `GET /api/exp/board?set=<set>` is the aggregate. Both apply §6.2 wholesale.

**Where I differ from M13 on ordering:** M13 makes the aggregate View 1 and the per-problem board View 2 (`:906`, `:958`). I would make **the per-problem grid the default**. M13's own analysis concedes that per-case verdicts are descriptive below N=10 and that its shipped starter corpus makes every aggregate split unrankable by construction (`:1026-1037`). At the corpus sizes this harness will actually have for its first year — three to twenty problems — an aggregate is a number with an interval wider than the effect. The grid is where the researcher's real question lives: *where does this arm fall over?*

---

## 7. Sharing and reproduction

### 7.1 The two artifacts

**A link**, for the colleague on the same LazyAF: `http://<host>/#/experiments/studies/<id>` — every trial, every attempt, every step's logs, the grade manifest, the pinned hashes. Nothing is deleted; a study is a permanent record.

**A file**, for everyone else: `lazyaf exp bundle <study_id> -o <name>.tar.zst`.

```
reviewer-vs-solo/
  METHOD.md                  # what was measured, the controls, the caveats, the re-run command
  NOT-PINNED.md              # §7.3 — what cannot be reproduced and what that means
  study.json                 # the study row + every resolved cap + the machine profile
  results.jsonl              # ONE LINE PER TRIAL, full envelope, raw
  arms/                      # the arm tree at its exact commit, bytes
    coder-reviewer/{arm.yaml,agents/*.md,graph.yaml}
  problems/                  # the problem tree at its exact commit, bytes
    warmup/hello-world-bash/{problem.yaml,checks/**}
  repos/
    bundles/*.bundle         # git bundles where the licence permits redistribution
    fetch/*.json             # {source_url, commit_sha, sha256_of_tree} where it does not
  provenance.json            # §7.2
```

`results.jsonl` rather than CSV, because CSV cannot carry the envelope of §6.2 and a CSV consumer will quote the bare float. A CSV *derived from* the JSONL, with the `presentation` string as a column, is fine and is what a spreadsheet gets.

### 7.2 What is pinned

| pinned | how |
|---|---|
| the problem's start state | `source_url` + `commit_sha` (content-addressed; substitution is impossible) + a git bundle or a tree hash |
| the problem definition | `problem_content_hash` over the canonical `problem.yaml` + every byte under `checks/` |
| the arm | `arm_content_hash` over `arm.yaml` + each agent file's bytes + `graph.yaml` |
| the agent files individually | per-file sha256 in `agents_json` |
| the model | endpoint id, `base_url`, the model string sent on the wire, and — for self-hosted — `gpu_node_id` and `rate_usd_hour` |
| LazyAF itself | git sha + the `lazyaf-*:dev` image **digests** (not tags) |
| every check's container | image digest per check |
| the caps | the resolved `effective_caps`, per trial |
| the machine | `machine_profile` string, host CPU count, whether Docker was local |
| seed / temperature | whatever the provider exposes, recorded per attempt; **absent means absent**, not zero |

The **content hashes exclude every machine observation** — `validation_status`, quarantine sets, timings, `repo_id` (a per-install UUID). This is M13 §3.1's bug and its fix (`:515-556`), and it must land before the first bundle is published: two people who `git clone` the identical corpus must compute the identical hash, or their numbers refuse to pool on the happy path.

### 7.3 What CANNOT be pinned — `NOT-PINNED.md`, shipped in every bundle

This file is not a disclaimer; it is a result. Its content, in the owner's register:

> **The model.** `claude-sonnet-4-6` is a name pointing at weights we do not hold. It can change under the same name and we would not see it. We record the identifier the provider returned and the date; that is the strongest claim available.
>
> **Provider-side behaviour.** Prompt caching, routing, load-dependent latency, and rate limiting all affect cost and wall clock and none of them are observable from here.
>
> **The network.** Any check declaring `network: allowed` reached a package index whose contents change. `pip install -e .` in six months is not the same command.
>
> **Wall clock.** Reproducible only on comparable hardware at comparable contention. `machine_profile` and `contention_peak` are recorded so a mismatch is visible rather than silent. In a parallel study, wall clock is descriptive.
>
> **The upstream repo.** A github repo can be deleted or made private. The sha protects against substitution, never against disappearance. Where the licence permitted, this bundle carries a git bundle and is self-sufficient; where it did not, `repos/fetch/` is a best-effort recipe that will eventually break for some problems.
>
> **Therefore:** re-running this bundle reproduces the **method exactly** and the **numbers within variance**. Every number in `results.jsonl` carries its own interval and its own `n`. If your re-run lands outside those intervals, that is a finding — report it — not a bug in either of us.

### 7.4 The re-run command

One line, in `METHOD.md`, and **tested to execute verbatim**:

```
lazyaf exp replay reviewer-vs-solo.tar.zst
```

`replay` imports the arms, problems and repo bundles, re-queues the same study with the same caps and the same N, and on completion prints a **comparison against the bundle's numbers** — per metric, whether the new value falls inside the bundled interval. It never says "reproduced". It says `15/15 metrics within the bundled interval` or names the ones that were not.

`test_bundle_replay_on_virgin_tree.py`: import into a container with an empty git-repos volume and a scrubbed database, then replay with the `null-arm` and a mock model. Asserts no arm or problem resolves through a pre-existing `repo_id`. M13 makes the same point — *"a clean database is not a clean machine"* (`:1460`) — and it is the one test that turns most of §7 from an intention into a build failure.

---

## 8. The UI

### 8.1 The rule: no massive forms

The referent is concrete. `frontend/src/lib/components/experiments/MatrixBuilder.svelte` is **639 lines**, and its form is: name, repo select, target type, target select, N model rows × (agent select, model text, label text), N prompt rows × (template select, label text), repeat, budget, cell timeout, concurrency, a push-branches toggle, and verify image / command / timeout (`MatrixBuilder.svelte:200-410`). That is roughly fourteen distinct controls plus two repeating row groups, before you have said anything about what you are trying to learn.

**The fix is not a smaller form. It is moving the fields into git.** Every one of those settings — which model each agent uses, the budget, the timeouts, what "solved" means, the verify command — belongs to an arm or a problem, is authored once in a file, is reviewed in a diff, and travels in the bundle. What is left in the UI is the *question*, which is four controls.

### 8.2 Three surfaces

```
┌─ Experiments ────────────────────────────────────────────────────────────┐
│ [ Studies ]  Arms   Problems   Board                        [ + New ]    │
├──────────────────────────────────────────────────────────────────────────┤
│ RUNNING                                                                  │
│  ● reviewer vs solo, warmup        compare   2×6×5   38/60   $11.20/$18  │
│    ├──────────────────────────────────░░░░░░░░░░  slots 3/3              │
│ QUEUED                                                                   │
│  ○ fanout K sweep                   matrix   4×6×3   0/72    est $22.00  │
│    position 1 · starts when a slot frees                    [Cancel]     │
│ DONE                                                                     │
│  ✓ hello-world jitter, solo         jitter   1×1×15  15/15   $0.31  2d   │
│  ✓ solo across apps                 benchmark 1×12×3 34/36   $48.02  6d  │
│    ⚠ 2 errored — not counted as unsolved                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

**New study — four controls and one honest sentence.**

```
┌─ New study ──────────────────────────────────────────────────────────────┐
│ arms      lazyaf-lab @ 3f9a1c2  (synced 4m ago)              [resync]    │
│           [x] solo             [x] coder-reviewer    [ ] null-arm        │
│           [ ] reference-arm                                              │
│                                                                          │
│ problems  lazyaf-problems @ 8b2e1f0                          [resync]    │
│           [x] warmup (6)        [ ] apps (4, 1 INVALID)                  │
│                                                                          │
│ N         [ 5 ]                                                          │
│                                                                          │
│ run       ( ) serial   — 9h,  wall clock is a RESULT                     │
│           (o) parallel — 3h,  wall clock is DESCRIPTIVE     slots 3      │
│                                                                          │
│ ─────────────────────────────────────────────────────────────────────────│
│ compare · 2 arms × 6 problems × 5 = 60 trials                            │
│ est. $18.40  (historical median; 1 model unpriced → LOWER BOUND)         │
│ ⚠ endpoint `local-4090` has max_concurrency 1 — trials will queue on it  │
│                                                                          │
│                                    [ Dry run ]  [ Queue ]  [ Start now ] │
└──────────────────────────────────────────────────────────────────────────┘
```

Everything below the rule is computed, not typed. `apps (4, 1 INVALID)` is the validation gate showing its work — the invalid one is unselectable and hovering says why. The estimate reuses the shipped dry-run machinery, including its refusal to present a partial estimate as anything but a lower bound (`backend/app/routers/experiments.py:322-335`, `DryRunPanel.svelte`). The **Queue is not a separate feature** — it is `Queue` vs `Start now` on one row.

**Study detail — the grid, and one dot per trial.**

```
┌─ reviewer vs solo, warmup ──────────────── compare · N=5 · parallel ─────┐
│ ⚠ parallel: wall clock is DESCRIPTIVE on this study                      │
│                                                                          │
│                          solo                    coder-reviewer          │
│ problem                  solved  wall   $        solved  wall   $        │
│ ──────────────────────────────────────────────────────────────────────── │
│ hello-world-bash         ●●●●●   0:48  0.04      ●●●●●   1:12  0.09      │
│ echo-args                ●●●●○   1:31  0.07      ●●●●●   1:58  0.14      │
│ fizzbuzz-tested          ●●○●●   2:10  0.11      ●●●●●   2:44  0.22      │
│ csv-summing              ●○○●○   4:52  0.31      ●●●○●   5:31  0.58      │
│ retry-with-backoff       ○○✕○○   —     0.44      ●●○●●   6:02  0.71      │
│ cli-exit-codes           ●●●○●   3:20  0.19      ●●●●●   3:41  0.33      │
│ ──────────────────────────────────────────────────────────────────────── │
│ solved                   24/30                   28/30                   │
│ error                    1 (✕)  — excluded from the denominator          │
│                                                                          │
│ ● solved  ○ unsolved  ✕ error (never counted as unsolved)                │
│ Click any dot for the trial. NOT SEPARABLE on wall clock; separable on $.│
└──────────────────────────────────────────────────────────────────────────┘
```

**Trial detail** is a thin wrapper over surfaces that already exist: the attempt list (each row a `PipelineRun`, opening the shipped `PipelineRunViewer.svelte`), the grade manifest as a check-by-check table, the diff of `base_commit_sha..final_commit_sha`, and the provenance block. Agent-feedback test rows are shown and are visibly labelled as not scoring.

### 8.3 Playwright specs (R8)

Named, in the phase that ships each surface:

| spec | asserts |
|---|---|
| `frontend/e2e/exp-studies.spec.ts` | new-study composer has **four** controls; selecting 1 arm × 1 problem × N=5 stamps `kind: jitter`; an INVALID problem is unselectable and names its reason; `Queue` leaves the study `queued` with nothing dispatched |
| `frontend/e2e/exp-trial.spec.ts` | a `null-arm` trial on `hello-world-bash` reaches `unsolved` — **verbatim, and `not.toContain('solved')`**; a trial whose grade produced no manifest renders `error`, and `not.toContain('unsolved')` |
| `frontend/e2e/exp-board.spec.ts` | a parallel study's wall-clock column carries `TRUST: contended` verbatim; two arms in one band share a rank number; the not-ranked note renders verbatim |

The `not.toContain` assertions are the point. The shipped experiments spec already uses exactly this technique for the `"N/A"` vs `"0%"` distinction (`frontend/e2e/experiments.spec.ts:248-302`), and it is the only way to test that a surface *refused* to say something.

---

## 9. What is already built vs. what this needs

Verified by reading, not by grep count. "Built" means it executes today on `wip/p6-and-qol`.

| capability | state | evidence |
|---|---|---|
| Run a v2 graph with fan-out and fan-in | **BUILT** | `pipeline_executor`; fan-in verified in M13 §4.3 |
| Workspace cloned at a pinned commit | **BUILT** | `pipeline_executor.py:2725`, `:2790`, `:3832` |
| Branch + workspace per worker | **BUILT** | migration `0012_workspaces_per_worker.py` |
| Per-step cost, tokens, role | **BUILT** | `StepUsage`, migration `0005` |
| Test manifest → `TestRun`, worst-status-wins | **BUILT** | `test_ingestion.py:136-172` |
| Red suite demotes a step regardless of exit code | **BUILT** | `pipeline_executor.py:4168-4231` |
| CAS-claimed pump under a concurrency cap | **BUILT** | `experiment_service.py:716`, `:789-792`, `:844-858` |
| Observed-spend budget cap + overrun recorded | **BUILT** | `experiment_service.py:798-802`, `:1186-1197` |
| Restart resume, wired into lifespan | **BUILT** | `experiment_service.py:1410-1443`, `main.py:123-127` |
| Metrics that refuse: `None` not `0.0`, error≠failed, coverage, `ranked=False` | **BUILT** | `experiment_metrics.py:66-70`, `:151-161`, `:205-216`, `:348-371` |
| Immutable versioning by content hash | **BUILT** (for prompts) | `PromptVersion`, `models/experiment.py:319-345` |
| Ad-hoc pipeline run from an internal path only | **BUILT** | `ADHOC_TRIGGER_TYPES`, `schemas/pipeline.py:566-567` |
| — | — | — |
| **Matrix axes are models × prompts × repeats — there is no graph axis** | **GAP, and it is the headline gap** | `MatrixSpec` is `{models, prompts, repeat}` and nothing else (`schemas/experiment.py:179-203`); a cell's step list is built literally in Python as `[agent]` or `[agent, verify]` (`experiment_service.py:988-1007`); `RESERVED_STEP_CONFIG_KEYS` refuses to let a matrix smuggle one in (`schemas/experiment.py:85-103`). **An experiment cell cannot run a user-authored graph today.** |
| **Any notion of a problem** — repo@commit + definition of solved | **GAP — nothing** | No `benchmarks`/`cases`/`problems`/`trials` table in any of the 22 tables; migrations `0001`–`0015` create none; the only occurrences of "bench" in `backend/app` are docstrings pointing forward at M13 |
| **Commit pinning on an experiment** | **GAP** | A cell clones `repo.default_branch` at dispatch time (`experiment_service.py:959`); there is no `commit_sha` column on `experiments` or `experiment_runs`, and `commit`/`base_branch` are reserved keys a matrix may not set |
| **`solved` decided by evidence** | **GAP, and it is an R4 hole** | `classify_cell` returns `passed` whenever the run succeeded, without reading a `TestRun` row (`experiment_service.py:1281-1282`). For a benchmark this is a false solve. |
| **A trial = many pipeline runs** | **GAP** | `ExperimentRun` is one row ↔ one run (`models/experiment.py:301-303`); there is no attempt table |
| **`on_trial_commit`** — matching without definition sync | **GAP** | `on_push` syncs before matching (`trigger_service.py:447-460`) and a pipeline with no `branches:` pattern matches every branch (`:478-479`) |
| **`Repo.mode`** | **GAP** | `models/repo.py:10-33` — no `mode`, no `license` |
| **Clone a github repo from a URL at a sha** | **GAP** | there is no clone-from-remote path anywhere; shipped ingest takes a local path |
| **A grade run isolated from the agent's workspace** | **GAP** | the shipped `verify` step runs in the **same** run and the **same** workspace as the agent (`experiment_service.py:997-1007`), on the tree the agent just wrote — it is feedback, not a grade |
| **`TestRun.origin`** | **GAP** | no such column; every manifest scores identically |
| **A grade that guarantees a manifest** | **GAP** | plugin writes nothing when nothing was recorded (`pytest_lazyaf.py:210-213`); `ingest_manifest` returns early on empty results — so "the suite never ran" and "solved nothing" are the same observation |
| **A global concurrency governor** | **GAP, and it is worse than "no setting"** | `_spawn_task` per step, unconditionally (`pipeline_executor.py:2424-2436`); no semaphore, no pool, no queue in the dispatch path; `Settings` has no such field (`config.py:310-364`). The only backpressure that exists at all is the per-endpoint admission gate, and it covers only `openai-harness` steps on a `direct`/`proxy` endpoint (`services/model_endpoints/scheduler.py:181-224`, skipped for `runner-local` at `:44`, `:247`) |
| **Contention recording** | **GAP** | nothing samples concurrent-run count |
| **An arm's graph running against a different repo** | **GAP — structurally impossible today** | the step task re-loads the repo from `pipeline.repo_id` (`pipeline_executor.py:3089`), so passing repo B to `start_pipeline` clones repo A anyway. `Pipeline.is_template` exists (`models/pipeline.py:70`) and is **inert** — no apply, clone or reassign path anywhere. §2.2.1 works around it rather than changing it. |
| **The commit→re-trigger loop** | **BUILT — and deliberately guarded against** | `resolve_agent_work_branch` (`pipeline_executor.py:299-353`) exists precisely to stop it, because "nothing in the loop was rate-limited or depth-capped: it stopped when the budget did" (`:306-315`). The harness re-enables it and supplies the depth cap. §5.5. |
| **Durable attempt idempotence** | **GAP** | `TriggerDeduplicator` is a process-local dict with a 10s window (`workspace/trigger_dedup.py:69-71`, `trigger_service.py:40`) — no restart survival, no "have we ever run this sha" |
| **An arm loader that refuses a malformed definition** | **GAP, and the existing paths do the opposite** | `sync_repo_pipelines` keeps the previous definition on an empty or unparseable file (`trigger_service.py:363-390`); `upsert_materialized_pipeline` keeps the previous graph on a refused conversion (`:130-147`); `agent_resolver._get_repo_agent` swallows a parse error into `except Exception: pass` and falls through to a platform agent of the same name (`agent_resolver.py:149-150`) |
| **Bundle / export / replay** | **GAP** | designed in M13 §7, zero implementation |
| **Nine-tenths of M13's corpus machinery** — `test_patch`/`reference_patch`, `bench case derive`, `lazyaf-oracle`, `LAZYAF_TEST_ID_MODE=nodeid`, the 9-case starter suite | **GAP, and DEFERRED by this document** | all designed in `leaderboards-and-corpus.md`, zero implementation. §0.4 row 7: none of it is needed for a greenfield problem, which is where the ladder starts. |

**Summary in one line:** the *execution substrate* is built and is good. The *instrument* — a problem, a trial, an isolated grade, and a graph as an axis — does not exist at all.

---

## 10. The phased plan

Each phase is independently green and independently useful. **P0 produces a real measured number end to end**; everything after it makes that number mean more.

### P0 — one real number (smallest honest slice)

**Target: `hello-world-bash` × one arm × N=1, serial, one check, graded in a clean container. A CLI command prints the wall clock, the agent seconds and the cost, and a `null-arm` run of the same problem prints `unsolved`.**

| # | deliverable | size |
|---|---|---|
| 1 | `bench/problems/warmup/hello-world-bash/` — the §3.2 file, verbatim | trivial |
| 2 | `backend/app/services/exp/problem_loader.py` — disk → `problems` row, `problem_content_hash`, `definition_error` on refusal (**never** a `logger.warning` that keeps a stale definition) | small |
| 3 | `backend/app/services/exp/arm_loader.py` — same, for `arms/`. **Parses and validates every agent file and refuses**, because `agent_resolver` will not (`agent_resolver.py:149-150`). Computes `max_parallel_steps` from the graph. | small |
| 4 | Migration `0016_experiment_harness.py` — the five tables of §2.4, `down_revision='0015'` | small |
| 5 | `Repo.mode` column + the `on_push` guard for `bench` repos | trivial |
| 6 | `trial_commit` in `ADHOC_TRIGGER_TYPES` + `TriggerService.on_trial_commit()` (matching only, **no** `sync_repo_pipelines`) | small |
| 7 | `backend/app/services/exp/grade.py` — build the grade graph: fresh clone at `final_commit_sha`, materialize the check closure from the problem mirror, run checks, guarantee a manifest | medium |
| 8 | `backend/app/services/exp/bench_grade.py` — the **pure** scoring function, no DB, ~60 lines. `missing` is derived, never a column. | small |
| 9 | `TestRun.origin` + set it from the attempt kind | small |
| 10 | `backend/app/services/exp/orchestrator.py` — start a trial: create the work repo (`mode='bench'`) and branch at the start commit, materialize the arm's agent files, create the ephemeral `__lazyaf_trial__:` pipeline row owned by that repo with the arm's graph (§2.2.1), run it once, grade, apply caps, finalize | medium |
| 11 | `GET/POST /api/exp/{problems,arms,studies,trials}` — minimum to queue one and read it back | small |
| 12 | `lazyaf exp run <arm> <problem>` — prints the three numbers, or the `error_class` | small |

**Not in P0:** N>1, multiple arms, the attempts loop, `ingest-remote`, the board, the UI, the bundle, anything SWE-bench-shaped.

**Exit gate.** `tdd/integration/exp/test_first_number.py`: a mock-agent arm solves `hello-world-bash` and the trial reports `solved=true` with a non-null `wall_clock_ms`; the **`null-arm`** on the same problem reports `solved=false`, `error_class=cap_attempts`, and — asserted explicitly — **not** `solved`; a problem whose start state already passes is refused by `problem validate` before any trial runs; and a trial whose grade container is killed reports `error_class=grade_no_manifest`, **not** `unsolved`.

Those four assertions are the whole of R4 for this milestone. If P0 ships with them, the instrument is trustworthy at the easy end of the ladder, and everything after is scale.

### P1 — N, and jitter

`Study.repeats`; the pump with `LAZYAF_TRIAL_SLOTS`; `contention_peak`; `Study.run_mode`; `resume_stalled_studies()` in the lifespan; the `jitter` view and its spread. **Exit gate:** N=5 on one problem reports a median and a spread and refuses any comparison; killing the backend mid-study and restarting resumes it without re-running a completed trial.

### P2 — arms from another repo, and the A/B

Arm sync from a second mirrored repo (this is the owner's *"take graphs from one repo and apply them to another"*); `kind: compare` and its validation; paired differences with a bootstrap interval; rank bands and `NOT SEPARABLE`. **Exit gate:** two arms on one problem produce a band with a stated verdict; two arms differing only by noise render `NOT SEPARABLE` rather than a ranking.

### P3 — the attempts loop

`trial_attempts` populated by real `trial_commit` events; the **durable attempt claim** (unique `(trial_id, trigger_commit_sha, kind)`, insert-before-start, never auto-released); the three caps with their precedence; grading per attempt; `first_solved_attempt`; re-materializing the arm's agent files at the start of every attempt. This is the phase where "how the agents trigger each other" becomes the variable rather than a fixed harness policy, and it is the phase that re-enables the loop `resolve_agent_work_branch` exists to prevent (§5.5) — so its exit gate is where the depth cap gets proven. **Exit gate:** a two-agent arm where the reviewer's commit re-enters the graph records ≥2 `arm` attempts and one `grade` per attempt; an arm that loops forever terminates at `cap_attempts` and its trial is `unsolved`, **not** `error`; a backend restart mid-trial does not re-run an attempt that already claimed its commit.

### P4 — the UI (R8)

The three surfaces of §8 and their three Playwright specs. Nothing else in this phase.

### P5 — many problems, `kind: benchmark`, the per-problem grid

Plus `POST /api/repos/ingest-remote`, `Repo.license`, and the `refs/heads/bench/problem/<slug>` ref — i.e. `kind: repo` problems become authorable. `network` recorded and surfaced. **Exit gate:** one arm across 5 problems renders a grid, an aggregate marked `DESCRIPTIVE`, and a per-problem board; a problem whose `solved_when` includes a `network: allowed` check carries `TRUST: network-dependent`.

### P6 — the bundle, and the stranger

`lazyaf exp bundle` / `replay`, `METHOD.md`, `NOT-PINNED.md`, `results.jsonl`, the provenance block, and `test_bundle_replay_on_virgin_tree.py`. **Exit gate:** the stated re-run command executes verbatim on a virgin tree, and the comparison prints per-metric in/out-of-interval rather than the word "reproduced".

### Later, and explicitly not now

The full-application tier (T2 at scale — it is authoring work, not platform work); T3 and everything it needs (`test_patch`, `bench case derive`, `lazyaf-oracle`, `LAZYAF_TEST_ID_MODE=nodeid`); the judge; `kind: matrix` splits by vertical / complexity / contamination; K-sweeps and fan-out arms. Each of these is designed — mostly in `leaderboards-and-corpus.md` — and each is cheaper to build once there is a working instrument to check it against.

---

## 11. Open tensions, stated rather than hedged

Five places where the ask, the stack, or the prior design pull against each other. Each carries a recommendation, not a shrug.

1. **Wall clock is the headline, and parallelism destroys it.** The most important one. *Recommendation:* §5.4 — `run_mode: serial` is the only mode whose wall clock is rankable; `agent_seconds` + `cost_usd` are the parallelism-robust headline; `contention_peak` is recorded and the board refuses cross-contention wall-clock comparisons. Say the trade in the UI at the moment it is chosen.

2. **"A full application satisfying a set of requirements" is not objectively gradeable for free.** Somebody writes the acceptance suite, and for a real application that is a day. *Recommendation:* be honest about it (§3.3), make the easy end genuinely free so the corpus can start, and refuse the tempting shortcut — a judge model in `solved_when` — structurally rather than by convention (§4.5). A corpus of twenty T0/T2 problems is worth more than three T5 ones.

3. **M13 is a large, careful design for a narrower question.** Adopting it whole would gate the first number behind `test_patch`, `bench case derive`, `lazyaf-oracle`, `ingest-remote`, a nodeid id mode and nine authored cases. *Recommendation:* take M13's *judgement* — its refusals, its controls, its hashing discipline, its metric envelope — and defer its *SWE-bench machinery* until a bug-fix problem is actually wanted. This is a large scope reduction and it should be a conscious owner decision, not a silent one.

4. **The shipped `Experiment` stack is 12.6.5's, and M13 recommends reusing it.** I recommend against, for the reason in §0.4 row 4: a trial is many runs, a cell is one, and M13 itself already concedes `classify_cell` cannot be reused. *Recommendation:* new tables, additive; copy the pump's proven shape; leave the shipped feature and its five Playwright tests alone. The cost is one more table family; the benefit is that neither feature's semantics is bent to fit the other.

5. **`LAZYAF_TRIAL_SLOTS` default.** I have proposed 3 from the mechanism, not from measurement. It is a guess, and it should be replaced by a measured value from the first P1 study on the owner's actual box, and the doc updated. Guessed defaults that never get measured are how a "concurrency ceiling" becomes folklore.

6. **The harness's slot cap is about to become the platform's only backpressure, and it is the wrong layer for it.** There is no bound on concurrent step dispatch anywhere (`pipeline_executor.py:2424-2436`), so a wide arm times a few slots is an unbounded number of containers on one Docker socket. `LAZYAF_MAX_TRIAL_CONTAINERS` (§5.2) is a static-width guess wearing a cap's clothing; the real fix is a dispatch-level semaphore in the executor, which is out of this milestone's scope and belongs to whoever owns `pipeline_executor`. *Recommendation:* ship the crude bound, name it as crude in the code comment, and file the executor-level gate as its own item rather than letting the harness pretend to have solved it.

---

## 12. Amendments implied elsewhere

Not made here. Listed so they are not discovered later.

| file | change |
|---|---|
| `docs/milestone-13/leaderboards-and-corpus.md:1453` | the migration numbers are stale — head is `0015`; the harness takes `0016+` |
| `docs/milestone-13/leaderboards-and-corpus.md:327-351` | `checks:` is promoted from escape hatch to the trunk scoring vocabulary; `fail_to_pass`/`pass_to_pass` becomes a check generator |
| `docs/milestone-13/leaderboards-and-corpus.md:1420` | the recommendation to reuse `Experiment`/`ExperimentRun` as the trial matrix is superseded — see §0.4 row 4 |
| `docs/milestone-13/api-surface.md:828` | "cells" means two different things in two documents (M13 already flags this). This document uses **trial** and **attempt** and never "cell" |
| `upcoming/shadow-ci.md` §4 | `on_trial_commit` is the second consumer of the same argument; the two should share `Repo.mode` and the "no definition sync on a foreign commit" rule rather than each inventing one |
| `PLAN.md:1320-1590` | the M13 body predates Amendment A and now also predates this; a status line pointing here is the minimum |
| `backend/app/services/experiment_service.py:1281-1282` | not a change *to* it — a **tombstone comment** saying why the benchmark path must not reuse it, so the next person to look does not "unify" them |
| `backend/app/services/pipeline_executor.py:299-353` | `resolve_agent_work_branch`'s docstring should name the harness as the one caller that deliberately supplies an explicit `branch:` to re-enter the loop, and point at the caps that bound it — otherwise the next reader assumes the harness is the bug the guard exists to catch |
| `backend/app/services/agent_resolver.py:149-150` | the bare `except Exception: pass` is a silent arm substitution. Not fixed here (it would change card behaviour), but the arm loader must not depend on it, and a comment should say so |
| `backend/app/services/pipeline_executor.py:3089` | the line that makes cross-repo graphs impossible. Not changed by this design (§2.2.1 works around it); worth a comment naming it as the load-bearing one, since two documents now plan around it |
