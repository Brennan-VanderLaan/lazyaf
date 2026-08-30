# Wave 5 - Phase 12.6 Wiring Design: RemoteExecutor, the runner protocol, and the loopback lane

Status: DESIGN - implementers build from this verbatim.
Inputs: `PLAN.md` Phase 12.6 + R1-R8, `upcoming/failure_01-salvage-audit.md` section 12.6,
`upcoming/wave2-123-wiring.md` (the control layer this phase carries onto another host),
`upcoming/wave4-125-wiring.md` (12.5 - this design extends exactly that machinery and
reuses its vocabulary), the dormant contract suite
(`tdd/unit/execution/test_websocket_protocol.py`, `test_job_recovery.py`,
`test_runner_state_machine.py`), `backend/app/services/execution/runner_state.py`,
`local_executor.py`, `backend/app/services/pipeline_executor.py`,
`backend/app/services/workspace/execution_router.py`,
`backend/app/services/runner_pool.py`, `backend/app/routers/runners.py`,
`backend/app/models/runner.py`, `backend/app/services/websocket.py`,
`backend/app/main.py`, and the failure_01 references
(`remote_executor.py`, `runner_protocol.py`, `job_recovery.py`, `ws_runners.py`,
`runner-agent/`, `a1b2c3d4e5f6_enhance_runners_table.py`).

---

## 0. Ground truth found during recon (read before arguing with the design)

- **The dormant suite is the spec and it is stricter than it looks.** 74 tests wake up the
  moment `app.services.execution.runner_protocol` and `app.services.execution.job_recovery`
  exist. `test_runner_state_machine.py` (74 tests) is already live against
  `runner_state.py`. Three constraints fall straight out of the test bodies and are
  NOT negotiable:
  1. `RegisterMessage(runner_id=..., name=..., runner_type=..., labels={})` constructs with
     exactly those four kwargs, and `validate_runner_message` returns `len(errors) == 0`
     for a register carrying only those four. **Auth and protocol version therefore cannot
     be required message fields.** Auth is a transport concern (section 1.3).
  2. `to_dict()` assertions check named keys only, never key COUNT. Extra fields are legal.
     Every field added below is defaulted so the four-kwarg constructors still work.
  3. `job_recovery` is tested against `AsyncMock` sessions whose `db.execute(...)` returns
     an object exposing `.scalar_one_or_none()` (single-row paths) or
     `.scalars().all()` (the sweep). **The implementation must load rows via
     `db.execute(select(...))`, never `db.get()`** - `db.get()` on those mocks returns a
     coroutine-shaped MagicMock and every assertion fails.
- `ExecutorMode.REMOTE = "remote"` **already exists** in `backend/app/models/pipeline.py`.
  `pipeline_executor._decide_route` currently raises a hard `RuntimeError` on it:
  `"...which has no execution path until Phase 12.6"`. That RuntimeError and the router's
  pin branch are the two lines this phase flips.
- `StepExecution.runner_id` **already exists** (`String(36)`, nullable, annotated
  `# Remote executor only`). Nothing writes it.
- **The `runners` table is dead.** Five columns (`id`, `container_id`, `status`,
  `current_job_id`, `last_heartbeat`), imported by `routers/runners.py` and never queried.
  `RunnerPool` is 100% in-memory. 12.6 defines this table's real shape.
- **`requires:` has no grammar.** `_RUNNER_PIN_KEYS = ("runner_type", "requires")` and the
  router only tests KEY PRESENCE; the value is never inspected. arch/has/runner_id parsing
  is greenfield. Agent steps skip the pin branch entirely, so an agent `requires:` is
  dropped today with zero log output.
- LocalExecutor's event stream is **plain dicts, no dataclass**, with exactly three type
  strings: `{"type":"status","status":...}`, `{"type":"log","line":...}`,
  `{"type":"result","status":...,"exit_code":...}`. Ordering is fixed: `status:preparing`
  -> `status:running` -> zero or more `log` (suppressed in control mode) -> `status:<final>`
  -> the result dict itself. Optional result keys: `error`, `timeout_seconds`, `log_tail`,
  `cached`. **This is the whole executor contract** and RemoteExecutor reproduces it.
- Control mode is the local shape 12.6 must carry over the wire: `create -> put_archive ->
  start`, config at `/workspace/.control/<step_execution_id>.json` announced via
  `CONFIG_PATH`, `secret_environment` file-only, the container reporting to
  `POST /api/steps/{id}/status|logs|heartbeat|test-results|usage` with a step JWT.
  **The producers (`generate_step_config`, `generate_agent_config`) are already
  single-sourced and reusable; only the DELIVERY is local-daemon-bound.**
- Step tokens: `control_layer/auth.py`, HS256, claims `{step_id, execution_key, iat, exp}`,
  TTL = `timeout + 120 + 300`. `execution_key` is carried but never verified. A runner
  identity token is a separate concern - do not overload the step token.
- `ConnectionManager` has **no channel scoping**: `broadcast(type, payload)` fans to every
  UI socket. `runner_status` is already a frame type on both sides. Any NEW frame must be
  added to `frontend/src/lib/stores/websocket.ts` (`ServerMessageType` +
  `HANDLED_MESSAGE_TYPES` + the switch) or `websocket.test.ts` fails - it greps the backend
  source for drift.
- `recover_orphaned_executions` unconditionally fails everything in
  `[pending, assigned, preparing, running, completing]` at startup. Its own docstring flags
  the 12.6 divergence. It must learn about remote steps (section 2.6).
- `job_queue.enqueue` has **exactly one live call site** (`pipeline_executor.py:1531`,
  the agent-only `executor: legacy` hatch). The deletion commit's blast radius is small.
- Alembic head is **`0005`** (`0004` and `0005` are untracked-but-present in the working
  tree). 12.6's migration is `0006`, `down_revision = "0005"`, written idempotently with
  `inspector.has_table` / `has_index` guards per the 0002/0004/0005 convention.
- failure_01's implementations are the negative space. The defects to NOT reproduce,
  named: `current_step_execution_id` never written (which neutered all of job_recovery);
  `_requeue_step` duplicating `JobRecoveryService`; `find_idle_runner` TOCTOU with no
  reservation; IDLE runners never death-checked; `DEAD -> DISCONNECTED` throwing out of a
  `finally:`; `log` broadcast-only and never persisted; `step_complete` never writing the
  StepExecution row; execution inline in the receive loop; no auth; no version; no cancel;
  `execution_key` transmitted and unused; `network_mode="host"` on spawned containers.

---

## 1. THE WIRE PROTOCOL

`backend/app/services/execution/runner_protocol.py` is the single source of truth for the
wire (R3). Plain `dataclasses`, no pydantic - the contract tests construct them directly.
`type` is `field(default=..., init=False)` on every message class.

### 1.1 Constants (verbatim - the four with tests are load-bearing)

```python
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})

REGISTRATION_TIMEOUT = 10   # runner must send register within 10s of connect
ACK_TIMEOUT          = 5    # runner must ACK an assignment within 5s
HEARTBEAT_INTERVAL   = 10   # runner sends heartbeat every 10s
DEATH_TIMEOUT        = 30   # no heartbeat for 30s => dead

# Derived / operational. NAMED, never inline literals (failure_01 had a bare `5`).
RECEIVE_TIMEOUT        = HEARTBEAT_INTERVAL * 2   # 20s: server-side read deadline
DEATH_MONITOR_INTERVAL = 5                        # death sweep tick
DISPATCH_SWEEP_INTERVAL = 15                      # dispatcher self-heal tick
MAX_ASSIGN_ATTEMPTS    = 3                        # per step, before failing it
NO_RUNNER_TIMEOUT      = 300                      # no matching runner for 5min => fail
DRAIN_GRACE            = 30                       # drain: finish current step, then close

# Back-pressure
MAX_MESSAGE_BYTES         = 1_048_576
MAX_LOG_LINES_PER_MESSAGE = 500
MAX_LOG_LINE_BYTES        = 16_384
INBOUND_BUDGET_MESSAGES   = 200    # per connection per 10s window
```

`RECEIVE_TIMEOUT < DEATH_TIMEOUT` is deliberate and must stay so: a read timeout provokes a
server `ping` (keepalive), the death monitor is the sole authority on death. failure_01 had
these two values fighting each other with no stated relationship.

### 1.2 Message catalogue

**Runner -> Backend**

| type | Fields (defaults shown) | Sent when |
|---|---|---|
| `register` | `runner_id: str`, `name: str`, `runner_type: str`, `labels: dict = {}`, `protocol_version: int = PROTOCOL_VERSION`, `agent_version: str = ""`, `token: str \| None = None`, `resume: dict \| None = None` | first frame, within `REGISTRATION_TIMEOUT` |
| `ack` | `step_id: str` | within `ACK_TIMEOUT` of `execute_step` |
| `heartbeat` | (none) | every `HEARTBEAT_INTERVAL`, always, including mid-step |
| `log` | `step_id: str`, `lines: list[str]`, `seq: int \| None = None` | runner-origin lines ONLY (section 1.6) |
| `step_complete` | `step_id: str`, `exit_code: int`, `error: str \| None = None` | terminal outcome, exactly once per assignment |
| `ping` | (none) | optional client liveness probe |

**Backend -> Runner**

| type | Fields (defaults shown) | Sent when |
|---|---|---|
| `registered` | `runner_id: str`, `protocol_version: int = PROTOCOL_VERSION`, `heartbeat_interval: int = HEARTBEAT_INTERVAL`, `death_timeout: int = DEATH_TIMEOUT`, `resume_action: str = "idle"`, `resume_step_id: str \| None = None` | registration accepted |
| `execute_step` | `step_id: str`, `execution_key: str`, `config: dict` | assignment |
| `cancel_step` | `step_id: str`, `reason: str = ""` | user cancel, reassignment abort, run failure |
| `cleanup_workspace` | `retain_key: str` | run completes |
| `drain` | `reason: str = ""` | graceful shutdown |
| `pong` | (none) | reply to `ping`/`heartbeat` |
| `ping` | (none) | keepalive after `RECEIVE_TIMEOUT` |
| `error` | `message: str`, `code: str \| None = None`, `fatal: bool = False` | protocol / validation / auth |

`step_complete.error` is always emitted, `null` on success - never omitted.
`registered` carrying `heartbeat_interval` and `death_timeout` is how the runner learns the
server's timing without a second config source (failure_01 had the agent's 10s, the
server's 20s read deadline and the 30s death timeout drifting independently).

### 1.3 Auth: connect-time, not in `register`

The contract tests forbid a required `token` field. Auth is therefore performed at the
**HTTP upgrade**, before `accept()`:

1. `Authorization: Bearer <secret>` header on the WS handshake (what the runner-agent uses).
2. `?token=<secret>` query parameter (fallback for clients that cannot set headers).
3. `register.token` (last resort; accepted only if 1 and 2 are absent).

The secret is `settings.runner_auth_secret` (env `LAZYAF_RUNNER_AUTH_SECRET`), defaulting to
a dev constant exactly as `step_auth_secret` does - the established house pattern from 12.3.
Comparison is `hmac.compare_digest`. Failure: send `error{code:"auth", fatal:true}` then
`close(4003)`. Never `accept()` an unauthenticated socket and then close it - the failure
must be visible in the handshake.

The endpoint calls one function, `authenticate_runner_connection(websocket) -> RunnerPrincipal`,
returning `{"runner_id": str | None, "scope": "enroll"}`. **A shared secret does not bind an
identity**, so `runner_id` is client-asserted and the real guards are elsewhere:

- **Duplicate-connection rejection.** A second live connection claiming a `runner_id` that
  the registry already holds is refused with `close(4004)`. The FIRST connection wins; the
  impostor cannot displace it.
- **Step-scoped message gating.** Every inbound `ack` / `log` / `step_complete` is dropped
  with a WARN unless `step_execution.runner_id == connection.runner_id` AND
  `runner.websocket_id == connection.websocket_id` (section 2.5).

Per-runner JWTs (mint on first enrollment, return in `registered`, runner persists) are the
named upgrade path and touch only `authenticate_runner_connection` and `runner_token.py`.
They are NOT built in 12.6.

`backend/app/services/execution/runner_token.py` ships now with `mint_runner_token(runner_id,
ttl)` / `verify_runner_token(token) -> dict | None` (HS256, claims `{typ:"runner", sub, iat,
exp}`), used by nothing on the default path and covered by unit tests. It exists so the
upgrade is a config flip, not a design change. That is a deliberate, stated,
one-module-with-tests seam - not a stub branch (R4).

### 1.4 Version negotiation

- `register.protocol_version` absent => treated as `1` (a pre-version agent).
- Not in `SUPPORTED_PROTOCOL_VERSIONS` => `error{code:"protocol_version", fatal:true,
  message:"backend speaks protocol version(s) 1, runner offered 2"}` then `close(4002)`.
- `registered.protocol_version` tells the runner what the backend speaks. The runner logs a
  WARNING when it differs from its own and continues (the backend already accepted it).
- The version gates the SHAPE of `execute_step.config` (section 3.2) and nothing else. A
  new required config key is a version bump; a new OPTIONAL key is not.

### 1.5 Sequencing

```
runner                                backend
  |---- TCP + WS upgrade (Bearer) ------->|  authenticate_runner_connection
  |<---------------- accept --------------|
  |---- register{id,name,type,labels} --->|  <= REGISTRATION_TIMEOUT (10s)
  |                                       |  registry.connect() -> DISCONNECTED->CONNECTING->IDLE
  |                                       |  job_recovery.on_runner_reconnect()
  |<--- registered{resume_action, ...} ---|
  |                                       |
  |---- heartbeat ---------------------->|  every 10s, forever, mid-step included
  |<--- pong ----------------------------|
  |                                       |
  |<--- execute_step{step_id, key, cfg} --|  IDLE->ASSIGNED (CAS in DB first)
  |---- ack{step_id} ------------------->|  <= ACK_TIMEOUT (5s) ; ASSIGNED->BUSY
  |---- log{step_id, lines} ------------>|  runner-origin only ("[runner] pulling image")
  |     (step container POSTs its OWN status/logs/heartbeat/test-results/usage
  |      to /api/steps/* over HTTP - it does not travel this socket)
  |---- step_complete{step_id, rc, err} ->|  BUSY->IDLE ; yields the executor `result` event
  |<--- cleanup_workspace{retain_key} ----|  when the whole run finishes
```

Failure branches:

| Trigger | Backend action | Runner action |
|---|---|---|
| no `register` in 10s | `error` + `close(4000)` | reconnect with backoff |
| bad/unknown register | `error` + `close(4001)` | reconnect with backoff |
| bad mid-session message | `error` frame, **connection kept** | log, continue |
| no `ack` in 5s | ASSIGNED->DEAD, step -> `pending`, wake dispatcher, attempt+1 | (may still be alive; its late `ack` is dropped by the step gate) |
| no `heartbeat` in 30s | `*`->DEAD, `on_runner_death`, requeue, close socket | reconnect; `registered.resume_action` decides |
| socket closes mid-step | `*`->DISCONNECTED, `on_runner_disconnect`, requeue | kill the container, reconnect |
| step reassigned, runner returns | `registered{resume_action:"abort"}` + `cancel_step{reason:"reassigned"}` | kill the container, discard, go IDLE |
| backend draining | `drain` to every runner; wait `DRAIN_GRACE` | stop accepting, finish current step, close(1000) |

**Close codes:** 1000 normal / drain complete; 4000 registration timeout; 4001 invalid
registration; 4002 unsupported protocol version; 4003 authentication failed; 4004 duplicate
connection for runner_id; 4005 back-pressure or protocol abuse; 4009 server draining.

### 1.6 Which channel carries what (the central R3 decision)

**Decided: the step container keeps POSTing to `/api/steps/*`. The WS carries only what is
about the RUNNER and the ASSIGNMENT.**

| Datum | Channel | Writer |
|---|---|---|
| step status (`running`), step heartbeat, step logs, test-results manifest, usage manifest | **HTTP POST `/api/steps/{id}/...`** with the step JWT, exactly as on the local path | the step container's control runtime, unchanged |
| runner lifecycle (register / heartbeat / death) | WS | runner agent |
| assignment + ACK + cancel + drain + workspace cleanup | WS | backend / runner agent |
| **runner-origin** log lines | WS `log` | runner agent |
| **terminal step outcome** | WS `step_complete` | runner agent |

Rationale, and it is the whole point: reimplementing test-results and usage over the WS
would be a second ingestion path for two channels that 12.2.6 and 12.5 just single-sourced -
precisely the R3 violation this arc exists to remove. All five control channels work on a
remote host on day one with **zero new server code**, because the step JWT is
location-independent. NAT is not an argument for tunnelling: a host that can open a
WebSocket can POST.

`log` over WS is genuinely necessary and genuinely different: it carries the lines a step
container **cannot** emit because it does not exist yet or failed to start - `[runner]
provisioning workspace`, `[runner] pulling lazyaf-test-runner:dev`, `[runner] ERROR: docker
daemon unreachable`. That is the answer to "how does a remote step that dies before it runs
explain itself".

`step_complete` is the remote analogue of LocalExecutor's `container.wait()`: exit code is
ground truth for step outcome in both modes, and the executor always observes it (section 3).

Runner-origin lines are written by the SAME writer the `/logs` router uses. This phase
extracts `backend/app/services/execution/step_logs.py`:

```python
async def append_step_logs(
    db, execution: StepExecution, lines: list[str], *, source: str = "container"
) -> None:
    """Sole writer of StepRun.logs in control mode, for BOTH channels.

    source="container": lines appended verbatim (the /logs router contract - content
        already carries its trailing newline).
    source="runner":    each line is prefixed "[runner] " and newline-terminated.
    Publishes manager.publish_step_logs(run_id, step_index, lines) after commit.
    """
```

`routers/steps.py`'s `/logs` handler is reduced to auth + `_reject_terminal_writes` + one
call to it. `ws_runners.py` calls it with `source="runner"`. Two callers, one writer (R3).

### 1.7 Back-pressure

**Runner side.** The agent owns a bounded outbound queue (`AGENT_OUTBOUND_QUEUE = 1000`
lines) drained by a sender task. Overflow drops OLDEST and increments a counter; at flush
time the agent emits one synthetic line `[runner] WARNING: N log lines dropped
(back-pressure)`. Telemetry never blocks execution and never wedges the socket - the same
hard rule the 12.3 log budget follows. Lines are batched to at most
`MAX_LOG_LINES_PER_MESSAGE` per frame, flushed on batch-full or every 1.0s.

**Backend side.** Per connection: frames larger than `MAX_MESSAGE_BYTES` are answered with
`error{code:"too_large"}` and DROPPED (never a close - one huge line must not kill a live
step). Lines longer than `MAX_LOG_LINE_BYTES` are truncated with a `...[truncated]` marker.
More than `INBOUND_BUDGET_MESSAGES` in a rolling 10s window => `error{code:"rate", fatal:true}`
+ `close(4005)`; the runner reconnects with backoff, and the step is requeued by the normal
disconnect path. A runner that floods loses its work rather than the backend losing its
event loop.

### 1.8 Message helpers (exact behavior the tests pin)

```python
def parse_runner_message(data: dict) -> RunnerMessage:
    """ValueError("Missing message type") when 'type' is absent;
       ValueError(f"Unknown message type: {t}") otherwise.
       Every field read with .get() and a default - parse never raises KeyError.
       The endpoint ALWAYS calls validate_runner_message first."""

def validate_runner_message(data: dict) -> list[str]:
    """Required-field table (empty list == valid):
         register      -> runner_id, runner_type      (NOT name, NOT labels)
         ack           -> step_id
         heartbeat     -> (none)
         ping          -> (none)
         log           -> step_id, lines
         step_complete -> step_id, exit_code
       Missing type  -> ["Missing 'type' field"]
       Unknown type  -> [f"Unknown message type: {t}"]
       Field errors  -> f"Missing '{field}' field"  """

def create_backend_message(msg_type: str, **kwargs) -> BackendMessage:
    """registered | execute_step | cancel_step | cleanup_workspace | drain | pong |
       ping | error. Anything else: ValueError(f"Unknown message type: {msg_type}")."""

def normalize_arch(value: str) -> str:
    """x86_64|amd64|x64 -> 'amd64'; aarch64|arm64|armv8 -> 'arm64';
       armv7l|armhf -> 'armv7'; anything else -> value.lower().
       Applied BACKEND-SIDE to both register labels and parsed requirements, so
       there is exactly one implementation and the agent ships raw
       platform.machine() (R3)."""

def build_execute_step_config(step_config, exec_context, step_config_file, agent_config_file) -> dict:
    """The ONLY producer of execute_step.config (section 3.2). Owned here so the
       backend cannot drift from the agent's consumer."""
```

`error` messages carry `"Missing message type"` / `"Unknown message type: X"` verbatim
because the tests match on those substrings.

---

## 2. RUNNER REGISTRY + STATE

### 2.1 The model (`backend/app/models/runner.py`, rewritten)

`RunnerStatus` is **deleted**. The vocabulary is `RunnerState` from
`services/execution/runner_state.py` - one enum for the machine, the column, and the API
(R3). `disconnected | connecting | idle | assigned | busy | dead`.

| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | client-asserted runner id, or uuid4 when absent |
| `name` | `String(255)` null | display name |
| `runner_type` | `String(50)` NOT NULL | server_default `'claude-code'` |
| `status` | `String(50)` NOT NULL | a `RunnerState` value; indexed |
| `labels` | `Text` null | JSON dict; `get_labels()` / `set_labels()` |
| `current_step_execution_id` | `String(36)` null | FK `step_executions.id` - **written on every assignment** (failure_01's fatal omission) |
| `websocket_id` | `String(64)` null | uuid4 per connection; unique index; split-brain fence |
| `protocol_version` | `Integer` null | forensics |
| `agent_version` | `String(64)` null | forensics |
| `connected_at` | `DateTime` null | |
| `last_heartbeat` | `DateTime` NOT NULL | **set backend-side at receipt**, never from the wire |
| `created_at` | `DateTime` null | |
| `container_id`, `current_job_id` | `String` null | polling-stack leftovers; dropped in `0007` |

```python
def matches_requirements(self, requirements: dict) -> bool:
    """Requirement grammar (section 2.4). Empty requirements match everything.

    runner_id   -> exact match against self.id
    runner_type -> exact match against self.runner_type; "any" matches everything
    arch        -> normalize_arch equality against labels["arch"]
    has         -> set(requirements["has"]) <= set(labels.get("has", []))
    any other k -> equality against labels.get(k)   <-- NOT silently ignored

    failure_01 ignored unknown keys, so `requires: {gpu: a100}` matched every
    runner. Generic label equality makes free-form labels useful AND makes an
    unsatisfiable pin visibly unsatisfiable.
    """
```

`is_available` (`status == "idle"`) and `is_connected` (`status in {connecting, idle,
assigned, busy}`) stay as properties, delegating to `RunnerState` so the two cannot drift.

### 2.2 Migration `backend/alembic/versions/0006_runner_registry.py`

`revision = "0006"`, `down_revision = "0005"`. Idempotent throughout
(`inspector = sa.inspect(op.get_bind())`, `has_table` / `has_index` / column-name guards)
per the 0002/0004/0005 convention - a pre-alembic dev DB is healed by `create_all` before
being stamped. `tdd/integration/test_migrations.py` pins parity.

Inside `with op.batch_alter_table("runners") as b:` - ADD ONLY, nothing is dropped here:
`name`, `runner_type` (server_default `'claude-code'`, NOT NULL), `labels`,
`current_step_execution_id` (+ FK `fk_runners_step_execution` -> `step_executions.id`),
`websocket_id`, `protocol_version`, `agent_version`, `connected_at`, `created_at`.
Indexes: `ix_runners_websocket_id` (unique), `ix_runners_status`.

**Data migration** (the gap failure_01's version left open, named in the audit):
`UPDATE runners SET status='disconnected' WHERE status IN ('offline','')`. On upgrade, every
existing row is also forced to `disconnected` with `websocket_id = NULL` and
`current_step_execution_id = NULL` - no connection survives a migration, and pretending one
did is how a fresh backend hands work to a ghost.

On `step_executions`, also ADD: `runner_requirements` (`Text` null, JSON - the dispatcher
must be able to re-match a requeued step after a backend restart, so requirements have to be
durable, not held in the dispatch closure) and `assigned_at` (`DateTime` null - ACK-timeout
forensics). Index `ix_step_executions_status` if absent (the dispatcher scans
`status = 'pending'`).

`0007_drop_polling_runner_columns.py` (drops `container_id`, `current_job_id`) ships in the
DELETION commit, not here. 0006 is non-destructive by design.

### 2.3 The registry (`backend/app/services/execution/runner_registry.py`)

Replaces `runner_pool.py`. **The `RunnerStateMachine` is the in-memory authority for a live
connection; the DB row is its durable projection.** One machine per live connection, owned by
the registry, and every transition goes through one method so the two can never diverge:

```python
class RunnerRegistry:
    _connections: dict[str, WebSocket]
    _machines:    dict[str, RunnerStateMachine]
    _locks:       dict[str, asyncio.Lock]     # per-runner; guards connect/transition

    async def connect(self, db, websocket, register: RegisterMessage) -> Runner
    async def disconnect(self, db, runner_id: str, websocket_id: str) -> None
    async def transition(self, db, runner_id, to_state, *, reason="") -> None
    async def heartbeat(self, db, runner_id: str) -> None
    async def send(self, runner_id: str, message: BackendMessage) -> bool
    async def find_available(self, db, requirements: dict) -> list[Runner]
    def is_connected(self, runner_id: str) -> bool
    def machine(self, runner_id: str) -> RunnerStateMachine | None
    async def snapshot(self, db) -> list[dict]     # the API/UI read model
    async def drain(self, reason: str) -> None
    async def reset(self) -> None                  # test-mode hook (R6)
```

`transition()` does, in order: machine `transition_to` (raises
`InvalidRunnerTransitionError` on an illegal move - a LOUD protocol error, never a silent DB
write) -> DB `UPDATE runners` -> `manager.send_runner_status(...)` UI broadcast. That is how
the already-live state machine becomes load-bearing instead of decorative, and it is
directly the fix for failure_01's "DB status never leaves idle".

`connect()`:
1. Take the per-runner lock.
2. If `_connections` already holds this `runner_id` -> raise `DuplicateRunnerConnection`
   (endpoint closes 4004). The incumbent is untouched.
3. Upsert the `Runner` row; stamp a fresh `websocket_id = uuid4()`, `connected_at`,
   `last_heartbeat = utcnow()`, `protocol_version`, `agent_version`, normalized `labels`.
4. Build the machine at `DISCONNECTED` and walk it `-> CONNECTING -> IDLE`. **Never
   construct it directly at IDLE** (failure_01 did, so the state machine's own history was
   a lie and reconnect could never see in-flight work).
5. Wake the dispatcher.

`find_available()` returns ALL matching idle+connected runners (the dispatcher picks and
does the CAS), never a single pre-selected one - failure_01's `find_idle_runner` was a TOCTOU
by construction.

Startup and shutdown are symmetric: the lifespan calls `registry.reset()`-equivalent
bootstrapping that marks every row `disconnected` with `websocket_id = NULL` before any
socket can connect, and `registry.drain()` on shutdown.

### 2.4 The `requires:` grammar (greenfield, one parser)

```yaml
steps:
  - id: "flash-firmware"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires:
        runner_id: "pi-workshop-1"      # optional: pin to one runner
        runner_type: "generic"          # optional: "any" matches everything
        arch: arm64                     # normalized both sides
        has: [gpio, camera]             # subset containment
        zone: "workshop"                # any other key: equality against labels
      command: "python3 flash.py"
```

`ExecutionRouter.parse_requirements(step_config) -> dict` is the ONE parser. A top-level
`runner_type:` on a **script/docker** step is sugar for `requires.runner_type`. A top-level
`runner_type:` on an **agent** step keeps its 12.5 meaning (it names the AI flavor) and does
NOT route remote - only an explicit `requires:` block does. That asymmetry already exists in
the router and is preserved deliberately; it is now documented rather than accidental.

`ExecutionRouter.decide` (12.6 shape), in order:

1. `executor` override: `"legacy"` unchanged until the deletion commit; `"remote"` is now
   valid and returns `RoutingDecision("remote", "explicit-override", requirements)`; any
   other value raises.
2. `requires:` present (any step type) -> `RoutingDecision("remote", "runner-pin",
   parse_requirements(...))`. **This replaces the `pin-not-honorable-local-until-12.6`
   branch entirely** - that reason string and its WARNING are deleted in this phase.
3. `agent` -> `("local", "agent-default-local")`, unchanged.
4. `script` / `docker` -> `("local", f"{step_type}-default-local")`, unchanged.
5. Unknown step type -> legacy with a WARNING until the deletion commit; **raises
   ValueError** in it (no legacy left to fall back to).

`RoutingDecision` gains `requirements: dict = field(default_factory=dict)`.
`pipeline_executor._decide_route` drops the `RuntimeError` guard and accepts
`ExecutorMode.REMOTE`.

### 2.5 The dispatcher and the double-assign guard

failure_01's fatal gap was that requeued PENDING steps had no dispatcher. 12.6 ships one:
`backend/app/services/execution/runner_dispatcher.py`, a single asyncio task started in
lifespan. **Event-driven, not polling**: an `asyncio.Event` is set by (a) RemoteExecutor
when a step needs a runner, (b) the registry when a runner reaches IDLE, (c) JobRecovery
after any requeue, (d) a `DISPATCH_SWEEP_INTERVAL = 15s` safety tick so a missed wake
self-heals rather than stranding a pipeline.

Loop body: select `StepExecution` rows with `status == 'pending'` and non-null
`runner_requirements`, oldest first; for each, `registry.find_available(...)`; assign.

**Assignment is a compare-and-swap, and the DB is the arbiter.** Both statements run in one
transaction:

```python
step_res = await db.execute(
    update(StepExecution)
    .where(StepExecution.id == step_id,
           StepExecution.status == StepExecutionStatus.PENDING.value,
           StepExecution.runner_id.is_(None))
    .values(status=StepExecutionStatus.ASSIGNED.value,
            runner_id=runner_id, assigned_at=utcnow())
)
if step_res.rowcount != 1:
    await db.rollback(); continue          # someone else took it

run_res = await db.execute(
    update(Runner)
    .where(Runner.id == runner_id,
           Runner.status == RunnerState.IDLE.value,
           Runner.current_step_execution_id.is_(None))
    .values(status=RunnerState.ASSIGNED.value, current_step_execution_id=step_id)
)
if run_res.rowcount != 1:
    await db.rollback(); continue          # runner was taken; step stays pending
await db.commit()
```

Only after the commit does the registry transition the machine and send `execute_step`.
An in-process `asyncio.Lock` keeps one backend from racing itself; the CAS is what makes it
correct across restarts and (later) processes.

**The split-brain fence.** Every step-scoped inbound message (`ack`, `log`,
`step_complete`) is dropped with a WARN unless BOTH hold:
`step_execution.runner_id == connection.runner_id` and
`runner.websocket_id == connection.websocket_id`. A late ACK from a runner already declared
dead, a `step_complete` from a connection that was superseded, a `log` for a step that moved
on - all become inert facts instead of corrupting state. This one rule closes the
reconnect-vs-reassign race the audit names.

### 2.6 Recovery (`backend/app/services/execution/job_recovery.py`)

Singleton `get_job_recovery_service()` returning a process-wide `JobRecoveryService`.
Row loads are `db.execute(select(...))` + `.scalar_one_or_none()` (single) or
`.scalars().all()` (sweep) - forced by the mocks (section 0).

| Method | Behavior |
|---|---|
| `async on_runner_death(db, runner)` | set `runner.status = "dead"`. If `runner.current_step_execution_id` is falsy: commit and RETURN before any query. Else load the StepExecution; if its status is in `{assigned, preparing, running, completing}` set `status="pending"`, `runner_id=None`; clear `runner.current_step_execution_id`; commit; wake the dispatcher. A terminal step is left untouched. |
| `async on_runner_disconnect(db, runner)` | identical requeue, `runner.status = "disconnected"`. An idle/step-less runner returns **before touching `db.execute` at all** (pinned by `test_on_runner_disconnect_idle_runner_no_requeue`). |
| `async on_runner_reconnect(db, runner) -> dict` | no `current_step_execution_id` -> `{"action": "idle"}` (no query). Step row missing -> clear the reference, `{"action": "idle"}`. `step.runner_id == runner.id` -> `{"action": "continue", "step_id": ...}`. Anything else, **including `runner_id is None`** -> clear `runner.current_step_execution_id` and `{"action": "abort", "step_id": ...}`. |
| `async recover_orphaned_steps(db) -> list[StepExecution]` | `select(StepExecution).outerjoin(Runner)` where `StepExecution.status in (assigned, preparing, running, completing)` AND (`Runner.status in ('disconnected','dead')` OR `Runner.id IS NULL`); each -> `pending`, `runner_id=None`; commit once; return the list. Re-running over already-pending rows is a no-op that still returns them. |

`runner_id is None` mapping to **abort** is a deliberate fail-safe choice: the step was
requeued and the dispatcher may already be handing it elsewhere, so the returning runner's
in-flight container must die rather than race. It costs one re-execution and buys the
invariant "a step is executing on at most one runner".

The `resume` field on `register` is what makes this speakable on the wire (failure_01
designed the reconnect protocol and never gave it a message): the agent reports the step it
still holds, the backend's answer rides on `registered.resume_action`, and an `abort` is
followed immediately by `cancel_step{reason: "reassigned"}`.

**`recover_orphaned_executions` (startup) is split.** Local-path executions (StepExecution
with `runner_id IS NULL`) keep failing as they do today - their container died with the
backend. Remote executions (`runner_id IS NOT NULL`) go through
`recover_orphaned_steps` and return to `pending` for the dispatcher, because a runner
genuinely can reconnect and the step genuinely can be reassigned. The docstring's own
flagged divergence is closed here. A remote step whose PipelineRun no longer has a live
executor generator is still failed by the run-level orphan sweep - it is requeued at the
StepExecution layer and then reaped at the run layer, so nothing hangs.

### 2.7 The death monitor

One task, `DEATH_MONITOR_INTERVAL = 5`, iterating **every** machine - IDLE included. That
is exactly why `runner_state.py` carries `IDLE -> DEAD`: a runner that connects and then
silently vanishes must not sit `idle` forever collecting assignments (failure_01 checked
only ASSIGNED/BUSY). `machine.is_alive(DEATH_TIMEOUT)` false -> `transition(DEAD)` ->
`on_runner_death` -> close the socket. The close handler tolerates `DEAD -> DISCONNECTED`
because `runner_state.py` already allows it; the endpoint's `finally:` must never raise
(failure_01 threw `InvalidRunnerTransitionError` out of a `finally`, skipping both the DB
update and the requeue).

---

## 3. REMOTE EXECUTOR

### 3.1 The same contract, so dispatch does not special-case it

`backend/app/services/execution/remote_executor.py`:

```python
class RemoteExecutor:
    async def execute_step(
        self, step_config: dict, execution_context: dict
    ) -> AsyncGenerator[dict, None]:
        """Byte-for-byte the LocalExecutor event contract.

        Yields, in this order:
            {"type": "status", "status": "preparing"}
            {"type": "status", "status": "running"}            # on ACK
            {"type": "log",    "line": "..."}                  # runner-origin, control mode drops
            {"type": "status", "status": <final>}
            {"type": "result", "status": ..., "exit_code": ..., [error], [timeout_seconds]}

        By the time the result event is yielded the assignment is closed out:
        the runner is IDLE (or dead), the StepExecution is terminal, and no
        further wire message for this step_id will be honored.
        """
```

Same three type strings, same ordering, same optional result keys, same "the step is fully
torn down before `result`" invariant. `pipeline_executor._run_local_step` is generalized to
`_run_executor_step(mode, ...)` selecting the executor instance; `_consume_local_events`,
`_finish_local_step` and the whole control-mode reconciliation are **unchanged**. That is
the test of this design: if either had to learn what "remote" is, the contract was not met.

`execution_context` gains `runner_id` (filled at assignment) and the StepRun already records
`executor = "remote"` from `_decide_route` (R1 observability).

`RemoteExecutor` does NOT get an idempotency cache. LocalExecutor's
`_completed_executions` dict guards against a re-driven generator in one process; remote
idempotency lives where it belongs - the `execution_key` de-dupe on the AGENT (section 4.4)
and the CAS in the DB.

### 3.2 `execute_step.config` - the real schema

Produced ONLY by `runner_protocol.build_execute_step_config`. `step_config` and
`agent_config` are the verbatim output of `control_layer.workspace.generate_step_config` /
`generate_agent_config` - the 12.3 and 12.5 producers stay single-sourced (R3); only the
delivery changes.

```json
{
  "protocol_version": 1,
  "backend_url": "http://backend:8000",
  "workspace": {
    "volume":     "lazyaf-ws-9f2a11c4-...",
    "retain_key": "9f2a11c4-...",
    "mount_path": "/workspace",
    "repo_id":    "r9f8...",
    "clone_url":  "http://backend:8000/git/r9f8....git",
    "branch":     "main",
    "commit_sha": "2a513dd4..."
  },
  "container": {
    "image":        "lazyaf-test-runner:dev",
    "command":      null,
    "working_dir":  "/workspace/repo",
    "timeout":      1800,
    "memory_limit": null,
    "mounts":       [],
    "environment": {
      "HOME": "/workspace/home",
      "LAZYAF_PIPELINE_RUN_ID": "...", "LAZYAF_STEP_RUN_ID": "...",
      "LAZYAF_STEP_INDEX": "3", "LAZYAF_EXECUTION_KEY": "run:3:step",
      "LAZYAF_BACKEND_URL": "http://backend:8000", "LAZYAF_CONTROL": "1",
      "LAZYAF_USAGE_PROVIDER": "self-hosted",
      "CONFIG_PATH": "/workspace/.control/0f1c9d5e-....json"
    },
    "control_mode": true
  },
  "control_files": {
    "/workspace/.control/0f1c9d5e-....json":       { "...generate_step_config output..." },
    "/workspace/.control/agent.0f1c9d5e-....json": { "...generate_agent_config output, or absent..." }
  }
}
```

- `container.environment` is **exactly LocalExecutor's non-secret env table**. Secrets and
  the step JWT live only inside `control_files`, which the agent writes into the volume via
  put_archive - never into container env. Same rule, same test shape, on both hosts.
- `command` is `null` in control mode; the runtime reads it from the config file.
- `mounts` carries explicit `addressing: "volume" | "bind"` (R6 - never inferred from path
  shape). A remote runner REJECTS any `bind` mount not on its own allowlist and fails the
  step with a clear message: a backend must not be able to bind arbitrary host paths on a
  machine it does not own.

### 3.3 Assignment, ACK, and the attempt budget

```
attempt = 0
while attempt < MAX_ASSIGN_ATTEMPTS:
    runner = await dispatcher.claim(db, step_execution, requirements)   # CAS, section 2.5
        # blocks on the dispatcher's event until a match exists, or
        # NO_RUNNER_TIMEOUT (300s) elapses -> result{status:"failed",
        # error:"no runner matched {requirements}; connected runners: [...]"}
    await registry.send(runner.id, ExecuteStepMessage(step_id, execution_key, config))
    try:
        await asyncio.wait_for(ack_future, timeout=ACK_TIMEOUT)
    except asyncio.TimeoutError:
        await registry.transition(db, runner.id, RunnerState.DEAD, reason="ACK timeout")
        await recovery.on_runner_death(db, runner)     # step -> pending
        attempt += 1
        continue
    yield {"type": "status", "status": "running"}
    outcome = await self._await_terminal(step_execution)   # step_complete | death | cancel
    if outcome.requeued:                                   # runner died mid-step
        attempt += 1
        continue
    return outcome
# budget exhausted
yield result{status:"failed", error:"step was requeued 3 times; last runner: <id>"}
```

The ACK future is keyed by `(step_id, runner_id)`, **not** `step_id` alone - failure_01
keyed on `step_id` so two assignments of the same step collided on one future.

`NO_RUNNER_TIMEOUT` and `MAX_ASSIGN_ATTEMPTS` are both load-bearing: without them a typo'd
`requires:` hangs a pipeline until someone notices, which is indistinguishable from a hung
backend.

The generator **owns the step until terminal**, exactly as LocalExecutor owns the container
until `wait()` returns. A mid-step death does not yield a result; it re-dispatches. That is
what lets `pipeline_executor` stay ignorant of remoteness.

### 3.4 Workspace provisioning on a remote host

A remote host cannot see the backend's `lazyaf-workspaces` volume, so the AGENT provisions
its own from `config.workspace`:

1. Get-or-create the named volume `workspace.volume` on the agent's own daemon.
2. If `/workspace/repo` is absent, clone `workspace.clone_url` at `branch`, then check out
   `commit_sha` detached when given - the same script `population._build_clone_script`
   produces, reimplemented in ~60 lines in `lazyaf_runner/workspace.py`. The backend's
   `population.py` stays backend-only; a shared package would drag `backend/app` onto every
   runner host for one shell string.
3. Keyed by `workspace.retain_key` (= `pipeline_run_id`), so every step of a run reuses one
   volume and `HOME=/workspace/home` persistence works remotely exactly as locally.
4. Reaped on `cleanup_workspace{retain_key}` (sent by `_cleanup_pipeline_workspace` at run
   completion), on `drain`, and by an idle reaper at `WORKSPACE_IDLE_REAP_SECONDS = 3600`
   as the backstop for a backend that never sent the message.

**`clone_url` and `backend_url` are the single most likely remote deployment failure.**
`settings.container_backend_url` is `http://backend:8000` - meaningless off the compose
network. The agent therefore applies `LAZYAF_STEP_BACKEND_URL` (and
`LAZYAF_GIT_URL_TEMPLATE`) as overrides to BOTH before use, and logs the resolved values on
its first `[runner]` line of every step. One grep answers "why can't the step reach the
backend".

### 3.5 Cancellation

`RemoteExecutor.cancel_step(execution_key)` mirrors LocalExecutor's: look up the assignment,
send `cancel_step{step_id, reason}`, and wait up to `DRAIN_GRACE` for the runner's
`step_complete`. If none arrives, force the StepExecution terminal and let the death monitor
reap the runner. `cancel_all()` iterates and never raises. Cancel is also the abort half of
the reconnect protocol (section 2.6).

---

## 4. THE RUNNER AGENT

`runner-agent/` at repo root, package `lazyaf_runner`, console script `lazyaf-runner`.

### 4.1 Layout

```
runner-agent/
  README.md                     <- failure_01's pyproject declared one and shipped none,
  pyproject.toml                   so `pip install .` failed at metadata generation
  Dockerfile
  lazyaf_runner/
    __init__.py
    __main__.py                 # python -m lazyaf_runner
    cli.py                      # argparse over config.py
    config.py                   # RunnerConfig
    types.py                    # StepAssignment / StepOutcome  - NO docker imports
    client.py                   # connect / register / reconnect / heartbeat
    session.py                  # message dispatch, ACK, outbound queue, cancel
    workspace.py                # volume get-or-create + clone (section 3.4)
    orchestrator/
      base.py                   # StepOrchestrator ABC - NO docker imports
      docker_orch.py            # DockerOrchestrator (the only one in 12.6)
      registry.py               # ORCHESTRATORS = {"docker": DockerOrchestrator}
  tests/                        # failure_01 shipped zero
```

### 4.2 The pluggable, Docker-agnostic executor seam

```python
# lazyaf_runner/orchestrator/base.py  - imports: abc, asyncio, typing, ..types. Nothing else.
class StepOrchestrator(ABC):
    name: str

    @abstractmethod
    async def preflight(self) -> None:
        """Raise OrchestratorUnavailable with an actionable message if this host
        cannot execute steps (no daemon, no socket, no permissions)."""

    @abstractmethod
    def capabilities(self) -> dict:
        """Merged into register labels, e.g. {"orchestrator": "docker", "has": ["docker"]}."""

    @abstractmethod
    async def run_step(
        self,
        assignment: StepAssignment,
        *,
        on_log: Callable[[list[str]], None],
        cancel: asyncio.Event,
    ) -> StepOutcome: ...

    @abstractmethod
    async def cleanup_workspace(self, retain_key: str) -> None: ...
```

`StepAssignment` and `StepOutcome` are plain dataclasses in `types.py`. **`base.py` and
`types.py` import nothing from `docker`, and a unit test asserts that** by parsing their ASTs
for any `docker` import. That is the concrete, checkable form of "NativeOrchestrator is
deferred but not precluded".

How a socketless runpod-style pod slots in with no protocol change: it registers with
`capabilities() -> {"orchestrator": "native", "has": []}`, so a step carrying
`requires: {has: [docker]}` simply never matches it, while a step that needs only a shell
and a model endpoint does. The routing grammar (section 2.4) already expresses that; the
protocol never learns what an orchestrator is.

`DockerOrchestrator` reproduces LocalExecutor's control-mode sequence -
`create -> put_archive(control_files) -> start` - reusing `local_executor`'s pure helpers by
**copying** `build_control_archive`'s ~30 lines into `lazyaf_runner`, pinned by a shared
contract test that tars the same input on both sides and asserts byte equality. A runner
host must not need `backend/app` on its PYTHONPATH; a contract test is the cheaper R3
instrument here than a shared package. Explicitly NOT reproduced from failure_01:
`network_mode="host"` (the agent attaches to a configured network,
`LAZYAF_STEP_NETWORK`, default `bridge`) and the broken `list(coroutine)` log reader.

### 4.3 Connection lifecycle

- `run()` loops `connect -> serve`, reconnecting with **exponential backoff and full jitter**:
  `delay = random.uniform(0, min(30, 2 ** attempt))`, reset on a successful `registered`.
  failure_01's fixed 5s retry is a reconnect storm waiting for a backend restart with N
  runners attached.
- `_connect_and_serve` sets `Authorization: Bearer <token>` on the handshake and applies a
  **timeout to the `registered` wait** (`REGISTRATION_TIMEOUT`) - failure_01 blocked forever
  on `ws.recv()`.
- `error{fatal: true}` (auth, protocol version) stops the loop and exits non-zero with the
  server's message. Only NON-fatal errors are retried. failure_01 reconnect-looped on a
  permanently-invalid registration every 5s forever.
- Heartbeat is its own task, started after `registered`, sending immediately and then every
  `registered.heartbeat_interval`. It runs during step execution.
- **Step execution runs in `asyncio.create_task`, never inline in the receive loop**
  (failure_01's defect). The receive loop keeps serving `cancel_step`, `drain`, `ping`.
- `MAX_CONCURRENT_STEPS = 1` in 12.6 - the state machine has no two-step state. A second
  `execute_step` while busy is answered `error{code:"busy"}` and NOT acked, so the backend's
  ACK timeout reassigns it cleanly.
- The agent refuses to send `register` over a non-loopback `ws://` unless
  `LAZYAF_RUNNER_ALLOW_INSECURE=1`. The `execute_step` frame carries the step JWT and
  `secret_environment`; plaintext across a real network is not a default worth having.
- The agent never logs `config`. It logs `sorted(config.keys())`, the image, the workspace
  volume and the resolved backend URL. A unit test feeds a config containing a sentinel
  secret and asserts the sentinel appears in no emitted log line and in no `log` frame.

### 4.4 Idempotency and abort

The agent keeps a bounded LRU of the last 32 `execution_key -> StepOutcome` results. An
`execute_step` whose `execution_key` it has already completed is ACKed and answered with the
CACHED `step_complete` instead of re-running. That, plus the backend's step gate, is what
makes reconnect-after-reassign safe from both ends.

`cancel_step` sets the cancel event; the orchestrator kills the container and the agent
sends `step_complete{exit_code: 143, error: "cancelled: <reason>"}`. The backend drops it if
the step no longer belongs to this runner - the cancel path needs no special reply handling.

### 4.5 Config surface (env, with CLI overrides)

| Env | CLI | Default | Meaning |
|---|---|---|---|
| `LAZYAF_BACKEND_URL` | `--backend-url` | `http://localhost:8000` | ws URL = scheme swap + `/ws/runner` |
| `LAZYAF_RUNNER_ID` | `--runner-id` | `f"{socket.gethostname()}-{orchestrator}"` | **stable across restarts** (failure_01 minted a fresh uuid4 per process, so every restart orphaned a row) |
| `LAZYAF_RUNNER_NAME` | `--name` | = runner id | |
| `LAZYAF_RUNNER_TYPE` | `--type` | `generic` | |
| `LAZYAF_RUNNER_LABELS` | `--labels` | `""` | `arch=arm64,has=gpio,has=camera` - a repeated key becomes a list |
| `LAZYAF_ORCHESTRATOR` | `--orchestrator` | `docker` | key into `ORCHESTRATORS` |
| `LAZYAF_RUNNER_TOKEN` | `--token` | dev constant | shared enrollment secret |
| `LAZYAF_STEP_BACKEND_URL` | `--step-backend-url` | unset | overrides `config.backend_url` for step containers |
| `LAZYAF_GIT_URL_TEMPLATE` | - | unset | overrides `workspace.clone_url` |
| `LAZYAF_STEP_NETWORK` | - | `bridge` | network for spawned step containers |
| `LAZYAF_RUNNER_ALLOW_INSECURE` | - | `0` | permit `ws://` to a non-loopback host |
| `LAZYAF_BIND_ALLOWLIST` | - | `""` | host paths this runner will bind-mount |

`arch` is NOT configured: the agent always reports `platform.machine()` raw and the backend
normalizes (section 1.8). `heartbeat_interval` / `death_timeout` are NOT configurable -
they arrive in `registered`.

---

## 5. LOOPBACK LANE AND THE DOGFOOD RATCHET (R7)

Remote hardware is manual (owner decision, 2026-08-29). The TESTED path is a `lazyaf_runner`
process on the same host speaking real WebSocket to the real backend. Nothing about it is a
simulation: same protocol, same auth, same registry, same CAS, same control-mode container.
The only thing loopback does not exercise is physical network latency, and that is what the
risk register (section 7) is for.

### 5.1 Three lanes

**T1 (no Docker).** Protocol dataclasses, validate/parse/create, `matches_requirements`,
`normalize_arch`, `parse_requirements`, the recovery service against mocks (the dormant
suite), the registry against a real WS manager with a capturing transport (R6 - never an
AsyncMock), dispatcher CAS against a real SQLite session with two concurrent claims.

**T2 (Docker-real).** `tdd/integration/execution/test_loopback_runner.py`: spawn
`python -m lazyaf_runner` as a subprocess against the test backend, poll `GET /api/runners`
until it reports `idle`, dispatch a script step with
`requires: {runner_id: "loopback-test"}` on a **named volume** (R6), and assert:
the step ran, `StepRun.executor == "remote"`, `StepExecution.runner_id` is the loopback
runner, logs arrived via `POST /api/steps/{id}/logs` (control mode, so the container wrote
them), and at least one `[runner]`-prefixed line exists.
Plus `test_loopback_failover.py`: two agents, kill the busy one with SIGKILL, assert the step
is requeued and completed by the other within `DEATH_TIMEOUT + DISPATCH_SWEEP_INTERVAL`.

**Dogfood (R7).** A `runner-agent` compose service on `lazyaf-network` with the Docker socket
mounted (the same deliberate DooD tradeoff the T2 tier step already carries), labels
`{"arch": "amd64", "has": ["docker"], "lane": "dogfood"}`, and `LAZYAF_RUNNER_ID=dogfood-loopback`.

### 5.2 What `.lazyaf/pipelines/test-suite.yaml` gains

```yaml
  # 12.6 remote lane (R7): this step is pinned to the loopback runner-agent by a
  # label only that agent carries. If the agent is down, or the WS protocol,
  # registry, CAS, or workspace provisioning break, this step FAILS THE PUSH.
  # Remote execution is continuously covered, not covered once at the exit gate.
  - id: "remote-probe"
    name: "Remote lane: script step via the loopback runner agent"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires:
        has: ["remote-lane"]
      command: |
        echo "[remote-probe] executed on $(hostname)"
        ls /workspace/repo/PLAN.md
    on_success: next
    on_failure: stop
    timeout: 300
```

The `ls /workspace/repo/PLAN.md` is not decoration: it proves the AGENT populated its own
workspace volume by cloning from the backend's git server, which is the single piece of
remote execution with no local analogue.

**The `mock-agent` step MOVES to the remote lane** in the same commit (add
`requires: {has: ["remote-lane"]}`). That satisfies 12.5's stated precondition "US-2 e2e
green on the remote path, not just local" on every push, at zero cost, while
`tdd/e2e/test_us2_card_loop.py` keeps US-2 covered on the LOCAL path in T3. Both paths, both
continuously.

### 5.3 What `scripts/verify_executor.py` asserts (12.6 edition)

Stdlib-only, still on `lazyaf-base:dev` with `control: false` - the gate must not depend on
the runtime it verifies. Existing assertions 1-6 keep their numbering. New:

8. The `remote-probe` StepRun has `executor == "remote"` and delivered non-marker logs
   (control layer worked from the remote lane).
9. `GET /api/runners` reports at least one runner with `status in {"idle","busy"}` and
   `connection == "websocket"`; a vacuous pass (no runners at all) is a failure.
10. That step's `StepExecution` has a non-null `runner_id` matching a runner in the snapshot.
11. Every step NOT carrying `requires:` still has `executor == "local"` - a global accidental
    flip to remote is as much a regression as a fallback to legacy.
12. The `mock-agent` step has `executor == "remote"` AND a `StepUsage` row with non-null
    tokens - the 12.5 usage channel survives the trip to another host.

Assertion 7 (`GET /api/runners/status` reports `queued_jobs == 0`) is **deleted in the
deletion commit**, in the same commit that deletes `job_queue`, and replaced by assertion 9.
Leaving a gate assertion pointing at a removed subsystem is how a gate rots.

`tdd/tier_floors.json`: T1 rises by the 74 waking dormant tests plus this phase's new units;
T2 by the loopback suite; T3 unchanged. **Re-measure after the wave, do not guess a number**,
and write the reason into the `note` field as every prior raise did.
`tdd/skip_baseline.json`'s `"12.6-dormant:"` entry is **removed in the deletion commit** -
by then the modules exist, the suite runs, and a baseline entry that can no longer match is a
lie the gate would happily accept forever.

### 5.4 The UI (R8)

`stores/runners.ts` is rebuilt as **snapshot fetch + WS deltas**: `GET /api/runners` once on
mount, then a `Map<string, Runner>` updated from the existing `runner_status` frame (already
in `ServerMessageType` and `HANDLED_MESSAGE_TYPES` - no new frame type, no drift-guard
change). The 2000ms polling intervals are deleted. This is exactly the audit's verdict on
failure_01's store: right pattern, fatal omission (it deleted the HTTP path and showed an
empty panel on reload).

`RunnerPanel.svelte` loses the docker-command modal and the log-polling modal (both are
polling-stack artifacts) and gains labels, state, current step, and connection age.

`frontend/e2e/runners.spec.ts` (R8, workers=1 real tier): load the page with the loopback
agent connected, assert the runner appears from the SNAPSHOT; then dispatch a pinned step and
assert the row transitions idle -> assigned -> busy -> idle from WS DELTAS; then **reload the
page mid-step and assert the panel is populated immediately** - the snapshot-then-delta
assertion PLAN names.

---

## 6. THE DELETION COMMIT (R2)

Its own commit. Nothing else in it. It contains deletions plus the consumer migrations that
those deletions force, and nothing that could have landed earlier.

### 6.1 Preconditions - all TRUE and asserted before the commit is written

1. The dormant suite runs at **zero skips**. `pytest tdd/unit/execution -rs` reports no
   `12.6-dormant:` reason.
2. The dogfood pipeline is green through the loopback runner agent, with
   `remote-probe.executor == "remote"` asserted by `verify_executor.py` via the API, on a
   real push.
3. `tdd/unit/services/test_no_legacy_enqueue.py` still green (card start, card retry,
   playground start, agent pipeline step -> zero `job_queue.enqueue` calls).
4. US-2 green on the remote path (the migrated `mock-agent` step) AND on the local path
   (`tdd/e2e/test_us2_card_loop.py`).
5. `test_loopback_failover.py` green - a runner can die mid-step and the work still lands.
6. T1/T2/T3 floors re-measured and raised.

### 6.2 Inventory removed

| Removed | Because |
|---|---|
| `backend/app/services/runner_pool.py` (+ its tests) | replaced by `runner_registry.py` |
| `backend/app/services/job_queue.py`, `QueuedJob` | last `enqueue` call site dies with the legacy branch |
| `backend/app/routers/runners.py` polling surface: `POST /register`, `POST /{id}/heartbeat`, `GET /{id}/job`, `POST /{id}/complete`, `POST /{id}/logs`, `GET /{id}/logs`, `DELETE /{id}`, `POST /clear-queue`, `GET /docker-command` | the runners API becomes read-only over the registry |
| `runner-claude/`, `runner-gemini/`, `runner-mock/` (Dockerfiles + the three ~1100-1500 line polling entrypoints) | nothing dispatches to them |
| `runner_common/entrypoint.py`, `job_helpers.py`, `context_helpers.py` (incl. `build_prompt`, superseded by `agent_prompt.py`) | the monolith around the executors; the wrapper is the surviving surface |
| `ExecutionRouter`'s `executor: legacy` branch, `_VALID_EXECUTOR_OVERRIDES` legacy entry, `ExecutorMode.LEGACY` | no legacy path remains |
| `pipeline_executor._enqueue_legacy_step` and its two call sites | ditto |
| `runner-claude` / `runner-gemini` / `runner-mock` / `runner-mock-e2e` services in `docker-compose.yml` | replaced by `runner-agent` (dev + e2e profiles) |
| `is_playground` / `playground_session_id` / `playground_save_branch` / `required_runner_id` on the job wire, `RunnerRead`/`PoolStatus` polling schemas | last consumers deleted |
| `verify_executor.py` assertion 7 (`queued_jobs == 0`) | its subsystem is gone (replaced by assertion 9) |
| `tdd/skip_baseline.json` `"12.6-dormant:"` entry | can no longer match |
| `backend/alembic/versions/0007_drop_polling_runner_columns.py` | ADDS the drop of `runners.container_id` / `runners.current_job_id` |

### 6.3 Migrated IN the same commit (nothing may lag)

- `frontend/src/lib/stores/runners.ts`, `RunnerPanel.svelte`, `api/client.ts` runner methods,
  `api/types.ts` (`Runner`, drop `PoolStatus`/`DockerCommand`) - plus `frontend/e2e/runners.spec.ts`.
- `backend/app/routers/jobs.py` reads only `Job` rows written by `agent_run`.
- `backend/app/routers/test_api.py`: the `job_queue` resettable is replaced by a
  `runner_registry` resettable (R6 - the reset endpoint must reset in-memory singletons).
- `backend/app/main.py` lifespan: `runner_pool.start()/stop()` -> registry bootstrap,
  dispatcher task, death monitor task, `registry.drain()`.
- `scripts/verify_executor.py`, `.lazyaf/pipelines/test-suite.yaml`, `docker-compose.yml`,
  `tdd/tier_floors.json`.
- **`runner-common` still installs into the agent images** after `entrypoint.py` /
  `job_helpers.py` / `context_helpers.py` are gone - asserted by a `build_images.py` rebuild
  in the same commit. The surviving surface is `agent_wrapper`, `agent_config`, `executors`,
  `usage`, `git_helpers`, `pytest_lazyaf`.

12.5 already left the runners idle on every default path with that idleness ASSERTED
(`test_no_legacy_enqueue` + `verify_executor` assertion 7), which is exactly what makes this
commit contain only deletions. That was the point of doing it there.

### 6.4 `tdd/unit/services/test_no_legacy_code.py` - designed against fake-green

failure_01's `test_polling_removal.py` imported `runner_pool`, was self-skipped the moment
`runner_pool` was deleted 3.5 hours later, and stayed green over a system that could no
longer execute agent steps at all. The replacement uses two mechanisms, **neither of which
can skip**:

```python
# NO importorskip. NO try/except ImportError. NO pytest.mark.skipif. Ever.

GONE = [
    "app.services.runner_pool",
    "app.services.job_queue",
    "runner_common.entrypoint",
    "runner_common.job_helpers",
    "runner_common.context_helpers",
]

@pytest.mark.parametrize("module", GONE)
def test_legacy_module_is_gone(module):
    """Re-adding any of these is a test failure, not a silent skip."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


FORBIDDEN = ["runner_pool", "job_queue", "QueuedJob", "ExecutorMode.LEGACY",
             "runner-claude", "runner-gemini", "runner-mock",
             "LAZYAF_USE_LOCAL_EXECUTOR", "/api/runners/register", "is_playground"]
SEARCH_ROOTS = ["backend/app", "frontend/src", "runner-common/runner_common",
                "scripts", ".lazyaf", "docker-compose.yml"]
ALLOWLIST = ["PLAN.md", "upcoming/", "historical-documents/"]

def test_no_forbidden_references():
    """A dangling reference the import test cannot see (a compose service, a
    frontend fetch, a yaml key) fails here with file:line."""
```

Two orthogonal failure modes: re-adding a module trips the import test; leaving a reference
trips the grep. Both are unconditional. The one legitimate mention of these names is prose,
and prose lives in the allowlist.

---

## 7. RISK REGISTER

### 7.1 Inherited seams (12.3/12.5 environment lessons, now on another host)

| Seam | Status on the remote path | What pins it |
|---|---|---|
| **uid ownership** (fresh named volumes are root-owned; put_archive files need uid 1000) | IDENTICAL and unavoidable - the agent creates the volume, so the FIRST step on a remote host always hits a virgin root-owned volume. The image's root `entrypoint.sh` chown + the `find /workspace/.control -name '*.json' -exec chown` already covers it | T2 loopback test asserts the control files land 1000:1000 and the step's effective uid is 1000. The agent NEVER chowns from outside the container - one owner of that fix, and it is the image |
| **socket groups across gosu** | Unchanged mechanism, now on a host whose `docker` GID may differ from the backend host's. `needs: [docker]` still triggers `usermod -aG` from the socket's actual GID at entrypoint, which is why that fix was written GID-derived rather than hardcoded | T2 loopback test runs a `needs: [docker]` step through the agent. A host whose GID differs is exactly the case the 12.3 fix already handles |
| **network reachability** | The BIGGEST new exposure. Three distinct hops now: agent->backend WS, step container->backend HTTP, step container->git server. On compose all three are `lazyaf-network`; on a real remote host all three need a routable URL | `LAZYAF_STEP_BACKEND_URL` + `LAZYAF_GIT_URL_TEMPLATE` overrides (section 3.4); the agent logs all three resolved URLs on its first `[runner]` line per step; a clone failure produces a `[runner] ERROR` line with the helper's log tail, so the failure is readable without host access |
| **timeouts under DooD** | Amplified: image pull on a cold remote host can exceed the whole step timeout. Ownership is unchanged - ONE timeout owner in-container, executor backstop `+CONTROL_MODE_TIMEOUT_GRACE` | The agent's docker client uses an explicit long timeout (the 12.3 lesson), and **pull time is excluded from the step deadline**: the agent starts the container's clock at `start()`, not at `run_step()`. Pull progress is emitted as `[runner]` lines so a slow pull looks slow rather than hung |
| **tree-hash determinism** | Unaffected - `build_images.py` is unchanged and the agent pulls or builds nothing. A remote host must have the `lazyaf-*:dev` images; `find_missing_images` runs agent-side at preflight | The agent's `preflight()` reports missing images as a REGISTRATION-time WARNING in its labels (`has: ["images:stale"]`), and a step whose image is absent fails with `Image not found: <tag>` - the identical message the local path produces |
| **secret leakage** | NEW SHAPE: `secret_environment` and the step JWT now cross a network inside `execute_step.config` | `wss://` required off-loopback (`LAZYAF_RUNNER_ALLOW_INSECURE` to opt out, default off). File-only on the container side, unchanged. The agent never logs `config` (only `sorted(keys())`), pinned by a sentinel-secret test over every emitted log line and `log` frame. The backend logs config KEYS, never values. The step JWT TTL is still `timeout + 420`, so a captured frame expires with the step |

### 7.2 New seams this phase introduces

| Seam | The failure it produces | Mitigation, and where it is tested |
|---|---|---|
| **WS reconnect storms** | N runners, fixed retry delay, one backend restart -> a synchronized thundering herd that keeps the backend from finishing startup. failure_01 shipped a flat 5s retry | Exponential backoff with FULL jitter, 30s cap, reset on `registered`. `error{fatal:true}` (auth, version) exits instead of retrying, so a misconfigured fleet does not hammer forever. Unit test: 100 simulated agents produce reconnect delays whose spread exceeds a fixed-delay baseline; a fatal error yields exactly one attempt |
| **Split-brain assignment** | Two runners execute one step; two `step_complete` frames; the second corrupts a terminal StepRun. failure_01 had TOCTOU selection, an unwritten `current_step_execution_id`, and no gate | Three layers: (1) the DB CAS - assignment is `UPDATE ... WHERE status='pending' AND runner_id IS NULL` and `rowcount != 1` means someone else won; (2) the step gate - every step-scoped inbound message requires `step.runner_id == connection.runner_id` AND `runner.websocket_id == connection.websocket_id`; (3) duplicate connections for one `runner_id` are refused (4004). T1 test: two concurrent `claim()` calls on one pending step, exactly one succeeds. T2 test: reconnect-after-reassign gets `resume_action: "abort"` and its late `step_complete` changes no row |
| **Remote clock skew** | A runner with a clock hours off computes a deadline the backend disagrees with; a heartbeat "from the future" makes a dead runner immortal | **No timestamp from a runner is ever compared to a backend deadline.** `last_heartbeat` is stamped backend-side at receipt; every timeout is evaluated on backend time; the runner learns its intervals from `registered` rather than configuring them. Runner-supplied timestamps, if ever added, are forensic-only. Unit test: a heartbeat processed while the machine's clock is monkeypatched forward does not extend the death deadline |
| **Log ordering across a network** | Two streams now reach `StepRun.logs`: container logs over HTTP POST and `[runner]` lines over WS. Interleaved, they produce a log that reads as though events happened out of order | Structural, not best-effort: **the agent emits `[runner]` lines only BEFORE `container.start()` and AFTER the container exits.** The two streams cannot overlap in time, so append order is real order. A unit test drives a full `run_step` and asserts zero `on_log` calls between start and exit. WS is ordered per-connection; the optional `seq` field detects gaps across a reconnect and produces a visible `[runner] WARNING: log seq gap` rather than silent loss |
| **Backend restart mid-remote-step** | The `RemoteExecutor.execute_step` generator dies with the process while the container keeps running on the remote host | Symmetric with the local path, which has the same exposure and the same answer: the startup sweep. Remote StepExecutions return to `pending` (section 2.6) and the run-level orphan sweep fails runs whose generator is gone. The agent's `execution_key` LRU means a re-dispatch of the same key returns the cached outcome rather than running twice |
| **A `requires:` nobody can satisfy** | A typo silently hangs a pipeline forever - indistinguishable from a hung backend | `NO_RUNNER_TIMEOUT = 300` then a step failure whose message names the requirements AND the labels of every connected runner. `MAX_ASSIGN_ATTEMPTS = 3` bounds the requeue loop. Unit test asserts both messages |
| **Single-worker registry** | `_connections` is per-process; a multi-worker uvicorn would route assignments to a worker that holds no socket | Stated limit, not a hidden one: LazyAF runs single-worker today and `RunnerRegistry` documents it at the top. The seam is `registry.send()` - a future Redis/pubsub fan-out replaces one method. `main.py` gains a startup WARNING if `WEB_CONCURRENCY > 1` |

---

## 8. WAVE SPLIT - 5 agents, disjoint file ownership

Launch order: **A first and alone** (its protocol module wakes 55 dormant tests and every
other agent imports it). Then B, C, D in parallel. E starts with the frontend and the
`test_no_legacy_code` skeleton immediately and lands the ratchet once C and D are green.

### Agent A - protocol, model, migration, registry

**Owns exclusively:** `backend/app/services/execution/runner_protocol.py` (new),
`runner_token.py` (new), `runner_registry.py` (new), `backend/app/models/runner.py`
(rewrite), `backend/app/schemas/runner.py` (new), `backend/app/config.py` (adds
`runner_auth_secret` only), `backend/alembic/versions/0006_runner_registry.py` (new),
`tdd/unit/execution/test_runner_registry.py`, `tdd/unit/models/test_runner_model.py`.
Does NOT touch `runner_state.py` (already correct and already live).

**Test contract:**
1. The 55 dormant `test_websocket_protocol.py` tests pass **unmodified** - the acceptance
   bar for this agent's first commit. Verify with `-p no:cacheprovider -rs` showing zero
   `12.6-dormant:` skips for that module.
2. Defaulted-field parity: `RegisterMessage` builds from exactly the four documented kwargs;
   `validate_runner_message` returns `[]` for that payload; `to_dict()` round-trips through
   `parse_runner_message` with zero key loss including the new optional fields.
3. `create_backend_message` builds all eight backend types and raises
   `ValueError("Unknown message type: ...")` otherwise.
4. `normalize_arch` table (`x86_64`/`amd64`/`x64` -> `amd64`; `aarch64`/`arm64`/`armv8` ->
   `arm64`; `armv7l`/`armhf` -> `armv7`; passthrough lowercased).
5. `matches_requirements`: empty matches all; `runner_id`; `runner_type` with `"any"`
   wildcard; `arch` after normalization on both sides; `has` subset; **an unknown key is
   matched against labels, not ignored** (the explicit failure_01 regression).
6. Registry: `connect` walks DISCONNECTED->CONNECTING->IDLE (never constructs at IDLE);
   a duplicate `runner_id` raises `DuplicateRunnerConnection` and leaves the incumbent
   untouched; `transition` on an illegal move raises and writes NOTHING to the DB;
   every transition emits exactly one `runner_status` frame through the REAL WS manager
   with a capturing transport (R6).
7. Migration: `0006` upgrades from `0005`, is re-runnable, `status='offline'` becomes
   `'disconnected'`, and every row lands `websocket_id IS NULL`.
   `tdd/integration/test_migrations.py` parity.

### Agent B - RemoteExecutor, dispatcher, recovery, lifespan

**Owns exclusively:** `backend/app/services/execution/remote_executor.py` (new),
`runner_dispatcher.py` (new), `job_recovery.py` (new), `step_logs.py` (new),
`backend/app/services/execution/recovery.py` (the local/remote split),
the `/logs` handler delegation in `backend/app/routers/steps.py`,
`backend/app/main.py` (lifespan only),
`tdd/unit/execution/test_remote_executor_contract.py` (new),
`tdd/unit/execution/test_runner_dispatcher.py` (new),
`tdd/unit/services/test_step_logs.py` (new).

**Test contract:**
1. The 19 dormant `test_job_recovery.py` tests pass **unmodified**, including the
   `db.execute(...).scalar_one_or_none()` access pattern and the
   `mock_db.execute.assert_not_called()` early-return on an idle disconnect.
2. Executor contract parity, asserted mechanically: drive `RemoteExecutor.execute_step` and
   `LocalExecutor.execute_step` against a stub and assert the yielded event-type SEQUENCE
   and the result-dict KEY SET are identical. Written against the public API only - the
   audit's verdict on failure_01's contract tests was "right scenarios, wrong coupling
   (pokes private dicts, patches privates)".
3. Assignment CAS: two concurrent `claim()` calls on one pending step against a real session -
   exactly one wins, the loser leaves the step pending.
4. ACK timeout at 5s reassigns to a second runner; three failures fail the step with a
   message naming the last runner.
5. `NO_RUNNER_TIMEOUT` fails the step with a message naming the requirements and the
   connected runners' labels.
6. Death mid-step requeues and the SAME generator completes on a second runner without
   yielding an intermediate `result`.
7. `append_step_logs(source="runner")` prefixes `[runner] ` and publishes through the real
   WS manager; `source="container"` appends verbatim, preserving the `/logs` router's
   existing byte contract (asserted against the current router tests, which must still pass).
8. `recover_orphaned_executions` fails local (`runner_id IS NULL`) executions and requeues
   remote ones - one test per branch, no shared assertion.

### Agent C - WS endpoint, runners API, routing, dispatch

**Owns exclusively:** `backend/app/routers/ws_runners.py` (new),
`backend/app/routers/runners.py` (rewritten read-only surface),
`backend/app/services/workspace/execution_router.py`,
`backend/app/services/pipeline_executor.py` (`_decide_route`, `_run_executor_step`,
`_build_remote_execution_config`),
`backend/app/services/websocket.py` (registry broadcast helper only),
`tdd/unit/services/test_execution_router_requires.py` (new),
`tdd/unit/services/test_remote_step_dispatch.py` (new),
`tdd/integration/api/test_ws_runner_endpoint.py` (new).

**Test contract:**
1. Handshake matrix against a real `TestClient` WS: no token -> 4003; bad token -> 4003;
   no register in 10s -> `error` + 4000; malformed register -> `error` + 4001;
   `protocol_version: 2` -> `error` + 4002; duplicate `runner_id` -> 4004; a bad
   MID-SESSION message gets an `error` frame and the connection STAYS OPEN.
2. Per-message DB sessions - the endpoint holds NO session across the connection
   (the audit's explicit fix). Asserted by a test that keeps a socket open past the pool
   timeout and still serves messages.
3. Step gate: a `step_complete` for a step assigned to a different runner changes no row and
   logs a WARN; same for `log` and `ack`.
4. `decide()`: `requires:` on script/docker/agent -> `("remote", "runner-pin",
   parsed_requirements)`; the `pin-not-honorable-local-until-12.6` reason no longer exists
   anywhere; `runner_type` alone on an AGENT step still routes local; `executor: remote`
   override; unknown step type still legacy pre-deletion.
5. `parse_requirements` grammar table, including the top-level `runner_type` sugar on
   script/docker only.
6. `_decide_route` accepts `ExecutorMode.REMOTE` and `StepRun.executor == "remote"` is
   persisted (R1).
7. `build_execute_step_config` output contains the step JWT and `secret_environment` ONLY
   inside `control_files`, never inside `container.environment` - the remote twin of 12.5's
   secret-containment test.

### Agent D - the runner agent

**Owns exclusively:** the entire `runner-agent/` tree including `tests/` and `README.md`.
Imports nothing from `backend/app`.

**Test contract:**
1. `test_orchestrator_seam.py` - AST-parse `orchestrator/base.py` and `types.py` and assert
   no `docker` import appears in either. This is the checkable form of "NativeOrchestrator
   is not precluded".
2. `test_client_lifecycle.py` against a fake WS server: register-then-`registered`;
   `registered` timeout retries; `error{fatal:true}` exits without retrying; backoff is
   jittered and capped at 30s and resets after a successful registration; heartbeats keep
   flowing during a long step.
3. `test_session_concurrency.py` - `cancel_step` is handled WHILE a step runs (the receive
   loop is not blocked); a second `execute_step` while busy is answered `error{code:"busy"}`
   and is NOT acked.
4. `test_idempotency.py` - a repeated `execution_key` is acked and answered from the LRU
   without invoking the orchestrator.
5. `test_secret_hygiene.py` - a config carrying a sentinel secret produces that sentinel in
   no log line and no `log` frame.
6. `test_log_ordering.py` - `on_log` is called zero times between container start and exit.
7. `test_control_archive_parity.py` - the agent's tar builder and
   `local_executor.build_control_archive` produce byte-identical output for the same input
   (the R3 instrument for the one deliberate code copy).
8. `test_workspace.py` - get-or-create is idempotent; a populated volume is not re-cloned;
   `cleanup_workspace` removes only the named `retain_key`.

### Agent E - loopback lane, dogfood ratchet, frontend, deletion

**Owns exclusively:** `frontend/src/lib/stores/runners.ts`,
`frontend/src/lib/components/RunnerPanel.svelte`, `frontend/src/lib/api/client.ts` +
`types.ts` (runner surface only), `frontend/e2e/runners.spec.ts` (new),
`docker-compose.yml` (the `runner-agent` services), `.lazyaf/pipelines/test-suite.yaml`,
`scripts/verify_executor.py`, `tdd/tier_floors.json`, `tdd/skip_baseline.json`,
`tdd/integration/execution/test_loopback_runner.py` (new),
`tdd/integration/execution/test_loopback_failover.py` (new),
`tdd/unit/services/test_no_legacy_code.py` (new),
`tdd/unit/scripts/test_verify_executor.py`,
`backend/alembic/versions/0007_drop_polling_runner_columns.py` (deletion commit).

**Test contract:**
1. Loopback happy path (T2, named volume, real subprocess agent): step executes,
   `StepRun.executor == "remote"`, `StepExecution.runner_id` set, container logs arrived over
   `POST /api/steps/{id}/logs`, at least one `[runner]` line present, and
   `ls /workspace/repo/PLAN.md` succeeds (the agent cloned its own workspace).
2. Failover (T2): two agents, SIGKILL the busy one, the step completes on the other within
   `DEATH_TIMEOUT + DISPATCH_SWEEP_INTERVAL`, exactly one `StepRun` terminal row.
3. `verify_executor` unit tests for assertions 8-12 **including the negative case for each** -
   a run with a local `remote-probe`, a run with no connected runners, a run whose
   `mock-agent` lacks a `StepUsage` row must each FAIL the gate.
4. Playwright (R8, workers=1): snapshot on load; idle->assigned->busy->idle from WS deltas;
   **reload mid-step and the panel is populated immediately**.
5. Vitest: the runners store applies a `runner_status` delta for an unknown runner as an
   insert, for a known runner as an update, and a `disconnected` delta removes it.
6. `test_no_legacy_code` per section 6.4 - unconditional, two mechanisms, never skippable.

---

## 9. CROSS-AGENT CONTRACTS (pin these first; they are the only shared surfaces)

1. **The message catalogue** - `runner_protocol.py` section 1.2, including every defaulted
   optional field. Owner A. B, C and D consume it; **nobody but A adds a message type or a
   field**, and any addition requires a `PROTOCOL_VERSION` review in the same PR.
2. **`build_execute_step_config(step_config, exec_context, step_config_file,
   agent_config_file) -> dict`** - the sole producer of `execute_step.config` (section 3.2).
   Lives in `runner_protocol.py` (A), called only by `pipeline_executor` (C), consumed by
   `lazyaf_runner.session` (D). The round-trip test - backend produces, agent writes,
   `images/base/control/config.load_step_config` loads with zero key loss - is owned by C.
3. **The executor event contract** - three type strings, fixed order, the result-dict key
   set, and "the assignment is fully closed out before `result`". Owner B; the parity test
   against LocalExecutor is B's and is the gate on "dispatch does not special-case remote".
4. **`RunnerState` is the single status vocabulary** for the machine, the `runners.status`
   column, the API and the UI. `RunnerStatus` is deleted. Owner A; E mirrors it in
   `frontend/src/lib/api/types.ts`.
5. **The requirement grammar** - `runner_id | runner_type | arch | has | <label key>`, with
   `ExecutionRouter.parse_requirements` the only parser and `normalize_arch` applied
   backend-side to both labels and requirements. Parser owned by C, `normalize_arch` by A.
6. **`append_step_logs(db, execution, lines, source=)`** - the sole writer of
   `StepRun.logs` in control mode, for both the HTTP `/logs` router and the WS `log` frame.
   Owner B; C's `ws_runners.py` is its second caller. `source="runner"` prefixes `[runner] `.
7. **The step gate** - every step-scoped inbound message requires
   `step_execution.runner_id == connection.runner_id` AND
   `runner.websocket_id == connection.websocket_id`. Implemented once in C's endpoint,
   relied on by B's recovery and D's abort path.
8. **The CAS assignment statements** (section 2.5) - `rowcount != 1` is the only acceptable
   double-assign detection. Owner B; A's registry must never mutate `runners.status`
   outside `transition()`.
9. **`control_files` is the secret boundary.** Secrets and the step JWT appear only there,
   never in `container.environment`, never in a log line, never in a backend log statement.
   Producer C, carrier A, consumer D, asserted independently by C (test 7) and D (test 5).

---

## 10. Seams left open on purpose

- **`NativeOrchestrator` is not built.** The ABC, the registry, the capability-driven
  matching and the no-docker-import test are the guarantee that it can be added out-of-tree
  or in 12.7+ without touching the protocol. A socketless runpod pod registers with
  `{"orchestrator": "native", "has": []}` and simply never matches `requires: {has: [docker]}`.
- **Per-runner identity tokens.** `runner_token.py` ships with tests and no default-path
  caller; enabling it is a change to `authenticate_runner_connection` plus persisting the
  token returned in `registered`. The shared enrollment secret plus duplicate-connection
  rejection plus the step gate is the 12.6 security posture, stated rather than implied.
- **`MAX_CONCURRENT_STEPS = 1`.** `RunnerStateMachine` has no two-step state. Raising it
  means a per-assignment machine rather than a per-runner one - a real design change, not a
  constant bump.
- **Multi-worker backends.** `RunnerRegistry` is per-process. `registry.send()` is the one
  method a pubsub fan-out would replace; `main.py` warns on `WEB_CONCURRENCY > 1`.
- **`LAZYAF_GPU_NODE_ID` / `LAZYAF_GPU_FRACTION`** are read by `run.py` and priced by the
  server since 12.5 and still set by nothing. A real GPU node is where they get set:
  the agent will pass them through `container.environment` from its own env. That is a
  three-line change and deliberately not made against zero real hardware.
- **`container_seconds` as a true container lifetime.** 12.5 documented it as a lower bound
  measured from `run.py` start. A remote agent knows the real create-to-remove interval and
  could supply it; deferred until a node actually bills for it.
- **`PipelineRun.steps_snapshot`** (retiring ad-hoc Pipeline rows, fixing edited-mid-run)
  was parked as a 12.6/12.7 candidate in wave 4. It stays parked - it touches step loading
  for every run, and this phase is already moving where steps execute.
- **Debug re-run (12.7)** will want a breakpoint as a pre-step gate on the REMOTE path too.
  The seam is `RemoteExecutor.execute_step`'s `status: preparing` yield, the same place the
  local one gates.
