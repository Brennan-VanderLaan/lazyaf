# Phase 12.7 — Debug Re-Run Mode: wiring design

Lane C design doc. Two implementers. Written against main at `69f3ef0` +
the wave-6 working tree (migrations on disk through `0007`).

---

## 0. Bottom line

failure_01 shipped a **facade**: schemas, model, UI and CLI existed while the
three load-bearing pieces did not. This design keeps the shelf-ready leaves
and rebuilds the three broken ones against the 12.2-INT/12.6 executor, with
four scope decisions made up front and stated plainly rather than discovered
half-way:

1. **The breakpoint is a PRE-DISPATCH gate inside `_run_executor_step`.** It
   fires identically for LOCAL and REMOTE, because it sits above the point
   where those two paths diverge.
2. **Terminal attach is LOCAL-ONLY in 12.7. Remote attach is DEFERRED**
   (§5) — refused loudly with a reason, never silently degraded.
3. **Sidecar is the ONLY attach mode at a breakpoint.** "Shell into the
   running step container" is a contradiction at a *pre-step* gate: the step
   container does not exist yet. `--shell` is refused with that sentence
   (§6). Live-step shell is a separate, later feature.
4. **The run's status vocabulary does not change.** PLAN's proposed
   `debug_pending`/`debug_running`/`debug_waiting`/`debug_connected`
   `RunStatus` values are NOT added; debug state lives on the
   `DebugSession` row only (R3: one source of truth). `RunStatus` has five
   members pinned by dozens of tests and every UI colour map.

Deviations from the PLAN text are listed in §16 so the owner can veto any of
them before code is written.

---

## 1. Salvage manifest (what to lift from `failure_01`, and its required edits)

Read-only access: `git -C C:/projects/lazyaf show failure_01:<path>`.

| From failure_01 | Lands as | Required edits |
|---|---|---|
| `backend/app/services/execution/debug_state.py` | same path | 2 transitions added (§9). Nothing else. |
| `tdd/unit/execution/test_debug_session_state_machine.py` (33 tests) | same path | **Strip the `try/except ImportError` block AND the `pytestmark = pytest.mark.skipif(...)` at lines 30–53.** That is a module-wide conditional skip standing in front of every assertion — a direct R4 violation. Import the module unguarded. Then add 3 tests (§9). |
| `backend/app/schemas/debug.py` | same path | Drop `token` from `DebugSessionInfo` (§8 kills the GET oracle). `breakpoints: list[int]` → `list[str]` (§3). Add `attach_available`, `attach_unavailable_reason`, `breakpoints_hit`, `breakpoints_pending`, `end_reason`. |
| `backend/app/models/debug_session.py` | same path | Delete the duplicate `DebugSessionStatus` enum — import `DebugState` from `debug_state.py` (the audit's dedupe note). Drop the `token` column. Wire `state_history` (§3). Full column list in §3. |
| `images/debug-sidecar/Dockerfile` | same path | Re-base onto `lazyaf-base` instead of `ubuntu:22.04` (§14 — uid-1000 ownership). |
| `frontend/src/lib/components/DebugRerunModal.svelte` | same path | REFERENCE-grade: keep the layout, the breakpoint checkbox list, select-all/none, the commit radio pair. Retarget its API calls and switch breakpoint identity from index to step key. |
| `backend/app/services/execution/debug_session_service.py` | same path | Skeleton/API surface only. Every method body is rewritten (§4, §10). |
| `cli/lazyaf/cli.py` `debug` command | `cli/lazyaf/debug_cmd.py` (new module) | Duplex loop shape is reusable; protocol, auth, raw-TTY and escape handling are new (§13). |

**DISCARD entirely**: `routers/debug.py` (its WS command loop is dead code
inside `except ImportError`), `services/execution/debug_terminal.py` (TODO
stub, zero callers), the executor breakpoint hunks (guaranteed
`broadcast()` TypeError, and they hook the loop main replaced),
`tdd/frontend/e2e/stories/06-debug-rerun/` (7 `test.skip` shells — keep the
titles as a UX checklist only).

---

## 2. Where the breakpoint gate sits — the core decision

### 2.1 Why not the obvious places

`_dispatch_step_run` (line ~1455) is the shared LOCAL/REMOTE dispatch line and
looks like the natural gate. **It is not.** It is called from
`_execute_graph_step` and `_execute_step`, both of which run **under
`self._run_lock(pipeline_run.id)`** — from `start_pipeline` and from
`_finish_local_step_locked` → `_handle_graph_step_complete`. Awaiting a human
there holds the run lock for up to four hours: every sibling step of a
parallel graph would wedge trying to finish. A gate there deadlocks the run
it is trying to debug.

### 2.2 The placement

**The gate is the first statement of `_run_executor_step`** (line ~1736) —
the per-step `asyncio` task, which runs *outside* the run lock, has its own
session scope, and receives `mode` so it knows LOCAL from REMOTE.

```python
async def _run_executor_step(self, mode, session_factory, run_id, step_run_id,
                             params=None, *, requirements=None):
    # 12.7: breakpoint gate. FIRST statement — above the workspace, above
    # _prepare_control_mode, above the hard-deadline clock. See §2.3.
    gate = await self._debug_gate(session_factory, run_id, step_run_id, mode)
    if gate.outcome is DebugGateOutcome.ABORTED:
        return                      # cancel_run already owns every row
    is_remote = mode is ExecutorMode.REMOTE
    db = session_factory()
    ...
        loaded = await self._load_local_step_context(db, run_id, step_run_id)
        ...
        pipeline_run, pipeline, repo, step_run, graph, steps, step, is_graph = loaded
        if gate.outcome is DebugGateOutcome.FAILED:
            await self._finish_local_step(
                db, pipeline_run, pipeline, repo, step_run,
                graph, steps, step, is_graph,
                False, None, gate.error, None,
            )
            return
        ...  # unchanged from here down
```

`_debug_gate` is ~6 lines in `pipeline_executor.py` that delegate to
`debug_session_service.gate(session_factory, run_id, step_run_id, mode)`.
Blast radius in a 3966-line file: **one call site plus one early-return
branch.** Everything else lives in the service.

### 2.3 Four properties this placement buys for free

1. **It works for LOCAL and REMOTE identically** (§5), because it is above
   the `is_remote` fork.
2. **A paused step cannot be reaped as dead.** The gate is above
   `_prepare_control_mode`, so at a breakpoint there is **no `StepExecution`
   row**, therefore no `timeout_at`, no `last_heartbeat`, and nothing for
   `recover_orphaned_executions` to find. It is also above the
   `asyncio.wait_for(..., timeout=hard_deadline)` that wraps the event
   consumer, so the hard-deadline clock has not started. PLAN §"Debug mode
   integration" asks for "the normal heartbeat timeout is suspended at a
   breakpoint" — this achieves it **by construction, with zero special
   cases**. Do not add a suspension flag; there is nothing to suspend.
3. **It never holds the step task's DB session across the pause.** The gate
   opens and closes its own short sessions (`ws_runners.py` property 2: a
   multi-hour object must never pin a pooled connection or an aging
   transaction snapshot). `_run_executor_step` opens its session *after* the
   gate returns.
4. **Failure and abort reuse the ONE completion path.** A timed-out gate
   returns `FAILED(error)` and the step finishes through the ordinary
   `_finish_local_step`; an abort returns `ABORTED` and `cancel_run` has
   already done the work. No new terminal path exists for debug runs.

### 2.4 What the gate does, step by step

```
gate(session_factory, run_id, step_run_id, mode):
  s1  open session; SELECT DebugSession WHERE pipeline_run_id=run_id
        AND status NOT IN ('ended','timeout')            # one indexed query
      none -> close, return RESUME                       # ordinary run, ~0 cost
  s2  load StepRun; key = debug_step_key(step_run)
      key not in breakpoints, or key already in hit_breakpoints
        -> close, return RESUME
  s3  LOCAL only: workspace_service.get_or_create(...) then .acquire(...)
        -> the WORKSPACE PIN (§7). The volume is populated at the requested
           commit BEFORE the human is told to look at it.
  s4  transition PENDING -> WAITING_AT_BP; stamp current_step_{key,name,index},
      current_step_executor=mode.value, breakpoint_hit_at, expires_at;
      append key to hit_breakpoints; persist state_history.
  s5  write ONE notice line into StepRun.logs via the debug source (§C11) and
      broadcast `debug_session_status` (§11). Close the session.
  s6  wait loop (NO session held):
        while True:
          remaining = expires_at - utcnow()
          if remaining <= 0: outcome = FAILED("debug session timed out ...")
          await wait_for(event.wait(), timeout=min(remaining, 5.0))  # §C7
          re-open a short session, re-read the row (the row is the truth,
          the event is only a wakeup), close it
          status ended-by-resume  -> outcome = RESUME
          status ended-by-abort   -> outcome = ABORTED
          status timeout          -> outcome = FAILED(...)
          otherwise (still waiting / connected / extended) -> loop
  s7  end-of-pause: release the workspace pin (own short session),
      tear the sidecar down (§7 ordering), broadcast, return outcome
```

---

## 3. Model + migration 0009

`backend/app/models/debug_session.py`, table `debug_sessions`.

| Column | Type | Notes |
|---|---|---|
| `id` | String(36) PK | uuid4 |
| `pipeline_run_id` | String(36) FK `pipeline_runs.id` NOT NULL **UNIQUE** | one debug session per run; the gate's lookup key |
| `original_run_id` | String(36) NULL, indexed | the failed run this re-runs |
| `status` | String(32) NOT NULL default `'pending'` | vocabulary = `DebugState` (§9), **not** a second enum |
| `breakpoints` | Text NOT NULL default `'[]'` | JSON list of **step keys** (strings) |
| `hit_breakpoints` | Text NOT NULL default `'[]'` | JSON list; a key is appended when its gate fires. Makes multi-breakpoint bookkeeping durable and stops a re-dispatch from re-pausing at the same step. |
| `current_step_key` / `current_step_name` | String(255) NULL | |
| `current_step_index` | Integer NULL | |
| `current_step_executor` | String(16) NULL | `'local'` \| `'remote'` — the ONLY input to `attach_available` |
| `sidecar_container_id` | String(64) NULL | set when a terminal first attaches |
| `connection_mode` | String(16) NULL | `'sidecar'` (only value in 12.7) |
| `timeout_seconds` | Integer NOT NULL default 3600 | |
| `max_timeout_seconds` | Integer NOT NULL default 14400 | |
| `expires_at` | DateTime NULL | authoritative pause deadline; `/extend` moves it |
| `created_at` | DateTime NOT NULL | |
| `breakpoint_hit_at` / `connected_at` / `ended_at` | DateTime NULL | |
| `end_reason` | String(255) NULL | R1: a session never ends without saying why — `resumed`, `resumed (run to completion)`, `aborted by user`, `timed out at breakpoint`, `pipeline completed`, `backend restarted while paused` |
| `state_history` | Text NULL default `'[]'` | **wired** — `DebugStateMachine.to_dict()`; failure_01 declared it and never wrote it |

Indexes: unique on `pipeline_run_id`; plain on `status` and `original_run_id`.
Relationship: `pipeline_run` with `foreign_keys=[pipeline_run_id]`. Every
executor/router read of a session goes through `selectinload` if it touches
`pipeline_run` (R5).

**No `token` column.** See §8.

**Step keys, not indices.** PLAN's API sketch uses step *ids*
(`["step-id-1"]`) while failure_01's schema uses *indices* (`list[int]`).
Neither works for both pipeline formats. One resolver, one source of truth:

```python
# backend/app/services/execution/debug_state.py  (or debug_keys.py)
def debug_step_key(step_run) -> str:
    """The breakpoint identity of a step. Graph (v2) steps are addressed by
    their stable step_id; legacy (v1) steps have no id and are addressed by
    index. ONE function: the gate, the create-endpoint validator and the UI
    checkbox list all derive keys from it."""
    return step_run.step_id or str(step_run.step_index)
```

The create endpoint validates every requested key against the pipeline
definition and **400s on an unknown key** — an unknown key would otherwise
be a breakpoint that silently never fires (R1).

### Migration `0009_debug_sessions.py`

- `revision = '0009'`.
- `down_revision`: **`'0008'` if 12.6.6 lands one, otherwise the actual head
  at integration time.** As of this writing `0007_drop_polling_runner_columns`
  is on disk (untracked) and `0008` does not exist. The integrator confirms
  the parent; the implementer must not guess. This is the one line in 12.7
  that another lane can invalidate.
- Same re-runnable style as `0004`–`0007`: inspector-guarded `create_table` /
  `create_index`, no bare `except`.
- `tdd/integration/test_migrations.py` gets the table + its unique index.

---

## 4. Session service — the three failure_01 bugs, named and fixed

`backend/app/services/execution/debug_session_service.py`, singleton via
`get_debug_session_service()`, with a `reset()` registered in test-mode.

| failure_01 bug | Fix |
|---|---|
| `create_debug_rerun` **never started the run** | `create()` builds the `DebugSession` row **first** (so the gate cannot race a step past a breakpoint) and *then* calls `pipeline_executor.start_pipeline(...)`, in that order, in one transaction boundary. A T1 test asserts the row exists before `start_pipeline` is entered. |
| `resume` **ended the session**, killing multi-breakpoint | `resume()` transitions to `PENDING`, never `ENDED` (§9). Only `abort`, `timeout`, and run completion reach a terminal state. |
| timeout monitor **never started** | There is no monitor task. **The paused gate is the timeout owner** (§2.4 s6) — it already has a deadline and re-arms on every wake. One owner, no task to leak, nothing for `reset()` to strand. |
| in-memory / DB dual truth | **The row is the truth. The `asyncio.Event` is a wakeup only.** Every wake re-reads the row. The bounded 5s poll (§C7) means a lost signal degrades to ≤5s of latency, never a hang. |

Public surface: `create`, `gate`, `resume(clear_remaining: bool)`, `abort`,
`extend(minutes)`, `get`, `end_for_run(run_id, reason)`, `mark_connected`,
`mark_disconnected`, `reset`, `sweep_orphan_sidecars`.

---

## 5. LOCAL vs REMOTE — what works and what is deferred

| Capability | LOCAL | REMOTE |
|---|---|---|
| Breakpoint pause / resume / abort / extend | yes | **yes** |
| UI context (commit, runtime, step, logs, countdown) | yes | yes |
| Workspace pinned + populated at the pause | yes | n/a (agent owns its own volume) |
| **Terminal attach (sidecar)** | yes | **DEFERRED** |

**Why remote attach is deferred, precisely.** A remote step's workspace is a
volume on the runner host, provisioned by the agent from `config.workspace`;
the backend's Docker client cannot see it (`_run_executor_step` deliberately
does not create, populate, acquire or release a local workspace for a remote
step). Attaching would require a **new runner-protocol frame pair** carrying
terminal I/O plus its own auth on the agent — and the 12.6 protocol froze in
January. That is a protocol-version change with an agent-side attack surface,
i.e. 12.6.6/M13 work, not 12.7.

**How the deferral is surfaced (R1, no silent fallback):**
- `DebugSession.current_step_executor` is stamped at the gate.
- `GET /api/debug/{id}` returns `attach_available: false` and
  `attach_unavailable_reason: "terminal attach is not available for steps
  running on a remote runner (12.7 ships local attach only)"`.
- The terminal WS refuses the upgrade with close code **4403** and that same
  sentence, before `accept()`.
- The UI hides the join command and shows the reason inline.
- The CLI prints the reason and exits non-zero.

A breakpoint on a remote step is therefore still *useful* (pause, read logs,
resume/abort) and never *silently* less than a local one.

---

## 6. Sidecar vs shell

**Sidecar only, in 12.7.**

A breakpoint is a **pre-step** gate: it fires before `_prepare_control_mode`,
before `executor.execute_step`, before any container is created. There is no
step container to exec into. `StepExecution.container_id` reinforces this —
the column exists on main but **nothing ever writes it**; the only live
container handle is `LocalExecutor._running_containers[execution_key]`, in
memory, and at a breakpoint it holds no entry for this step.

So:
- **`--sidecar`** (default, and the only mode that resolves at a breakpoint):
  spawn `lazyaf-debug-sidecar:dev`, mount the run's workspace volume at
  `/workspace`, exec a login shell. The user sees exactly the bytes the step
  is about to see.
- **`--shell`** at a breakpoint is refused with: *"no step container exists
  at a pre-step breakpoint — the step has not started. Use --sidecar to
  inspect the workspace it is about to run against."* Not a fallback, not a
  downgrade: an error with the reason.
- Live-step shell (exec into a *currently running* step container) is a
  genuinely different feature. It needs `LocalExecutor` to expose a public
  `container_for(execution_key)` and the local path to persist
  `StepExecution.container_id`. **Out of 12.7's exit gate**; listed in §17 as
  a follow-on.

The mount is **read-write**, matching failure_01, and this is deliberate: the
point of a debug re-run is to poke at the workspace and resume. It must be
documented in the sidecar MOTD and in the CLI banner: *"edits under
/workspace are seen by the resumed step."* The `--read-only` variant is a
one-line follow-on, not 12.7.

---

## 7. Workspace lifecycle, and the teardown ordering

**The workspace pin.** At a LOCAL breakpoint the gate calls
`workspace_service.get_or_create(...)` then `.acquire(...)`. `acquire` holds
its exclusive lock only long enough to bump `use_count` and commit, so the
pause holds **a durable refcount, not a lock**. That refcount is what keeps
the volume alive: the workspace state machine filters `CLEANING` out of the
valid targets while `use_count > 0`. The pin is released exactly once, when
the pause ends. Acquire and release each use their own short session.

For the FIRST step of a run this is also what makes the sidecar useful at
all: without it, a breakpoint on step 0 would attach to a volume that does
not exist yet.

**Teardown order at session end — this order is load-bearing:**

```
1. transition to terminal state (ENDED/TIMEOUT) + end_reason, persist
2. close the attached terminal socket (1000 normal / 4403 with a reason)
3. stop + remove the sidecar container
4. release the workspace pin
5. (abort only) pipeline_executor.cancel_run(...)
6. set the gate event
7. broadcast debug_session_status
```

3 before 4 before 5: `cancel_run` and `_complete_pipeline` both call
`_cleanup_workspace`, and Docker refuses to remove a volume that a running
container still mounts. Getting this backwards produces a logged cleanup
failure and a volume that only `audit_orphans` eventually reaps.

**Run completion must not strand a session.** `_complete_pipeline` gains one
line before `await self._cleanup_workspace(...)`:

```python
await debug_session_service.end_for_run(db, run_id, reason="pipeline completed")
```

and `cancel_run` gains the same with `reason="run cancelled"`. A session that
ends this way reports any breakpoints that never fired:
`end_reason = "pipeline completed; 2 breakpoint(s) never reached"` — an
unreachable breakpoint (its step's upstream failed) is a visible fact, not
silence.

**Backend restart while paused.** A paused gate is an in-process task; a
restart kills it and the run is unrecoverable. Honest handling, at startup
next to `recover_orphaned_executions`: sweep non-terminal `DebugSession`
rows, end each with `end_reason="backend restarted while paused"`, fail its
pipeline run with that message, and `sweep_orphan_sidecars()` removes
containers labelled `lazyaf.type=debug-sidecar` whose session is terminal.
No dark half-alive runs.

---

## 8. Auth, and what the token is honestly worth

**There is no token column and no stored secret.** The join credential is a
JWT minted the same way `generate_step_token` mints step tokens
(`app/services/control_layer/auth.py`), with
`{"debug_session_id": ..., "exp": min(now + 15min, session.expires_at)}`.

- `POST /api/debug/{id}/join-token` mints one on demand. The UI calls it when
  the user clicks "copy join command"; the CLI calls it when `--token` is
  absent.
- **`GET /api/debug/{id}` never returns a token.** That kills the audit's
  "token-in-GET-response oracle": a UI that polls session state would
  otherwise spray a long-lived secret through logs, caches and browser
  history.
- Revocation is free: the WS upgrade re-reads the row and refuses any session
  in a terminal state, whatever the JWT says.
- The token is short-lived and *re-mintable*, which is why it is **not**
  "one-time" as PLAN's security section says. Single-use tokens are
  incompatible with 12.7c reconnection, and a single-use secret that must
  survive a copy-paste into a terminal is worse than a 15-minute one.

**Say this out loud in the docstring:** the backend has **no** authentication
system today — every other endpoint is open. This token bounds the *terminal*
to a capability minted through the API; it does not bound access to the API.
12.7 does not ship auth and must not be described as if it did (R1).

Upgrade-time auth, mirroring `ws_runners.py` property 1: `Authorization:
Bearer` header first, then `?token=`; failure closes **before `accept()`**, so
the refusal is visible in the handshake rather than one frame later.

---

## 9. State machine + multi-breakpoint resume semantics

Port `debug_state.py` verbatim except `VALID_TRANSITIONS`:

```python
VALID_TRANSITIONS = {
    DebugState.PENDING:       {DebugState.WAITING_AT_BP, DebugState.ENDED},
    DebugState.WAITING_AT_BP: {DebugState.CONNECTED, DebugState.TIMEOUT,
                               DebugState.ENDED,
                               DebugState.PENDING},      # + resume w/o connecting
    DebugState.CONNECTED:     {DebugState.ENDED, DebugState.TIMEOUT,
                               DebugState.WAITING_AT_BP, # disconnect
                               DebugState.PENDING},      # + resume  <-- THE FIX
    DebugState.TIMEOUT:       set(),
    DebugState.ENDED:         set(),
}
```

`PENDING` means **"executing, not at a breakpoint"** — the state a resumed
session returns to. failure_01's `resume` went to `ENDED`, which is why
multi-breakpoint never worked: the second breakpoint had no live session to
pause into.

**These two edges break none of the 33 ported tests.** The only invalidity
assertions in that file are `test_pending_to_connected_invalid`,
`test_pending_to_timeout_invalid`, and the two terminal-state tests; nothing
asserts that `WAITING_AT_BP → PENDING` or `CONNECTED → PENDING` is refused.
Verified by reading the file, not by assumption.

Three tests added (36 total):

- `test_waiting_at_bp_to_pending_valid` — resume without ever connecting
- `test_connected_to_pending_valid` — resume after connecting
- `test_multi_breakpoint_cycle` — `PENDING → WAITING → CONNECTED → PENDING →
  WAITING → CONNECTED → ENDED` in one machine, asserting `history` length 6

**Resume semantics:** `POST /api/debug/{id}/resume` takes
`{"clear_remaining": false}`. Default: continue to the **next** breakpoint.
`clear_remaining: true` empties `breakpoints` first, i.e. "run to
completion". Both go to `PENDING`.

---

## 10. HTTP API

```
POST /api/pipeline-runs/{run_id}/debug-rerun      -> DebugRerunResponse
  { breakpoints: [step_key, ...],                    # validated, 400 on unknown
    use_original_commit: bool = true,
    commit_sha: str|null, branch: str|null,
    timeout_seconds: int|null }                      # clamped to max
  -> { run_id, debug_session_id, join_command }      # NO token (§8)

GET  /api/debug/{session_id}                      -> DebugSessionInfo
  status, current_step{key,name,index,type}, commit{sha,message},
  runtime{host,orchestrator,image,image_sha}, logs, join_command,
  expires_at, breakpoints, breakpoints_hit, breakpoints_pending,
  attach_available, attach_unavailable_reason, end_reason

POST /api/debug/{session_id}/join-token           -> { token, expires_at, join_command }
POST /api/debug/{session_id}/resume               -> { status, next_breakpoint|null }
POST /api/debug/{session_id}/abort                -> { status: "ended", end_reason }
POST /api/debug/{session_id}/extend               -> { expires_at }
GET  /api/debug                                   -> [DebugSessionInfo]   # non-terminal
WS   /api/debug/{session_id}/terminal?mode=sidecar&token=...
```

**The re-run itself** is an ordinary `start_pipeline` call, which is why
commit selection needs no new machinery: `workspace_service.get_or_create`
already takes `(branch, commit_sha)` off `trigger_context`.

**`trigger_context` is REBUILT, not copied.** Only `branch` and `commit_sha`
carry over from the original run. `on_pass`, `on_fail`, `card_id` and
everything else are **dropped**. Consequence, and it is the desired one: a
debug re-run can never merge a branch, and can never walk a card to
`in_review`. A T1 test pins this by debug-re-running a `card_work` run with
`on_pass: merge` and asserting neither fires.

`trigger_type = "debug_rerun"` (new, §18). `agent_run.on_run_complete`
already no-ops on any type outside `ADHOC_TRIGGER_TYPES`, so it is inert —
verified, not assumed.

---

## 11. Terminal transport

WS `/api/debug/{session_id}/terminal`, JSON text frames, **base64 payloads**
for byte data (a raw terminal emits invalid UTF-8; failure_01 sent raw text
and would have corrupted on any binary output).

```
client -> server   {"v":1,"type":"stdin","data":"<b64>"}
                   {"v":1,"type":"resize","cols":120,"rows":40}
                   {"v":1,"type":"command","command":"@resume"|"@abort"|"@status"|"@help"}
                   {"v":1,"type":"ping"}
server -> client   {"v":1,"type":"ready","mode":"sidecar","container_id":"..."}
                   {"v":1,"type":"stdout","data":"<b64>"}
                   {"v":1,"type":"notice","text":"..."}
                   {"v":1,"type":"closed","reason":"..."}
                   {"v":1,"type":"pong"}
```

**Special commands are their own frame type, never sniffed out of stdin.**
Scanning the byte stream for a leading `@` corrupts any program that
legitimately reads `@…`. The CLI reserves **Ctrl-]** as the escape: it drops
raw mode, shows a `debug>` prompt, and sends one `command` frame. All four
verbs are *also* plain HTTP subcommands, so nothing about controlling a
session depends on having a TTY.

**Docker I/O without blocking the loop (R5).** `container.exec_run(...,
stdin=True, tty=True, socket=True, user="1000:1000")` returns a blocking
socket. Reuse the exact pattern `LocalExecutor` already uses for log
streaming (`_pump`, local_executor.py ~line 901): a daemon thread reads the
socket and hands chunks to the loop via `loop.call_soon_threadsafe` into an
`asyncio.Queue`. Writes go through `run_in_threadpool`. **No blocking read on
the event loop, ever.**

**Bounds** (all settings-driven, all with a stated failure mode):

| Bound | Value | On breach |
|---|---|---|
| inbound frame size | 64 KiB | close `4009`, reason names the limit |
| inbound rate | window/count mirroring `ws_runners` | close `4009` |
| outbound queue | 256 chunks | close `4009 output backpressure` — **never silently drop bytes** |
| concurrent terminals per session | 1 | refuse `4004 duplicate terminal` |
| idle | session `expires_at` | close `4403`, session times out |

Close codes: `4401` bad/missing token · `4403` not attachable (terminal
session, or remote step per §5) · `4404` unknown session · `4004` duplicate
terminal · `4009` bound exceeded · `1000` normal.

**Sidecar lifecycle:** created lazily on **first attach** (no container until
someone actually looks), removed at **session end** only — so a dropped CLI
can reconnect into the same shell host. Bounded by `max_timeout_seconds`
(4h), labelled `lazyaf.type=debug-sidecar` + `lazyaf.debug-session=<id>` +
`lazyaf.pipeline-run=<run_id>`, and swept at startup. Network:
`settings.container_network` — **not** failure_01's `network_mode="host"`,
which is wrong under the compose stack and needlessly wide. Resource limits:
`mem_limit` + `nano_cpus` from settings.

**New broadcast**, on `ConnectionManager` via the typed publish API (12.2-INT
fix 7 — explicit signature so an arity mistake is a loud call-site error, not
a misshapen frame; failure_01's breakpoint hook died on exactly this):

```python
async def publish_debug_session(self, session: dict) -> None:
    await self.broadcast("debug_session_status", session)
```

---

## 12. Where a debug pause is visible

A `StepRun` sits `RUNNING` while its step is paused, because
`_dispatch_step_run` already committed and broadcast it before the task ran.
Three surfaces make that honest:

1. the `debug_session_status` frame, carrying `current_step_key`, which the
   UI overlays as a "paused at breakpoint" badge on that step;
2. a notice line appended to `StepRun.logs`, so even the plain log view says
   `[debug] paused before step "build" — join: lazyaf debug attach <id>`;
3. `end_reason` on the session, always set.

---

## 13. CLI surface

`lazyaf debug` is a **click group** (`cli/lazyaf/debug_cmd.py`, registered in
`cli.py` with one `cli.add_command(debug)` line):

```
lazyaf debug rerun <run-id> --break <key> [--break <key> ...]
                            [--commit <sha> | --branch <name>] [--timeout 3600]
lazyaf debug list
lazyaf debug status  <session-id>
lazyaf debug attach  <session-id> [--sidecar|--shell] [--token <t>]
lazyaf debug resume  <session-id> [--all]      # --all = clear_remaining
lazyaf debug abort   <session-id>
lazyaf debug extend  <session-id> --minutes 30
```

Deviation from PLAN's `lazyaf debug <id> --resume` flag syntax, stated for the
owner: a flag that changes the verb is not a flag. A group gives real
per-verb `--help`, and the CLI already has the precedent (`lazyaf tests
reconcile`). If the owner prefers the flag forms, they are trivial aliases on
`attach`.

- `attach` puts the terminal in **raw mode** (`termios`/`tty` on POSIX,
  `msvcrt` on Windows), reserving Ctrl-] for the `debug>` escape prompt.
- Without `--token` it mints one via `POST /api/debug/{id}/join-token`.
- `websockets>=12.0` is added to `cli/pyproject.toml` (the CLI ships its own
  dependency set; `httpx` cannot speak WS).
- **The frame codec lives in a module that imports nothing from
  `websockets` at import time** (`cli/lazyaf/debug_protocol.py`), so the
  shared contract test (§C13) can import it inside the backend test env, the
  way `tdd/unit/scripts/test_cli_tests_reconcile.py` already imports `cli.py`
  behind a `rich` stub.

---

## 14. Sidecar image + build story

`images/debug-sidecar/Dockerfile`, **`FROM lazyaf-base`** — not
`ubuntu:22.04`.

Why: the workspace volume is owned by uid/gid **1000** (`lazyaf`), pinned by
the base image precisely so `put_archive` and chown are deterministic. A root
Ubuntu sidecar can read everything but every file it *creates* is root-owned,
and the resumed step — which runs as uid 1000 — then trips over them. Deriving
from base inherits the user, the `/workspace` layout, the XDG/HOME env block
and the entrypoint's chown+gosu drop. It also inherits the build story
(content-hash label, `:dev` tag) for free.

```dockerfile
ARG BASE_IMAGE=lazyaf-base:dev
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      vim nano less htop tree jq file bash-completion tmux procps \
    && rm -rf /var/lib/apt/lists/*
# base already provides git, curl, python3
COPY motd /etc/lazyaf-debug-motd
RUN cat /etc/lazyaf-debug-motd >> /home/lazyaf/.bashrc
LABEL lazyaf.control-layer=0        # explicit: this image runs no steps
LABEL lazyaf.debug-sidecar=1
ARG CONTENT_HASH=dev
LABEL lazyaf.content-hash=$CONTENT_HASH
# base ENTRYPOINT (chown + gosu) is kept; LAZYAF_CONTROL is unset => CMD passthrough
CMD ["sleep", "infinity"]
```

`scripts/build_images.py` `IMAGES` gains, after `base`:

```python
("debug-sidecar", "lazyaf-debug-sidecar", "base", []),
```

The MOTD must state that `/workspace` is read-write and that edits are seen by
the resumed step.

**Check before you build:** `tdd/unit/control_runtime/test_image_contract.py`
may enumerate images and assert `lazyaf.control-layer=1`. The sidecar
deliberately declares `0`; if that test iterates all images it needs an
explicit exclusion, and that exclusion is a *stated* fact, not a skip.

---

## 15. File ownership

Absolute. Anything not listed goes in the implementer's report as a requested
edit.

### Implementer A — backend, executor, migration

**Creates**
- `backend/app/models/debug_session.py`
- `backend/app/schemas/debug.py`
- `backend/app/routers/debug.py`
- `backend/app/services/execution/debug_state.py`
- `backend/app/services/execution/debug_session_service.py`
- `backend/app/services/execution/debug_terminal.py`
- `backend/app/services/execution/debug_terminal_protocol.py`
- `backend/alembic/versions/0009_debug_sessions.py`
- `tdd/unit/execution/test_debug_session_state_machine.py`
- `tdd/unit/execution/test_debug_gate.py`
- `tdd/unit/execution/test_debug_step_key.py`
- `tdd/unit/execution/test_debug_session_service.py`
- `tdd/integration/api/test_debug_api_contract.py`
- `tdd/integration/services/execution/test_breakpoint_execution.py`  *(T2, real Docker — **that directory, deliberately**: `scripts/run_tier.py` selects T2 as `tdd/integration/services` and T1 as everything else under `tdd/integration`, so a Docker-real suite placed at `tdd/integration/execution/` would run in the NO-DOCKER tier and fail there. The 12.6 design made exactly this mistake; see the T2 note in `tdd/tier_floors.json`.)*
- `tdd/e2e/test_debug_rerun.py`  *(T3)*

**Modifies (narrow, named hunks only)**
- `backend/app/services/pipeline_executor.py` — the gate call + early return
  in `_run_executor_step` (§2.2); `_debug_gate` helper; one
  `end_for_run` line in `_complete_pipeline`, one in `cancel_run`; debug
  teardown in `reset()`.
- `backend/app/services/websocket.py` — `publish_debug_session` only.
- `backend/app/services/execution/step_logs.py` — add `SOURCE_DEBUG` and the
  `step_run_id`-addressed entry point (§C11).
- `backend/app/schemas/pipeline.py` — the `debug_rerun` trigger type (§18).
- `backend/app/routers/pipelines.py` — extend the ad-hoc guard (§18).
- `backend/app/routers/test_api.py` — two `register_resettable` lines (§18).
- `backend/app/main.py` startup — the paused-session restart sweep (§7). **If
  main.py is integrator-owned in this wave, hand this over as a requested
  edit instead.**
- `tdd/integration/test_migrations.py` — the new table.
- `cli/pyproject.toml` is **B's**; A must not touch it.

### Implementer B — clients: UI, CLI, sidecar image

**Creates**
- `images/debug-sidecar/Dockerfile`, `images/debug-sidecar/motd`
- `cli/lazyaf/debug_cmd.py`, `cli/lazyaf/debug_protocol.py`
- `frontend/src/lib/components/DebugRerunModal.svelte`
- `frontend/src/lib/components/DebugPanel.svelte`
- `frontend/src/lib/stores/debug.ts`
- `frontend/e2e/debug-rerun.spec.ts`  *(R8)*
- `tdd/unit/scripts/test_cli_debug.py`
- `tdd/unit/debug/__init__.py` + `tdd/unit/debug/test_terminal_protocol_contract.py`
  *(pins both sides, §C13. Every `tdd/unit/*` subpackage carries an
  `__init__.py`; a new one without it collects inconsistently.)*

**Modifies**
- `scripts/build_images.py` — one `IMAGES` tuple
- `cli/lazyaf/cli.py` — one `cli.add_command(debug)` line
- `cli/pyproject.toml` — `websockets>=12.0`
- `frontend/src/lib/api/client.ts`, `types.ts` — the `debug` API group
- `frontend/src/lib/stores/websocket.ts` — one `case 'debug_session_status'`
- `frontend/src/lib/components/PipelineRunViewer.svelte` — the "Debug Re-run"
  button on failed runs + the `DebugPanel` mount

**Off-limits to both:** `backend/app/main.py` router registration,
`backend/app/models/__init__.py`, `frontend/src/App.svelte`, and everything
under `tdd/qa/`, `frontend/e2e/qa/`, `upcoming/qa-*.md`.

**A blocks B** on `debug_terminal_protocol.py` and the API shapes. B can start
on the sidecar image and the modal immediately (neither depends on A).

---

## 16. Numbered contracts

Each is a statement an implementer can be held to and a test can pin.

- **C1** The breakpoint gate is the FIRST statement of
  `_run_executor_step` — above the workspace block, above
  `_prepare_control_mode`, above the `hard_deadline` clock. It is never
  called from `_dispatch_step_run`, `_execute_graph_step` or `_execute_step`,
  because all three run under the run lock. *Test: a graph run with two
  parallel entry steps, one breakpointed, asserts the sibling completes while
  the first is paused.*
- **C2** Breakpoint identity is `debug_step_key(step_run) = step_run.step_id
  or str(step_run.step_index)`, defined once and imported by the gate, the
  create-endpoint validator and the UI. An unknown key is a 400 at create
  time, never a breakpoint that silently never fires.
- **C3** A paused step has **no `StepExecution` row**. Heartbeat suspension at
  a breakpoint is achieved by placement, not by a flag. *Test: pause a step,
  advance the clock past `default_timeout_for(...) +
  LOCAL_STEP_HARD_TIMEOUT_GRACE`, assert `recover_orphaned_executions`
  returns nothing and the step is still RUNNING.*
- **C4** The gate holds no DB session across the pause. Every read/write
  inside the wait loop opens and closes its own session.
- **C5** `resume` transitions to `PENDING`, never `ENDED`. Only `abort`,
  `timeout` and run completion reach a terminal state. *Test:
  `test_multiple_breakpoints_work` pauses at two breakpoints in one run.*
- **C6** The `DebugSession` row is the truth; the `asyncio.Event` is a wakeup
  only. Every wake re-reads the row before acting.
- **C7** The paused gate is the sole timeout owner. No background timeout
  task exists. The wait is `min(remaining, 5.0)` per iteration, so a lost
  signal costs ≤5s of latency and never a hang.
- **C8** At a LOCAL breakpoint the gate holds a workspace pin
  (`get_or_create` + `acquire`), released exactly once when the pause ends.
  *Test: assert `use_count == 1` while paused and `0` after resume.*
- **C9** Session teardown order is: terminal state → close terminal → remove
  sidecar → release pin → (abort) `cancel_run` → set event → broadcast.
  *Test: assert the sidecar container is gone before `_cleanup_workspace`
  runs.*
- **C10** A debug re-run's `trigger_context` carries `branch` and
  `commit_sha` and nothing else. It can never merge and never move a card.
  *Test: debug-re-run a `card_work` run carrying `on_pass: merge`; assert no
  merge, no card write.*
- **C11** `StepRun.logs` keeps exactly ONE writer. The gate's notice line goes
  through a new `SOURCE_DEBUG` entry point in `step_logs.py`; the existing
  `append_step_logs` is refactored to delegate to it. No second append path.
- **C12** Terminal payloads are base64. Raw text frames are a bug.
- **C13** The terminal frame codec has ONE definition, in
  `backend/app/services/execution/debug_terminal_protocol.py`, and
  `cli/lazyaf/debug_protocol.py` is pinned to it by
  `tdd/unit/debug/test_terminal_protocol_contract.py`, which imports both and
  round-trips every frame type in both directions (R3).
- **C14** Terminal auth happens at the HTTP upgrade, before `accept()`, and
  re-reads the session row so a terminal session is refused whatever the JWT
  says. `GET /api/debug/{id}` never returns a token.
- **C15** Every bound (frame size, rate, outbound queue, duplicate terminal)
  closes the socket with a reason. Nothing is silently dropped or truncated.
- **C16** Remote steps pause, resume and abort; they do not attach. The
  refusal states the reason at all four surfaces (API field, WS close, UI,
  CLI). No fallback to a sidecar over the wrong volume.
- **C17** `--shell` at a breakpoint is an error naming the reason, not a
  silent downgrade to sidecar.
- **C18** No new `RunStatus` members. Debug state lives only on
  `DebugSession`.
- **C19** The ported state-machine test file carries no `try/except
  ImportError` and no `pytestmark = skipif`. Any genuinely new skip is
  baselined in `tdd/skip_baseline.json` (R4).
- **C20** A backend restart never leaves a half-alive debug run: the startup
  sweep ends the session with `end_reason="backend restarted while paused"`,
  fails the run, and removes stray sidecars.

---

## 17. Test plan / exit gate

| Tier | File | What it pins |
|---|---|---|
| T1 | `tdd/unit/execution/test_debug_session_state_machine.py` | 36 tests — 33 ported (guards stripped) + 3 for the resume edges |
| T1 | `tdd/unit/execution/test_debug_step_key.py` | C2, both pipeline formats |
| T1 | `tdd/unit/execution/test_debug_gate.py` | C1, C3, C4, C6, C7 |
| T1 | `tdd/unit/execution/test_debug_session_service.py` | C5, C9, C10, C20 |
| T1 | `tdd/integration/api/test_debug_api_contract.py` | create/get/resume/abort/extend/join-token; C14 (no token in GET); 400 on unknown key |
| T1 | `tdd/unit/debug/test_terminal_protocol_contract.py` | C12, C13 |
| T1 | `tdd/unit/scripts/test_cli_debug.py` | verb parsing, C17's refusal text |
| **T2** | `tdd/integration/services/execution/test_breakpoint_execution.py` | real Docker, **named volumes not `tmp_path` binds (R6)**: pause before a step, assert the volume holds the repo at the requested commit, spawn a sidecar, exec `ls /workspace/repo`, resume, hit a **second** breakpoint, resume to completion. Also `test_workspace_preserved_at_breakpoint` and the C8 refcount assertions. **Directory is load-bearing** — see the note in §15. |
| **T3** | `tdd/e2e/test_debug_rerun.py` | the exit gate: dogfood-style run fails → debug re-run with a breakpoint → CLI attaches → inspects → `@resume` → pipeline completes |
| **R8** | `frontend/e2e/debug-rerun.spec.ts` | button on a failed run → modal → checkboxes + commit choice → panel shows waiting, countdown, join command → Resume → run completes. Terminal I/O is **not** driven from Playwright (it is the CLI's path, covered by T3). |

**R7 (dogfood ratchet):** add a debug-mode leg to the dogfood suite — a
pipeline that pauses at one breakpoint and is resumed by the CI driver over
HTTP, proving the gate does not wedge the ratchet.

**Explicitly NOT in the exit gate:** live-step shell attach, remote attach,
`--read-only` sidecar mounts, sidecar idle reaping.

**Baseline discipline:** `cd backend && uv run pytest ../tdd -m "not slow"`
stays green; tier counts via `python scripts/run_tier.py` (T1 2406 / T2 70 /
T3 19 before this phase). Both implementers re-measure and **ratchet
`tdd/tier_floors.json` UP** at the end of the phase (T1 for the unit/api
suites, T2 for the breakpoint suite, T3 for the e2e), with a note naming what
was added — never a guessed number, and never down.

**Build preflight:** `scripts/run_tier.py` runs `build_images.py --check`
before T2 and T3. Adding the sidecar to `IMAGES` therefore makes
`lazyaf-debug-sidecar:dev` a **preflight requirement of both tiers** — B must
confirm the image builds before A's T2 suite can go green.

---

## 18. Registration lines for the integrator

**`backend/app/main.py`** — add to the router imports and includes:

```python
from app.routers import debug
...
app.include_router(debug.router)
```

**`backend/app/models/__init__.py`**:

```python
from app.models.debug_session import DebugSession
```
and add `"DebugSession"` to `__all__`.

**`frontend/src/App.svelte`** — **no change required.** `DebugRerunModal` and
`DebugPanel` mount inside `PipelineRunViewer.svelte`, which B owns.

**Shared-file edits that need the integrator's sign-off** (exact content, for
whoever ends up applying them):

`backend/app/schemas/pipeline.py`, after `ADHOC_TRIGGER_TYPES`:
```python
#: Stamped ONLY by routers/debug.create_debug_rerun (12.7). Not settable on
#: the public run endpoint: a debug re-run deliberately drops on_pass/on_fail
#: and card routing, so letting a caller stamp it would be a way to launder a
#: run past its own trigger actions.
DEBUG_TRIGGER_TYPES = ("debug_rerun",)

KNOWN_TRIGGER_TYPES = PUBLIC_TRIGGER_TYPES + ADHOC_TRIGGER_TYPES + DEBUG_TRIGGER_TYPES
```

`backend/app/routers/pipelines.py`, in `run_pipeline`, widen the existing
guard:
```python
if request.trigger_type in ADHOC_TRIGGER_TYPES + DEBUG_TRIGGER_TYPES:
```
(and extend the 400 detail string to name the debug case).

`backend/app/routers/test_api.py`, next to the other `register_resettable`
calls:
```python
from app.services.execution.debug_session_service import debug_session_service
from app.services.execution.debug_terminal import debug_terminal_service
register_resettable("debug_sessions", debug_session_service.reset)
register_resettable("debug_terminals", debug_terminal_service.reset)
```

`scripts/build_images.py`, in `IMAGES`, immediately after the `base` row:
```python
("debug-sidecar", "lazyaf-debug-sidecar", "base", []),
```

`cli/lazyaf/cli.py`, after the `tests` group:
```python
from lazyaf.debug_cmd import debug as debug_group
cli.add_command(debug_group)
```

**Migration parent:** `0009_debug_sessions.py` must set `down_revision` to the
real head at integration time. `0007_drop_polling_runner_columns` is present
(untracked) and `0008` does not yet exist; if 12.6.6 ships one, the parent is
`'0008'`, otherwise `'0007'`. **The integrator confirms this value.**

---

## 19. Open questions for the owner

1. **Remote attach deferral (§5)** — confirm. The alternative is a
   runner-protocol version bump inside 12.7, which reopens a contract frozen
   in January.
2. **Sidecar-only at a breakpoint (§6)** — confirm. Live-step shell is real
   and useful, but it is not a *breakpoint* feature and needs
   `StepExecution.container_id` actually written first.
3. **`lazyaf debug` as a click group (§13)** rather than PLAN's
   `--resume`/`--abort` flag forms.
4. **Read-write sidecar mount (§6)** — edits under `/workspace` are seen by
   the resumed step. That is the useful behaviour and the dangerous one.
5. **No `debug_*` `RunStatus` members (§0.4)** — PLAN's "Pipeline Run States
   (Extended)" table is dropped in favour of the session row.
6. **Tokens are 15-minute re-mintable JWTs, not one-time (§8)** — PLAN's
   security section says one-time; that is incompatible with reconnection.
