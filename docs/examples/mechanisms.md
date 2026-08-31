# Mechanisms

The reference behind [`catalog.md`](catalog.md). Every claim here is the
behaviour of the code as of **2026-08-30**, with the file that owns it named so
you can check it. The last section lists what does **not** exist, which is the
half a reader cannot guess.

---

## 1. The file

`.lazyaf/pipelines/*.yaml`, validated by `app.schemas.lazyaf_yaml.PipelineYaml`:

```yaml
name: "..."            # required, <= 200 chars, stripped, non-blank
description: "..."     # optional, <= 10000 chars
triggers: []           # list of TriggerConfig
steps: []              # ordered list; the executor walks it front to back
```

A step (`PipelineStepYaml`):

```yaml
- id: "collect-diff"        # optional but write one - see below
  name: "Collect the diff"  # required
  type: script              # script | docker | agent   (default: script)
  config: {}                # everything type-specific lives here
  on_success: next          # next | stop | trigger:{card_id} | merge:{branch}
  on_failure: stop          # same vocabulary
  timeout: 300              # seconds
  continue_in_context: false
```

**Write an `id`.** It is the graph node id after the 12.8 conversion, the debug
breakpoint key, and what a human reads in the graph view. A step without one
becomes `step_0`, `step_1`, … and gets renamed under you.

**Set `timeout:` on every agent step.** The schema stamps `timeout: 300` when
you omit it, and the executor's own 1800-second agent default only applies when
the key is absent — which, coming from YAML, it never is. An agent step that
says nothing gets **five minutes**, not thirty.

`type: docker` and `type: script` are the same code path today: both run
`config.command` in `config.image`. Only `agent` branches.

### A broken file does not break the push

`sync_repo_pipelines` catches every parse and validation error, logs a warning,
and **keeps the previous definition**. So a typo does not fail your push — it
silently leaves yesterday's pipeline in place. Run `validate.py` before you push
a change to a pipeline you care about.

---

## 2. Triggers

`TriggerConfig`, the same shape the platform stores:

```yaml
triggers:
  - type: push
    config:
      branches: ["main", "feature/*"]   # fnmatch globs; omit to match all
  - type: card_complete
    config:
      status: in_review                 # the card status that fires it
    on_pass: merge                      # nothing | merge | merge:{branch}
    on_fail: reject                     # nothing | fail | reject
    enabled: true
```

* **`push`** — `TriggerService.on_push`. Matches `branches` with `fnmatch`, so
  `*` matches everything **including slashes**. Two pushes of the same
  `(pipeline, branch, sha)` inside 10 seconds start one run.
* **`card_complete`** — `TriggerService.on_card_status_change`. Fires when a
  card reaches `status`. `on_pass` / `on_fail` are run-level actions applied when
  the run finishes: `merge` approves and merges the card, `reject` sends it back
  to todo, `fail` marks it failed.

**Definitions sync only from the default branch.** `sync_repo_pipelines` returns
immediately unless the push was to `repo.default_branch`. A feature-branch push
still matches triggers — against the trunk's definition of them.

**Two triggers that do not exist:** there is no `schedule` trigger and no path
filter. See [§9](#9-what-does-not-exist).

---

## 3. Actions, and what makes a run red

`describe_step_action` is the single definition of the vocabulary, and it is
**closed** — an unknown action fails the run naming the offender, rather than
being treated as `stop` (which used to turn one typo into a silently truncated,
green run).

| Action | Effect |
|---|---|
| `next` | run the following step |
| `stop` | complete the run **with this step's verdict** |
| `trigger:{card_id}` | clone that card as a template, start it as an agent run, **and continue** |
| `merge:{branch}` | merge the run's branch into `{branch}`, **and continue** |
| `trigger:pipeline:{id}` | start another pipeline. Still dispatchable on the array path; **retired** in the graph vocabulary (it had no users and no execution test), so do not write new ones. |

### The verdict rule

This is the single most surprising thing in the format.

* `stop` completes the run with the finishing step's own success/failure.
* Walking off the end of the array completes the run **passed** — unconditionally.

So a step that fails and continues with `on_failure: next` records a red
`StepRun`, and the run can still finish green. The executor says so itself, in
`_spawn_fix_card`: *"when the triggering step is the LAST one, continuing past it
completes the run PASSED even though the action fired from `on_failure`."*

The pattern that fixes it, used by several recipes here: the failing step writes
a marker, and a final step re-reads it and exits non-zero with
`on_failure: stop`.

(The graph format the executor also runs derives the verdict from every
`StepRun` instead, so this is a property of the array format specifically — and
the array format is what `.lazyaf/pipelines/` produces.)

---

## 4. The workspace

* **One volume per run**: `lazyaf-ws-{run_id}`, mounted by every step.
* A helper container clones the repo into `/workspace/repo` (full history and
  tags — no `--depth`) and checks out the pushed commit **detached**, then
  chowns it to uid 1000, which is what the steps run as.
* `/workspace/home` is `HOME`, and the images bake `PIP_USER`,
  `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX` and `PATH` to point there — so
  per-step installs persist across the steps of a run.
* `origin` points at `http://backend:8000/git/{repo_id}.git`. A step can push
  to it with no credentials, which is how agent steps commit their work.

**`continue_in_context` is accepted and ignored** on the local path. The
executor logs, once: *"continue_in_context is obsolete for locally-executed
steps: the persistent workspace volume already carries state between steps."*
The flag stays legal because pipelines in the wild carry it; do not reach for it
expecting behaviour.

The practical consequence: **write intermediate artifacts to the checkout and
the next step will find them.** Two habits keep that clean:

```sh
mkdir -p .lazyaf-run
grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
  || echo '/.lazyaf-run/' >> .git/info/exclude
```

`.git/info/exclude` is local to the checkout and never committed, which matters
because the agent wrapper commits with `git add -A` — anything you leave lying
in the tree ends up in the agent's commit otherwise.

---

## 5. Step config: script and docker

```yaml
config:
  image: "lazyaf-base:dev"     # default: python:3.12
  command: |                   # the shell script; bash
    ...
  working_dir: "/workspace/repo"  # this is already the default
  environment: {KEY: "value"}  # non-secret env
  shell: "bash"
  memory_limit: "2g"
  needs: ["docker"]            # sugar: bind-mounts the docker socket
  control: false               # opt OUT of the in-container control runtime
  requires: {has: ["gpu"]}     # pin to a remote runner - see §8
  executor: remote             # explicit remote override
```

`needs: ["docker"]` is the only capability; anything else is a loud error. It
translates to the docker-socket bind mount in one place, which is the seam a
later phase rewires.

`control: false` drops the step to plain stdout capture instead of the
in-container control runtime. LazyAF's own gate step uses it deliberately — the
gate that verifies the control runtime must not depend on it.

### Environment a step actually gets

Injected into every step container:
`LAZYAF_PIPELINE_RUN_ID`, `LAZYAF_STEP_RUN_ID`, `LAZYAF_STEP_INDEX`,
`LAZYAF_EXECUTION_KEY`, `LAZYAF_BACKEND_URL`, `LAZYAF_CONTROL`,
`LAZYAF_USAGE_PROVIDER`, plus `LAZYAF_ROLE` / `LAZYAF_GPU_NODE_ID` /
`LAZYAF_GPU_FRACTION` when set.

**The push's shas are not among them.** To get the commit range, read the run:

```sh
curl -sf "$LAZYAF_BACKEND_URL/api/pipeline-runs/$LAZYAF_PIPELINE_RUN_ID"
```

`trigger_context` on that response carries `branch`, `commit_sha`, `old_sha` and
`push_ref` for a push-triggered run. The read needs no token from inside the
container network — that is how `scripts/verify_executor.py`, which runs as a
step of LazyAF's own pipeline, does its job. `old_sha` is all zeros for a new
branch, so guard it.

---

## 6. Step config: agent

```yaml
type: agent
config:
  agent: claude-code          # claude-code | gemini | openai-harness | mock
  model: "..."                # optional; or "endpoint:<name>" sugar
  endpoint: "local-4090"      # required for openai-harness
  task: "one line"            # -> {{title}}
  title: "one line"           # -> {{title}} (takes precedence over task)
  description: "..."          # -> {{description}}
  prompt_template: |          # replaces the default prompt body entirely
    ... {{title}} ... {{description}} ... {{spec_context}}
  branch: "lazyaf/thing"      # explicit work branch
  commit: false               # or a mapping, below
  spec_context: false         # turn off the curated spec bundle
  agent_file_ids: []          # platform AgentFile ids (not names)
  harness: {}                 # openai-harness budgets, below
  stream: true
```

**There is no default agent.** `resolve_agent_type` raises naming the whole
vocabulary, because guessing one is how a step silently bills the wrong
provider. `runner_type:` is accepted as a historical spelling of `agent:`.

### The prompt

Rendered **backend-side** (`services/agent_prompt.py`) and shipped as finished
text — the container has no database, so nothing is re-templated there. The
placeholder vocabulary is frozen at three: `{{title}}`, `{{description}}`,
`{{spec_context}}`, substituted in **one pass**, so a value that comes out of a
substitution is never re-scanned and a diff cannot smuggle in another
placeholder.

Omit `prompt_template` and you get the default body (implement the feature, write
tests, commit, keep it minimal). Supply one and it replaces the body entirely.

### Previous-step logs, free

An agent step is automatically handed the logs of the step at
`step_index - 1`, rendered into the prompt under `## Previous Step Output` and
capped before rendering. No config. It is strictly the step *before* this one,
not "the last failure" — so an explainer must sit **immediately** after the step
it explains.

### Committing and pushing

```yaml
commit: false              # analysis-only: no commit, no push, no branch
commit:                    # or the full form
  enabled: true
  message: "chore: ..."
  push: true
  allow_empty: false
```

The work branch, in order: an explicit `config.branch:`; then an ad-hoc caller's
branch when it differs from the base; otherwise an isolated
`lazyaf/agent-<8 hex of the StepRun id>`.

**Only an explicit `branch:` can resolve to the run's own trigger branch**, and
that guard exists because the alternative was a self-triggering loop with a
provider bill on every lap. The executor asserts it rather than assuming it.

The wrapper then runs `git checkout -B <branch>` — **at current HEAD**. In a
multi-attempt pipeline that means attempt 2 branches from attempt 1's commit
unless a step in between resets the tree. It commits with `git add -A` and
pushes with `git push -u origin <branch>`.

### Agent files and sub-agents

`agent_file_ids` takes **platform `AgentFile` row ids**, not names — there is no
`agents: [name]` YAML surface. Resolution happens backend-side and
`.lazyaf/agents/{name}.yaml` overrides a platform agent of the same name
(`AgentResolver`). An unresolvable id is skipped with a warning rather than
failing the step.

`openai-harness` **refuses** `agent_file_ids` outright: the harness runs one loop
and does not do sub-agents. Put the roles in the pipeline, where they are visible
and costed separately.

---

## 7. Self-hosted endpoints (`agent: openai-harness`)

The newest mechanism here, and the one that makes per-commit agent steps
affordable.

```yaml
config:
  agent: openai-harness
  endpoint: "local-4090"      # explicit
  # model: "endpoint:local-4090"   # the sugar - same thing, one parser
```

`parse_endpoint_reference` is the only parser of both spellings. The sugar
exists because `model` is the field every selection surface already populates,
so a self-hosted model reaches the dispatcher from all of them with no schema
change.

**The endpoint must be registered and probed first:**

```
POST /api/model-endpoints            {name, base_url, model, server_kind}
POST /api/model-endpoints/{id}/probe
```

Dispatch **refuses** an endpoint that has never been probed, is disabled, has
failed three consecutive times, or is probed-ok with no tool-calling
observation. Refusing beats silently routing the no-tools fallback, and a
30-minute step is not the place to discover the model cannot tool-call. A
*stale* record does not refuse: it runs, warns, and re-probes in the background.

`reach: runner-local` on the endpoint adds its capability label to the step's
`requires:` block, so the step routes to the runner that can see that model —
which is what makes a GPU behind NAT work, since the runner dialled out.

### Harness budgets

```yaml
harness:
  mode: auto              # auto | tools | text  (pin `text` to force the
                          # no-tools fallback protocol)
  max_iterations: 40
  max_total_tokens: 400000
  time_budget_seconds: <timeout - 60>
  max_tool_calls_per_turn: 4
  shell_timeout_seconds: 120
  tool_output_max_bytes: 8192
  temperature: 0
  top_p: null
  seed: null
  require_changes: <true when the step commits>
  debug_transcript: false
```

`time_budget_seconds` defaults to the step's hard `timeout` minus 60 seconds, so
the loop stops itself *inside* the watchdog and still gets to commit its partial
work and write the usage manifest instead of being SIGKILLed with nothing to
show for it.

`require_changes` defaults to whether the step commits at all — which is why
`commit: false` is the right key for a reviewer: "succeeded and changed nothing"
is a legitimate outcome for an analysis step and the most expensive possible
failure for an implementation one.

### The tools the model gets

`list_files`, `read_file`, `write_file`, `apply_patch`, `run_shell`, `finish`.

The sandbox refuses any path outside the repo checkout ("path escapes the
workspace"), so **artifacts you want an agent to read must live under
`/workspace/repo`**. `run_shell` runs with the repo as cwd and with the
environment stripped to `LAZYAF_PIPELINE_RUN_ID` and `LAZYAF_STEP_RUN_ID` — the
model never holds the endpoint key — and pushing is denied with a stated reason:
*"the platform commits and pushes this step's work; do not push."*

---

## 8. The remote lane

```yaml
config:
  requires:
    has: ["remote-lane", "gpu"]   # subset containment against the runner's labels
    arch: amd64                   # normalized on both sides
    runner_type: "..."            # exact; "any" matches everything
    runner_id: "..."              # exact
  # executor: remote              # explicit override, requirements optional
```

`requires:` on **any** step type routes it to a runner agent over the WebSocket
the runner opened outward. A bare `runner_type:` is sugar for
`requires.runner_type` and pins **script/docker steps only** — on an agent step
it keeps its older meaning (the AI flavour) and does not route.

A remote step provisions its **own** volume on its own daemon and clones the repo
from LazyAF's git server, so it does **not** see files a local step left in
`lazyaf-ws-{run_id}`. Keep a handoff between two local steps, or push a branch.

---

## 9. What does not exist

Stated plainly, because a reader will otherwise assume it does.

* **No scheduler.** `push` and `card_complete` are the only events. Nothing
  polls a clock. Use your own cron against `POST /api/pipelines/{id}/run`.
* **No path filter on a trigger.** `push` reads `branches:` and nothing else.
* **No conditional step.** Nothing in the action vocabulary means "skip".
* **No graph in the repo YAML.** The executor runs a graph internally — parallel
  fan-out, fan-in, conditional edges — but `PipelineYaml.steps` is a flat list
  and there is no way to author edges from `.lazyaf/pipelines/`.
* **No per-worker checkouts, yet.** One volume per run.
  `services/workspace/worker_key.py` defines the lane key
  (`lazyaf_workspace` in a step config) and `generate_volume_name()` takes it,
  but as of 2026-08-30 the executor still asks for the default lane and no
  schema field selects one. This is being built now; re-check before relying on
  the workaround in [fan-out](catalog.md#8-fan-out-k-attempts-one-checkout).
* **No card comments.** `routers/cards.py` exposes no comment endpoint, so
  "post the findings back to the card" has no literal implementation. An
  agent's findings reach a human three ways instead: the **step log** (rendered
  live in the run view), a **commit on its own branch**, or a **fix card**
  spawned by `trigger:{card_id}`.
* **No `params` from a trigger.** `PipelineRunCreate.params` become step env
  vars, but the push and card triggers do not set them; read
  `trigger_context` off the run instead ([§5](#environment-a-step-actually-gets)).
