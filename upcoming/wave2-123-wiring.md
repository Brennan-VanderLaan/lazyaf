# Wave 2 — Phase 12.3 Wiring Design: one reporting path, pinned contracts

Status: DESIGN — implementers build from this verbatim.
Inputs: `backend/app/routers/steps.py` (live), `backend/app/services/control_layer/*` (main),
`backend/app/services/execution/local_executor.py`, `pipeline_executor.py`
(`_consume_local_events` / `_finish_local_step`), `failure_01:images/**`,
`upcoming/failure_01-salvage-audit.md` §12.3-images, PLAN.md R1–R8 + Phase 12.3.

---

## 0. Ground truth found during recon (read before arguing with the design)

- `POST /api/steps/{step_id}/status|logs|heartbeat` is live, registered in `main.py`,
  Bearer-JWT-authed (`control_layer/auth.py`), keyed by **StepExecution.id**, and
  covered by `tdd/unit/services/control_layer/test_step_api_endpoints.py`.
- The router **does write StepRun** today: `/logs` appends `line.content` **verbatim
  (no newline added)** to `StepRun.logs`; `/status` mirrors terminal status onto
  `StepRun.status` using the `"completed"/"failed"` vocabulary — which is **not**
  main's `RunStatus` vocabulary (`passed`/`failed`). It broadcasts **nothing** over WS.
- **Nothing on main creates StepExecution rows on the local path.** `pipeline_executor`
  never imports StepExecution; only `execution/idempotency.py` can create them and it
  has no production caller. A control-layer POST today would 404.
- The 12.2-INT stdout path is the only live reporting path:
  LocalExecutor event stream → `_consume_local_events` → StepRun.logs (batched commits)
  + `manager.publish_step_logs` / `publish_step_update`; terminal state via
  `_finish_local_step` (RunStatus.PASSED/FAILED, `send_step_run_status`,
  `send_pipeline_run_status`, continuation).
- main's `control_layer/` split: `auth.py` (in use — keep), `workspace.py`
  (`generate_step_config` with **auth_token / working_directory** keys — this is the
  frozen producer contract), `protocol.py` / `docker.py` / `environment.py` /
  `image.py` (unwired; image.py is Dockerfile-string fiction).
- failure_01 `images/base/control/` runtime: modular, tested, but reads `token` /
  `working_dir`, sends logs as **plain strings** (422s against `LogLine`), never
  enforces `timeout_seconds`, and only flushes logs when a new line arrives.
- `backend/docker/{base,claude,gemini,control,agent_wrapper}` exists on main = the
  broken-COPY-context copies. Retire.
- Workspace population (`workspace/population.py`) already proves the helper-container
  pattern; `docker-compose.yml` has `lazyaf-network`, backend reachable at
  `http://backend:8000` (settings.container_backend_url).

---

## 1. THE REPORTING PATH (R3: one source of truth, per datum, per mode)

Two modes, chosen at dispatch time, never mid-flight:

### Mode selection: image capability label + per-step override

- `lazyaf-base` (and children) bake `LABEL lazyaf.control-layer=1`. The image
  *declares* the capability at build time — this is explicit declaration by the image
  author, not path-shape inference (R6-compatible).
- LocalExecutor inspects the image label once per tag (threadpooled
  `client.images.get(image).labels`, cached in a dict on the executor; cache cleared
  in `reset()`). Label present ⇒ **control mode**; absent ⇒ **stdout mode**.
- Per-step override: `config.control: false` forces stdout mode on a labeled image
  (debug escape hatch). There is no `control: true` on unlabeled images — an image
  without the runtime can't be promoted by config.
- Stock images (`python:3.12`, anything user-supplied) therefore keep working with
  **zero behavior change** — the entire 12.2-INT stdout path is untouched for them.

### Ownership table (the actual R3 decision)

| Datum | stdout mode (today, unchanged) | control mode (12.3) |
|---|---|---|
| StepRun.logs + `step_log` WS frames | `_consume_local_events` flush | **`POST /logs` router — sole writer** |
| Intermediate status (`running`) + `step_update` WS | executor status events via `_consume_local_events` | **`POST /status` router** |
| StepExecution telemetry (status, heartbeat, progress, timeout_at) | n/a (no row) | `POST /status` + `/heartbeat` |
| **Terminal StepRun state, steps_completed, continuation, `step_run_status`/`pipeline_run_status` WS** | executor `result` event → `_finish_local_step` | **same — executor `result` event → `_finish_local_step`** |
| Timeout enforcement | executor deadline | control runtime enforces `timeout_seconds` in-container (graceful); executor deadline (`timeout + grace`) remains the backstop |

Rationale: the container exit code is ground truth for step outcome in both modes and
the executor always observes it (`container.wait`). So terminal state keeps exactly
one owner (`_finish_local_step`) regardless of mode, and a dead/wedged control runtime
degrades to a failed step, never a stuck one. Logs and liveness move to the POST path
in control mode because that is the 12.3 exit gate and the 12.6 remote-runner shape.

### No double-logging, mechanically

- The control runtime still echoes every line to container stdout (docker-logs
  forensics). In control mode `_consume_local_events` **drops** `log` and `status`
  events (debug-level logger only, no buffer append, no WS) — the stream is consumed
  solely for liveness/backstop-timeout and the `result` event. Mode is passed in
  `exec_context["control_mode"]` so the consumer doesn't guess.
- The router never writes StepRun terminal status again (see bridge below), so the
  `"completed"`-vs-`RunStatus.PASSED` vocabulary divergence dies here.

### The bridge in `routers/steps.py` (contract change, update its unit tests in the same commit)

1. `/logs`: after commit, `await manager.publish_step_logs(run_id, step_index, lines)`
   where lines are the received contents rstripped of `\n` (matching the frames the
   frontend already consumes). run_id/step_index come from the StepRun row already
   loaded (add `pipeline_run_id`/`step_index` to the select; both are columns on StepRun).
2. `/status`: keep all StepExecution writes. Broadcast
   `await manager.publish_step_update(run_id, step_index, request.status)` for
   `running` only. **Delete the StepRun mirror block entirely** (status,
   started_at/completed_at, error) — `_finish_local_step` owns it. Exception kept:
   on `running`, set `step_run.started_at` if unset (harmless, useful, no vocabulary).
3. `/heartbeat`: unchanged. `extend_seconds` updates `StepExecution.timeout_at` only;
   it does **not** move the executor's hard deadline at 12.3 (documented limitation,
   revisited when 12.4 makes long steps real).
4. Auth secret: add `settings.step_auth_secret` (env `LAZYAF_STEP_AUTH_SECRET`,
   default = today's constant for dev); `main.py` startup calls
   `auth.set_secret_key(settings.step_auth_secret)`.

### The bridge in the dispatch path (`pipeline_executor` + LocalExecutor)

Before spawning a control-mode container, the local dispatch path must create what the
router authenticates against:

- `IdempotencyService.get_or_create(execution_key, step_run_id)` → StepExecution row
  (status PREPARING, `timeout_at = now + timeout + LOCAL_STEP_HARD_TIMEOUT_GRACE`).
  Same `execution_key` format already used: `"{run_id}:{step_index}:{step_run_id}"`.
- `generate_step_token(step_id=execution.id, execution_key=...)` — token lifetime
  = `timeout + grace + 1h` slack, not the 24h default.
- Both go into the step config file (§2). `exec_context` gains
  `control_mode: bool` and `step_execution_id: str`.

---

## 2. CONFIG DELIVERY: config file into the volume via put_archive (decided)

**Decision: `/workspace/.control/step_config.json`, written by the backend through the
Docker API onto the created-but-not-started step container. No helper container, no
backend-CWD writes, no token in `docker inspect` env.**

Why not env-only: the audit's mandated renames (`auth_token`, `working_directory`)
only exist to match main's `generate_step_config` — the file IS the frozen producer
contract; 12.2.6 (`/workspace/.control/test_results.json`) and 12.5 (agent config with
`previous_step_logs`, far too big for env) both extend this directory. Env-only would
be a second wire contract to retire in one phase.

Mechanics in LocalExecutor (control mode only):

1. `container = client.containers.create(image, **run_kwargs)` — mounts are bound at
   create, so the volume is addressable. `command=None` (entrypoint ignores CMD in
   control mode).
2. Build an in-memory tar containing `.control/step_config.json`
   (mode `0o600`, uid/gid `1000` — see image contract), then
   `container.put_archive("/workspace", tar_bytes)` (threadpooled).
3. `container.start()`.
   Everything downstream (log pump, deadline, `wait`, cleanup-before-result) is
   identical to today.

Exact file contract — **verbatim the output of main's
`control_layer/workspace.generate_step_config`** (R3: producer stays the single
source; do not fork the shape):

```json
{
  "step_id":          "<StepExecution.id>",
  "step_run_id":      "<StepRun.id>",
  "execution_key":    "<run_id>:<step_index>:<step_run_id>",
  "command":          "<raw user command string, unwrapped>",
  "backend_url":      "<settings.container_backend_url>",
  "auth_token":       "<JWT>",
  "environment":      {"...step env + params..."},
  "timeout_seconds":  <step timeout>,
  "working_directory":"<settings.step_working_dir or step override>"
}
```

Consumer rules (control runtime, fixes applied while porting):
- `config.py` reads `auth_token` and `working_directory` (the audit renames);
  `command` is a **string**, shell-wrapped by the runtime as
  `["bash", "-c", "set -e\n" + command]` — same semantics as
  `local_executor.build_step_command`, so scripts behave identically in both modes.
- The runtime **deletes step_config.json immediately after loading it**
  (consume-once): the volume persists across steps and a stale config re-executing a
  previous step is exactly the class of landmine 12.3 exists to kill.
- Optional keys with defaults stay in-file capable: `heartbeat_interval` (10.0),
  `log_batch_size` (100), `log_batch_interval` (1.0).
- Env still carries the non-secret identifiers exactly as today
  (`LAZYAF_PIPELINE_RUN_ID`, `LAZYAF_STEP_RUN_ID`, `LAZYAF_STEP_INDEX`,
  `LAZYAF_EXECUTION_KEY`, `LAZYAF_BACKEND_URL`, `HOME`) plus `LAZYAF_CONTROL=1|0` —
  the entrypoint's mode switch (§3, entrypoint contract). The token appears **only**
  in the file.

---

## 3. THE PORT MAP

Target root: **`images/` at repo root** (matches failure_01 paths and PLAN 0d's
expectation; the control runtime imports nothing from `backend/app`, so it must not
live under `backend/` where the backend image build context would swallow it — and
`docker build ./images/base` stays a 5-file context).

| failure_01 file | Target on main | Fixes to apply (audit + recon) |
|---|---|---|
| `images/base/Dockerfile` | `images/base/Dockerfile` | Add `LABEL lazyaf.control-layer=1` + `LABEL lazyaf.content-hash=<hash>` (build script arg). Pin `useradd -u 1000 -g 1000` (deterministic chown/tar ownership). Bake the XDG/PIP/NPM env block from main's `environment.py` as `ENV` lines: `XDG_CACHE_HOME/XDG_CONFIG_HOME/XDG_DATA_HOME`, `PIP_CACHE_DIR`, `PIP_USER=1`, `PYTHONUSERBASE=/workspace/home/.local`, `NPM_CONFIG_PREFIX=/workspace/home/.npm-global`, `PATH` with `.local/bin` + `.npm-global/bin`. Drop `USER lazyaf`; new root `ENTRYPOINT ["/control/entrypoint.sh"]` (below). Install `gosu`. |
| — (new) | `images/base/control/entrypoint.sh` | Root entrypoint, the chown-at-entrypoint fix: idempotent `chown lazyaf:lazyaf` of `/workspace` top-level + `mkdir -p` home subdirs, then: if `LAZYAF_CONTROL=1` → `exec gosu lazyaf python3 /control/run.py`; else → `exec gosu lazyaf "$@"` (CMD passthrough — lazyaf-base degrades to a stock image, which is what `control: false` rides on). |
| `images/base/control/run.py` | `images/base/control/run.py` | Rename config fields at call sites (`config.auth_token`, `config.working_directory`). Delete config file after load (consume-once). Report `timeout` status when the executor signals timeout. |
| `images/base/control/config.py` | `images/base/control/config.py` | `token`→`auth_token`, `working_dir`→`working_directory`; `command` is `str` not `List[str]`; keep defaults for heartbeat/log batching; on missing/invalid config print the reason to stderr (today it returns None silently). |
| `images/base/control/executor.py` | `images/base/control/executor.py` | (a) **Enforce `timeout_seconds`**: `Popen(..., start_new_session=True)`, watchdog kills the process group SIGTERM→5s→SIGKILL at deadline, return exit 124, runtime reports status `timeout`. (b) **Log flush on quiet processes**: reader thread + main loop flushing every `log_batch_interval` on a timer, not only on line arrival. (c) Shell-wrap the command string (`bash -c 'set -e…'`). |
| `images/base/control/backend_client.py` | `images/base/control/backend_client.py` | (a) **LogLine wrapping**: `send_logs` posts `{"lines": [{"content": line_with_trailing_newline, "stream": "stdout"}]}` — content **must include `\n`**, the router concatenates verbatim. (b) heartbeat sends `{"extend_seconds": int(6 * heartbeat_interval)}`. (c) Cap the log path retry budget: `MAX_RETRIES=3`, total ≤15s, count dropped lines; runtime appends `[control] WARNING: N log lines failed to reach backend` to the final status `error` if nonzero. Status/heartbeat keep the patient budget. |
| `images/base/control/heartbeat.py` | `images/base/control/heartbeat.py` | Port as-is. |
| `images/base/requirements.txt` | `images/base/requirements.txt` | Port as-is (`requests`). |
| `images/claude/Dockerfile` | `images/claude/Dockerfile` | Port; `FROM lazyaf-base:dev` (tag scheme §4). |
| `images/test-runner/Dockerfile` | `images/test-runner/Dockerfile` | Port; `FROM lazyaf-base:dev`; **quote `"pytest>=7.0"`** (unquoted it's a shell redirect); keep uv install; drop the trailing `RUN ... --version` verification layer or keep — harmless. |
| `images/base/control/agent_wrapper.py` | **NOT PORTED at 12.3** | 12.5 rebuilds it as a runner-common shim (audit: ADAPT). Do not copy it now. |
| `images/gemini/Dockerfile` | **DISCARD** | Fiction (imports a nonexistent module). |
| `images/debug-sidecar/Dockerfile` | **PARK** | Copy at 12.7. |
| failure_01 control unit tests (`tdd/unit/execution/test_control_layer_protocol.py`) | `tdd/unit/images/test_control_runtime.py` | Re-target imports via conftest `sys.path.insert(images/base/control)`; add cases for the four fixes (timeout kill, quiet-flush timer, LogLine payload shape, auth_token rename). |

### Retired in the same phase (R2: delete in its own commit after the round-trip test is green)

| Retired | Replaced by |
|---|---|
| `backend/app/services/control_layer/image.py` (Dockerfile string generators) + `tdd/unit/services/control_layer/test_base_image_contract.py` | Real `images/` tree; rewrite the contract test to assert on the **actual Dockerfile files** (entrypoint present, label present, HOME=/workspace/home, chown in entrypoint) — cheap text assertions, no docker needed. |
| `backend/app/services/control_layer/protocol.py` (in-container client/executor duplicates) + its unit test file | `images/base/control/*` is the only in-container runtime. Contract pinned by a producer↔consumer test: `generate_step_config()` output loads through `images/base/control/config.load_step_config` with zero key loss (R3). |
| `backend/app/services/control_layer/docker.py` | LocalExecutor's MountSpec machinery (already the live path). |
| `backend/app/services/control_layer/environment.py` + `test_home_persistence.py` unit assertions on it | ENV block baked into `images/base/Dockerfile`; keep the *behavioral* HOME-persistence coverage in the e2e (§5 cross-step tool test). |
| `backend/docker/` tree (broken copies) | `images/` tree. |
| `control_layer/__init__.py` exports of all of the above | trim to `auth` + `workspace` exports. |

Kept in `backend/app/services/control_layer/`: `auth.py` (router dependency),
`workspace.py` (`generate_step_config` producer + `initialize_workspace`/layout —
the config producer the executor now actually calls).

---

## 4. BUILD STORY (no phantom `:latest`)

- **Names/tags:** `lazyaf-base:dev`, `lazyaf-claude:dev`, `lazyaf-test-runner:dev`.
  `:dev` is the moving local tag every reference uses; each build also stamps
  `LABEL lazyaf.content-hash=<sha256[:12] of the image dir tree>` for staleness checks.
  No `:latest` anywhere — grep-able rule.
- **`scripts/build_images.py`** (stdlib + docker SDK, mirrors population.py's client
  handling): builds `base → claude → test-runner` in dependency order;
  computes the content hash; **skips** a build when the local `:dev` image's
  `lazyaf.content-hash` matches (`--force` overrides); `--check` exits nonzero
  listing missing/stale images without building. Windows-host friendly (pure SDK,
  no shell).
- **Compose:** step images are not services — no compose build targets. The dev
  workflow is `python scripts/build_images.py` once (and after editing `images/`);
  document in `tdd/README.md` + PLAN phase notes.
- **Backend tolerance:** `population.pre_pull_images` already logs-and-continues when
  a pull fails; verify it tolerates locally-built-unpullable tags (lazyaf-* images
  must never be added to the pre-pull list). LocalExecutor's existing
  `ImageNotFound` handler already fails a step loudly with `Image not found:
  lazyaf-base:dev` — that message plus `--check` is the rebuild trigger story; do not
  auto-build from the backend (a surprise 5-minute docker build inside a pipeline run
  is worse than a loud failure).
- **CI/dogfood:** the dogfood run itself asserts the images work (exit gate). A T2
  integration test asserts `docker image inspect lazyaf-base:dev` has the
  control-layer label when Docker is available (skip reason baselined per R4
  until build runs in CI).

---

## 5. DOGFOOD RATCHET — `.lazyaf/pipelines/test-suite.yaml` next shape

- **`install-uv` step: DELETED** (uv baked into `lazyaf-test-runner`; base stays
  lean — uv/node belong to the test-runner tier image).
- **`sync-deps`, `tier1`, `tier2`, `tier3`: `image: "lazyaf-test-runner:dev"`**
  (need uv + pytest + node). Drop every `export PATH="$HOME/.local/bin:$PATH"` line
  — baked ENV. tier2/tier3 keep their explicit docker-socket bind mounts (unchanged
  mechanism, retired at 12.4).
- **`verify-executor`: `image: "lazyaf-base:dev"`** (python + requests suffice) —
  and it now doubles as the live control-layer probe: extend
  `scripts/verify_executor.py` to also assert via the API that this run's StepRuns
  have logs that arrived while a StepExecution row exists per script step
  (executor=local + control path exercised — R1 observability).
- All steps thereby run in **control mode**: the dogfood run IS the standing
  round-trip acceptance (control runtime → POST /api/steps/* → StepRun rows → same
  `step_update`/`step_log` WS frames the UI already renders).
- HOME-persistence contract test (phase deliverable): a two-step slow e2e on
  `lazyaf-base:dev` — step 1 `pip install --user cowsay` (lands in
  `/workspace/home/.local` via baked `PIP_USER`), step 2 runs `cowsay` — proving
  cross-step tool persistence on a **named volume** (R6).
- The 12.2-INT container-logs→StepRun round-trip test is re-run over the POST path:
  same scenario, image = `lazyaf-base:dev`, asserting the router (not the stdout
  consumer) produced the row + frames — assert e.g. that `_consume_local_events`
  persisted nothing by checking the StepExecution row exists and logs match the
  POSTed batches.

---

## 6. Test/verification checklist for the implementing agents

1. Unit: control runtime fixes (timeout kill, quiet flush, LogLine shape, renames,
   consume-once config delete) — ported test file + new cases.
2. Unit: producer↔consumer config contract round-trip (generate_step_config →
   load_step_config).
3. Unit (updated): `test_step_api_endpoints.py` — logs broadcast `step_log` frames,
   status broadcasts `running`, terminal status **no longer** touches
   StepRun.status; real WS manager with capturing transport (R6), never AsyncMock.
4. Integration (Docker, T2): named-volume put_archive delivery — file lands in the
   volume, owned readably, consumed and deleted.
5. Slow e2e: full control-mode round trip + HOME persistence pair + dogfood yaml on
   the new images.
6. `cd backend && uv run pytest ../tdd -m "not slow"` stays green (1441-ish + new).

Seams left open on purpose: heartbeat-driven deadline extension (12.4), agent config
file + wrapper (12.5), test_results.json manifest channel (12.2.6 — same `.control/`
directory, already reserved).
