# Milestone 13 — Phase Specifications, Metrics Math & Method

**Goal**: turn the Milestone 13 scoping decisions into something implementable — five phases with concrete deliverables and named contract tests, a metrics layer defined mathematically enough that two people computing it get the same number, a variance rule that decides when the board is allowed to rank anything, and the `METHOD.md` that ships inside every exported bundle.

> **Why this doc exists separately from the roadmap:** the roadmap says "median cost-to-solve" and "the board refuses to rank overlapping intervals". Both of those are one sentence away from being wrong in a way nobody notices — cost-to-solve computed over solved trials only is survivorship bias, and "overlapping marginal intervals" is not the test anyone should run. This document pins the arithmetic before the board is built, because a board with a subtly-wrong denominator is worse than no board: it produces confident, publishable, false numbers.

---

## 13.0 — Conventions used by every phase

### Vocabulary (fixed; the code uses these words)

| Term | Meaning |
|------|---------|
| **case** | one `BenchmarkCase`: a repo pinned at `base_commit_sha` + a task + `fail_to_pass` / `pass_to_pass` oracle ids |
| **variant** | the thing being compared: `(strategy_template, model_assignment, prompt_version, loop_policy)`. The board ranks variants, never trials. |
| **trial** | one execution of one variant against one case. `R` trials of the same (variant, case) pair are **repeats**. |
| **iteration** | one pipeline run of the strategy graph inside a trial. Fan-out happens *within* an iteration. |
| **oracle** | the pinned test command + the `fail_to_pass` / `pass_to_pass` id sets. Agent-inaccessible by construction (see oracle tampering, 13.2). |
| **cap** | the trial's hard limits: `budget_usd`, `max_iterations`, `wall_clock_ceiling_s`. |
| **board** | the aggregation surface (`GET /api/bench/board`) + its UI. |

### Standing rules this milestone inherits

- **R3** — the case file schema and the bundle manifest are wire contracts: one pydantic model each, round-trip tested (`disk -> API -> DB -> export -> disk`).
- **R4** — every phase raises `tdd/tier_floors.json`; no `pass # the oracle guarantees this` tests. Metric functions are pure and tested against golden fixtures, not against live trials.
- **R6** — fan-out trials in integration tests use **named volumes**, one per worker, not `tmp_path` bind mounts. A fan-out that only works on bind mounts is not a fan-out.
- **R8** — the effectiveness board is a UI surface; 13.3 and 13.4 each ship a Playwright spec named in the deliverables.
- **R1 / R7** — trials execute through the *default* executor path and the dogfood pipeline gains a `bench validate` step at 13.1; a benchmark harness that runs on a private code path measures a thing nobody else runs.

### Additive model fields this milestone needs

These are additions to the models already specced in "Specification Layer Models". They are listed here once so the migrations in 13.1/13.2 are not surprises.

```python
# BenchmarkCase (13.1)
    test_command: str            # the pinned oracle invocation, e.g. "pytest -q"
    oracle_file_hashes: dict     # {path: sha256} for every file carrying an oracle id
    quarantined_tests: list[str] # ids ejected by the flake screen, kept for the record
    reference_patch: str | None  # gold patch, if upstream has one -> enables the solvable control
    solvable_verified: bool      # gold-patch control passed
    machine_profile_required: str | None

# Trial (13.2)
    integration_merges_attempted: int   # denominator for conflict rate
    budget_overrun_usd: Decimal         # tokens already in flight when the cap hit; honesty field
    queued_ms: int                      # excluded from wall_clock; recorded, not hidden
    machine_profile: str                # "local-16c-64g" | "runpod-a100" | ...
    host_concurrency_limit: int         # what the fan-out was actually allowed to run
    error_class: str | None             # "infra" | "provider" | "oracle_tampered" | "base_state_invalid"
    target_met: bool                    # all fail_to_pass green at final commit
    clean: bool                         # zero pass_to_pass broken at final commit
```

`solved == target_met and clean`. Storing the two halves separately is not pedantry — it is the only way to compute a regression rate that is not definitionally zero (see M4).

---

## Part 1 — The metrics, defined

### Notation

For a variant `v` over a case set `C` with `R` repeats per case:

```
T(v, c)        = the set of trials of variant v on case c,  |T(v, c)| = R
t              = one trial
X_i(t)         = cumulative USD spent through iteration i of t   (monotone increasing)
W_i(t)         = cumulative wall-clock ms through iteration i of t (monotone increasing)
green_i(t)     = 1 if, at iteration i's commit, all fail_to_pass pass AND no pass_to_pass broken
solve_i(t)     = min{ i : green_i(t) = 1 }, or INF if never
solved(t)      = 1 if solve_i(t) is finite, else 0
cost(t)        = X_final(t)         (total spend, including the failed iterations)
```

Every metric below is computed by a pure function in `backend/app/services/bench_metrics.py` and is unit-tested against a golden fixture file — no metric is computed inline in an endpoint or a template.

### The three ways a trial ends, and what each does to a denominator

| Terminal status | Counts as | In cost denominators | Notes |
|---|---|---|---|
| `solved` | success | yes | oracle green at final commit |
| `budget_exhausted` / `max_iterations` / `wall_clock_exhausted` | **censored failure** | yes (as spend), no (as a solve) | we know cost-to-solve > what was spent; we do NOT know it is infinite |
| `error` (`infra`, `provider`, `oracle_tampered`, `base_state_invalid`) | **excluded** | no | excluded from every metric AND reported as `error_rate` |

**The error rule, stated once:** infrastructure and provider failures are excluded from metric denominators and reported separately. An agent that writes bad code is *not* an error — it is an unsolved trial. If `error_rate(v) > 0.10`, the board marks every number for `v` **UNRELIABLE** and refuses to rank it. Silent exclusion of errors is the single easiest way to manufacture a good result, so exclusion is always accompanied by a printed count.

---

### M1 — Solve-rate at a shared budget  *(the fairness normalizer)*

**Unit:** fraction (rendered as %).

```
solved_B,W(t) = 1 if solve_i(t) = k is finite
                    AND X_k(t) <= B
                    AND W_k(t) <= W
                else 0

p_hat(v, c; B, W) = ( sum over t in T(v,c) of solved_B,W(t) ) / |T(v,c)|      # per-case rate

solve_rate(v; B, W) = ( 1 / |C| ) * sum over c in C of p_hat(v, c; B, W)      # MACRO average
```

**Macro, not micro.** Equal weight per case. A pooled (micro) rate lets a case with more completed repeats dominate; it is printed as a footnote, never as the headline.

**Budget re-sweep is free downward, never upward.** Because `TrialIteration` stores the cumulative cost curve, a trial run at a `$8` cap can answer "what would solve-rate at `$2` have been?" by re-truncating the curve — no re-execution. The rule: **the board may report at any `B <= min(cap over all compared variants)` and at no `B` above it.** A request for a higher `B` returns `422` naming the binding cap, rather than a quietly wrong number.

**Vulnerable to:** the choice of `B` (a strategy can win at `$1` and lose at `$10` — that is a real finding, not a defect); ceiling effects (if every variant solves everything, the metric has no resolution left); case-mix (a suite skewed to `trivial` flatters weak variants).

**Presented as:** `62% (B=$4.00, W=900s, n=9 cases x 5 repeats, 2 errors excluded)`. Never without `B`. Two solve-rates computed at different `B` are never placed in the same column.

---

### M2 — Cost-to-solve  *(HEADLINE; this is censored data)*

**Unit:** USD.

The naive computation — *median cost over trials that solved* — is **survivorship bias in one line**: a variant that only ever solves the trivial cases posts a beautiful cost-to-solve. Three defenses, all reported.

**M2a — paired median over the shared solved set (the ranked number).**

```
C_shared = { c in C : every variant under comparison solved c in at least one repeat }

cost_case(v, c)   = median{ X_{solve_i(t)}(t) : t in T(v,c), solved(t) = 1 }   # cost AT the solve, not total spend
cost_to_solve(v)  = median over c in C_shared of cost_case(v, c)
```

Paired over the same cases for every variant, so difficulty is held constant. `|C_shared|` and the list of excluded cases are printed with the number; if `|C_shared| < 5`, the metric renders `INSUFFICIENT` and cannot be ranked.

**M2b — amortized cost per solve (the number that does not discard failures).**

```
cost_per_solve(v) = ( sum over all c, all t of cost(t) ) / ( sum over all c, all t of solved(t) )
```

All dollars spent, including on failures, divided by the number of solutions obtained. This is what it actually costs a user to get one working change. Undefined at zero solves -> renders `no solves (spent $X over n trials)`.

**If M2a and M2b rank differently, say so.** That means the variant is cheap when it works and expensive when it does not, and the board prints exactly that sentence instead of choosing a winner.

**M2c — the censored view (Kaplan-Meier over dollars).**

Cost-to-solve is a **time-to-event measurement with right-censoring**, where "time" is dollars. Unsolved trials are not missing data and not infinite cost — they are observations that say *"> the amount spent"*.

```
Event axis:   x = X_{solve_i(t)}(t)   for solved trials
Censor axis:  x = X_final(t)          for unsolved-but-terminated trials

S_hat(x) = product over distinct event costs x_j <= x of ( 1 - d_j / n_j )
             d_j = solves at cost x_j
             n_j = trials still "at risk" (neither solved nor censored below x_j)

cost_to_solve_p50(v) = inf{ x : S_hat(x) <= 0.5 }
```

If `S_hat` never reaches `0.5`, the median **does not exist** and the board prints `> $X.XX (only 38% solved)` — never a number. This is the presentation rule that stops a censored median from being quoted as a fact.

KM is computed pooled across cases for the *cost curve figure* (labeled "pooled across cases — reads as a curve, not as a ranking") and per-case for the drill-down. The ranked headline stays M2a.

**Vulnerable to:** survivorship (M2a alone), budget-cap censoring (all three), provider price changes between runs (pin `model_version` and price date in provenance), and cost misattribution when `cost_source = "gpu-node"` (an occupancy model, not a bill — flagged per trial).

**Presented as:**
```
cost-to-solve   $1.84   (paired median, 7 of 9 cases shared, 95% CI $1.31-$2.44)
  amortized     $3.02   per solve, all 45 trials
  censored p50  $1.91   (KM, 71% solved within cap)
```
All three, always, in that block. Cost-to-solve is never printed as a lone scalar.

---

### M3 — Regression rate  *(does it break things while fixing things)*

**Unit:** fraction.

A definitional trap first: since `solved = target_met AND clean`, a regression rate computed over *solved* trials is identically zero. The number worth having is conditional on the agent believing it succeeded.

```
regression_rate(v)      = |{ t : target_met(t) = 1 AND clean(t) = 0 }| / |{ t : target_met(t) = 1 }|
regression_rate_all(v)  = |{ t : clean(t) = 0 }| / |{ t : t terminated, not error }|
transient_regression(v) = ( # iterations with pass_to_pass_broken > 0 ) / ( # iterations )
```

- `regression_rate` — "when it says it fixed the bug, how often did it break something else". This is the CI-trust number and it is the one on the board.
- `regression_rate_all` — the unconditional rate, for completeness.
- `transient_regression` — thrash: a loop that breaks and re-fixes `pass_to_pass` three times before landing clean is a different animal from one that never breaks it, even if both end clean. `TrialIteration.pass_to_pass_broken` exists for precisely this.

**Vulnerable to:** `pass_to_pass` suite size — a case with 4 guard tests cannot detect much, so the board prints `median |pass_to_pass|` next to the column; and **flaky tests**, which inflate it for free. Mitigation is upstream, in 13.1: the flake screen runs the guard suite `k=3` times at `base_commit_sha` and ejects any test that is not deterministically green into `quarantined_tests`. A quarantined test never enters an oracle.

**Presented as:** `regression rate 11% (5/44 target-met trials, median |p2p| = 23)`. Always with the conditioning denominator spelled out, because "11%" alone is ambiguous between the three definitions above.

---

### M4 — Iterations-to-solve distribution  *(does it converge or thrash)*

**Unit:** iterations (discrete); the derived efficiency is solves per USD.

Same censoring, discrete axis. Report the whole curve, not a scalar:

```
F(k) = P(solve by iteration <= k)     estimated by KM on the iteration axis
                                      (unsolved trials censored at iterations_used)

marginal_solve_gain(k) = F(k) - F(k-1)
marginal_cost(k)       = median{ X_k(t) - X_{k-1}(t) : t reached iteration k }
iteration_efficiency(k)= marginal_solve_gain(k) / marginal_cost(k)      # solves per USD at the margin
```

And the number the plan actually asks for — *"was iteration 4 worth paying for"*:

```
k_star(v) = the smallest k where iteration_efficiency(k) < ( overall solves / overall dollars )
```

`k_star` is the recommended stopping iteration for that variant: past it, the marginal dollar buys fewer solves than the average dollar did. It is a *recommendation derived from data*, printed with its interval, not a policy the harness enforces.

**Vulnerable to:** `max_iterations` truncation (the curve is unknowable past the cap — the board draws it as a hard stop, not a fading line); and iteration-size confounding, since one strategy's "iteration" may contain 8 parallel workers and another's may contain one step. **Iterations are therefore never compared across strategies of different shape without cost on the same row.**

**Presented as:** a step CDF plus `p50 = 2 iters, p90 = 5 iters, k* = 4 (95% CI 3-6), 29% never solved within 6`.

---

### M5 — Wall-clock-to-solve

**Unit:** seconds.

```
wall_to_solve(t) = W_{solve_i(t)}(t)          # trial start -> end of the solving iteration
                                              # INCLUDES orchestration + integration overhead
                                              # EXCLUDES queued_ms (time waiting on the harness's own limiter)
wall_to_solve(v) = KM p50 over trials, censored at W_final for unsolved
```

`queued_ms` is recorded and printed separately. Folding harness queueing into wall-clock would make a fan-out look slow purely because the host was oversubscribed.

**Vulnerable to:** host contention above all. An 8-way fan-out on a 4-core box is a serial run wearing a costume. Two hard guards:

1. Every trial records `machine_profile` and `host_concurrency_limit`.
2. **The board refuses to place wall-clock or speedup for two variants in the same comparison when their `machine_profile` values differ.** It renders `BLOCKED: mixed machine profiles (local-16c-64g, runpod-a100)` and names them. Cost and solve-rate comparisons are still allowed across profiles; latency ones are not.

**Presented as:** `wall-clock-to-solve 412s (KM p50, machine=local-16c-64g, concurrency<=8, queued 37s median excluded)`.

---

### M6 — Speedup, and the honesty companion

**Unit:** dimensionless (`x`).

```
serial_equivalent_ms(t) = sum over all steps s in t of StepUsage[s].wall_clock_ms
speedup(t)              = serial_equivalent_ms(t) / wall_clock_ms(t)
speedup(v)              = median over trials
```

`speedup < 1` is possible and meaningful: orchestration overhead exceeded the parallel gain. It is printed as-is, never floored at 1.

**The failure mode this metric has:** a fan-out of 8 that discards 7 branches has a magnificent speedup and a terrible cost. Speedup measures work done *simultaneously*, not work done *usefully*. So it ships with a companion computed from git lineage:

```
merged_work_fraction(t) =
    ( sum of StepUsage.wall_clock_ms over steps whose branch is an ancestor of final_commit_sha )
    / serial_equivalent_ms(t)
```

Unit: fraction. `0.18` means 82% of the compute was thrown away. That is the real cost of aggressive fan-out and it is invisible in both cost-to-solve and speedup.

**Presented as:** speedup and cost-to-solve are rendered **in the same row and the board cannot be sorted by speedup alone** — the sort control offers `cost`, `solve-rate`, `regression`, and a `cost x wall-clock` frontier view, and speedup is a column, not a ranking key. `merged_work_fraction` sits immediately beside it.

---

### M7 — Integration conflict rate and integration cost share  *(the tax on parallelism)*

**Unit:** fraction / fraction.

```
integration_conflict_rate(v) = ( sum over t of integration_conflicts )
                               / ( sum over t of integration_merges_attempted )

conflict_resolution_rate(v)  = ( sum over t of conflicts_resolved )
                               / ( sum over t of integration_conflicts )

integration_cost_share(t)    = integration_cost_usd(t) / cost(t)
integration_cost_share(v)    = median over trials
```

**Attribution is by declared role, not by heuristic.** `integration_cost_usd` is the sum of `StepUsage.cost_usd` over steps whose graph role is in `{integrator, resolver}`, plus machine time for merge/rebase operations. Because roles are declared in the `StrategyTemplate`, there is no guessing and no drift; a template that hides integration work inside a `worker` role is a mis-authored template and 13.2's graph validator rejects it (a step that calls `merge_branch` must declare an integration role).

**Vulnerable to:** denominator absence — a serial strategy attempts zero merges. `0/0` is not `0%`.

**Presented as:** `n/a` for single-worker templates, **never `0%`**, so nobody averages a structural zero into a fleet number. For parallel templates: `conflict rate 34% (17/50 merges), resolved 82%, integration share 21% of spend`.

---

### M8 — Cost-by-role  *(the actual test of the owner's hypothesis)*

**Unit:** USD, plus fraction.

```
cost_by_role(v)[r]  = sum of StepUsage.cost_usd over steps with role = r, across all trials of v
cost_share(v)[r]    = cost_by_role(v)[r] / total_cost(v)
```

This is the measurement that makes "an expensive planner directing cheap workers" a testable claim rather than a slogan: the prediction is that `cost_share["planner"]` stays small while solve-rate rises versus one-shot at the same total spend. Report `cost_share` alongside `solve_rate` at a shared `B` and the hypothesis either survives or does not.

**Vulnerable to:** role vocabulary mismatch. A one-shot template with a single `worker` role is trivially 100% worker, and comparing that share to a four-role template is meaningless.

**Presented as:** a stacked bar per template, **grouped, never ranked**. The board explicitly disables the sort control on role columns and prints `role shares are comparable only across templates declaring the same roles`.

---

### The censoring summary the board must never lose

| Censoring source | Naive computation says | The rule |
|---|---|---|
| Budget cap | drop unsolved -> "cost-to-solve $1.10" | right-censored; report M2a paired + M2b amortized + KM p50, and `> $X` when the median is unreached |
| Iteration cap | "median 2 iterations" | KM on the iteration axis; the CDF stops dead at the cap |
| Wall-clock cap | "fast!" | censored the same way; `queued_ms` excluded and printed |
| Infra/provider error | quietly vanishes | excluded from denominators, printed as `error_rate`, `>10%` marks the variant UNRELIABLE |
| Cases nobody solved | invisible | `C_shared` exclusions are listed by slug under every cost number |

---

## Part 2 — Variance and separability

### How many repeats

Two sources of noise, and they need different resources:

- **Within-case noise** (sampling temperature, tool-call ordering, provider variability) -> repeats `R`.
- **Between-case noise** (some cases are just easier) -> more **cases**, which are far more expensive to author.

The unit of analysis is therefore the **case**, not the trial. Defaults:

| Setting | R | Cases | Use |
|---|---|---|---|
| pilot | 3 | >= 5 | exploring; board renders numbers but **all ranking disabled**, labeled `PILOT` |
| published | 5 | >= 9 | the default for anything that leaves the machine |
| headline claim | 10 | >= 9 | only for <= 3 variants, when a specific claim carries the write-up |

`R` is a dial and 13.4's job is to set it empirically: measure the within-case spread on the starter suite and record the observed value in `METHOD.md`. Below 5 cases or below 3 repeats, ranking is structurally disabled — not discouraged, disabled.

### The statistic: medians and a cluster bootstrap

**Not means, not t-tests.** Cost is heavy-tailed (one runaway trial moves a mean by 40%), censored (t-tests have no opinion about `> $8`), and trials on the same case are not independent (which is exactly the assumption a t-test needs). Medians with bootstrap intervals answer the question without any of those assumptions.

```
Paired hierarchical (cluster) bootstrap, B = 10000 resamples, seeded:

  for b in 1..B:
      resample CASES with replacement from C          # cluster level - cases are the cluster
      for each resampled case c:
          resample the R repeats of c with replacement, per variant   # within-cluster
      recompute the statistic for every variant on this resample
      record delta_b = stat(v_a) - stat(v_b)          # PAIRED: same resampled case set for both

  CI_95(delta) = [ percentile(delta, 2.5), percentile(delta, 97.5) ]
```

Cases are resampled as clusters because that is the level at which observations are independent. The bootstrap seed is recorded in the bundle so the interval reproduces exactly.

### The separability rule (this is the exact rule the board implements)

> **The board ranks two variants on a metric if and only if the 95% bootstrap interval of their *paired difference* excludes zero. Otherwise it prints NOT SEPARABLE and does not order them.**

Two clarifications that matter:

1. **Paired difference, not marginal overlap.** Overlapping marginal intervals do *not* imply non-significance — the overlap heuristic is strictly weaker and would make us refuse real findings. The board still *displays* marginal intervals because they are readable, but the ranking decision uses `CI(delta)`.
2. **Multiplicity.** Ranked claims are limited to comparisons **against the one-shot baseline** (`K-1` comparisons), Holm-corrected at family-wise `alpha = 0.05`. Every other pairwise comparison is available in the UI and is labeled `EXPLORATORY - not corrected for multiplicity`.

**Practical equivalence band (ROPE).** With enough n, trivial differences become separable. A difference is declared practically equivalent when it is smaller than:

| Metric | Band |
|---|---|
| cost-to-solve | 10% relative |
| solve-rate | 3 percentage points |
| regression rate | 3 percentage points |
| wall-clock | 15% relative |

A separable-but-inside-band result prints `SEPARABLE BUT WITHIN NOISE FLOOR`, which is different from both "wins" and "not separable".

### The exact presentation strings

The board emits these verbatim; `tdd/unit/benchmark/test_separability_rule.py` pins them as strings.

```
SEPARABLE:
  planner-fanout-8 beats one-shot on cost-to-solve: -$0.42
    (95% CI -$0.71 .. -$0.13; paired over 7 shared cases x 5 repeats; Holm-adjusted, family of 3)

NOT SEPARABLE:
  planner-fanout-8 vs adversarial-3 on cost-to-solve: NOT SEPARABLE
    (delta = -$0.08, 95% CI -$0.40 .. +$0.26; 7 cases x 5 repeats)
    To separate an effect of this size: est. R ~ 14 repeats, or ~ 21 cases at R=5.

WITHIN NOISE FLOOR:
  test-first beats one-shot on cost-to-solve: -$0.06 (95% CI -$0.11 .. -$0.02)
    SEPARABLE BUT WITHIN NOISE FLOOR (<10% relative). Reported, not ranked.

BLOCKED:
  wall-clock comparison BLOCKED: mixed machine profiles (local-16c-64g, runpod-a100).

UNRELIABLE:
  gemini-worker-mix: 6/45 trials errored (13%) - numbers shown, ranking disabled.
```

The required-repeats estimate uses interval half-width scaling:

```
n_required = n_current * ( half_width_observed / target_half_width )^2
  target_half_width = the metric's ROPE band
```

It is an estimate and is printed as `est.` — but "you need roughly 3x the data" is far more useful to the reader than a bare "not significant".

### Controls, run as first-class variants

| Control | What it proves | Failure means |
|---|---|---|
| **base-state** | at `base_commit_sha`, every `fail_to_pass` FAILS and every `pass_to_pass` PASSES | the case is broken; `bench validate` rejects it, and a trial that starts green aborts as `error:base_state_invalid` |
| **null-agent** | a template whose only step commits nothing must score **0% solved** | the oracle is measuring something other than the fix; the board **refuses to render** and names the offending cases |
| **gold-patch** | applying `reference_patch` makes the oracle green | the case may be unsolvable or the oracle unreachable; case flagged `solvable_verified = false` and excluded from headline boards |
| **flake screen** | the guard suite is green `k=3` times at base | nondeterministic tests are quarantined out of the oracle, not silently inflating regression rate |
| **oracle tamper** | `oracle_file_hashes` unchanged at final commit | the agent edited the tests; trial scored `error:oracle_tampered`, never `solved` |

The null-agent and gold-patch controls run in **every published experiment**, not once at setup. A control that ran three months ago proves nothing about today's run.

---

## Part 3 — `METHOD.md` (the template that ships in every bundle)

Generated by `lazyaf bench export` from `backend/app/services/bench_method_doc.py`. `{{ }}` placeholders are filled from the experiment, its trials, and the provenance block. **Every placeholder is mandatory** — a field the harness could not determine renders the literal string `NOT RECORDED`, never an empty cell, and `test_method_doc_generation.py` asserts that no placeholder survives unrendered.

```markdown
# METHOD - {{experiment_name}}

Bundle `{{bundle_id}}` - generated {{generated_at_utc}} UTC by LazyAF `{{harness_version}}`.
Suite `{{suite_name}}` @ `{{suite_version}}`.

## 1. What was measured

{{n_variants}} strategy variants were run against {{n_cases}} benchmark cases,
{{repeats}} repeats each: {{n_trials_total}} trials, of which {{n_trials_errored}}
errored and were excluded from all metrics ({{error_rate_pct}}%).

The independent variable is the STRATEGY - the shape of the agent workflow. Models
are a resource each strategy allocates to graph roles, and are held as a covariate:
{{model_covariate_statement}}

Variants:

| Variant | Strategy | Role -> model binding | Loop policy |
|---|---|---|---|
{{variant_rows}}

## 2. The claim

{{claim_statement}}

### What these numbers do NOT claim

- They do not claim a ranking of AI MODELS. Models were bound to roles as a
  strategy resource; a different binding may reorder these results.
- They do not claim generalization beyond this corpus: {{n_cases}} cases across
  verticals {{verticals}} and complexities {{complexities}}. Software is wider
  than this.
- They do not claim any comparison marked NOT SEPARABLE below. Those pairs are
  indistinguishable at this sample size and are deliberately unranked.
- They do not claim a wall-clock or speedup comparison across machine profiles.
  {{machine_profile_statement}}
- They do not claim reproducibility of exact dollar figures. Provider pricing,
  model versions, and sampling nondeterminism move them. The claim is that the
  RANKING reproduces within the stated intervals.
- {{additional_non_claims}}

## 3. Corpus and contamination disclosure

Cases are real repositories pinned at a fixed commit. Most are public, which
means they may appear in the training data of the models under test. This is a
deliberate trade - realism over hermeticism - and it is disclosed rather than
mitigated away.

| Case | Vertical | Complexity | Contamination risk | Source | License |
|---|---|---|---|---|---|
{{case_rows}}

Contamination split: {{n_high_risk}} high / {{n_medium_risk}} medium /
{{n_low_risk}} low.

**The skeptic's check:** section 7 reports every headline metric split by
contamination risk. {{contamination_split_verdict}}

## 4. Oracle and scoring

A trial is SOLVED when, at its final commit, evaluated in a clean container from
a fresh clone:

  - every `fail_to_pass` test id passes, AND
  - every `pass_to_pass` test id passes, AND
  - the files carrying oracle test ids are byte-identical to their base-commit
    hashes (anti-tamper).

Oracle command per case: `{{test_command_summary}}`.
Partial progress ({{example_partial}}) is recorded per iteration but is NOT
partial credit: "solved" is binary.

Tests quarantined by the flake screen (run {{flake_k}}x at base commit, ejected
if not deterministically green): {{quarantined_test_count}}. Listed in
`cases/*.yaml` under `quarantined_tests`.

## 5. Controls

| Control | Expected | Observed | Verdict |
|---|---|---|---|
| Base-state (every case) | f2p fail, p2p pass at base | {{base_state_observed}} | {{base_state_verdict}} |
| Null-agent | 0% solved | {{null_agent_observed}} | {{null_agent_verdict}} |
| Gold-patch solvability | 100% solved where a reference patch exists | {{gold_patch_observed}} | {{gold_patch_verdict}} |
| Oracle tamper | 0 tampered trials | {{tamper_observed}} | {{tamper_verdict}} |

{{control_failure_note}}

## 6. Provenance

| Field | Value |
|---|---|
| Harness version | `{{harness_version}}` ({{harness_commit_date}}) |
| Suite version | `{{suite_version}}` |
| Image content hashes | {{image_hash_table}} |
| Model versions | {{model_version_table}} |
| Determinism knobs | {{determinism_table}} |
| Network mode during trials | {{network_mode}} |
| Machine profile(s) | {{machine_profiles}} |
| Host concurrency limit | {{host_concurrency_limit}} |
| Cost source mix | {{cost_source_mix}} |
| Provider price date | {{price_date}} |
| Bootstrap seed | `{{bootstrap_seed}}` |

{{determinism_caveat}}

## 7. Metrics as computed

Formulas are fixed in `upcoming/m13-phase-specs.md` Part 1 and implemented in
`backend/app/services/bench_metrics.py`. Caps in force for this run:
budget `${{budget_cap}}`, max iterations `{{max_iterations}}`, wall-clock ceiling
`{{wall_ceiling}}s`. All budget-normalized figures are reported at
`B = ${{shared_budget}}`, which is <= every variant's cap.

{{board_tables}}

Censoring: {{censored_trial_count}} of {{n_trials_total}} trials terminated
without solving and are right-censored, not dropped. Where a median was not
reached within the cap it is printed as `> $X`, never as a number.

## 8. Variance and separability

{{repeats}} repeats per (variant, case). Statistic: medians with
95% percentile intervals from a paired hierarchical bootstrap
({{bootstrap_b}} resamples; cases resampled as clusters, repeats resampled
within case). Ranking requires the paired-difference interval to exclude zero;
comparisons are against the `one-shot` baseline, Holm-corrected at
family-wise alpha = 0.05.

Separable comparisons:
{{separable_list}}

Refused comparisons (reported, deliberately unranked):
{{not_separable_list}}

Observed within-case spread on this suite: {{within_case_spread}}.

## 9. Threats to validity

1. **Training contamination.** {{contamination_threat}}
2. **Censoring.** Every cost and latency figure is bounded above by the budget
   and iteration caps. A variant that would have solved a case for $12 is
   recorded as "did not solve within $8", and no method recovers that.
3. **Small n.** {{n_cases}} cases is a small sample of software. Intervals
   express sampling noise within this corpus; they say nothing about the corpus
   being representative.
4. **Provider drift.** Model endpoints change without version changes. Re-running
   later measures a different system; that is why `model_version` and the run
   date are recorded.
5. **Host contention.** Wall-clock and speedup depend on the machine.
   {{contention_threat}}
6. **Oracle scope.** `pass_to_pass` guards catch only what the repo already
   tests. Median guard-suite size here: {{median_p2p_size}}. Real regressions
   outside that surface are invisible to this benchmark.
7. **Case-mix.** Results are macro-averaged per case; a different mix of
   verticals or complexities would move the aggregate.
8. **Network access.** {{network_threat}}
9. **Discarded work.** Parallel strategies' `merged_work_fraction` is
   {{merged_work_fraction}}: compute that never reached the final commit is paid
   for and counted in cost.
10. {{additional_threats}}

## 10. Re-run it yourself

Verbatim, from a clean checkout:

```
git clone {{repo_url}} && cd lazyaf && git checkout {{harness_version}}
docker compose up -d
python scripts/build_images.py --check          # must report the hashes in section 6
lazyaf bench import {{bundle_filename}}
lazyaf bench validate {{suite_name}}            # controls must pass before any trial runs
lazyaf bench run {{experiment_slug}} --repeats {{repeats}} --budget {{budget_cap}}
lazyaf bench board {{experiment_slug}} --budget {{shared_budget}}
```

Replaying the shipped results without spending anything:

```
lazyaf bench import {{bundle_filename}} --with-results
lazyaf bench board {{experiment_slug}} --budget {{shared_budget}}   # reproduces section 7 exactly
```

If your harness version or image hashes differ from section 6, `bench board`
prints a PROVENANCE DRIFT banner and refuses to present your results and these
side by side. That is intended.

## 11. Changes from the previous bundle

{{changelog}}

## 12. Corpus licensing

{{n_bundled}} cases ship as git bundles ({{bundled_licenses}}).
{{n_fetch}} cases ship as fetch instructions + a patch, because their license
does not permit redistribution: {{fetch_case_list}}. `lazyaf bench import`
fetches those from upstream and verifies `base_commit_sha` before use.
```

---

## Phase 13.1 — Corpus & fixtures

**Goal**: benchmark cases exist as versioned data with a validator strong enough that an invalid case cannot enter a suite.

> **Why now:** every later phase's numbers are downstream of case quality. A case whose `fail_to_pass` is already green at base scores every strategy at 100% and looks like a triumph. The validator is not a convenience — it is the first control.

### Deliverables

- **Models + migration** `backend/alembic/versions/00XX_benchmark_corpus.py`: `benchmark_suites`, `benchmark_cases` (incl. the additive fields in 13.0).
- **On-disk case format** — the source of truth lives in-repo at `bench/suites/<suite>/cases/<slug>.yaml`; the DB is a projection of it (R3: one pydantic model, `BenchmarkCaseSpec`, used by loader, API, and exporter).

```yaml
slug: flask-api.missing-pagination
suite: core-v1
repo: gh-mirror/flask-api-demo          # ingested into the internal git server
base_commit_sha: 4a1c9e2f8b7d6c5a4b3e2d1c0f9e8d7c6b5a4938
task_statement: |
  GET /items returns every row. Add limit/offset pagination with a default
  page size of 50 and a Link header for the next page.
vertical: web-api
complexity: small
test_command: "pytest -q"
fail_to_pass:
  - api.items.pagination_limits_results
  - api.items.pagination_link_header
pass_to_pass:
  - api.items.list_returns_200
  - api.items.list_serializes_fields
  - api.auth.requires_token
quarantined_tests: []
user_story_id: null
loop_defaults: {max_iterations: 6, budget_usd: 4.00, per_step_timeout: 900}
contamination_risk: high
source_url: https://github.com/example/flask-api-demo
license: MIT
reference_patch: patches/flask-api.missing-pagination.diff
machine_profile_required: null
```

- **CLI** (`cli/lazyaf/bench.py`, registered as `lazyaf bench`):
  - `lazyaf bench ingest <url> --at <sha> --license <spdx> --contamination <high|medium|low>` — mirror a public repo into the internal git server and record provenance.
  - `lazyaf bench case add <repo> --at <sha> --f2p ... --p2p ...` — author interactively from a real repo state.
  - `lazyaf bench validate <suite> [--case <slug>]` — the control battery.
  - `lazyaf bench lint <suite>` — schema-only, no containers, runs in T1.
- **Validator** (`backend/app/services/bench_validation.py`) running, per case, in a clean control-mode container at `base_commit_sha`:
  1. every `fail_to_pass` id **fails**; every `pass_to_pass` id **passes**;
  2. flake screen — guard suite `k=3`, non-deterministic ids ejected to `quarantined_tests` with a warning, not a silent drop;
  3. gold-patch control where `reference_patch` is set -> sets `solvable_verified`;
  4. oracle hash capture -> `oracle_file_hashes` for every file carrying an oracle id;
  5. metadata completeness — `license` and `contamination_risk` are required; missing either is a hard failure, because the bundle cannot legally or honestly ship without them.

```
$ lazyaf bench validate core-v1
core-v1 (9 cases)
  OK    flask-api.missing-pagination     f2p 2 fail / p2p 3 pass / gold-patch green
  OK    pandas-etl.null-coalesce         f2p 1 fail / p2p 11 pass / gold-patch green
  WARN  svelte-ui.form-validation        quarantined 1 flaky p2p id (ui.form.debounce_settles)
  FAIL  cli-tool.exit-codes              f2p id 'cli.exit.nonzero_on_error' ALREADY PASSES at base
                                          -> case cannot measure anything; fix or remove
8 ok, 1 failed. Exit 1.
```

- **Starter suite `core-v1`** — 9 cases, 3 verticals (`web-api`, `data-pipeline`, `cli`) x 3 complexities (`trivial`, `small`, `medium`), each with `contamination_risk` recorded honestly.
- **Dogfood hook (R7)**: `lazyaf bench lint core-v1` joins the dogfood pipeline so a malformed case breaks CI the day it lands.

### Tests

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_case_schema.py` | `BenchmarkCaseSpec` round-trips YAML -> model -> YAML; empty `fail_to_pass` rejected; missing `license` or `contamination_risk` rejected; unknown `vertical`/`complexity` rejected |
| `tdd/unit/benchmark/test_case_loader.py` | on-disk suite dir loads to DB rows; slug uniqueness within a suite; DB is a projection, re-load is idempotent |
| `tdd/unit/benchmark/test_bench_cli_authoring.py` | `bench case add` writes a well-formed YAML from flags; `bench lint` exits non-zero on a bad case without touching Docker |
| `tdd/integration/api/test_bench_corpus_api.py` | suite/case CRUD; case create rejects an unknown `repo_id`; `GET /api/bench/cases?suite=` filters |
| `tdd/integration/benchmark/test_base_state_validation.py` | (Docker-real, named volume per R6) at `base_commit_sha` the f2p ids fail and the p2p ids pass on a real fixture |
| `tdd/integration/benchmark/test_validation_catches_miswired_case.py` | a case whose f2p is already green is REJECTED with the exact reason; a case whose p2p already fails is rejected too |
| `tdd/integration/benchmark/test_flake_screen.py` | a deliberately nondeterministic guard test is quarantined after k=3 and never enters the oracle |
| `tdd/integration/benchmark/test_gold_patch_control.py` | applying `reference_patch` turns the oracle green -> `solvable_verified = true`; a broken patch leaves it false and warns |
| `tdd/integration/benchmark/test_oracle_hash_capture.py` | `oracle_file_hashes` covers every file carrying an oracle id and changes when the test file changes |

### Definition of Done

- [ ] `benchmark_suites` / `benchmark_cases` tables + migration, `alembic upgrade head` clean from an existing dev DB
- [ ] `BenchmarkCaseSpec` is the single schema for loader, API, and exporter (R3)
- [ ] `lazyaf bench ingest | case add | validate | lint` implemented and documented in `cli/README`
- [ ] Validator runs all five checks and reports per-case, per-check results
- [ ] `core-v1` seeded: 9 cases, 3 verticals x 3 complexities, all `validate`-green
- [ ] Every case carries `license` and `contamination_risk`; no case ships without both
- [ ] `bench lint core-v1` in the dogfood pipeline (R7)
- [ ] `tdd/tier_floors.json` raised for T1 and T2 in the same commit (R4)

### EXIT GATE

`lazyaf bench validate core-v1` is green on all 9 starter cases; a deliberately miswired case (f2p already passing) is rejected by name and reason; the flake screen quarantines an injected nondeterministic guard test; and every case with a `reference_patch` reports `solvable_verified = true`.

---

## Phase 13.2 — Strategy templates & the trial orchestrator

**Goal**: a strategy is authorable as data, a trial runs it to termination against a case without ever exceeding its cap, and everything the metrics need — per-iteration cost by role, integration conflicts, git lineage — is recorded as it happens.

> **Why now:** this is the only phase that touches execution. 13.3-13.5 are aggregation, presentation, and packaging over the rows this phase writes. Anything not recorded here is unrecoverable later, which is why `merged_work_fraction`, `queued_ms`, `budget_overrun_usd` and `integration_merges_attempted` are deliverables now and not "we can add that to the board".

### Deliverables

- **Models + migration** `00XX_benchmark_trials.py`: `strategy_templates`, `trials`, `trial_iterations` (incl. 13.0's additive fields).
- **Strategy templates as data** — `bench/strategies/<slug>.yaml`, loaded like cases. Seed catalog: `one-shot` (the mandatory baseline), `test-first`, `adversarial-3`, `planner-fanout-8`, `gated`, plus the `null-agent` control template.

```yaml
slug: planner-fanout-8
description: One high-end planner writes instructions; 8 cheap workers execute
             them on their own branches; an integrator merges.
roles: [planner, worker, integrator]
graph:
  entry_points: [plan]
  steps:
    - id: plan
      role: planner
      prompt: implement-from-story
    - id: work
      role: worker
      fanout: 8
      branch_per_worker: true      # trial/{trial_id}/w{k} off base_commit_sha
      depends_on: [plan]
    - id: integrate
      role: integrator
      join: work                   # all-upstream-satisfied
      calls: [merge_branch]
loop_policy: {max_iterations: 6, budget_usd: 4.00, stop_on: [solved, budget, iterations]}
parallelism: {max_concurrent_workers: 8, branch_per_worker: true}
integration: {policy: sequential-merge, on_conflict: resolver-agent}
```

- **Graph validator** (`backend/app/services/strategy_validation.py`): every step declares a role; every declared role is in `roles`; every role is bound by the trial's `model_assignment`; the join step exists for any fan-out; no unreachable steps; **a step that calls `merge_branch`/`rebase_branch` must declare an integration role** (this is what makes M7's cost attribution honest); `budget_usd` and `max_iterations` are required.
- **Trial orchestrator** (`backend/app/services/trial_orchestrator.py`):
  - reset: fresh workspace cloned at `base_commit_sha` on branch `trial/{trial_id}`; **assert the base-state control before the agent runs** — a trial that starts green aborts as `error:base_state_invalid` rather than scoring a free solve;
  - drive N sequential pipeline runs of the strategy graph, one per iteration, feeding the previous iteration's failing oracle output forward (the graph is a DAG; iteration is the orchestrator's loop, not a cycle);
  - per iteration: score the oracle in a **clean container from a fresh clone** of the iteration commit, verify `oracle_file_hashes` (tamper -> `error:oracle_tampered`), write a `TrialIteration` row with cost, tokens, churn, f2p/p2p counts;
  - terminate on `solved` | `budget_exhausted` | `max_iterations` | `wall_clock_exhausted` | `error`.
- **Hard budget enforcement, fleet-wide**: an admission check before every step dispatch against `remaining = budget_usd - spent - reserved_in_flight`, applied across the whole fan-out rather than per worker. A step that cannot fit is not dispatched and the trial terminates `budget_exhausted`. Tokens already streaming when the cap is hit cannot be recalled — that overage lands in `budget_overrun_usd` and is reported, not absorbed.
- **Integration executor** (`backend/app/services/trial_integration.py`): dispatches `integration.policy` to the platform's existing `merge_branch` / `rebase_branch` / cherry-pick, and `on_conflict` to `fail` | `resolver-agent` (hands the structured conflict payload from `POST /api/cards/{id}/resolve-conflicts` to an agent step) | `human`. Records `integration_merges_attempted`, `integration_conflicts`, `conflicts_resolved`, `integration_cost_usd`.
- **Lineage capture**: at trial end, walk `final_commit_sha`'s ancestry to mark which worker branches landed -> the input to `merged_work_fraction` (M6).
- **Scripted mock provider** (`runner-mock`): a deterministic "solver" that solves at a configured iteration `k` with a configured per-step cost, plus a "thrasher" that never solves. Every orchestrator test uses it — no test in this milestone spends money.

### Tests

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_strategy_graph_validation.py` | unbound role rejected; fan-out without a join rejected; unreachable step rejected; a `merge_branch` step without an integration role rejected; missing `budget_usd` rejected |
| `tdd/unit/benchmark/test_role_binding.py` | `model_assignment` must cover every declared role; extra bindings warn; the same model may fill several roles and cost still attributes per role |
| `tdd/unit/benchmark/test_budget_enforcement.py` | a step that would exceed remaining budget is never dispatched; the cap holds across an 8-way fan-out, not per worker; `budget_overrun_usd` records in-flight overage instead of hiding it |
| `tdd/unit/benchmark/test_stop_conditions.py` | each of solved / budget / iterations / wall-clock terminates with the right status; precedence when two fire on the same step |
| `tdd/unit/benchmark/test_iteration_feedback.py` | iteration N+1's prompt context contains iteration N's failing oracle output and nothing from an unrelated trial |
| `tdd/unit/benchmark/test_lineage_merged_fraction.py` | steps on branches not merged into `final_commit_sha` are excluded from merged work; a fully-discarded fan-out yields a low fraction |
| `tdd/integration/benchmark/test_trial_orchestrator_serial.py` | mock solver at k=3: trial is `solved`, `solved_at_iteration = 3`, 3 `TrialIteration` rows with a monotone cost curve |
| `tdd/integration/benchmark/test_trial_orchestrator_fanout.py` | (named volumes, R6) 4 workers get 4 branches off the base commit and 4 isolated workspaces; no worker sees another's uncommitted files |
| `tdd/integration/benchmark/test_integration_policies.py` | `sequential-merge` and `rebase-onto-trunk` both produce a valid `final_commit_sha`; `on_conflict: fail` terminates and records the conflict rather than silently dropping the branch |
| `tdd/integration/benchmark/test_resolver_agent_on_conflict.py` | a real conflict is handed to a resolver step as structured data; resolution increments `conflicts_resolved` and its cost lands in `integration_cost_usd` under the `resolver` role |
| `tdd/integration/benchmark/test_base_state_guard_aborts_trial.py` | a case that is green at base aborts the trial `error:base_state_invalid` before any agent step runs |
| `tdd/integration/benchmark/test_oracle_tamper_detection.py` | an agent that edits a file carrying an oracle id scores `error:oracle_tampered`, never `solved` |
| `tdd/e2e/test_trial_cost_curve.py` | (`@slow`) full stack: a mock trial produces a complete per-iteration cost curve with per-role attribution visible via the API |

### Definition of Done

- [ ] `strategy_templates` / `trials` / `trial_iterations` tables + migration
- [ ] Strategy YAML loader + graph validator; the six seed templates load and validate
- [ ] Orchestrator drives iterations, scores in a clean container, writes a `TrialIteration` per cycle
- [ ] Budget cap holds fleet-wide; `budget_overrun_usd` populated and non-hidden
- [ ] Fan-out allocates branch + workspace per worker off `base_commit_sha`
- [ ] Integration policies + `on_conflict` paths implemented over existing merge/rebase/resolve-conflicts
- [ ] Lineage walk populates merged-work inputs
- [ ] Mock solver/thrasher in `runner-mock`; no orchestrator test calls a paid provider
- [ ] Trials run through the default executor path with `StepRun.executor` observable (R1)
- [ ] Tier floors raised (R4)

### EXIT GATE

A mock-model trial on a starter case solves at the configured iteration with a complete per-iteration cost curve attributed by role; a deliberately-unsolvable case terminates `budget_exhausted` with `total_cost_usd <= budget_usd + budget_overrun_usd` and the overrun reported; an 8-way fan-out trial produces 8 branches off the base commit, rejoins via the declared policy, and records conflicts and integration cost as trial outcomes.

---

## Phase 13.3 — Strategy experiments & the effectiveness board

**Goal**: run a matrix of `strategy x model_assignment x repeat` over a suite and render the metrics of Part 1 — correctly, with every denominator visible.

> **Why now:** the metrics are the deliverable of this milestone. They are built as pure functions over golden fixtures first, so that a board bug is a failing unit test rather than a wrong claim in a public write-up.

### Deliverables

- **Matrix extension**: `Experiment.matrix` gains `strategies` and `model_assignments` axes -> `{strategies: [...], model_assignments: [...], repeat: N}`, fanned out over a suite's cases. `one-shot` is auto-injected into every matrix as the baseline; removing it requires an explicit `--no-baseline` and the board then prints `NO BASELINE - comparisons are relative only`.
- **`backend/app/services/bench_metrics.py`** — pure functions, no DB access, input is a list of trial/iteration dicts: `solve_rate`, `cost_to_solve` (M2a/b/c incl. the KM estimator), `regression_rates`, `iteration_cdf` + `k_star`, `wall_clock_to_solve`, `speedup` + `merged_work_fraction`, `integration_stats`, `cost_by_role`. Every function returns a value **plus** its provenance: `n`, denominator, caps, exclusions.
- **Budget re-sweep** (`resweep_at_budget(trials, B)`): re-truncates cost curves downward; raises `BudgetAboveCapError` for `B` above any compared variant's cap.
- **Board API** `GET /api/bench/board?experiment_id=&budget=&group_by=strategy|vertical|complexity|contamination_risk` -> a typed response carrying values, intervals, `n`, exclusions, and any `BLOCKED` / `UNRELIABLE` / `INSUFFICIENT` flags.
- **Board UI** (`frontend/src/routes/bench/board`): one row per variant; cost and wall-clock always co-rendered; role-cost stacked bars grouped not ranked; the cost curve (pooled KM) and the iteration CDF as figures; every cell hover shows `n` and the exclusion list.
- **Export**: `GET /api/bench/board?format=csv|json` and `lazyaf bench board <experiment> --budget X`, byte-identical between CLI and UI (same service call).
- **Presentation rules enforced in code** — P1..P9 of Part 1 are implemented in the board serializer, not in the template, so the CSV cannot present something the UI would refuse.

### Tests

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_metrics_solve_rate.py` | macro vs micro; a variant with unequal repeat counts does not skew macro; `B`/`W` truncation applied at the solving iteration |
| `tdd/unit/benchmark/test_metrics_cost_to_solve.py` | M2a paired over `C_shared` only; M2b amortized includes failed-trial spend; KM p50 matches a hand-computed fixture; an unreached median renders `> $X` and never a float |
| `tdd/unit/benchmark/test_metrics_censoring.py` | dropping unsolved trials produces a provably different (lower) number than the specified computation — the survivorship-bias regression test |
| `tdd/unit/benchmark/test_metrics_regression_rate.py` | conditioned on `target_met`; the unconditional and transient variants differ on a fixture where the loop breaks then re-fixes a guard test |
| `tdd/unit/benchmark/test_metrics_iterations_distribution.py` | discrete KM with censoring at `iterations_used`; `marginal_solve_gain` sums to `F(max)`; `k_star` on a fixture with a deliberately worthless iteration 5 |
| `tdd/unit/benchmark/test_metrics_speedup.py` | `speedup < 1` preserved, not floored; `merged_work_fraction` low on a fixture where 7 of 8 branches are discarded |
| `tdd/unit/benchmark/test_metrics_integration.py` | conflict rate `n/a` (not `0%`) for a zero-merge serial template; integration cost attributed only to integration roles |
| `tdd/unit/benchmark/test_metrics_cost_by_role.py` | per-role sums reconcile to `total_cost_usd`; role columns are not sortable in the serialized response |
| `tdd/unit/benchmark/test_budget_resweep.py` | re-sweep down reproduces a directly-run lower-budget result on a fixture; re-sweep above a cap raises rather than extrapolating |
| `tdd/unit/benchmark/test_board_presentation_rules.py` | P1-P9: every value carries `n` and caps; a filtered metric carries its exclusion count; mixed machine profiles emit `BLOCKED`; `error_rate > 10%` emits `UNRELIABLE` |
| `tdd/integration/api/test_board_api.py` | board over a seeded 3-strategy x 3-case x 3-repeat experiment; `group_by` splits; CSV and JSON agree cell-for-cell |
| `tdd/integration/api/test_experiment_matrix_strategies.py` | matrix fan-out creates the right trial set; `one-shot` auto-injected; `--no-baseline` flagged in the response |
| `frontend/tests/e2e/bench-board.spec.ts` | (R8) board renders rows, cost and wall-clock in the same row, speedup not sortable, hover reveals `n` and exclusions |

### Definition of Done

- [ ] `Experiment.matrix` supports `strategies` x `model_assignments` x `repeat`, baseline auto-injected
- [ ] `bench_metrics.py` implements M1-M8 as pure functions with provenance in every return
- [ ] Golden-fixture suite covers each metric including its known bias mode
- [ ] Board API + UI + CSV/JSON export from a single service call
- [ ] P1-P9 enforced in the serializer
- [ ] Playwright spec shipped in this phase (R8)
- [ ] Tier floors raised (R4)

### EXIT GATE

A 3-strategy matrix (`one-shot` / `adversarial-3` / `planner-fanout-8`) over 3 cases, repeated, produces a board where the same case is comparable across strategy shapes on cost, wall-clock, and regression rate, with `one-shot` present as the control in every comparison; a board request at a budget above any variant's cap is refused with the binding cap named; and the survivorship-bias regression test proves the shipped cost-to-solve differs from the naive computation.

---

## Phase 13.4 — Variance, controls, and the "real or noise" question

**Goal**: every reported figure carries an interval, the board refuses to rank what it cannot separate, and the controls run as first-class variants inside every experiment.

> **Why now:** 13.3 makes the board correct. 13.4 makes it *honest*, which is a different property. Publishing before this phase would mean publishing point estimates over N=3 with no statement of noise — precisely the failure mode the write-up is meant to avoid.

### Deliverables

- **`backend/app/services/bench_stats.py`**: the paired hierarchical bootstrap (cases resampled as clusters, repeats within case), `B = 10000`, seeded from the experiment id so intervals reproduce exactly. Returns `(point, ci_low, ci_high)` for any metric function and `(delta, ci_low, ci_high)` for any pair.
- **Separability decision** (`separability(metric, v_a, v_b)`) implementing the Part 2 rule: paired-difference interval excludes zero; Holm correction over the baseline family; ROPE band per metric; returns one of `SEPARABLE` | `NOT_SEPARABLE` | `WITHIN_NOISE_FLOOR` | `BLOCKED` | `INSUFFICIENT` with the exact presentation string.
- **Required-repeats estimator**: `n_required = n_current * (half_width / target_half_width)^2`, rendered on every `NOT SEPARABLE` verdict.
- **Controls as templates**: `null-agent` (commits nothing) and `gold-patch` (applies `reference_patch`) run in every published experiment. **If `null-agent` scores above 0% solved on any case, the board refuses to render at all** and names the case — that is an oracle defect, not a data point.
- **Ranking gates**: fewer than 5 cases or fewer than 3 repeats -> `PILOT` mode, all ranking disabled; `error_rate > 10%` -> `UNRELIABLE`, ranking disabled for that variant.
- **Splits**: `group_by=vertical|complexity|contamination_risk`. The contamination split answers the skeptic's question directly ("does the gap survive on low-risk cases?"); a low-risk subset under 5 cases renders `INSUFFICIENT`, never a number.
- **Empirical `R`**: measure within-case spread on `core-v1` and record the observed value plus the recommended default in this document and in `METHOD.md` — the roadmap's open question, closed with data.

### Tests

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_bootstrap_intervals.py` | seeded bootstrap is bit-reproducible; clustering by case widens the interval versus a naive per-trial bootstrap on correlated fixture data (the reason clustering exists) |
| `tdd/unit/benchmark/test_separability_rule.py` | paired-difference interval excluding zero -> `SEPARABLE`; containing zero -> `NOT_SEPARABLE`; the exact presentation strings are pinned verbatim |
| `tdd/unit/benchmark/test_separability_not_overlap.py` | a fixture with overlapping marginal intervals but a paired difference excluding zero is ranked SEPARABLE — proving the board does not use the overlap fallacy |
| `tdd/unit/benchmark/test_holm_correction.py` | a family of 3 baseline comparisons adjusts thresholds; a borderline comparison flips to not-separable under correction |
| `tdd/unit/benchmark/test_practical_equivalence_band.py` | a statistically separable 4% cost difference renders `WITHIN NOISE FLOOR` and is excluded from ranking |
| `tdd/unit/benchmark/test_required_repeats_estimator.py` | halving the target half-width quadruples the estimate; the estimate is labeled `est.` in the output string |
| `tdd/unit/benchmark/test_ranking_gates.py` | 4 cases -> `PILOT`, ranking disabled; 12% error rate -> `UNRELIABLE`, ranking disabled |
| `tdd/integration/benchmark/test_null_agent_control.py` | the null-agent template scores 0% solved across the starter suite; a case injected to score above zero causes the board to REFUSE and name it |
| `tdd/integration/benchmark/test_gold_patch_control_in_experiment.py` | the gold-patch control runs inside the experiment (not only at validation) and reports 100% on `solvable_verified` cases |
| `tdd/integration/api/test_board_refuses_to_rank.py` | two variants seeded with a noise-level difference are reported as NOT SEPARABLE with a required-N estimate, not ordered |
| `tdd/integration/api/test_contamination_split.py` | `group_by=contamination_risk` splits the board; a 3-case low-risk subset renders `INSUFFICIENT` |
| `frontend/tests/e2e/bench-board-separability.spec.ts` | (R8) a not-separable pair renders the refusal banner and the required-N line; the sort control is disabled in `PILOT` mode |

### Definition of Done

- [ ] Seeded paired cluster bootstrap; intervals on every board figure
- [ ] Separability verdicts with the five states and their verbatim strings
- [ ] Required-repeats estimator on every refusal
- [ ] `null-agent` and `gold-patch` controls run inside every published experiment; null-agent above zero blocks rendering
- [ ] `PILOT` / `UNRELIABLE` / `INSUFFICIENT` gates implemented
- [ ] Vertical / complexity / contamination splits
- [ ] Observed within-case spread on `core-v1` measured and recorded; default `R` set from it
- [ ] Playwright spec shipped (R8); tier floors raised (R4)

### EXIT GATE

A 3-repeat matrix reports medians with bootstrap intervals throughout; a deliberately noise-level difference is flagged `NOT SEPARABLE` with a required-repeats estimate rather than ranked; the null-agent control scores 0% and a seeded above-zero null-agent blocks the board from rendering; and the contamination split renders on the starter suite with `INSUFFICIENT` where the low-risk subset is too small.

---

## Phase 13.5 — The reproducible bundle

**Goal**: a stranger downloads one file and either re-runs the whole thing or replays the shipped results, and the bundle tells them loudly when their environment is not ours.

> **Why now:** this is what makes the write-up a claim rather than an assertion. It is last because it packages everything above; it is not optional because a benchmark nobody can re-run is a blog post.

### Deliverables

- **Bundle layout** (`<suite>-<experiment>-<bundle_version>.tar.zst`):

```
METHOD.md                          # generated, Part 3 template, no unrendered placeholders
manifest.json                      # bundle_version, harness_version, suite_version,
                                   # sha256 per file, created_at, generator version
suite/
  suite.yaml
  cases/*.yaml                     # incl. oracle ids, contamination_risk, license, quarantined_tests
  patches/*.diff                   # reference patches
repos/
  bundled/<repo>.gitbundle         # where the license permits redistribution
  fetch/<repo>.json                # {source_url, base_commit_sha, sha256_of_tree} where it does not
strategies/*.yaml
results/
  trials.jsonl                     # one row per trial, full provenance block
  iterations.jsonl                 # the cost curves - the science
  board.json                       # the rendered board, as published
  stats.json                       # bootstrap seed, intervals, separability verdicts
```

- **`lazyaf bench export <suite> [--with-results] [--experiment <slug>]`** — license-gated repo packaging (bundle vs fetch-instructions + patch), full provenance per trial, `METHOD.md` generation, sha256 manifest.
- **`lazyaf bench import <bundle> [--with-results]`** — reconstructs suite, cases, strategies, and (optionally) trials + iterations; verifies every sha256; for `fetch/` repos, clones from `source_url`, checks out `base_commit_sha`, and **verifies the tree hash** before use (a moved tag or a force-push is caught, not silently absorbed).
- **Provenance drift detection**: on import and on every board render of imported results, compare `harness_version`, image content hashes, and `model_version` against the current tree. Any difference prints a `PROVENANCE DRIFT` banner listing each differing field, and **the board refuses to place imported and locally-produced results in the same comparison** — it renders them as separate, labeled tables.
- **`METHOD.md` generator** (`bench_method_doc.py`) filling the Part 3 template from the experiment; unrendered placeholders are a build error.
- **Re-run verification in CI (R7)**: the dogfood pipeline exports the starter suite, imports it into a clean database, and asserts the reconstructed case set is identical — so the bundle path cannot rot silently between releases.

### Tests

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_bundle_manifest.py` | manifest lists every file with sha256; a mutated file fails verification; `bundle_version` bumps on content change |
| `tdd/unit/benchmark/test_license_gating.py` | permissive license -> git bundle; restrictive/unknown -> fetch-instructions + patch, never a bundled tree; a case with no `license` blocks export entirely |
| `tdd/unit/benchmark/test_method_doc_generation.py` | no `{{placeholder}}` survives; a field the harness could not determine renders `NOT RECORDED`; the re-run command block contains the actual suite/experiment slugs |
| `tdd/unit/benchmark/test_method_doc_sections.py` | all 12 sections present; controls table reflects the actual control outcomes; the "does NOT claim" section lists every NOT SEPARABLE pair |
| `tdd/integration/benchmark/test_bundle_roundtrip.py` | export -> import into a clean DB reconstructs suites, cases, strategies byte-for-byte on the YAML; slugs, oracle ids and hashes preserved |
| `tdd/integration/benchmark/test_bundle_results_roundtrip.py` | `--with-results` re-imports trials + iterations and the re-rendered board equals `board.json` cell-for-cell |
| `tdd/integration/benchmark/test_fetch_repo_verification.py` | a `fetch/` repo whose upstream tree no longer matches the recorded hash fails import loudly instead of proceeding |
| `tdd/integration/benchmark/test_provenance_drift_warning.py` | an imported bundle with a different harness version or image hash emits `PROVENANCE DRIFT` naming each field, and the board refuses to merge the two result sets |
| `tdd/e2e/test_bundle_replay.py` | (`@slow`) clean checkout -> import -> `bench validate` -> replay a mock trial -> board matches within the stated variance; the `METHOD.md` re-run command block executes verbatim |

### Definition of Done

- [ ] `lazyaf bench export` / `import` implemented with the layout above
- [ ] License gating: bundle vs fetch+patch, and export blocked on a missing license
- [ ] sha256 manifest verified on import; fetch repos tree-hash verified
- [ ] `METHOD.md` generated with all 12 sections and zero unrendered placeholders
- [ ] Provenance drift banner + refusal to co-present drifted results
- [ ] Export/import round-trip in the dogfood pipeline (R7)
- [ ] Tier floors raised (R4)

### EXIT GATE

Export -> import on a clean checkout reconstructs the suite and replays a trial; the bundle's stated re-run command works verbatim as written in `METHOD.md`; a bundle whose harness or image hashes differ from the current tree prints `PROVENANCE DRIFT` naming each differing field and refuses to compare the two result sets in one table; and an export attempted on a case with no `license` is refused.

---

## Milestone exit gate (all of 13.x)

The milestone is done when a stranger can be handed one file and a URL and, without asking a question:

1. import the bundle, run `bench validate`, and see the controls pass;
2. read `METHOD.md` and know what was measured, what was not, and what the numbers refuse to claim;
3. re-run the experiment with the verbatim command and land inside the published intervals;
4. find at least one comparison the board **declined to rank**, because a board that ranks everything is a board that is not measuring noise.

Point 4 is the real gate. Anyone can publish a leaderboard; the credible thing is a leaderboard with holes in it where the data does not support an answer.

---

## Open questions carried into implementation

- **Network access during a trial.** Leaning allow-with-provenance: record `network_mode` per trial and offer `--cached-only` (proxy + warm package cache) for published runs. Decide at 13.2 when the first real dependency install fails offline.
- **`R` default.** Set empirically at 13.4 from the measured within-case spread on `core-v1`; `5` is the placeholder, not the answer.
- **Partial credit.** `fail_to_pass` 3/5 is recorded per iteration and drives the convergence figures, but "solved" stays binary. Revisit only if the iteration CDFs show partial progress predicting eventual solves — which would make it a leading indicator worth reporting rather than a softer scoring rule.
- **Cross-machine cost comparability under `cost_source = "gpu-node"`.** An occupancy model is not a bill. Trials mixing `cli-reported` and `gpu-node` costs are flagged in the board; whether they may be ranked together is a 13.3 decision informed by how far the two sources diverge on the same workload.
