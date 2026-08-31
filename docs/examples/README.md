# Workflow examples: what a pipeline can do when a step is an agent

Ordinary CI runs commands. LazyAF runs commands **and agents**, in the same
pipeline, on the same checkout, on every push — because it hosts the git remote
you pushed to.

That one difference is what this directory is about. A step of `type: agent` is
a model sitting in your repository with a shell, reading the diff you just
pushed, and writing something back. It costs real money or real GPU time, so
every recipe here says what it costs before it says what it does.

## The three files

| File | What it is |
|---|---|
| [`catalog.md`](catalog.md) | Eleven recipes. What it does, why you'd want it, the complete YAML, what it costs. |
| [`mechanisms.md`](mechanisms.md) | The reference. Every key the executor actually reads, and — just as important — what does **not** exist yet. |
| [`pipelines/`](pipelines/) | The same YAML as standalone files, ready to copy. |

`validate.py` checks all of it against the real schema:

```
cd backend && uv run python ../docs/examples/validate.py
```

It parses every example with `app.schemas.lazyaf_yaml.PipelineYaml` — the same
class the push handler constructs — checks the agent and action vocabularies
against `pipeline_executor`, runs each one through the 12.8 `array_to_graph`
converter, and asserts the YAML fenced in `catalog.md` is byte-identical to the
files. Every example in this directory passes it.

## Nothing here is armed

Files in **`.lazyaf/pipelines/`** are live. A push to the LazyAF git remote
re-reads that directory at the pushed commit and runs whatever it finds. Files
in `docs/examples/` are read by nobody. To arm one, copy it into
`.lazyaf/pipelines/` — and read the cost line first.

## How a file in `.lazyaf/pipelines/` becomes a run

1. You push to LazyAF's own git server (`/git/{repo_id}.git`; from inside the
   container network that is `http://backend:8000/git/{repo_id}.git`, which is
   also what `origin` points at in a step's checkout).
2. `TriggerService.on_push` re-reads `.lazyaf/pipelines/*.yaml` **at the pushed
   commit** and upserts a platform pipeline row per file — so a push that
   changes CI is gated by its own new definition, not the previous one.
   Definitions sync **only on pushes to the repo's default branch**: the CI
   definition follows the trunk. A feature-branch push still matches triggers,
   against the trunk's definition.
3. Triggers are matched. A `push` trigger compares the branch to its
   `branches:` globs with `fnmatch`; two pushes of the same
   `(pipeline, branch, sha)` within 10 seconds produce one run.
4. A workspace volume `lazyaf-ws-{run_id}` is created and a helper container
   clones the repo into `/workspace/repo` and checks out the pushed commit
   detached. **One volume per run** — every step of the run shares it.
5. Steps run in order, each in its own container, each POSTing its own status,
   logs, test results and usage to `/api/steps/{id}/*`.

The pipeline shows up in the UI named `[repo] <your name>`.

## The cost model, in one table

This is the part a normal CI doc doesn't need. An agent step is a billed model
call; every one of them writes a `StepUsage` row, and
`GET /api/pipeline-runs/{run_id}/usage` adds them up.

| `agent:` | What runs | `cost_source` on the usage row | What you actually pay |
|---|---|---|---|
| `claude-code` | the Anthropic CLI, in the step container | `cli-reported` when the CLI reports a cost | real API dollars, the provider's own number |
| `gemini` | the Gemini CLI | `cli-reported` when the CLI reports a cost | real API dollars |
| `openai-harness` | LazyAF's own tool-calling loop against an endpoint **you** host | `gpu-node` | node rate × elapsed × GPU fraction — `0.00` on hardware you own |
| `mock` | a scripted fake, no network | provider `self-hosted` | nothing; this is what LazyAF's own pipeline uses to exercise the agent path for free |
| *(script / docker steps)* | your command | `unknown` | wall clock |

`cost_source: unknown` means "the provider told us nothing", and it is recorded
as a fact rather than guessed at. A row is never missing: the dogfood gate fails
if a passed step has no usage row at all.

**This is why `endpoint:` matters more than any other key in this catalog.**
`agent: openai-harness` plus `endpoint: <name>` points a step at any
OpenAI-compatible server — ollama, vLLM, anything speaking that wire format —
including one on a machine behind NAT, because the runner opens the connection
outward. A reviewer that costs API dollars per push is a reviewer you turn off
in a week. A reviewer that costs GPU seconds is one you leave on. Most recipes
here lead with it.

## The index

| Recipe | Trigger | Model cost per fire |
|---|---|---|
| [Leak gate](catalog.md#1-leak-gate) | every push, every branch | **none** — stdlib scan, about a second |
| [Per-commit cheap lane](catalog.md#10-the-cheap-lane) | every push, every branch | **none** |
| [AI review, self-hosted](catalog.md#2-ai-code-review-on-your-own-gpu) | push to main / feature/* | GPU seconds |
| [AI review, hosted API](catalog.md#3-the-same-review-on-a-paid-api) | push to main | **one billed call per push** |
| [Test-gap detection](catalog.md#4-what-did-this-diff-leave-untested) | push to main / feature/* | one agent step |
| [Doc drift → fix card](catalog.md#5-doc-drift-and-a-card-that-fixes-it) | push to main | one agent step, plus a whole fix run when it finds drift |
| [Migration safety review](catalog.md#6-a-reviewer-that-only-cares-about-migrations) | push to main / feature/* | one **short** turn per push, a full one only when `alembic/` changed |
| [Explain this failure](catalog.md#7-explain-this-failure) | push to main / feature/* | one short turn per push, a full one only on red |
| [Fan-out: K attempts](catalog.md#8-fan-out-k-attempts-one-checkout) | a card reaching `in_review` | **three full agent runs** |
| [Release notes](catalog.md#9-release-notes-from-the-commit-range) | nothing — you fire it | one agent step, when you say |
| [Nightly expensive lane](catalog.md#11-the-expensive-lane) | nothing — your cron fires it | three agent steps, once a night |

## Five traps, before you copy anything

**1. The push loop.** An agent step commits and pushes. Pushing to the LazyAF
remote fires push triggers. If a trigger's `branches:` glob matches the branch
your agent pushes to, the run starts itself again, with a real bill attached to
every lap, and nothing depth-caps it. There is one guard: a step that names no
`branch:` gets an isolated `lazyaf/agent-<8 hex>` branch, and pushing to the
run's own trigger branch requires an explicit `branch:` in the step config. That
guard does **not** cover a `branches: ["*"]` trigger, which matches
`lazyaf/anything`. Keep agent work branches out of your globs.

**2. `on_failure: next` does not keep the run red.** On the array format the
run's verdict is whatever the last `stop` says, and reaching the end of the
array completes it **passed** regardless of what failed earlier — a stated
limitation of the v1 executor path. If you continue past a failure to explain
it, a later step has to re-state the failure. Every recipe here that does this
ends with a `verdict` step; see [Explain this failure](catalog.md#7-explain-this-failure).

**3. There is no conditional step.** `on_success` / `on_failure` take `next`,
`stop`, `trigger:{card_id}` and `merge:{branch}`. None of them means "skip the
next step". There is also no path filter on a trigger — a `push` trigger reads
`branches:` and nothing else. "Only run when `alembic/` changed" is approximated
with a cheap script step that sets the scope and an agent told to stop
immediately; you still pay for one short turn. See
[the migration reviewer](catalog.md#6-a-reviewer-that-only-cares-about-migrations).

**4. There is no scheduler.** `TriggerService` handles `push` and
`card_complete`. Nothing polls a clock. "Nightly" is your own cron calling
`POST /api/pipelines/{id}/run`; `schedule` is an accepted `trigger_type` on that
endpoint, which labels the run — it does not schedule it.

**5. One run, one checkout.** Every step of a run mounts the same
`lazyaf-ws-{run_id}` volume. That is what makes "collect the diff in a script
step, read it in an agent step" work with no plumbing — and it is what makes
genuine parallel fan-out impossible today. Per-worker workspace lanes are being
built right now (`backend/app/services/workspace/worker_key.py` defines the lane
key and `generate_volume_name()` takes it); as of 2026-08-30 the pipeline
executor still asks for one volume per run and no YAML key selects a lane.
