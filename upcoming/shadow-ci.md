# Shadow CI — mirroring somebody else's repo and running our own pipelines on their commits

**Status:** design. Not started. Lands after 12.8 (v1 pipeline retirement) — see §11.
**Owns:** this file. Touches no code yet.

---

## 1. What this is, and what it is not

Shadow CI lets LazyAF **mirror a repo it does not own**, land that repo's commits in the internal git server on a timer, and **fire LazyAF's own pipelines** — including agent steps — as those commits arrive. It is CI you run *at* somebody, without asking them and without touching their infrastructure.

The boundary that decides how soon this is playable: **everything the owner actually asked for works on a PUBLIC repo with no credential anywhere.** An anonymous HTTPS `git fetch` is the whole read path; the internal git server, workspace population (`backend/app/services/workspace/population.py:151`, a plain `git clone` with no credential of any kind), and every step container are already credential-free. So Phase 1 ships **public repos only**, enforced — an `upstream_url` that is not `https://` is a 422 at create time, and every fetch runs with `GIT_TERMINAL_PROMPT=0` so a private repo fails in two seconds naming the limitation instead of hanging a helper container on a password prompt nobody is watching. A **read** credential for private repos (§6) is a real design but a later phase. A **write** credential — the one that opens a PR upstream — is **explicitly gated** behind two prerequisites this repo does not meet today (§7).

What it is not, in v1: it does not open pull requests, it does not push anything anywhere, it does not run the mirrored repo's own `.lazyaf/pipelines/*.yaml`, and it does not touch private repos. The last two are not omissions to be filled in later by whoever gets there first; they are load-bearing refusals with reasons attached (§4.1, §6).

---

## 2. The sync

### 2.1 New module, not `GitRepoManager`

The fetch lands in a **new** `backend/app/services/upstream_sync.py`. Nothing in the tree fetches from a remote today: `Repo.remote_url` (`backend/app/models/repo.py:18`) is an inert stored string, and `git_server.py` is entirely inbound — `create_bare_repo` (:47), `push_from_local` (:95), `handle_upload_pack` (:1788), `handle_receive_pack` (:2014). A new module also keeps this out of `git_server.py`, which wave10's **A2 · EXECUTOR** owns (`upcoming/wave10-v1-retirement.md` §3.2).

**Subprocess `git`, not dulwich.** `git` is already in the backend image (`backend/Dockerfile:9`), `push_from_local` already shells out to it for this class of work (`git_server.py:95`), and the in-tree smart-protocol code is hand-rolled: `handle_upload_pack` walks the object graph itself and materialises every object in memory before packing (`entries = list(get_objects())`, `git_server.py:1930`), and `handle_receive_pack` applies ref updates with no old-sha compare-and-swap (`git_server.py:2131-2135`). Real `git` gives protocol v2 negotiation, `--prune`, force-refspec semantics and credential helpers for free. Run it off the event loop via `starlette.concurrency.run_in_threadpool`.

### 2.2 Refspec and namespacing — `refs/upstream/*`, not `refs/heads/upstream/*`

```
git ls-remote --symref <url> 'refs/heads/*' HEAD                    # pre-check, every tick
git fetch --prune --no-tags <url> '+refs/heads/*:refs/upstream/*'   # only when a tip moved
```

Mirrored refs live **outside `refs/heads/`**. This was the sharpest disagreement between the design lanes and it is worth stating why the other answer loses.

The case for `refs/heads/upstream/<branch>` is genuinely attractive: it is clonable with zero changes, and it appears to buy security for free, because `sync_repo_pipelines` early-returns unless the pushed branch equals `repo.default_branch` (`backend/app/services/trigger_service.py:280`) — so a commit landing on `upstream/main` would never adopt upstream's pipeline definitions.

**That safety property is contingent on a mutable DB field that other code writes automatically.** `GET /api/repos/{id}/branches` adopts the bare repo's HEAD into `repo.default_branch` whenever that branch exists in `list_branches` (`backend/app/routers/repos.py:320-327`), and `handle_receive_pack` re-points HEAD at the first non-`lazyaf/` branch it sees whenever HEAD is unset or still `refs/heads/main` (`git_server.py:2149-2163`). Two code paths that know nothing about shadow CI can therefore set `repo.default_branch = "upstream/main"`, at which point the gate at `trigger_service.py:280` **passes** and an arbitrary third party's YAML defines what runs on the owner's Docker socket. A security boundary whose enforcement is "a string field happens not to be equal to another string field" is not a boundary.

`refs/upstream/*` is structurally unreachable instead. Every branch-facing operation in the tree filters on `refs/heads/`: `list_branches` (`git_server.py:221`, filter at :226-227), `cleanup_orphaned_branches` (:324), `get_branch_commit` (:598), `verify_repo_integrity` (:424). So mirrored refs are invisible to the branch UI, invisible to the orphan sweep that would otherwise delete them, and unreachable by `delete_branch`. The refspec is the other half: mirroring **only** into `refs/upstream/*` means a fetch can never write a `refs/heads/` ref at all, and no existing UI path can push into `refs/upstream/*`. Fetch and operator/agent pushes cannot collide because they occupy disjoint namespaces.

**The cost, paid deliberately.** `git clone --branch <name>` resolves only against `refs/heads/*` and `refs/tags/*`, and workspace population is exactly that clone (`population.py:151`), fed from `branch = context.get("branch") or repo.default_branch` in the executor (`pipeline_executor.py:2798`). So when shadow CI decides to actually **run** a mirrored commit, the sync service creates one disposable head:

```
refs/heads/lazyaf/shadow/<short-sha>   ->  <commit_sha>
```

and passes *that* name as `branch` in the trigger context. The `lazyaf/` prefix is already a recognised namespace with the right properties, verified: `handle_receive_pack` excludes `lazyaf/` branches from HEAD adoption (`git_server.py:2137-2140`) and `list_branches`' default-branch fallback skips them (`repos.py:337-339`), so a disposable head can never become `repo.default_branch`. It is deleted when the run's workspace is cleaned, with an age sweep as backstop.

This keeps `population.py`, `pipeline_executor.py:2798` and `workspace_service` completely untouched — all three are contested or load-bearing. The cleaner long-term fix is to teach population to fetch by sha instead of `clone --branch` (it would work: `handle_upload_pack` serves any want-sha with no reachability check, and `get_info_refs` already advertises `allow-tip-sha1-in-want allow-reachable-sha1-in-want`, `git_server.py:1757`), which would let the disposable head go away. That is a Phase 4 cleanup, not a Phase 1 risk.

### 2.3 Which refs, and the default branch

Mirror **all** heads; fire on an explicit glob. `mirror_branch_globs` defaults to `[<upstream default branch>]`. Keeping the object store complete means a later widening needs no re-fetch, while the trigger surface — the side that spends money — stays small. `--no-tags` in v1: tags are a second namespace with their own force-update semantics and nothing in the trigger vocabulary (`backend/app/schemas/pipeline.py:555-563`) consumes them.

Resolve the upstream default branch on every tick from the `--symref` line of `ls-remote` and store it in a **new** `upstream_default_branch` column. **Never write `Repo.default_branch` from a fetch.** That field is LazyAF's own HEAD — `create_bare_repo` points the bare repo at it (`git_server.py:47`), `get_default_branch` reads it back (:208), and `sync_repo_pipelines` gates on it (`trigger_service.py:280`). Letting an upstream rename silently rewrite it moves which branch may redefine pipelines, out from under the operator. When `upstream_default_branch` changes, record a sync event and surface it as something the operator confirms.

### 2.4 Force-push and rewritten history

Force-update (`+` refspec) and `--prune`. The mirror always tells the truth about upstream **now**; a mirror that refuses to rewind is no longer a mirror and "what does upstream say" becomes unanswerable. Durability comes from two explicit mechanisms instead:

1. **`upstream_ref_event`** records every observed movement — `(repo_id, ref, old_sha, new_sha, kind, observed_at)` with `kind ∈ {create, advance, rewrite, delete, skipped}`. Without it, "the fetch brought 40 commits and we deliberately fired once" and "the trigger silently failed" look identical.
2. **Keep-refs.** Every commit shadow CI actually runs gets `refs/lazyaf/keep/<sha>`, swept `keep_days` (default 30) after that run finishes.

In-flight runs are **not** cancelled when their commit stops being reachable upstream. The checkout is already pinned (`git checkout --detach`, `population.py:155`) and a rewrite upstream does not make a finished measurement wrong — it makes it historical. The object side is safe today too, but *by accident*: nothing in this tree ever runs `git gc`, `repack` or `prune` (the only `gc` in `git_server.py` is Python's `gc.collect()` inside `reinitialize_repo`). Relying on an absent garbage collector is not a design; the keep-ref makes retention deliberate and survives the day someone adds one.

### 2.5 Cadence

`ls-remote` is the pre-check and does double duty — one HTTP round trip, zero objects, returns every tip sha **and** the upstream HEAD symref that §2.3 needs. Skip `git fetch` entirely when nothing moved.

- `poll_interval_seconds`, default **300**, floor **60** refused at the API.
- **±10% jitter**, so N repos never align on one tick.
- Exponential backoff to a **1h** ceiling on error, reset on success.

At 300s that is ~288 requests/repo/day — unremarkable, and roughly what any CI mirror does. Rejected: the GitHub REST API as the pre-check (host-specific, and it is the endpoint with the hard 60/hr unauthenticated limit). **Webhooks** are the honest long-term answer and cost one round trip instead of 288 — but they need an inbound-reachable URL and a signature-verified endpoint, and every human-facing router here is unauthenticated, so an unauthenticated webhook is a trigger anyone on the network can fire. Poll first; webhook after the auth story of §7.

### 2.6 First sync is a distinct ADOPT mode

`POST /api/repos/{id}/fetch` enqueues and returns **202** with a sync-event id; the periodic loop does the work. The first sync fetches, writes `refs/upstream/*`, persists every observed tip as last-seen, **fires zero triggers**, and transitions `never_synced -> idle`.

Adopt-not-trigger is an explicit mode, not the emergent behaviour of "fire nothing when there is no previous tip" — that rule silently stops applying the moment a new branch appears upstream, which is exactly when you want it.

Sync state on the repo: `never_synced | syncing | idle | error`, plus `last_synced_at`, `last_sync_error`, and a `phase` field (`ls-remote | fetching | indexing | done`) written from `git fetch --progress` stderr. Broadcast as a new `repo_sync` websocket event. Show elapsed seconds and phase; do not promise a percentage.

**The thing to say plainly:** the first sync is the *cheap* part. Every subsequent **workspace clone** is the expensive part, because the internal upload-pack has no shallow support — `shallow` is advertised in the capabilities (`git_server.py:1757`) but the request parser only ever handles `want`/`have`/`done`, never a `deepen` or `shallow` line — and it materialises the entire requested object set in backend process memory (`git_server.py:1930`). Mirroring a 100k-object repo means every shadow run pins that whole graph in backend RSS. **Cap the first target repo's size deliberately.** Shallow/partial-clone support in `handle_upload_pack` is the prerequisite for shadow CI on anything large, and it is Phase 4, not Phase 1.

---

## 3. The scheduler (which does not exist, and which `schedule` has been promising)

`schedule` is in `PUBLIC_TRIGGER_TYPES` (`backend/app/schemas/pipeline.py:561`) and is **vocabulary only**. Grep confirms: no apscheduler, no croniter, no cron, and no code anywhere reads `trigger_type == "schedule"` — the only other hit is a comment on the model column (`backend/app/models/pipeline.py:73`). A pipeline can declare a schedule trigger today, the validator accepts it with a 201, and nothing will ever fire it. That is the same defect class the codebase elsewhere calls out as an invisible downgrade, and shadow CI is the phase that builds the substrate which closes it.

### 3.1 One primitive, two callers

Extract `backend/app/services/periodic.py`: the tick/except/sleep skeleton copied from `_orphan_audit_loop` (`backend/app/services/workspace_service.py:919-935`), a `claim_due()` CAS helper, and a `start_*() -> factory` pair matching `start_orphan_audit` (:940-966) so tests can inject interval and `session_factory` without sleeping. Wired into lifespan beside `orphan_audit_task = start_orphan_audit()()` (`backend/app/main.py:129`) and cancelled with it.

No new dependency. The tree already runs three lifespan-owned loops (`runner_dispatcher.start`, `playground_service.start`, the orphan audit) and has a house shape for them; adding apscheduler would put a second scheduling model beside the one this process already runs three times.

**Coarse tick: wake every 30s and ask the DB what is due.** Not sleep-until-next-fire (a restart invalidates the computed sleep and you need the durable row anyway), not a 1s tick (86400 SQLite polls a day to do nothing).

The sync poller and the `schedule` trigger share **the shape and the CAS helper, and nothing else** — different cadence, different tables, different failure semantics. A lost fetch tick is retried in a minute; a lost schedule tick is gone.

### 3.2 Durable schedule row

```
pipeline_schedules
  id            str(36) pk
  pipeline_id   str(36) fk -> pipelines.id, indexed
  spec          str(64)          # v1: interval seconds, see below
  enabled       bool default true
  next_fire_at  datetime  INDEXED
  last_fired_at datetime | null
  last_run_id   str(36) | null
```

**Missed-tick policy, named and written down: COALESCE, NEVER REPLAY.** On boot, any row whose `next_fire_at` is in the past fires **once** and advances to the next slot **in the future**. `next_fire_at += interval` in a loop after a weekend outage on a 15-minute schedule is 192 runs at once, and under shadow CI those are billed agent steps. The precedent for "recover into a sane state, do not re-run history" is already in lifespan (`recover_orphaned_executions`, `sweep_paused_sessions`, `main.py:94,106`).

**Claim by compare-and-set, never read-then-write:**

```sql
UPDATE pipeline_schedules
   SET next_fire_at = :computed, last_fired_at = :now
 WHERE id = :id AND next_fire_at = :the_value_just_read
```

`rowcount == 1` means this caller won the tick — the same CAS the experiment dispatcher claims a cell with (`backend/app/services/experiment_service.py:858`). It is the only part of this design that still works when the single-worker assumption breaks, and that assumption is a *warning*, not an enforcement (`main.py` logs above `WEB_CONCURRENCY=1` and carries on). **Claim first, dispatch second.** A lost tick is a delay; a duplicate tick is a duplicate bill. Take the delay — the deliberate inverse of `on_push`, which *releases* its dedup key when a start throws (`trigger_service.py:526-538`) because a push will not come again.

**v1 spec syntax: interval seconds.** No cron parser exists in `backend/pyproject.toml` and none is worth adding to close a dark contract. Say so in the trigger's validation message. Timezone: naive UTC, like everything else in the tree.

### 3.3 Lifecycle — including the bug this would otherwise introduce

Schedule rows materialize from the pipeline's triggers JSON inside `upsert_materialized_pipeline` (`trigger_service.py:92`), so there is one source of truth. **Critically, the yaml-removed sweep must also disable the schedule row.** That sweep clears `pipeline.triggers = "[]"` when a yaml file vanishes but deliberately keeps the row because run history hangs off it (`trigger_service.py:397-408`). If the schedule lives in a separate table, the sweep will not touch it and **a deleted pipeline file keeps firing forever**. That is a bug this change would introduce, not one that exists. Naming it now is cheaper than finding it later.

---

## 4. The trigger

### 4.1 A new type — `upstream_commit` — and a separate entry point

Add `"upstream_commit"` to `PUBLIC_TRIGGER_TYPES` (`backend/app/schemas/pipeline.py:555-563`) and add a **new** `TriggerService.on_upstream_commit()` that does **trigger matching only**.

**Do not route mirror-landed commits through `on_push`. This is the finding that gates the whole phase.**

`on_push` (`trigger_service.py:422`) does two things in order and the first is the problem: it calls `sync_repo_pipelines` **before** matching (`trigger_service.py:455`), which reads `.lazyaf/pipelines/*.yaml` at the incoming commit and hands it to `upsert_materialized_pipeline` (`trigger_service.py:92`), which overwrites the materialized row's description, steps **and triggers** from that yaml. Trigger matching then runs immediately in the same call. A step yaml may declare `type: agent`; an agent step is handed the platform's `ANTHROPIC_API_KEY` at dispatch; the step runs in a container spawned through the Docker socket the backend mounts (`docker-compose.yml:14`) on a daemon published at `0.0.0.0` (`docker-compose.yml:5`). And a step's image is taken straight from its config with no allowlist (`pipeline_executor.py:3771`), so `type: docker` with an arbitrary image is equally reachable.

So: mirror someone else's repo, route the landed commit through `on_push`, and **any commit that repo's authors land is arbitrary code execution on the owner's machine with the owner's API key** — unattended, and indistinguishable from a normal run in the run list. Worse, `on_push` catches a sync failure into `logger.error` and proceeds (`trigger_service.py:456-459`), so an adopted definition does not even look anomalous.

Two further reasons the type must be new rather than reused:

- **Existing yaml must not start meaning something new.** Every `push` trigger in the tree — including `.lazyaf/pipelines/test-suite.yaml`, in the working set right now — was written meaning "when *I* push". Reusing `push` retroactively rewrites all of them.
- `upstream_commit` belongs in `PUBLIC_TRIGGER_TYPES`, not `ADHOC`: unlike `card_work` it routes to nothing on completion, so a caller stamping it manually ("run as if commit X landed") is useful and harmless.

**Rejected: a `sync_definitions=False` flag on `on_push`.** A boolean that disables the dangerous half of a function is the kind of default the next refactor flips back. A separate entry point makes the refusal structural. **Also rejected: sanitising upstream yaml by allowlisting step types** — an allowlist that must stay ahead of every future step type is a losing race, and a `script` step with a shell command is not meaningfully safer than an agent step when the Docker socket is mounted.

**Consequence the owner should hear now:** shadow pipelines **cannot be authored in the mirrored repo**. He writes them in LazyAF, or in a `.lazyaf/` directory of a repo he controls. If a future change assumes otherwise, this decision gets quietly reversed and the RCE comes back.

### 4.2 What fires, and how often

**One trigger per ref-tip movement.** A fetch advancing `main` from A to Z fires exactly **one** event carrying `{branch, new_sha: Z, old_sha: A, commit_count: N}`. Never one per commit.

Tip movement is what every downstream consumer is already shaped for — `on_push`'s `(branch, commit_sha, old_sha)` signature, the dedup key `pipeline:branch:sha` — and it is what GitHub Actions does on a multi-commit push. Forty runs from one fetch is not hypothetical: a merged 40-commit PR would otherwise spawn 40 full-history workspace clones and 40 agent steps from one poll the owner did not initiate. (A per-commit mode is worth exposing *later* as an opt-in for cheap script-only pipelines where bisecting is the actual goal; it must never be reachable without a run budget.)

### 4.3 Idempotence — two different questions, two mechanisms

**The existing deduplicator cannot do this job.** `TriggerDeduplicator` is a plain dict, explicitly process-local with no DB state (`backend/app/services/workspace/trigger_dedup.py:69-71`), with a 10-second window (`trigger_service.py:40`) and 600-second eviction (:44). It answers "two push events arrived for one sha in two seconds" well and should keep doing so. It cannot survive a restart, and shadow CI asks a question with no window.

1. **"Has this ref moved?"** — `upstream_ref_state`, the durable last-seen tip per `(repo_id, ref)`. A tip is eligible exactly once; advancing the stored tip happens in the **same transaction** that records the event.
2. **"Have we EVER run this sha for this pipeline?"** — `shadow_run_claim`, with a unique index on `{repo_id}:{pipeline_id}:{branch}:{commit_sha}`, inserted **before** the run starts, using insert / IntegrityError / rollback / re-read exactly as `get_or_create_execution` does (`backend/app/services/execution/idempotency.py`) against `StepExecution.execution_key` (`models/pipeline.py:145`, `unique=True`).

The key includes `pipeline_id` on purpose — two shadow pipelines on one repo should both run on one commit. It excludes any attempt counter, because re-running a shadow commit is a manual act (the `debug_rerun` path), not something a poll loop may decide.

**Do not auto-release the claim on failure.** `on_push` releases because its retry source is a human. Here the retry source is a loop that will come back in 60 seconds forever. Prefer "ran zero times, visibly stuck at `claimed`" over "ran a hundred times". Rows stuck at `claimed` with a NULL `pipeline_run_id` are swept at startup, same shape as `recover_orphaned_executions` (`main.py:94`).

**Skips are data, not log lines.** Claim statuses: `claimed`, `dispatched`, `skipped_cap`, `skipped_budget`, `skipped_dry_run` — for the same reason `SKIPPED_BUDGET` is a first-class cell outcome in experiments (`backend/app/models/experiment.py:128`). "Why didn't my repo build" must be answerable without reading logs.

### 4.4 What the run sees

Trigger context, with `branch` and `commit_sha` **under their existing names** — that is not cosmetic. The executor reads exactly those two keys to pin the workspace (`pipeline_executor.py:2798`) and to resolve an agent step's base branch (:3358). Rename them and every shadow run silently gets a trunk checkout.

```json
{
  "branch": "lazyaf/shadow/a1b2c3d",
  "commit_sha": "a1b2c3d4...",
  "old_sha": "9f8e7d6...",
  "upstream_ref": "refs/upstream/main",
  "upstream_branch": "main",
  "upstream_author": "...",
  "upstream_message": "...",
  "upstream_committed_at": "...",
  "upstream_commit_count": 12,
  "shadow": true,
  "on_pass": "nothing",
  "on_fail": "nothing"
}
```

Everything here is free from the mirror: `get_branch_commit` (`git_server.py:598`) and `get_commit_log` (:607, which already returns `{sha, short_sha, message, author, timestamp}`) supply it, and `old_sha` is the tip the fetch already had to know to detect the change. Carry the diff **range**, not the diff — the workspace is checked out detached at the pinned sha, so a step runs `git diff $LAZYAF_UPSTREAM_OLD_SHA..HEAD` itself. A large diff has no business in a DB column.

**`on_pass`/`on_fail` must be forced to `"nothing"`, refused with a 422 at materialization rather than silently dropped.** They are read straight out of trigger_context at run completion (`pipeline_executor.py:1706`), so `on_pass: "merge"` on a shadow trigger auto-merges a stranger's commit into the owner's default branch.

**The gap nobody's brief listed, and it is required, not nice-to-have:** trigger_context never reaches a step container. The env block is fixed (`LAZYAF_PIPELINE_RUN_ID`, `LAZYAF_STEP_RUN_ID`, … at `backend/app/services/execution/local_executor.py:682`, mirrored in `runner_protocol.py`), and the only user channel — `params` — **is not persisted** (there is no `params` column on `PipelineRun`, `models/pipeline.py:68-80`), so it lives only in the in-process dispatch chain. Today a shell step in a shadow run could learn the commit only via `git rev-parse HEAD` and could not learn the author or the range at all. Ship a `LAZYAF_UPSTREAM_SHA` / `_OLD_SHA` / `_BRANCH` / `_AUTHOR` / `_COMMIT_COUNT` block in **both** executors. Agent steps are better off already — the prompt builder reads context keys directly (`pipeline_executor.py:3358`).

---

## 5. The safety valve

Somebody else's commit rate drives the owner's spend. **The defaults are the product.** They are lifted from the Experiments engine rather than invented, because that engine solved this exact problem and its numbers carry their reasoning: `EXPERIMENT_MAX_CELLS = 200` exists "so the refusal is a 422 naming the count rather than a bill naming it" (`backend/app/models/experiment.py:75-78`), and the concurrency cap "bounds DISPATCH, so the maximum budget overshoot is whatever this many in-flight cells cost" (:80-86).

| Control | Default | Where enforced | Why this value |
|---|---|---|---|
| `sync_enabled` | **false** | poll loop | Two switches, not one: fetching is harmless, running is not. |
| `shadow_enabled` | **false** | dispatch | Typing a URL into a form must never start spending. A DB column, not an in-memory pause — this is also the kill switch, and it survives a restart. |
| `poll_interval_seconds` | **300** (floor 60, 422 below) | API + loop | ~288 req/day/repo. The floor stops a 5s loop from being typed in. |
| `mirror_branch_globs` | `[upstream_default_branch]` | eligibility | Mirror all heads, fire on one. A repo with 200 dependabot branches is not 200 candidate triggers. |
| `max_runs_per_fetch` | **1** (tip only) | dispatch | Highest-leverage default in the table. A merged 40-commit PR is ONE run, by construction rather than by budget arithmetic. |
| `max_concurrent_shadow_runs` | **1** per repo | dispatch, DB-derived count | One below the experiment default of 2: shadow work is background work and must never contend with the owner's own runs. |
| `budget_usd` | **REQUIRED at arm time, no default** | dispatch, recomputed per run | A default cap is a cap nobody chose; the 422 is the feature. Required when `shadow_enabled` flips true — *not* at repo-add, so mirroring stays frictionless. |
| `budget_window` | rolling 24h | dispatch | Cannot be gamed by a fetch at 23:59. |
| `keep_days` | **30** | keep-ref sweep | Retention of run-against commits (§2.4). |
| `dry_run` | **false** | dispatch | *Departure from one lane's advice — see below.* |
| fetch-error backoff | ×2 to **1h** ceiling | poll loop | A repo renamed or made private must escalate, not fail every 300s forever. |

Enforce every one of these **at dispatch, recomputed per run**, never batched up front — the `_pump_once` shape at `backend/app/services/experiment_service.py:774-810`: count live from the DB, compare to cap, recompute observed spend, compare to budget, CAS-claim, dispatch **one**.

**On `dry_run`:** one lane recommended defaulting it true on first arm. I am overruling that. Two off-by-default gates (`shadow_enabled`, plus a budget that must be typed) already prevent accidental spend, and a third gate that must be discovered and switched off turns the ten-minute demo into a fifteen-minute one whose centrepiece is a run that deliberately did not happen. Keep `dry_run` as an explicit flag — it is genuinely useful for "what would this have cost" — but do not make it the default. The concern behind the recommendation is real and is answered elsewhere: a repo that looks armed and never runs is exactly what the `skipped_*` claim statuses (§4.3) and the sync event log (§2.4) exist to make visible.

**The dollar budget is only partially enforceable, and the design must say so out loud.** `observed_spend` counts **zero** for any usage row whose `cost_usd` is NULL or whose `cost_source` is `unknown` (`backend/app/services/experiment_metrics.py:186-202`), which is the only defensible arithmetic and is why `cost_coverage` is surfaced next to it. A Claude or Gemini agent step yields `cli-reported` with a real number, so the cap works for the case the owner cares about most; an M14 self-hosted endpoint is in `UNBILLABLE_PROVIDERS` (`backend/app/services/usage_ingestion.py:95`) and prices only through the gpu-node rate table, whose default is `{}` — so the cap is **silently unenforced** there. Therefore: **the run-count caps are the primary control; the dollar cap is secondary**, and `cost_coverage` must be surfaced on the shadow repo's status exactly as the experiment board does it. Do not present `budget_usd` as a hard guarantee; that would be a lie the codebase already refuses to tell elsewhere.

---

## 6. Credentials

### 6.1 Public-first, enforced

Phase 1 handles **no credential at all**. Enforced mechanically:

- `upstream_url` must be `https://` — 422 at create/update, message naming the private-repo limitation. `ssh://` implies a private key at rest; `git://` is unauthenticated but unencrypted and unverifiable.
- `GIT_TERMINAL_PROMPT=0` on every git invocation.
- A URL containing userinfo (`https://token@host/...`) is **structurally refused**, not merely discouraged — argv is world-readable in `/proc`.

Making this a deliberate, enforced product boundary rather than an accident of what got built first is what keeps the M14 decision intact while still shipping something the owner can point at a repo today.

### 6.2 The private path, when it is needed

Mirror the ModelEndpoint precedent **exactly** rather than departing from it. That decision is recorded verbatim at `backend/app/models/model_endpoint.py` and restated at `backend/app/services/model_endpoints/secrets.py:1-24`: no encryption key, no KMS, plain-file SQLite backups, unauthenticated GETs — "a stored key would be a new class of exposure introduced for the convenience of one form field." It applies here unchanged and with higher stakes: `GET /api/repos` is equally unauthenticated (`backend/app/routers/repos.py:42`) and `repo_to_dict` already serialises `remote_url` into every websocket broadcast, so a token in a repo column would be broadcast to every connected browser tab and copied into every plaintext backup.

```
Repo.upstream_auth_ref : str | None      # the NAME of a backend env var
UPSTREAM_SECRET_REF_RE = ^LAZYAF_UPSTREAM_[A-Z0-9_]{1,48}$
```

- **The prefix allowlist is load-bearing and must be copied, not approximated.** Without it, `upstream_auth_ref: "ANTHROPIC_API_KEY"` turns a stored repo row into an exfiltration route — precisely what `ENDPOINT_SECRET_REF_RE` (`secrets.py:39`) exists to prevent. A ref failing the regex is a **422 at CREATE time**; a ref that passes but resolves to nothing is a **fetch failure naming the variable**, never the value.
- Resolve `<REF>_FILE`-first through the existing two-channel read.
- **Delivery: a 0600 `.git-credentials` file** written with `container.put_archive` onto a created-but-not-started fetch container, plus `credential.helper=store`. Never in the URL, never in env (visible in `docker inspect`, which the 12.5 channel exists to avoid), never in argv.
- Expose `upstream_secret_present: bool` on the API, mirroring `secret_present` (`secrets.py:150`), so the UI can say "not set in the backend environment" without ever seeing a value.

**The failure mode that decides the delivery mechanism:** `git clone https://token@host/...` writes that URL into `.git/config` **inside the workspace volume** — and that volume is mounted into every step container. An untrusted upstream's own pipeline step then reads the operator's token with `cat .git/config`. Credential-helper delivery keeps the secret out of the remote URL and out of the workspace entirely; the fetch goes into the bare mirror, and workspace clones continue to come from the internal git server with no credential at all.

**Build the fetch helper with the create → put_archive → start shape in Phase 1 even though Phase 1 carries no credential.** Population currently uses `containers.run`, which starts immediately and cannot receive a file. Retrofitting the shape later is the change nobody makes, and the token ends up in the URL instead.

Two more small things that cost nothing now and matter later: **do not log the fetch URL at INFO** (log repo id and upstream host only — population logs its clone URL today, `population.py:210`), and **run any upstream git stderr through `scrub_secrets`** (`secrets.py:182`) before persisting it to a run record. Git echoes remote URLs back in errors; that is the path by which a credential reaches the database despite never being stored there.

---

## 7. The upstream PR path — designed, and GATED

**Do not build this yet.** State the prerequisite plainly, where the next person will find it.

### 7.1 The gate

Before a GitHub **write** credential enters this process, three things must be true:

1. **The mutating operator routes carry an auth dependency.** Today every human-facing router is mounted with none. A grep across `backend/app/routers/` finds auth only in `ws_runners.py` (constant-time compare against the runner secret), `debug.py` (HS256 join token), and `test_api.py`'s `require_test_mode` — which is a mode gate, not authentication. This is not a research project: the `debug.py` JWT pattern generalizes and needs no new secret.
2. **The default bind is loopback** — `127.0.0.1:8000:8000`, not `docker-compose.yml:5`'s `"8000:8000"`, which Docker publishes on all interfaces. The same process mounts the Docker socket (:14).
3. The credential is a **fine-grained token scoped to `contents:write` + `pull_requests:write` on ONE repo**, delivered through §6.2's file channel.

Until (1) and (2), the honest product statement is: **shadow CI reads and reviews; the human pushes.** That is not a degraded mode — it is the correct mode for a tool that mirrors other people's code. "Make it opt-in per repo" does not close this: opt-in controls *which* repo the token touches, not *who* can reach the token.

### 7.2 The shape, for when the gate opens

- The unit of upstream offer is **a branch in the local mirror that a human explicitly promoted**. Promotion is the only event that can move bytes off the machine.
- The shadow run already produces a branch in the internal bare repo — no new capability needed. A human reviews the diff with the existing `get_diff` (`git_server.py:1348`) and clicks **Offer Upstream** on that specific branch, with the diff on screen. No "approve all", no remembered approval.
- **Push to a FORK under the operator's own account, never to the upstream directly.** This is the highest-leverage choice in the section: a token scoped to the operator's own fork cannot write refs into the target repository at all, so the worst case of a leaked credential is a mess in the operator's own namespace.
- Then `POST /repos/{owner}/{repo}/pulls` with head `operator:lazyaf/shadow/<short-sha>`, via `httpx` (already a dependency), not a new SDK.
- **Disclose AI authorship in the PR body's first line** — agent, model, pipeline name, run id, and the exact upstream commit sha it was based on. Not decoration: an undisclosed AI PR is the fastest way to get the operator's account blocked.
- Reuse `CardStatus.IN_REVIEW` (`backend/app/models/card.py:14`) as the state; it already means "an agent finished, a human has not looked".

### 7.3 Social gates (cheap, and they are product features)

- `may_offer_upstream`, default **off**, enabled only by re-typing the upstream URL — an affirmative act, not a toggle.
- **A hard invariant instead of a rate limit: ONE open LazyAF PR per upstream repo at a time.** An invariant reads better than a window and cannot be misconfigured. Without it, shadow CI on a busy upstream opens a PR per green run, the maintainer blocks the operator's account, and the first real contribution he makes by hand is auto-closed.
- Before enabling Offer, fetch the upstream's `CONTRIBUTING.md` and surface any AI-contribution prohibition as a warning. One extra fetch, and it is the difference between a tool that reads the room and one that does not.

**The trap to name:** an "auto-offer when the pipeline passes" option. If definition sync were ever enabled for mirrors, the upstream author would control both the test that gates the PR and the PR itself — self-certifying automation. §4.1's refusal is what makes this safe, and it is a second, independent reason for it.

---

## 8. The data model

### 8.1 A mode on `Repo`, not a new entity

Everything that makes a repo useful keys off `repo_id`, and several of those keys are structural: `TestRef` identity is the unique index `(repo_id, lazyaf_test_id)`; Cards and Pipelines are cascade-delete relationships on `Repo` (`models/repo.py:24-25`); workspace population clones from `internal_git_url`, derived from `repo.id` (:28-30). A separate `ShadowRepo` entity needs a parallel TestRef index, a parallel population path and a parallel pipeline FK — that is a rewrite for no gain.

Rejected: a `kind` enum (`normal | shadow | bench`). It forces a premature three-way split when the only real behavioural difference is a boolean plus a ref layout, and it makes "a bench case pinned inside a repo I also shadow" unrepresentable.

**New columns on `repos`** (all nullable/defaulted, so a NULL `upstream_url` is exactly today's repo):

```
upstream_url                str(1024) | null
upstream_default_branch     str(255)  | null
mirror_branch_globs         text          default '[]'    # JSON array
sync_enabled                bool          default false
poll_interval_seconds       int           default 300
sync_state                  str(32)       default 'never_synced'
last_synced_at              datetime  | null
last_sync_error             text      | null
shadow_enabled              bool          default false
max_runs_per_fetch          int           default 1
max_concurrent_shadow_runs  int           default 1
budget_usd                  numeric   | null   # required when shadow_enabled
dry_run                     bool          default false
upstream_auth_ref           str(128)  | null   # Phase 5 (§6.2)
```

**Do NOT reuse `remote_url`.** It already means *push destination* — `lazyaf land` pushes to it, and the UI labels it "Remote Origin". Shadow's source and land's destination are usually the same URL but they are different **rights**, read versus write, and that distinction is exactly where §6.2's credential design attaches. Collapsing them now means a fetch-only mirror silently carries push intent.

**New tables:**

```
upstream_ref_state                      upstream_ref_event
  repo_id  fk, part of pk                 id, repo_id fk
  ref      str(255), part of pk           ref, old_sha, new_sha
  last_sha str(40)                        kind: create|advance|rewrite|delete|skipped
  updated_at                              detail (text), observed_at

shadow_run_claim
  id, repo_id fk, pipeline_id fk, branch, commit_sha
  claim_key  str(255) UNIQUE INDEX   -- {repo}:{pipeline}:{branch}:{sha}
  status     claimed|dispatched|skipped_cap|skipped_budget|skipped_dry_run
  pipeline_run_id str(36) | null
  created_at

pipeline_schedules                       -- §3.2
```

**Migration numbering — read this before writing one.** The head on disk is `0013_endpoint_modalities.py`. Wave10's B3 plans to create `0012_pipeline_steps_to_graph.py` and `0013_drop_pipeline_steps.py` — **both of those numbers are already taken on disk** (`0012_workspaces_per_worker.py`, `0013_endpoint_modalities.py`). That collision is wave10's to resolve, but it means shadow CI must **not hardcode a number**: claim the next free revision at implementation time and set `down_revision` to whatever head actually is.

### 8.2 Shared machinery with the M13 benchmark corpus — yes, the primitive

`docs/milestone-13/leaderboards-and-corpus.md:494-501` already specifies `POST /api/repos/ingest-remote {source_url, commit_sha, license, contamination_risk}` as a 13.1 **backend** deliverable, noting there is no clone-from-remote-URL path anywhere in the tree — which grep confirms.

Bench and shadow want **the same primitive and opposite lifecycles**: bench wants one commit, pinned, immutable, never refetched, on `refs/heads/bench/case/<slug>` (`leaderboards-and-corpus.md:145`); shadow wants a moving branch, refetched on a timer, firing triggers.

So: **build one fetch service and one `ingest-remote` endpoint, with `commit_sha` OPTIONAL and the destination refspec a parameter.** Bench passes a sha and `refs/heads/bench/case/<slug>` (a head, because bench cases must be clonable by `clone --branch`); shadow passes no sha and `+refs/heads/*:refs/upstream/*`. The distinguishing state is whether `sync_enabled` is set — not a type discriminator. If these ship separately, the corpus's reproducibility claim ends up resting on whichever fetch path was written less carefully.

### 8.3 The commit → run join, for free

The UI's centrepiece question is "which upstream commits has LazyAF run?" `PipelineRun.trigger_context` is an unindexed JSON `Text` column (`models/pipeline.py:74`), so joining through it means a `LIKE` scan that works on a ten-commit repo and degrades invisibly on a real one.

**Use `trigger_ref` instead.** It is a real `String(255)` column (`models/pipeline.py:73`) and the push path already writes `f"{branch}:{sha[:8]}"` there (`trigger_service.py:521`). For `upstream_commit`, write **the full 40-char sha** and nothing else. The join becomes `WHERE trigger_type = 'upstream_commit' AND trigger_ref IN (:shas)` — an equality query on a real column, no schema change, no JSON scan. Adding `index=True` to `trigger_ref` is a worthwhile Phase 4 follow-up but it touches `models/pipeline.py`, which wave10's B3 holds (§11), so it is explicitly not a Phase 1 dependency. **This removes the one item another lane flagged as a mid-build budget surprise.**

---

## 9. The operator experience

### 9.1 The happy path

```
lazyaf shadow add https://github.com/someone/thing
lazyaf shadow pipeline <repo_id> --file ./review.yaml
lazyaf shadow arm <repo_id> --budget 5.00
```

`add` derives the name from the URL, adopts upstream's real HEAD as `upstream_default_branch` at fetch time, prints objects/size/tip/last-sync, and prints the exact next command.

The existing entry points cannot express this. `lazyaf ingest` requires a local working copy (its `repo_path` is `click.Path(exists=True)` and it hard-exits without a `.git`); shadow CI's whole point is that there is no local clone. **The UI form is worse than useless here:** `RepoSelector.svelte` already renders a "Remote URL (optional)" field — the exact field the owner would reach for — which writes the inert `Repo.remote_url`. He types the GitHub URL, gets a repo, and lands on RepoInfo's *"No branches yet. Push your repo to get started."* The app answers his question with a form field that does nothing and then tells him to do the opposite of what he asked. That field must either drive the mirror or stop pretending it does something.

### 9.2 Endpoints

| Method | Path | Meaning |
|---|---|---|
| `POST` | `/api/repos/ingest-remote` | create Repo + bare repo + first fetch (adopt mode); shared with M13 bench (§8.2) |
| `POST` | `/api/repos/{id}/fetch` | fetch now — **202** + sync-event id |
| `GET` | `/api/repos/{id}/upstream` | sync state, phase, tips per mirrored ref, recent events, next fetch, last error, `cost_coverage` |
| `PATCH` | `/api/repos/{id}/shadow` | the §5 controls; 422 if `shadow_enabled` set true without `budget_usd` |
| ws | `repo_sync` event | alongside the existing `send_repo_created` |

**`POST /api/repos/{id}/sync` is already taken and means something else** — `sync_repo_from_disk`, re-read refs from disk, described in its own docstring as a break-glass operation for corrupted state (`backend/app/routers/repos.py:530`, `git_server.py:530`). Naming the fetch endpoint `/sync` would either shadow that break-glass op or force the two into one endpoint with a mode flag whose halves share nothing. **Use `/fetch`, in the CLI verb too**, so a later refactor cannot reintroduce the collision by "making the names consistent".

### 9.3 UI

`RepoInfo.svelte`, shadow mode:

- **Suppress the Push Updates block** (`git remote add lazyaf …` / `git push lazyaf <default_branch>`) — for a mirror that is an invitation to write a ref the next fetch overwrites. Suppress the "Then push to GitHub" block — for a third-party repo that is a push he has no right to make.
- **Replace both push-flavoured empty states** ("No branches yet. Push your repo to get started." and the not-ingested "run lazyaf ingest" panel) with "Not synced yet" plus a **Fetch now** button.
- **Add an Upstream block**: source URL, last sync as relative time (`formatRelative` is already imported), refs updated, next scheduled fetch, last error, `cost_coverage`, and the kill switch.
- **Add a run-status dot per row on the existing commit graph** — it already renders per-commit rows with branch tags, and run status already broadcasts over the websocket, so the dots go queued → running → green live with no polling. This is the single frame that makes shadow CI legible.
- **Ahead/behind: only "as of last sync", never as a live number.** LazyAF cannot know upstream's tip without fetching, and a number that silently means "as of 40 minutes ago" gets trusted once and then burns him.

`RepoSelector.svelte`: a mirror marker on the repo row, near the ingested dot.

### 9.4 CLI

A `shadow` click group matching the house style of the `tests` and `debug` groups (a `@cli.group()` with a one-line docstring naming the phase, every command taking `--server/-s`): `add`, `fetch`, `list`, `arm`, `pause`, `resume`, `pipeline`. All HTTP through one `_shadow_request` helper cloned from `_debug_request`, which prints the **server's** `detail` rather than a bare status code — shadow fetch has exactly that character ("upstream returned 404", "fetch rejected: non-fast-forward"). Add a mirror marker to the existing `lazyaf list` output.

### 9.5 The ten-minute demo

**Shadow a small public repo the owner controls. Attach an AI *review* step — not a test step — filtered to the mirrored default branch. From a second terminal, push a commit to the real GitHub repo. Within the poll interval the commit appears in LazyAF's commit graph with a status dot that goes queued → running → green, and clicking through shows an agent's review of that diff.**

Design everything backwards from that frame. An AI step rather than a test step for three reasons: it shows the thing LazyAF has that GitHub Actions does not; it needs no knowledge of the mirrored repo's build system, which is otherwise the demo's biggest failure risk (a stranger's repo whose test suite will not install eats the ten minutes); and "the agent read their commit and said something about it" is the visible gesture toward the PR-back ambition without building any of §7. A repo he controls lets him make a commit land on cue.

The demo needs exactly: fetch, the timer, the commit→run join, the status dot, and the shadow panel. It needs **none** of: PR opening, credentials, private repos, a settings page, ahead/behind, tag mirroring.

**Do not demo on LazyAF's own GitHub repo**, tempting as it is because `.lazyaf/pipelines/test-suite.yaml` is real CI. It is the one case where the pipeline he wants **is** the mirrored repo's own YAML — exactly what §4.1 forbids — so the demo would either require the unsafe path or require him to explain why it does not work the obvious way.

---

## 10. Phased build order

**Phase 1 — the mirror (public, no triggers).** `upstream_sync.py` with `ls-remote` + `fetch` into `refs/upstream/*`; the `repos` columns; `upstream_ref_state` and `upstream_ref_event`; `POST /api/repos/ingest-remote` and `POST /api/repos/{id}/fetch`; `periodic.py` and the poll loop in lifespan; adopt-mode first sync; https-only refusal. **Makes possible:** point LazyAF at a public repo and watch commits land on a timer, visible in the event log. Nothing runs, nothing spends. This is the smallest thing that is a real result.

**Phase 2 — commits fire pipelines.** `upstream_commit` in `PUBLIC_TRIGGER_TYPES`; `on_upstream_commit` (matching only, never `sync_repo_pipelines`); the disposable `refs/heads/lazyaf/shadow/<sha>` head; `shadow_run_claim` + the startup sweep; keep-refs; the §5 controls and the dispatch pump; the `LAZYAF_UPSTREAM_*` env block in both executors; the 422 on `on_pass`/`on_fail`. **Makes possible:** the ten-minute demo, end to end.

**Phase 3 — the operator can see it.** `GET /api/repos/{id}/upstream`; the `repo_sync` websocket event; RepoInfo shadow mode and the commit-graph status dots (via `trigger_ref`, §8.3); the `shadow` CLI group; README. **Makes possible:** using this without reading logs — and answering "why didn't my repo build" from the `skipped_*` statuses.

**Phase 4 — close the dark contract and lift the ceiling.** The `schedule` trigger on the Phase 1 substrate (`pipeline_schedules`, CAS claim, coalesce policy, the orphan-sweep fix of §3.3); `index=True` on `trigger_ref`; shallow/partial-clone support in `handle_upload_pack`; optionally move population to fetch-by-sha and retire the disposable head. **Makes possible:** a declared `schedule` trigger actually fires, and shadow CI stops being capped to small repos.

**Phase 5 — private upstreams.** `upstream_auth_ref` + the prefix allowlist + the 0600 credential-file channel (§6.2). **Makes possible:** mirroring a private repo without introducing a secret at rest.

**Phase 6 — GATED, do not schedule.** The upstream PR path (§7), unblocked only by an auth dependency on the mutating routes and a loopback default bind.

---

## 11. Sequencing against 12.8

Two waves are writing this tree now. **Nothing in this design may be implemented in a file another agent holds.** From `upcoming/wave10-v1-retirement.md` §3.2:

| File this design needs | Wave10 owner | Verdict |
|---|---|---|
| `backend/app/schemas/pipeline.py` (`PUBLIC_TRIGGER_TYPES`, :555-563) | **A1 · GRAPH-SCHEMA** | **BLOCKED.** One-line addition; land after A1 finishes P1/P2. |
| `backend/app/services/trigger_service.py` (`on_upstream_commit`, the §3.3 sweep fix) | **B1 · BOUNDARIES** | **BLOCKED.** Land after B1's P2/P3. |
| `backend/app/services/git_server.py` | **A2 · EXECUTOR** | **AVOIDED BY DESIGN.** The fetch lives in a new `upstream_sync.py`; §2.2's namespacing needs no change to any existing ref-walking function. |
| `backend/app/services/pipeline_executor.py` | **A2 · EXECUTOR** | **AVOIDED BY DESIGN.** The disposable head exists precisely so `:2798` is untouched. |
| `backend/app/services/execution/local_executor.py`, `runner_protocol.py` (the `LAZYAF_UPSTREAM_*` env block) | **A3 · DOGFOOD-GATE** | **BLOCKED.** Phase 2. |
| `backend/app/main.py` (lifespan wiring) | **A5 · AD-HOC WRITERS** | **BLOCKED.** Two lines; land after A5's P3. |
| `backend/app/models/pipeline.py` (`trigger_ref` index) | **B3 · MIGRATION** | **BLOCKED — and deliberately deferred to Phase 4** so Phases 1–3 need it not at all (§8.3). |
| `frontend/src/lib/api/types.ts`, `client.ts` | **A4 · FRONTEND-WIRE** | **BLOCKED.** Phase 3. |
| `cli/lazyaf/cli.py` | **B5 · DEBUG KEYS** | **BLOCKED.** Phase 3. |
| `README.md`, `PLAN.md` | **B6 · GUARDS & DOCS** | **BLOCKED.** Phase 3. |
| `backend/app/models/repo.py`, `backend/app/routers/repos.py`, `backend/app/schemas/repo.py`, new `upstream_sync.py`, new `periodic.py` | *nobody* | **FREE.** Phase 1 is almost entirely here. |

**What must land first:** 12.8 P6. Concretely, the two one-line-ish additions (`upstream_commit` in the vocabulary, `on_upstream_commit` in trigger_service) are the only hard blockers for Phase 2, and both sit in files whose owners finish at P3. **Phase 1 can start immediately** — every file it needs is uncontested.

**Migration numbering: do not hardcode.** The on-disk head is `0013_endpoint_modalities.py`, and wave10's B3 plans two revisions whose filenames (`0012_…`, `0013_…`) **already exist**. Claim the next free number at implementation time against whatever head actually is.

**One citation to re-verify at implementation time:** `PUBLIC_TRIGGER_TYPES` has moved twice during analysis (a brief cited :477, an earlier pass :548, it is :555 now). Grep for the symbol; do not trust the line number.

---

## Open questions for the owner

1. **Which repo first?** Its object count decides whether the upload-pack memory ceiling (§2.6) is a Phase 1 blocker or a Phase 4 cleanup. This is the single answer that most changes the plan.
2. **Is it public?** If yes — and "shadow CI on repos at will" suggests it is — §6 is deferred entirely and Phase 5 never blocks the demo.
3. **Does "commits land locally" mean he also wants to `git clone` the mirror from his laptop and see upstream branches?** If yes, `refs/upstream/*` is invisible to a default clone refspec and he will see nothing. That would argue for additionally maintaining read-only `refs/heads/upstream/<branch>` mirrors — at the cost of branch-UI noise, the clobber risk in `handle_receive_pack`, and reopening the `default_branch` adoption hazard of §2.2. Worth knowing before the mirror layout goes into a migration.
4. **Rolling 24h budget window, or calendar-day reset?** Rolling cannot be gamed by a fetch at 23:59; calendar-day is what people picture when they say "a daily cap".
5. **Should a shadow run's failure be visible anywhere other than the run list?** A red run on somebody else's commit is not actionable the way his own is, and mixing them may make the board useless. Possibly a `shadow: true` filter; possibly nothing in v1.
6. **GitHub's `refs/pull/*/head`.** Shadow-CI-ing incoming PRs is plausibly the actual killer feature and it would change the ref-namespace design. Worth deciding the shape now even if it is not built now.
7. **Does a mirrored repo accept agent pushes and cards at all?** The namespace makes it mechanically safe, but the UI has to state the answer, and it determines whether `lazyaf land` should refuse on a mirror pending §7.
8. **Adjacent live exposure, not created by this design but made reachable by it:** the internal git server accepts unauthenticated pushes. With mirroring, a mirror stops being "a repo only I push to". Acceptable on the owner's network, or does receive-pack need the same gate as §7.1's prerequisite (1)?
