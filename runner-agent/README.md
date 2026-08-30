# lazyaf-runner

The LazyAF **remote runner agent** (Phase 12.6). A process you run on a machine
the LazyAF backend does not own. It connects out to the backend over a
WebSocket, registers with labels, heartbeats, receives step assignments, and
executes them locally through a pluggable orchestrator.

The backend never connects *in*. A runner behind NAT works as long as it can
open a WebSocket and the step containers it spawns can reach the backend over
HTTP.

```
pip install .
lazyaf-runner --backend-url http://localhost:8000 --runner-id workshop-pi
```

## What travels over which channel

This is the one thing worth internalizing before debugging anything:

| Datum | Channel |
|---|---|
| step status, step heartbeat, **step logs**, test-results manifest, usage manifest | the STEP CONTAINER's own `POST /api/steps/{id}/...` with the step JWT |
| runner lifecycle (register / heartbeat / death), assignment, ACK, cancel, drain, workspace cleanup | this agent's WebSocket |
| **runner-origin** log lines and the terminal step outcome | this agent's WebSocket |

The agent does not tunnel the step container's telemetry. Those five channels
already work from any host because the step JWT is location-independent, and
reimplementing them here would be a second ingestion path for something the
backend single-sources.

`log` frames from this agent carry only what a step container *cannot* say
because it does not exist yet or failed to start - `provisioning workspace`,
`ERROR: docker daemon unreachable`, `Image not found: ...`. The backend prefixes
them with `[runner] ` when it appends them, so **the agent sends raw lines**.

## Configuration

Env first, CLI flags override.

| Env | CLI | Default | Meaning |
|---|---|---|---|
| `LAZYAF_BACKEND_URL` | `--backend-url` | `http://localhost:8000` | WS URL is a scheme swap plus `/ws/runner` |
| `LAZYAF_RUNNER_ID` | `--runner-id` | `<hostname>-<orchestrator>` | **stable across restarts** - a fresh id per process orphans a registry row every time |
| `LAZYAF_RUNNER_NAME` | `--name` | = runner id | display name |
| `LAZYAF_RUNNER_TYPE` | `--type` | `generic` | matched by `requires.runner_type` |
| `LAZYAF_RUNNER_LABELS` | `--labels` | `""` | `has=gpio,has=camera,zone=workshop`; a repeated key becomes a list |
| `LAZYAF_ORCHESTRATOR` | `--orchestrator` | `docker` | key into `ORCHESTRATORS` |
| `LAZYAF_RUNNER_TOKEN` | `--token` | **none - required** | shared enrollment secret; must EQUAL the backend's `LAZYAF_RUNNER_AUTH_SECRET`. Also read, in precedence order, from `LAZYAF_RUNNER_TOKEN_FILE`, `LAZYAF_RUNNER_AUTH_SECRET_FILE`, `LAZYAF_RUNNER_AUTH_SECRET` (the `_FILE` forms hold a *path*, for docker secrets / k8s mounted Secrets). No default exists: the agent refuses to start without one rather than dialling and being rejected. |
| `LAZYAF_STEP_BACKEND_URL` | `--step-backend-url` | unset | backend URL for **step containers** |
| `LAZYAF_GIT_URL_TEMPLATE` | - | unset | overrides the clone URL; `{repo_id}` is substituted |
| `LAZYAF_STEP_NETWORK` | - | `bridge` | docker network for spawned step containers |
| `LAZYAF_RUNNER_ALLOW_INSECURE` | - | `0` | permit plaintext `ws://` to a non-loopback host |
| `LAZYAF_BIND_ALLOWLIST` | - | `""` | host paths this runner will bind-mount |
| `LAZYAF_EXPECT_IMAGES` | - | `""` | images whose absence is advertised as `has=[images:stale]` |
| `LAZYAF_RUNNER_LOG_LEVEL` | `--log-level` | `INFO` | |

Two values are deliberately **not** configurable:

- **`arch`.** The agent always reports raw `platform.machine()`; the backend
  owns the one alias table (`x86_64`/`amd64`/`x64` -> `amd64`, and so on). Two
  normalizers is how `arm64` stops matching `aarch64`.
- **`heartbeat_interval` / `death_timeout`.** They arrive in the `registered`
  frame. The agent's interval, the server's read deadline and the death timeout
  drifting apart independently is a real failure mode this closes.

### `LAZYAF_STEP_BACKEND_URL` is the setting you will actually need

There are three network hops, and only the first is visible from the backend:

1. agent -> backend (WebSocket)
2. **step container -> backend** (HTTP, the whole control layer)
3. **step container -> git server** (the workspace clone)

On one compose network all three resolve `http://backend:8000`. From a real
remote host, that name means nothing. Set `LAZYAF_STEP_BACKEND_URL` (and
`LAZYAF_GIT_URL_TEMPLATE` if your git URL differs) to something routable *from
this host*. The agent logs all three resolved values as its first runner-origin
line of every step, so one grep of a step's logs answers "why can't the step
reach the backend".

### Security posture

- The shared enrollment secret rides the WS handshake as
  `Authorization: Bearer <token>`.
- The agent **refuses to register over plaintext `ws://` to a non-loopback
  host** unless `LAZYAF_RUNNER_ALLOW_INSECURE=1`. The `execute_step` frame
  carries the step JWT and `secret_environment` inside `control_files`; in the
  clear across a real network that is a credential broadcast.
- The agent **never logs the assignment config** - only `sorted(config.keys())`,
  the image, the volume and the resolved backend URL.
- A `bind` mount that is not on this runner's `LAZYAF_BIND_ALLOWLIST` is
  refused and the step fails with a clear message. A backend must not be able
  to bind arbitrary host paths on a machine it does not own.

### Running a `needs: [docker]` step

A pipeline step declaring `needs: [docker]` is translated by the backend into a
**bind mount of `/var/run/docker.sock`**. This agent refuses bind mounts that
are not on its own allowlist, so such a step fails on a runner started without:

```
LAZYAF_BIND_ALLOWLIST=/var/run/docker.sock
```

That is deliberate friction. Handing a remote machine's docker socket to a step
because a backend asked for it is a decision the machine's owner has to make,
not one the backend gets to make for them. The failure message names the
variable.

## Loopback / dogfood recipe

The tested path is this agent running on the same host as the backend, speaking
real WebSocket to the real endpoint. From a checkout:

```bash
pip install ./runner-agent            # or: PYTHONPATH=runner-agent python -m lazyaf_runner

export LAZYAF_BACKEND_URL=http://localhost:8000
export LAZYAF_RUNNER_ID=loopback-test
export LAZYAF_RUNNER_LABELS='has=remote-lane,lane=dogfood'
export LAZYAF_STEP_NETWORK=lazyaf-network
export LAZYAF_STEP_BACKEND_URL=http://backend:8000
export LAZYAF_BIND_ALLOWLIST=/var/run/docker.sock
export LAZYAF_EXPECT_IMAGES=lazyaf-base:dev,lazyaf-test-runner:dev
lazyaf-runner
```

Notes for that recipe:

- `has=docker` does not need to be listed: the docker orchestrator contributes
  it from `capabilities()` and the two `has` lists are unioned.
- `LAZYAF_STEP_NETWORK=lazyaf-network` is what lets the SPAWNED step container
  resolve `backend:8000`; `LAZYAF_STEP_BACKEND_URL` is what it should be told to
  use. On a genuinely remote host both change, and neither is guessed.
- `LAZYAF_RUNNER_ID` must be stable. Restarting the agent with the same id
  reuses its registry row; a fresh id per start leaves a dead row behind each
  time.
- The agent connects out only. Nothing needs to reach it.

## Workspace provisioning

A remote host cannot see the backend's workspace volume, so the agent
provisions its own: get-or-create the named volume the assignment asks for,
clone the repo into `/workspace/repo` if it is not already there, and reuse it
for every step of the run (keyed by `retain_key`, which is the pipeline run id -
this is what makes `HOME=/workspace/home` persist between steps remotely,
exactly as it does locally).

Volumes are reaped on `cleanup_workspace`, on `drain`, and by an idle reaper
after an hour as the backstop for a backend that never sent the message.

## Adding an orchestrator

`orchestrator/base.py` and `types.py` import **nothing** from `docker`, and
`tests/test_orchestrator_seam.py` AST-parses both files to keep it that way.

That constraint exists for a concrete target: runpod-style nodes often run *as*
containers with no Docker socket at all. Such a host implements
`StepOrchestrator` with `capabilities() -> {"orchestrator": "native", "has": []}`
and registers normally. A step carrying `requires: {has: [docker]}` simply never
matches it; a step that needs only a shell and a model endpoint does. The wire
protocol never learns what an orchestrator is.

```python
class NativeOrchestrator(StepOrchestrator):
    name = "native"
    async def preflight(self) -> None: ...
    def capabilities(self) -> dict: return {"orchestrator": "native", "has": []}
    async def run_step(self, assignment, *, on_log, cancel): ...
    async def cleanup_workspace(self, retain_key): ...

ORCHESTRATORS["native"] = NativeOrchestrator
```

### The one rule an orchestrator must not break

`on_log` may be called **only before the step process starts and after it
exits** - never in between. The step container is POSTing its own logs to the
backend during that window, and the two streams append to the same
`StepRun.logs`. Keeping them disjoint in time is what makes the merged log's
order real rather than best-effort. `tests/test_log_ordering.py` asserts it.

## Tests

```
python -m pytest        # from runner-agent/
```

Two of them are contract tests against the backend source rather than unit
tests of this package:

- `test_control_archive_parity.py` asserts this package's control-file tar
  builder is **byte-identical** to `local_executor.build_control_archive`, and
  that the wire constants copied into `client.py`/`session.py` still match
  `runner_protocol.py`. The duplication is deliberate - a runner host must not
  need `backend/app` on its PYTHONPATH - and these tests are what keep it from
  drifting. They are unconditional: neither can skip.
- `test_orchestrator_seam.py` is the AST check described above.
