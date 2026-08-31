# The catalog

Eleven per-commit (and deliberately not-per-commit) workflows. Each one says
what it does, why you would want it, **what it costs**, and then gives the whole
file.

Start with [`README.md`](README.md) for the cost model and the five traps; keep
[`mechanisms.md`](mechanisms.md) open for the reference. Every file below is
also in [`pipelines/`](pipelines/), byte for byte — `validate.py` fails if they
ever drift.

Nothing here is armed. `docs/examples/` is read by nobody; `.lazyaf/pipelines/`
runs on the next push.

---

## 1. Leak gate

**What it does.** Refuses a push that carries a credential-shaped string
anywhere in the tracked tree. Three checks: `.env` is still git-ignored, no
`.env` file is tracked, and no tracked file contains a live-format key
(Anthropic, OpenAI, Google, GitHub, AWS, or any PEM private key).

**Why you'd want it.** It is the one gate whose absence you find out about from
strangers. It is also the cheapest thing in this catalog by an order of
magnitude, so there is no budget argument against running it on every branch.

LazyAF shipped `.github/scripts/scan_repo_secrets.py` and ran it in the release
workflow — but its own dogfood pipeline never called it, so a key committed and
pushed to LazyAF passed LazyAF. **That step now exists**, first in
`.lazyaf/pipelines/test-suite.yaml`. This recipe is the standalone version.

**What it costs.** No model call, no network, no dependency sync. Stdlib Python
over `git ls-files`. Measured on this repo while writing this: **660 tracked
files scanned, exit 0, 0.55 s** including interpreter startup.

**The point that makes it a gate and not a warning:** exit 1 is a finding, and
`on_failure: stop` completes the run with *this step's* verdict — so a finding
fails the run. Exit 2 ("the scanner could not run") fails it too, which is the
honest reading: a leak gate that could not look is not a leak gate that found
nothing.

```yaml
# EXAMPLE - INERT. This file is in docs/, so LazyAF never reads it.
# Copy it into .lazyaf/pipelines/ to arm it, and read docs/examples/README.md
# first: a file in .lazyaf/pipelines/ runs on the next push.
#
# COST: zero model calls. .github/scripts/scan_repo_secrets.py is stdlib-only.
# Measured on this repo at the time this was written: 660 tracked files
# scanned, exit 0, 0.55s including interpreter startup.
#
# branches: ["*"] is safe HERE because this pipeline has no agent step that
# pushes. See "the push loop" in docs/examples/README.md before copying that
# glob into a pipeline that does.
name: "Leak Gate"
description: "Refuse a push carrying a credential-shaped string. No model call: a stdlib scan of every tracked file, on every branch."

triggers:
  - type: push
    config:
      branches: ["*"]

steps:
  - id: "secret-scan"
    name: "Secret scan"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        python3 .github/scripts/scan_repo_secrets.py
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 2. AI code review on your own GPU

**What it does.** Collects the exact diff the push introduced, hands it to an
agent, and fails the run if the agent calls it a blocker. The review lands in
`.lazyaf-run/review.md` and in the step log.

**Why you'd want it.** A reviewer that reads *this* diff, in *this* repository,
with the rest of the codebase available to it, and that runs before anyone opens
the PR. Not a linter — a linter cannot tell you the migration in this diff drops
a column that another service still reads.

**What it costs.** No API bill. `agent: openai-harness` with `endpoint:` drives
an OpenAI-compatible server you host — ollama, vLLM, anything speaking that wire
format — and the usage row is priced from the node's hourly rate as
`cost_source: "gpu-node"`, which is `0.00` on hardware you own. What you spend is
wall clock and electricity.

**This is the recipe to read first if you want agent steps on every commit at
all.** The same review against a paid API (next recipe) is the version people
turn off after a week of bills.

**Before you arm it:** register the endpoint and **probe** it. Dispatch refuses
an unprobed endpoint on purpose — a fifteen-minute agent step is not the place to
discover the model cannot tool-call.

Three mechanisms are doing the work here, and none of them is obvious:

* the push's before/after shas are **not** in the step's environment, so the
  first step reads them off the run over HTTP;
* the two steps share one workspace volume, which is why the second step can
  just read the file the first one wrote;
* `commit: false` makes the step analysis-only *and* flips the harness's
  `require_changes` default to false, so "the reviewer changed nothing" is not
  treated as a failure.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# AI code review on every push, running against a model YOU host.
#
# COST: no API bill. `agent: openai-harness` + `endpoint:` drives an
# OpenAI-compatible server you registered (ollama, vLLM, anything that speaks
# the same wire format). The step is still billed in LazyAF's own ledger:
# usage_ingestion prices it from the endpoint's node rate as
# cost_source="gpu-node", which is 0.00 on owned hardware and a real number on
# rented hardware. Wall clock is the thing you actually spend.
#
# PREREQUISITE: an endpoint named `local-4090` must exist and be PROBED.
#   POST /api/model-endpoints            {name, base_url, model, server_kind}
#   POST /api/model-endpoints/{id}/probe
# resolve_step_endpoint refuses to dispatch against an unprobed endpoint on
# purpose - a 15-minute agent step is not the place to discover the model
# cannot tool-call.
name: "Review (self-hosted)"
description: "Read the pushed diff, write .lazyaf-run/review.md, and fail the run on a BLOCKER. Runs on a self-hosted OpenAI-compatible endpoint, so a per-commit agent step costs GPU seconds instead of API dollars."

triggers:
  - type: push
    config:
      branches: ["main", "feature/*"]

steps:
  # The push's before/after shas are on the RUN, not in the step's env. Every
  # step container gets LAZYAF_PIPELINE_RUN_ID and LAZYAF_BACKEND_URL, and the
  # run read endpoint needs no token from inside the network.
  - id: "collect-diff"
    name: "Collect the pushed diff"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        python3 - <<'PY'
        import json, os, subprocess, urllib.request
        run = json.load(urllib.request.urlopen(
            f"{os.environ['LAZYAF_BACKEND_URL']}/api/pipeline-runs"
            f"/{os.environ['LAZYAF_PIPELINE_RUN_ID']}", timeout=30))
        ctx = run.get("trigger_context") or {}
        new = ctx.get("commit_sha") or "HEAD"
        old = ctx.get("old_sha") or ""
        have = lambda r: subprocess.run(
            ["git", "cat-file", "-e", r + "^{commit}"]).returncode == 0
        if not old or set(old) == {"0"} or not have(old):
            old = new + "~1" if have(new + "~1") else new
        diff = subprocess.run(["git", "diff", old, new],
                              capture_output=True, text=True).stdout
        open(".lazyaf-run/diff.patch", "w", encoding="utf-8").write(diff[:200000])
        print(f"[diff] {old[:12]}..{new[:12]} -> .lazyaf-run/diff.patch "
              f"({len(diff)} bytes)")
        PY
    on_success: next
    on_failure: stop
    timeout: 300

  # commit: false makes this analysis-only. It also flips the harness's
  # `require_changes` default to false, so "the reviewer changed nothing" is
  # not treated as a failed step.
  - id: "review"
    name: "AI review"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Review the pushed diff and write .lazyaf-run/review.md"
      description: |
        Read .lazyaf-run/diff.patch. It is the whole change this push made.

        Write .lazyaf-run/review.md. Use exactly this shape:

          ## Verdict
          BLOCKER   (only if the diff is unsafe to ship as written)
          or
          OK

          ## Findings
          - <file>:<line> - <one sentence, what is wrong and why it matters>

        Rules:
        - Only report things you can point at a line for. No style opinions.
        - BLOCKER means: data loss, a credential, a broken migration, an
          unhandled failure path, or a test that cannot fail.
        - When the diff is fine, write the OK verdict with an empty findings
          list and stop. "Nothing to report" is a real answer.
      harness:
        max_iterations: 12
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 900

  # The reviewer's own exit code says "the loop finished", not "the code is
  # good". This step is what turns a finding into a red run.
  - id: "review-gate"
    name: "Fail on BLOCKER"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        if [ ! -f .lazyaf-run/review.md ]; then
          echo "the reviewer wrote no review.md" >&2
          exit 1
        fi
        cat .lazyaf-run/review.md
        if grep -qi '^BLOCKER' .lazyaf-run/review.md; then
          echo "review reported a BLOCKER" >&2
          exit 1
        fi
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 3. The same review, on a paid API

**What it does.** Identical shape, `agent: claude-code` instead. Also shows
`prompt_template`, which replaces the default prompt body entirely.

**Why you'd want it.** When the question genuinely deserves the better model.
Reviewing a security-sensitive subsystem is a different question from reviewing
a rename.

**What it costs. A real billed model call on every push to a matched branch.**
The Anthropic CLI runs in the step container, the backend injects
`ANTHROPIC_API_KEY` through the step config file (never through inspectable
container env), and the wrapper scrapes the CLI's own usage report into a
`StepUsage` row — recorded as `cost_source: "cli-reported"` when the CLI reports
a cost, and as `cost_source: "unknown"` when it does not, which is the recorded
fact rather than a guess. When it is there, the number on the run is the
provider's number, not an estimate — which is the point, but it is also a number
that multiplies by your push rate. A trunk taking 30 pushes a day pays 30 times.

If that is the wrong trade, the two ways out are [the cheap/expensive
split](#11-the-expensive-lane) and [your own GPU](#2-ai-code-review-on-your-own-gpu).

Note the trigger: `branches: ["main"]`, with no glob that could match
`lazyaf/*`. This pipeline does not push, but keeping agent work branches out of
a trigger is the habit that prevents the push loop.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# The same review as review-self-hosted.yaml, on a paid API instead.
#
# COST: A REAL BILLED MODEL CALL ON EVERY PUSH TO A MATCHED BRANCH.
# `agent: claude-code` runs the Anthropic CLI in the step container; the
# backend injects ANTHROPIC_API_KEY through the step config file, and the
# wrapper scrapes the CLI's own reported cost into a StepUsage row with
# cost_source="cli-reported". That row is what GET /api/pipeline-runs/{id}/usage
# adds up, so the number on the run is the provider's number, not an estimate.
#
# The bill scales with how much of the diff the agent reads, and the diff is
# bounded here at 200 kB. A busy trunk pushing 30 times a day pays 30 times.
# If that is the wrong trade, this is the recipe to move to a card_complete
# trigger (see cheap-and-nightly.yaml) or onto your own GPU
# (review-self-hosted.yaml).
#
# Note the branch list: no glob that could match `lazyaf/*`. This pipeline
# does not push, but keeping agent work branches out of a trigger is the habit
# that stops the push loop described in docs/examples/README.md.
name: "Review (hosted API)"
description: "AI code review on every push to main, running on the Anthropic CLI. One real billed model call per push."

triggers:
  - type: push
    config:
      branches: ["main"]

steps:
  - id: "collect-diff"
    name: "Collect the pushed diff"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        python3 - <<'PY'
        import json, os, subprocess, urllib.request
        run = json.load(urllib.request.urlopen(
            f"{os.environ['LAZYAF_BACKEND_URL']}/api/pipeline-runs"
            f"/{os.environ['LAZYAF_PIPELINE_RUN_ID']}", timeout=30))
        ctx = run.get("trigger_context") or {}
        new = ctx.get("commit_sha") or "HEAD"
        old = ctx.get("old_sha") or ""
        have = lambda r: subprocess.run(
            ["git", "cat-file", "-e", r + "^{commit}"]).returncode == 0
        if not old or set(old) == {"0"} or not have(old):
            old = new + "~1" if have(new + "~1") else new
        diff = subprocess.run(["git", "diff", old, new],
                              capture_output=True, text=True).stdout
        open(".lazyaf-run/diff.patch", "w", encoding="utf-8").write(diff[:200000])
        print(f"[diff] {old[:12]}..{new[:12]} -> .lazyaf-run/diff.patch "
              f"({len(diff)} bytes)")
        PY
    on_success: next
    on_failure: stop
    timeout: 300

  # prompt_template REPLACES the default prompt body. Only {{title}},
  # {{description}} and {{spec_context}} are substituted, in one pass - a
  # value that comes out of a substitution is never re-scanned, so nothing in
  # a diff can smuggle in another placeholder.
  - id: "review"
    name: "AI review"
    type: agent
    config:
      agent: claude-code
      commit: false
      title: "Review this push"
      description: "Read .lazyaf-run/diff.patch and write .lazyaf-run/review.md."
      prompt_template: |
        You are reviewing one commit range in this repository. You are sitting
        in the checkout; the diff is at .lazyaf-run/diff.patch.

        ## Task
        {{title}}

        {{description}}

        ## Output
        Write .lazyaf-run/review.md with a `## Verdict` line that is either
        BLOCKER or OK, then a `## Findings` list of `file:line - one sentence`
        entries. Only report what you can point at a line for.

        BLOCKER means the change is unsafe to ship as written: data loss, a
        credential, a broken migration, an unhandled failure path, or a test
        that cannot fail.

        ## Do not
        Do not change any source file. Do not run the test suite - a later
        step does that, and your budget is better spent reading.
    on_success: next
    on_failure: stop
    timeout: 1200

  - id: "review-gate"
    name: "Fail on BLOCKER"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        if [ ! -f .lazyaf-run/review.md ]; then
          echo "the reviewer wrote no review.md" >&2
          exit 1
        fi
        cat .lazyaf-run/review.md
        if grep -qi '^BLOCKER' .lazyaf-run/review.md; then
          echo "review reported a BLOCKER" >&2
          exit 1
        fi
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 4. What did this diff leave untested?

**What it does.** An agent reads the diff, then the test suite, and names the
behaviours the diff added or changed that **no test would go red for**.

**Why you'd want it.** Coverage tells you which *lines* ran. It cannot tell you
that the new error branch is executed by a test that never asserts on it. The
question "would a test fail if this regressed" needs someone to read both sides,
and it is exactly the shape of question an agent in the checkout can answer.

**What it costs.** One agent step per push. On a self-hosted endpoint that is
GPU seconds; swap `agent`/`endpoint` for `agent: claude-code` and it is an API
bill per push.

**It informs by default.** A named gap is not automatically a reason to refuse a
push, and a gate you argue with is a gate you disable. The last step carries the
two-line change that makes it blocking, commented out, so you can decide once
you have seen a week of its output.

The prompt does the load-bearing work here. "List missing tests" gets you a wish
list; demanding a concrete failing input per gap gets you something you can act
on, and it is also what stops the model padding.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# "What did this diff leave untested?"
#
# The thing a coverage tool cannot tell you: coverage says which LINES ran,
# not which BEHAVIOURS have an assertion. An agent reading the diff can name
# the behaviour and the test that should exist.
#
# COST: one agent step per push. On `endpoint: local-4090` that is GPU
# seconds (cost_source="gpu-node"); swap `agent`/`endpoint` for
# `agent: claude-code` and it is a real API bill per push.
#
# This lane INFORMS by default - a named gap is not automatically a reason to
# refuse a push, and a gate you argue with is a gate you turn off. The last
# step shows the two-line change that makes it blocking.
name: "Test Gap"
description: "An agent reads the pushed diff and names the behaviours it added or changed that no test exercises."

triggers:
  - type: push
    config:
      branches: ["main", "feature/*"]

steps:
  - id: "collect-diff"
    name: "Collect the pushed diff"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        python3 - <<'PY'
        import json, os, subprocess, urllib.request
        run = json.load(urllib.request.urlopen(
            f"{os.environ['LAZYAF_BACKEND_URL']}/api/pipeline-runs"
            f"/{os.environ['LAZYAF_PIPELINE_RUN_ID']}", timeout=30))
        ctx = run.get("trigger_context") or {}
        new = ctx.get("commit_sha") or "HEAD"
        old = ctx.get("old_sha") or ""
        have = lambda r: subprocess.run(
            ["git", "cat-file", "-e", r + "^{commit}"]).returncode == 0
        if not old or set(old) == {"0"} or not have(old):
            old = new + "~1" if have(new + "~1") else new
        diff = subprocess.run(["git", "diff", old, new],
                              capture_output=True, text=True).stdout
        open(".lazyaf-run/diff.patch", "w", encoding="utf-8").write(diff[:200000])
        print(f"[diff] {old[:12]}..{new[:12]} -> .lazyaf-run/diff.patch "
              f"({len(diff)} bytes)")
        PY
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "gap-scan"
    name: "Name the untested behaviour"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "List the behaviours this diff added or changed that no test asserts"
      description: |
        Read .lazyaf-run/diff.patch, then look at the test suite in the
        checkout you are sitting in.

        For every behaviour the diff ADDED or CHANGED, decide whether an
        existing test would FAIL if that behaviour regressed. A test that
        merely executes the line does not count - the question is whether it
        would go red.

        Write .lazyaf-run/test-gaps.md:

          ## Gaps
          - <source file>:<symbol> - <the behaviour, in one sentence>
            test: <the test file and test name that should exist>
            fails when: <what breaks it, concretely>

        Rules:
        - Name a concrete failing input for every gap. "Should test errors"
          is not a gap, it is a mood.
        - Skip pure refactors that changed no behaviour. Say so instead.
        - If nothing is missing, write "## Gaps" and nothing under it.
      harness:
        max_iterations: 20
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 1200

  - id: "report"
    name: "Report the gaps"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        cat .lazyaf-run/test-gaps.md
        # TO MAKE THIS BLOCKING, uncomment the two lines below. The run then
        # goes red whenever the gap list is non-empty, because on_failure is
        # `stop` and a `stop` completes the run with the STEP's verdict.
        # grep -q '^- ' .lazyaf-run/test-gaps.md \
        #   && { echo "untested behaviour in this push" >&2; exit 1; }
        echo "[test-gap] informational lane: this step fails only when the"
        echo "[test-gap] agent wrote no report at all, never on a gap."
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 5. Doc drift, and a card that fixes it

**What it does.** Checks the pushed diff against the documentation that
describes what it touched, and when the docs are now **false**, spawns a card to
fix them.

**Why you'd want it.** Documentation rots in one direction: it keeps claiming
what the code used to do. A per-commit check is the only time anyone still
remembers which sentence was true this morning.

**What it costs.** One agent step per push — plus a **second, full agent run**
every time drift is found, because the fix card runs whatever agent the template
card names. That one is an implement-and-commit run, not a read-only pass.
Budget for it.

**This is the recipe that shows `trigger:{card_id}`.** The action clones a
template card you already created, puts the clone in `in_progress`, and starts it
as an ad-hoc agent run. It is fire-and-forget: this run does not wait.

Two things to know before arming it:

* **Replace `TEMPLATE_CARD_ID` with a real card id.** A `trigger:` naming a card
  that does not exist logs *"Template card … not found for trigger action"* and
  the run continues. A typo is a silently missing fix card, not a failure.
* **The last step is not decoration.** A `trigger:` action continues to the next
  step, and reaching the end of the array completes the run *passed* regardless
  of what failed. The final `verdict` step, whose `on_failure` is `stop`, is what
  makes drift show up as a red run.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# "The diff changed behaviour. Do the docs still match?"
#
# This is the recipe that shows `trigger:{card_id}`: when the reviewer finds
# drift, the pipeline SPAWNS A CARD to fix it. The card is cloned from a
# template card you already created, put straight into in_progress, and run as
# an ad-hoc agent run. Fire and forget - this run does not wait for it.
#
# BEFORE YOU ARM THIS: replace TEMPLATE_CARD_ID below with the id of a real
# card. A `trigger:` naming a card that does not exist logs
# "Template card ... not found for trigger action" and the run CONTINUES -
# v1 behaviour, preserved deliberately - so a typo here is a silently missing
# fix card, not a failure.
#
# COST: one agent step per push, plus a SECOND agent run every time drift is
# found (the fix card runs whatever agent the template card names). Budget for
# the second one: it is a full implement-and-commit run, not a read-only pass.
name: "Doc Drift"
description: "An agent checks the pushed diff against the docs it should have updated, and spawns a fix card when they disagree."

triggers:
  - type: push
    config:
      branches: ["main"]

steps:
  - id: "collect-diff"
    name: "Collect the pushed diff"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        python3 - <<'PY'
        import json, os, subprocess, urllib.request
        run = json.load(urllib.request.urlopen(
            f"{os.environ['LAZYAF_BACKEND_URL']}/api/pipeline-runs"
            f"/{os.environ['LAZYAF_PIPELINE_RUN_ID']}", timeout=30))
        ctx = run.get("trigger_context") or {}
        new = ctx.get("commit_sha") or "HEAD"
        old = ctx.get("old_sha") or ""
        have = lambda r: subprocess.run(
            ["git", "cat-file", "-e", r + "^{commit}"]).returncode == 0
        if not old or set(old) == {"0"} or not have(old):
            old = new + "~1" if have(new + "~1") else new
        diff = subprocess.run(["git", "diff", old, new],
                              capture_output=True, text=True).stdout
        open(".lazyaf-run/diff.patch", "w", encoding="utf-8").write(diff[:200000])
        print(f"[diff] {old[:12]}..{new[:12]} -> .lazyaf-run/diff.patch "
              f"({len(diff)} bytes)")
        PY
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "doc-check"
    name: "Check the docs against the diff"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Decide whether this diff made the documentation wrong"
      description: |
        Read .lazyaf-run/diff.patch. Then read the docs in the checkout that
        describe the things it touched: README files, docs/, and the module
        docstrings of the files in the diff.

        You are looking for one thing only: a sentence that is now FALSE.
        Not a missing sentence, not an out-of-date example you would have
        written differently - a claim the code no longer honours.

        Write .lazyaf-run/doc-drift.md, first line exactly one of:

          DRIFT
          OK

        Then, under `## Stale claims`, one entry per false statement:
          - <doc file>:<line> says "<the claim>", but <file>:<symbol> now <what
            it actually does>.

        A doc that was already wrong before this diff is not drift from this
        push. Say so and answer OK.
      harness:
        max_iterations: 16
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 1200

  # Exits non-zero on drift, which fires on_failure -> the fix card. v1's
  # trigger action SPAWNS AND CONTINUES: this step goes to `verdict` either
  # way, which is what keeps the run's own status honest (see below).
  - id: "drift-gate"
    name: "Spawn a fix card on drift"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        cat .lazyaf-run/doc-drift.md
        if head -n 1 .lazyaf-run/doc-drift.md | grep -q '^DRIFT'; then
          echo "docs no longer match the code" >&2
          exit 1
        fi
    on_success: next
    on_failure: "trigger:TEMPLATE_CARD_ID"
    timeout: 120

  # WHY THIS STEP EXISTS. On the array format a `trigger:` action continues to
  # the next step, and reaching the end of the array completes the run PASSED
  # regardless of what failed earlier. That is a stated limitation of the v1
  # caller (pipeline_executor._spawn_fix_card), and it means a pipeline whose
  # LAST step fired a fix card reports green. A final step whose on_failure is
  # `stop` is how you get an honest red: `stop` completes the run with THIS
  # step's verdict.
  - id: "verdict"
    name: "Fail the run if the docs drifted"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        head -n 1 .lazyaf-run/doc-drift.md | grep -q '^OK'
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 6. A reviewer that only cares about migrations

**What it does.** Reviews Alembic migrations for the operations that take a
database down: a dropped or renamed column with no two-step deploy, a `NOT NULL`
column with no default over a non-empty table, a unique index over data that may
already violate it, an unbatched data migration, a downgrade that loses data.

**Why you'd want it.** Migration review is high-value and low-frequency —
exactly the profile that a human reviewer's attention is worst at. The failure
mode is also unusually expensive and unusually easy to state, which makes it a
good prompt.

**What it costs.** One **short** agent turn on every push, and a full review turn
only when a file under `backend/alembic/` actually changed.

### The honest version of "only runs when"

There is no path filter on a trigger: a `push` trigger reads exactly one key,
`branches`, matched with `fnmatch`. And there is no conditional step:
`on_success` / `on_failure` take `next`, `stop`, `trigger:{card_id}` and
`merge:{branch}`, none of which means "skip the next one". The graph format the
executor runs internally *does* have conditional edges — but
`.lazyaf/pipelines/*.yaml` has no way to author a graph, because
`PipelineYaml.steps` is a flat list.

So this recipe approximates it: a cheap script step computes the scope and
writes it to the shared workspace, and the agent's first instruction is to read
that file and finish immediately when it says `SKIP`. **You still pay for one
short round trip on commits that touch no migration.** On a self-hosted endpoint
that is a second of GPU; on a paid API it is a small but real charge on every
push — which is the reason this recipe leads with `endpoint:`.

There is a hack that skips the step entirely, and it is worth knowing so you can
decide against it: give the gate step `on_success: stop` and `on_failure: next`,
and invert its exit code. When nothing matched, the gate exits 0, `stop`
completes the run green, and the agent never runs. When something matched, the
gate exits **1** and the run continues. The price is that the gate's `StepRun` is
recorded FAILED every time there *is* a migration to review — you have bought
conditional execution with a permanently misleading status. Don't.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# A migration safety reviewer that only does work when alembic/ changed.
#
# READ THIS FIRST - THE HONEST VERSION OF "ONLY RUNS WHEN":
# There is no path filter on a trigger. A `push` trigger reads exactly one
# key, `branches`, and matches it with fnmatch. And there is no conditional
# step in the repo YAML format: on_success / on_failure take `next`, `stop`,
# `trigger:{card_id}` and `merge:{branch}` - none of which means "skip the
# next step". The graph format the executor runs internally DOES have
# conditional edges, but `.lazyaf/pipelines/*.yaml` has no way to author a
# graph: PipelineYaml.steps is a flat list.
#
# So "only when alembic/ changes" is approximated: a cheap script step decides
# the scope, and the agent step is told to stop immediately when the scope is
# SKIP. You still pay for ONE short round trip on commits that touch no
# migration. On a self-hosted endpoint that is a second of GPU; on a paid API
# it is a small but real charge on every push, which is the reason this recipe
# leads with `endpoint:`.
#
# COST: one short agent turn on every push; a full review turn only when a
# migration file changed.
name: "Migration Review"
description: "Review Alembic migrations for unsafe operations. A script step decides the scope so the agent no-ops on commits that touch no migration."

triggers:
  - type: push
    config:
      branches: ["main", "feature/*"]

steps:
  - id: "scope"
    name: "Did this push touch alembic/?"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        python3 - <<'PY'
        import json, os, subprocess, urllib.request
        run = json.load(urllib.request.urlopen(
            f"{os.environ['LAZYAF_BACKEND_URL']}/api/pipeline-runs"
            f"/{os.environ['LAZYAF_PIPELINE_RUN_ID']}", timeout=30))
        ctx = run.get("trigger_context") or {}
        new = ctx.get("commit_sha") or "HEAD"
        old = ctx.get("old_sha") or ""
        have = lambda r: subprocess.run(
            ["git", "cat-file", "-e", r + "^{commit}"]).returncode == 0
        if not old or set(old) == {"0"} or not have(old):
            old = new + "~1" if have(new + "~1") else new
        names = subprocess.run(
            ["git", "diff", "--name-only", old, new, "--", "backend/alembic/"],
            capture_output=True, text=True).stdout.split()
        verdict = "REVIEW" if names else "SKIP"
        body = subprocess.run(["git", "diff", old, new, "--",
                               "backend/alembic/"],
                              capture_output=True, text=True).stdout
        open(".lazyaf-run/migration-scope", "w").write(verdict + "\n")
        open(".lazyaf-run/migration.patch", "w", encoding="utf-8").write(body)
        print(f"[scope] {verdict}: {len(names)} migration file(s) changed")
        for n in names:
            print(f"  {n}")
        PY
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "migration-review"
    name: "Review the migration"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Review the Alembic migration in this push for unsafe operations"
      description: |
        FIRST, read .lazyaf-run/migration-scope. If it says SKIP, write a
        two-line .lazyaf-run/migration.md - the word SAFE, then the line
        "no migration changed in this push" - and finish immediately. Do not
        read anything else. This is the common case and it should cost one
        turn.

        If it says REVIEW, read .lazyaf-run/migration.patch and judge it
        against the tables it touches (backend/app/models/).

        Write .lazyaf-run/migration.md, first line exactly one of:

          UNSAFE
          SAFE

        UNSAFE means at least one of:
        - a column or table is dropped, or renamed, with no two-step
          deploy (add-and-backfill first, drop in a later release);
        - a NOT NULL column is added with no server_default and no backfill,
          so the migration fails on any non-empty table;
        - a unique constraint or index is added over data that may already
          violate it;
        - a data migration with no batching over a table that can be large;
        - no downgrade path, or a downgrade that loses data silently.

        Under `## Why`, one sentence per finding, naming the operation and
        the table. Under `## Safer`, the two-step version.
      harness:
        max_iterations: 16
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 900

  - id: "verdict"
    name: "Fail on an unsafe migration"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        cat .lazyaf-run/migration.md
        if head -n 1 .lazyaf-run/migration.md | grep -q '^UNSAFE'; then
          echo "migration review reported UNSAFE" >&2
          exit 1
        fi
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 7. Explain this failure

**What it does.** Runs the suite. When it goes red, an agent reads the failure
output, finds the first genuine failure, opens the file the traceback names, and
writes what probably caused it — before a human opens the run.

**Why you'd want it.** The gap between "CI is red" and "I know why" is where CI
time actually goes. The agent is not fixing anything; it is doing the first ten
minutes of triage, in the checkout, while you were still doing something else.

**What it costs.** One agent step per push, including green pushes. The prompt
tells the agent to stop immediately when there was no failure, so a green push
costs one short turn. It cannot cost zero — see
[recipe 6](#6-a-reviewer-that-only-cares-about-migrations) for why.

### The mechanism, and it needs no config

**An agent step is handed the previous step's logs automatically.** The backend
reads the `StepRun` at `step_index - 1` and renders it into the prompt under a
`## Previous Step Output` heading, capped first so a 40 MB pytest log does not
blow the prompt. There is no key to set.

It is strictly the step *before* this one — not "the last failing step". Insert
anything between the test step and the explainer and it will explain that
instead.

### And the trap it walks straight into

`on_failure: next` is what lets the run continue past a red suite so the
explainer can see it. It is **not** what keeps the run red. On the array format
the run's verdict is whatever the last `stop` says, and walking off the end of
the array completes the run **passed** — so this pipeline, written naively,
reports green on a failing suite.

That is why the test step writes a marker file and the last step re-reads it and
exits non-zero with `on_failure: stop`. Every "continue past a failure" pipeline
needs that final step.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# "Explain this failure": when the test step goes red, an agent reads the
# failure and says what probably caused it - before a human opens the run.
#
# THE MECHANISM THAT MAKES THIS WORK, and it needs no config:
# an agent step is handed the PREVIOUS step's logs automatically. The backend
# reads the StepRun at step_index - 1 and renders them into the prompt under a
# `## Previous Step Output` heading (pipeline_executor._load_previous_step_output
# + services/agent_prompt.py). It is strictly the step BEFORE this one - not
# "the last failing step" - so the explainer must sit immediately after the
# step it explains. Insert anything between them and it explains that instead.
#
# The logs are capped before rendering, so a 40 MB pytest log does not blow the
# prompt; the agent sees the tail, which is where the failure is.
#
# COST: one agent step per push, including the pushes where everything passed.
# The task tells the agent to stop immediately when there is no failure, so a
# green push costs one short turn. See migration-review.yaml for why it cannot
# cost zero.
name: "Test + Explain"
description: "Run the suite; when it fails, an agent reads the failure output and writes the likely cause before anyone opens the run."

triggers:
  - type: push
    config:
      branches: ["main", "feature/*"]

steps:
  # on_failure: next is the whole trick - the run CONTINUES past a red suite so
  # the explainer gets to see it. The failure is recorded in a marker the last
  # step re-reads, because a step that continues cannot make the run red by
  # itself (see the `verdict` step).
  - id: "tests"
    name: "Run the suite"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        rm -f .lazyaf-run/tests-failed
        cd backend
        uv sync --extra test
        if ! uv run pytest ../tdd/unit -q; then
          touch /workspace/repo/.lazyaf-run/tests-failed
          exit 1
        fi
    on_success: next
    on_failure: next
    timeout: 1800

  - id: "explain"
    name: "Explain the failure"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Explain why the previous step failed"
      description: |
        The previous step's output is in this prompt, under
        `## Previous Step Output`. It is the tail of the test run.

        If .lazyaf-run/tests-failed does not exist, the suite passed: write
        the single line "PASSED" to .lazyaf-run/failure.md and finish. Do not
        read anything else.

        Otherwise: find the FIRST genuine failure in that output - not the
        summary line, the first real assertion or error - and write
        .lazyaf-run/failure.md:

          ## Failing test
          <the test id, verbatim>

          ## What the output says
          <the assertion or exception, quoted, two or three lines>

          ## Likely cause
          <one paragraph. Open the file the traceback names and say what
           about it is wrong. If the output does not let you tell, say
           "cannot tell from this output" and name what you would need.>

          ## First thing to check
          <one concrete command or one file:line>

        Do not fix anything. Do not run the suite again - it takes as long as
        it just took, and a second red run tells nobody anything new.
      harness:
        max_iterations: 14
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 900

  # On the array format the run's verdict is whatever the last `stop` says, and
  # reaching the end of the array completes it PASSED. A step that continued
  # with `next` therefore cannot fail the run on its own. This step re-states
  # the test result so a red suite is a red run.
  - id: "verdict"
    name: "Re-state the test result"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        cat .lazyaf-run/failure.md 2>/dev/null || echo "(no explanation written)"
        if [ -f .lazyaf-run/tests-failed ]; then
          echo "the suite failed; see the explanation above" >&2
          exit 1
        fi
        echo "[verdict] suite passed"
    on_success: next
    on_failure: stop
    timeout: 120
```

---

## 8. Fan-out: K attempts, one checkout

**What it does.** Three agents attempt the same change on three branches, then a
script step runs the suite against each and reports which passed and how big
each diff was.

**Why you'd want it.** This is the owner's own research hypothesis: *does a
wider search beat a better model?* Three cheap attempts against a self-hosted
model, measured mechanically, is a different bet from one expensive attempt —
and it is a bet you can only settle with numbers, which is why the judge is a
script and not a fourth agent. Paying a model to do arithmetic is how an eval
becomes a vibe.

**What it costs. Three full agent runs per trigger**, each of which may commit.
This is not a per-commit lane. It fires on a card reaching `in_review`, so a
human asked for it.

### What is honest about this today

1. **The attempts are sequential, not parallel.** A repo YAML pipeline is a flat
   list and the executor walks it one step at a time. Parallel fan-out needs the
   graph format's edges and fan-in, which the executor runs internally but which
   `.lazyaf/pipelines/` cannot author.

2. **All three attempts share one checkout.** A run owns one workspace volume
   and every step mounts it. The *branches* are separate — each agent step
   commits and pushes its own — but the working tree is not, and the wrapper's
   `git checkout -B <branch>` creates the branch **at current HEAD**. Without the
   reset steps below, attempt 2 branches from attempt 1's commit and you are
   measuring a relay race.

3. **The reset steps are the isolation**, and they are a workaround.
   `git checkout --detach <base>` plus `git clean -fd` puts the tree back where
   attempt 1 found it. What it cannot buy back is concurrency.

Per-worker workspace lanes are being built right now: as of 2026-08-30
`backend/app/services/workspace/worker_key.py` defines the lane key and
`generate_volume_name()` takes it, but the pipeline executor still asks for one
volume per run and no YAML key selects a lane. Check whether that is still true
before you rely on the resets.

An explicit `branch:` per attempt is what makes them comparable — without it each
agent step still gets its own branch, but the name is derived from the `StepRun`
id and the judge could not find it. `branch:` is also the one way to push to the
branch the run was triggered on, so never point it at a branch a push trigger
watches.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# K agents attempt the same change, each on its own branch; a later step
# measures them and names the winner. This is the owner's own research
# hypothesis - "does a wider search beat a better model?" - expressed in the
# format that exists today.
#
# WHAT IS HONEST ABOUT THIS TODAY, and it is a lot:
#
#  1. THE ATTEMPTS ARE SEQUENTIAL, NOT PARALLEL. A repo YAML pipeline is a
#     flat list and the executor walks it one step at a time. Parallel fan-out
#     needs the graph format (edges + fan-in), which the executor runs
#     internally but which `.lazyaf/pipelines/*.yaml` has no way to author.
#
#  2. ALL THREE ATTEMPTS SHARE ONE CHECKOUT. A run owns one workspace volume,
#     lazyaf-ws-{run_id}, and every step mounts it. The BRANCHES are separate -
#     each agent step commits and pushes to its own - but the working tree is
#     not, and the agent wrapper's `git checkout -B <branch>` creates the
#     branch AT CURRENT HEAD. Without the reset steps below, attempt 2 would
#     branch from attempt 1's commit and you would be measuring a relay race,
#     not a fan-out.
#
#     Per-worker workspace lanes are being built right now: as of 2026-08-30
#     backend/app/services/workspace/worker_key.py defines the lane key and
#     generate_volume_name() takes it, but the pipeline executor still asks for
#     one volume per run and no YAML key selects a lane. Check whether that is
#     still true before you rely on the reset steps.
#
#  3. THE RESET STEPS ARE THE ISOLATION. `git checkout --detach <base>` plus
#     `git clean -fd` puts the tree back where attempt 1 found it. It is a
#     workaround, and it costs you the real thing a per-worker checkout would
#     buy: concurrency.
#
# COST: THREE FULL AGENT RUNS PER TRIGGER, each of which may commit. This is
# not a per-commit lane. It fires on a card reaching in_review, so a human
# asked for it. Point it at a self-hosted endpoint before pointing it at an
# API - the whole hypothesis is about spending width instead of depth, and
# width is what a paid API charges for.
name: "Fan-Out Attempts"
description: "Three agents attempt the same change on three branches, then a judge step runs the suite against each and names the winner. Sequential today: one run owns one checkout."

triggers:
  - type: card_complete
    config:
      status: in_review
    on_pass: nothing
    on_fail: nothing

steps:
  - id: "baseline"
    name: "Record the base commit"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        git rev-parse HEAD > .lazyaf-run/base-sha
        echo "[baseline] $(cat .lazyaf-run/base-sha)"
    on_success: next
    on_failure: stop
    timeout: 120

  # An explicit `branch:` is what makes the attempts comparable. Without it
  # each agent step still gets its OWN branch - lazyaf/agent-<8 hex of the
  # StepRun id> - but the name is not knowable ahead of time, so the judge step
  # could not find them.
  #
  # `branch:` is also the ONE way to push to the branch the run was triggered
  # on. Never point it at a branch a push trigger watches: the push fires the
  # trigger, the trigger starts the run again, and nothing depth-caps the loop.
  - id: "attempt-1"
    name: "Attempt 1"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      branch: "lazyaf/fanout-1"
      commit:
        enabled: true
        message: "fanout attempt 1"
        push: true
      task: "Implement the card, then make the suite green"
      description: |
        Implement the change this card describes. Then run the tests that
        cover it and keep working until they pass.

        Do not touch anything outside what the card asks for. A smaller diff
        that passes beats a larger one that also passes - a later step is
        going to compare you against two other attempts.
      harness:
        max_iterations: 30
        temperature: 0.6
    on_success: next
    on_failure: next
    timeout: 2700

  - id: "reset-1"
    name: "Reset the tree for attempt 2"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        git checkout --detach "$(cat .lazyaf-run/base-sha)"
        git clean -fd
        echo "[reset] back at $(git rev-parse --short HEAD)"
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "attempt-2"
    name: "Attempt 2"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      branch: "lazyaf/fanout-2"
      commit:
        enabled: true
        message: "fanout attempt 2"
        push: true
      task: "Implement the card, then make the suite green"
      description: |
        Implement the change this card describes. Then run the tests that
        cover it and keep working until they pass.

        Do not touch anything outside what the card asks for. A smaller diff
        that passes beats a larger one that also passes - a later step is
        going to compare you against two other attempts.
      harness:
        max_iterations: 30
        temperature: 0.6
    on_success: next
    on_failure: next
    timeout: 2700

  - id: "reset-2"
    name: "Reset the tree for attempt 3"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        git checkout --detach "$(cat .lazyaf-run/base-sha)"
        git clean -fd
        echo "[reset] back at $(git rev-parse --short HEAD)"
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "attempt-3"
    name: "Attempt 3"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      branch: "lazyaf/fanout-3"
      commit:
        enabled: true
        message: "fanout attempt 3"
        push: true
      task: "Implement the card, then make the suite green"
      description: |
        Implement the change this card describes. Then run the tests that
        cover it and keep working until they pass.

        Do not touch anything outside what the card asks for. A smaller diff
        that passes beats a larger one that also passes - a later step is
        going to compare you against two other attempts.
      harness:
        max_iterations: 30
        temperature: 0.6
    on_success: next
    on_failure: next
    timeout: 2700

  # The judge is a SCRIPT step on purpose. "Which attempt passes the suite with
  # the smallest diff" is arithmetic, and paying a model to do arithmetic is
  # how an eval becomes a vibe.
  - id: "judge"
    name: "Run the suite against each attempt"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        set -e
        cd /workspace/repo
        BASE="$(cat .lazyaf-run/base-sha)"
        git fetch origin
        : > .lazyaf-run/fanout-results.txt
        for n in 1 2 3; do
          BR="lazyaf/fanout-$n"
          if ! git rev-parse --verify "origin/$BR" >/dev/null 2>&1; then
            echo "$BR MISSING - -" >> .lazyaf-run/fanout-results.txt
            continue
          fi
          git checkout --detach "origin/$BR"
          git clean -fd
          CHANGED="$(git diff --shortstat "$BASE" HEAD | tr -d '\n')"
          if (cd backend && uv sync --extra test >/dev/null 2>&1 \
              && uv run pytest ../tdd/unit -q >/dev/null 2>&1); then
            echo "$BR PASS ${CHANGED:-no change}" >> .lazyaf-run/fanout-results.txt
          else
            echo "$BR FAIL ${CHANGED:-no change}" >> .lazyaf-run/fanout-results.txt
          fi
          git checkout --detach "$BASE"
        done
        echo "---- fan-out results ----"
        cat .lazyaf-run/fanout-results.txt
        grep -q ' PASS ' .lazyaf-run/fanout-results.txt || {
          echo "no attempt produced a green suite" >&2
          exit 1
        }
    on_success: next
    on_failure: stop
    timeout: 3600
```

---

## 9. Release notes from the commit range

**What it does.** Reads every non-merge commit since the last tag (or the last
50 if the repo has never been tagged), reads the diff where a subject line is not
enough, and commits `CHANGELOG-draft.md` to its own branch.

**Why you'd want it.** The commit log is the wrong artifact for a human and the
right artifact for a model: it knows what changed and needs someone to say what
that *means*. Committing the draft to a branch rather than to trunk keeps a human
in the loop where they belong — you read it before you tag.

**What it costs.** One agent step, when you fire it.

**This recipe also demonstrates `triggers: []`.** It fires on nothing. There is
no scheduler in LazyAF: `TriggerService` handles `push` and `card_complete` and
nothing polls a clock. "Nightly" is your host's cron calling
`POST /api/pipelines/{id}/run`, which is also what the Run button does.
`schedule` is an accepted `trigger_type` on that endpoint — a **label** for the
run, not a scheduler.

This is the only recipe here where the agent commits and pushes. It goes to a
`lazyaf/` branch that no trigger in this catalog watches, which is what keeps
that push from starting another run.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# Release notes written from the actual commit range, and committed to a
# branch you can read before you tag.
#
# NOTE THE EMPTY TRIGGER LIST. This pipeline fires on nothing. There is no
# built-in scheduler: TriggerService handles exactly two events, `push` and
# `card_complete`, and nothing in the backend polls a clock. "Nightly" is your
# host's cron calling
#
#     POST /api/pipelines/{pipeline_id}/run   {"trigger_type": "manual"}
#
# which is also what the Run button does. `schedule` is an accepted
# trigger_type on that endpoint - it is a LABEL for the run, not a scheduler.
#
# COST: one agent step per run, and you choose when to run it. This is the
# shape every expensive lane should have.
name: "Release Notes"
description: "An agent reads the commit range since the last tag and writes CHANGELOG-draft.md, committed to its own branch. Fired by hand or by your own cron, never by a push."

triggers: []

steps:
  - id: "commit-range"
    name: "Collect the commit range"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        # The clone carries full history and tags, so the range is a local
        # question. If the repo has never been tagged, fall back to a window.
        if FROM="$(git describe --tags --abbrev=0 2>/dev/null)"; then
          echo "[range] since tag $FROM"
        else
          FROM="$(git rev-list --max-count=50 HEAD | tail -n 1)"
          echo "[range] no tag found; using the last 50 commits from $FROM"
        fi
        echo "$FROM" > .lazyaf-run/range-from
        git log --no-merges --pretty='%h %s' "$FROM..HEAD" \
          > .lazyaf-run/commits.txt
        git diff --stat "$FROM..HEAD" > .lazyaf-run/range-stat.txt
        echo "[range] $(wc -l < .lazyaf-run/commits.txt) commits"
        cat .lazyaf-run/commits.txt
    on_success: next
    on_failure: stop
    timeout: 300

  # This step COMMITS, unlike every read-only reviewer in this catalog.
  # `branch:` is explicit so the draft lands somewhere you can find. It is a
  # `lazyaf/` branch and no trigger in this catalog watches `lazyaf/*`, which
  # is what keeps the push from starting another run.
  - id: "write-notes"
    name: "Write the release notes"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      branch: "lazyaf/release-notes"
      commit:
        enabled: true
        message: "docs: release notes draft"
        push: true
      task: "Write CHANGELOG-draft.md for the commit range"
      description: |
        .lazyaf-run/commits.txt is every non-merge commit in this release
        range, one per line. .lazyaf-run/range-stat.txt is the diffstat.

        Write CHANGELOG-draft.md at the repository root:

          # <the range, e.g. v0.4.0..HEAD>

          ## Added
          ## Changed
          ## Fixed
          ## Breaking

        Rules:
        - One bullet per user-visible change, in the words of someone who
          uses this software - not in the words of the commit message.
        - Group the commits that are one change into one bullet. Ten commits
          that build one feature are one line.
        - Read the actual diff when a subject line is not enough. You are
          sitting in the checkout.
        - Drop pure chores: version bumps, formatting, dependency pins with
          no behaviour change. A changelog nobody trims is a changelog
          nobody reads.
        - Leave a section out entirely if it is empty. Do not write
          "None".
        - Under Breaking, say what a reader has to DO, not just what moved.
      harness:
        max_iterations: 24
        temperature: 0.2
    on_success: next
    on_failure: stop
    timeout: 1800
```

---

## 10. The cheap lane

**What it does.** Leak gate, dependency sync, fast unit tier. That is all.

**Why you'd want it.** This is the half of the cheap/expensive split people get
wrong, by putting an agent in it. The rule: **a step belongs here if a machine
can answer it the same way twice.** Secrets, syntax, types, the fast tier.
Anything needing judgement goes in the nightly lane.

**What it costs.** Zero model calls. Wall clock is the whole budget, which is
why it can run on every push to every branch.

Ordering matters: the cheapest step that can refuse a push goes first. Every
second saved is a second a leaked key is not sitting in a public repo.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# THE CHEAP LANE. Everything here is deterministic, fast and free: no model
# call, no GPU, no API key. This is what should run on every push, on every
# branch, and it is the half of the split most people get wrong by putting an
# agent in it.
#
# The rule this lane follows: a step belongs here if a machine can answer it
# the same way twice. Secrets, syntax, types, the fast unit tier. Anything
# that needs judgement goes in nightly-expensive.yaml.
#
# COST: zero model calls. Wall clock is the whole budget.
name: "Per-Commit (cheap)"
description: "The free, deterministic lane: leak gate, then the fast unit tier. Runs on every push to every branch because it costs nothing to."

triggers:
  - type: push
    config:
      branches: ["*"]

steps:
  # Cheapest first, and the one that must never be skipped. A credential that
  # reaches the repo is already public; the sooner this runs, the smaller the
  # window.
  - id: "secret-scan"
    name: "Secret scan"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        python3 .github/scripts/scan_repo_secrets.py
    on_success: next
    on_failure: stop
    timeout: 120

  - id: "deps"
    name: "Sync dependencies"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        cd backend
        uv sync --all-extras
    on_success: next
    on_failure: stop
    # Cold-cache dependency resolution on a fresh workspace volume needs
    # headroom. HOME is /workspace/home, which persists across the steps of
    # this run, so the later steps get a warm cache.
    timeout: 720

  - id: "unit"
    name: "Fast unit tier"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        cd backend
        uv run pytest ../tdd/unit -q
    on_success: next
    on_failure: stop
    timeout: 1800
```

---

## 11. The expensive lane

**What it does.** The other half: full suite, then an AI review, a test-gap pass
and a doc-drift pass over everything that landed in the last day, then one report
with one verdict.

**Why you'd want it.** Judgement questions are worth asking — just not thirty
times a day. Batching them to a nightly window over a *day's* diff also gives the
model a better question than any single commit does: it can see the shape of the
whole change.

**What it costs.** Three agent steps per run, once a night instead of once a
push. On a trunk taking 30 pushes a day that is a 30x difference, which is the
entire reason the split exists. Every step names `endpoint:`, so the bill is
GPU-hours on hardware you own; swap in `agent: claude-code` on the one question
that deserves the better model.

### How to split your own pipelines

| | cheap lane | expensive lane |
|---|---|---|
| answers | deterministic | judgement |
| takes | seconds | minutes |
| costs | wall clock | a bill or a GPU |
| runs | every push, every branch | nightly, or on a card |
| fails the push | yes | no — it reports |

### How it fires

Not on a push. `triggers: []`, and your host's cron:

```
0 2 * * *  curl -fsS -XPOST \
  -H 'content-type: application/json' -d '{"trigger_type":"schedule"}' \
  http://lazyaf:8000/api/pipelines/<pipeline_id>/run
```

`schedule` is in the accepted `trigger_type` vocabulary, so the run is labelled
as scheduled in the run list and in the usage rollup.

```yaml
# EXAMPLE - INERT. Copy into .lazyaf/pipelines/ to arm it.
#
# THE EXPENSIVE LANE, and the other half of the split. Everything here needs
# judgement, takes minutes, and costs either GPU time or money. None of it
# belongs on a push.
#
# HOW IT FIRES: not on a push. `triggers: []` means nothing starts it
# automatically, because there is no scheduler in LazyAF - TriggerService
# handles `push` and `card_complete` and nothing else. Your host's cron does:
#
#     0 2 * * *  curl -fsS -XPOST \
#       -H 'content-type: application/json' -d '{"trigger_type":"schedule"}' \
#       http://lazyaf:8000/api/pipelines/<pipeline_id>/run
#
# `schedule` is in the accepted trigger_type vocabulary on that endpoint, so
# the run is LABELLED as scheduled in the run list and in the usage rollup.
# It is a label, not a scheduler.
#
# HOW TO DECIDE WHAT GOES WHERE:
#   cheap lane      deterministic, seconds, same answer twice     every push
#   expensive lane  judgement, minutes, a bill or a GPU           nightly
#
# COST: three agent steps per run, once a night rather than once a push. On a
# trunk taking 30 pushes a day that is a 30x difference, which is the entire
# reason the split exists. Every step here names `endpoint:` so the bill is
# GPU-hours on hardware you own (cost_source="gpu-node") rather than API
# dollars; swap in `agent: claude-code` per step when you want the better
# model on the one question that deserves it.
name: "Nightly (expensive)"
description: "The judgement lane: full suite, then an AI review, a test-gap pass and a doc-drift pass over everything that landed since yesterday. Fired by your own cron, never by a push."

triggers: []

steps:
  - id: "collect-range"
    name: "Collect everything that landed since yesterday"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        mkdir -p .lazyaf-run
        grep -qxF '/.lazyaf-run/' .git/info/exclude 2>/dev/null \
          || echo '/.lazyaf-run/' >> .git/info/exclude
        FROM="$(git rev-list -1 --before='24 hours ago' HEAD || true)"
        [ -n "$FROM" ] || FROM="$(git rev-list --max-count=20 HEAD | tail -n 1)"
        echo "$FROM" > .lazyaf-run/range-from
        git diff "$FROM..HEAD" > .lazyaf-run/diff.patch
        git log --no-merges --pretty='%h %s' "$FROM..HEAD" \
          > .lazyaf-run/commits.txt
        echo "[range] $FROM..HEAD, $(wc -l < .lazyaf-run/commits.txt) commits, \
        $(wc -c < .lazyaf-run/diff.patch) bytes of diff"
    on_success: next
    on_failure: stop
    timeout: 300

  - id: "full-suite"
    name: "Full test suite"
    type: script
    config:
      image: "lazyaf-test-runner:dev"
      command: |
        set -e
        cd backend
        uv sync --all-extras
        uv run pytest ../tdd/unit ../tdd/integration -q
    on_success: next
    on_failure: stop
    timeout: 3600

  - id: "review"
    name: "AI review of the day's diff"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Review everything that landed in the last day"
      description: |
        .lazyaf-run/diff.patch is everything that landed since yesterday and
        .lazyaf-run/commits.txt lists the commits.

        Write .lazyaf-run/review.md with a `## Verdict` line - BLOCKER or OK -
        then `## Findings` as `file:line - one sentence` entries. Only report
        what you can point at a line for. BLOCKER means unsafe to ship: data
        loss, a credential, a broken migration, an unhandled failure path, or
        a test that cannot fail.
      harness:
        max_iterations: 30
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 2700

  - id: "gaps"
    name: "Test-gap pass"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Name the behaviour this day's work left untested"
      description: |
        Read .lazyaf-run/diff.patch, then the test suite in the checkout.

        For every behaviour the diff added or changed, decide whether an
        existing test would GO RED if it regressed. Write
        .lazyaf-run/test-gaps.md as `## Gaps` and, per gap, the source symbol,
        the test that should exist, and a concrete input that breaks it.
      harness:
        max_iterations: 30
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 2700

  - id: "docs"
    name: "Doc-drift pass"
    type: agent
    config:
      agent: openai-harness
      endpoint: "local-4090"
      commit: false
      task: "Find the documentation this day's work made false"
      description: |
        Read .lazyaf-run/diff.patch, then the docs describing what it touched.

        You are looking for sentences that are now FALSE - not missing ones.
        Write .lazyaf-run/doc-drift.md: first line DRIFT or OK, then one entry
        per stale claim as `doc:line says "<claim>", but <file>:<symbol> now
        <what it does>`.
      harness:
        max_iterations: 24
        temperature: 0
    on_success: next
    on_failure: stop
    timeout: 2700

  # One report, one verdict. A nightly lane that fails silently is a nightly
  # lane nobody reads, so this step is the only one allowed to be red.
  - id: "report"
    name: "Nightly report"
    type: script
    config:
      image: "lazyaf-base:dev"
      command: |
        set -e
        cd /workspace/repo
        for f in review.md test-gaps.md doc-drift.md; do
          echo "================ $f ================"
          cat ".lazyaf-run/$f" 2>/dev/null || echo "(not written)"
        done
        FAIL=0
        if grep -qi '^BLOCKER' .lazyaf-run/review.md 2>/dev/null; then
          echo "review reported a BLOCKER" >&2
          FAIL=1
        fi
        if head -n 1 .lazyaf-run/doc-drift.md 2>/dev/null | grep -q '^DRIFT'; then
          echo "the docs drifted" >&2
          FAIL=1
        fi
        if [ "$FAIL" != "0" ]; then
          exit 1
        fi
        echo "[nightly] clean"
    on_success: next
    on_failure: stop
    timeout: 300
```
