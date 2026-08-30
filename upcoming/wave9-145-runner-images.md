# Wave 9 - Phase 14.5 Wiring Design: combined runner + inference images

Status: DESIGN - implementers build from this verbatim.

Inputs: `PLAN.md` "## Milestone 14" and its "### Phase 14.5" section (the owner's decisions of
2026-08-30 are BINDING and are not relitigated here: a COMBINED runner+inference image, not a
local compose profile; servers vLLM and ollama; we do NOT rebuild the inference servers),
`upcoming/wave8-m14-wiring.md` (14.1-14.4, being implemented right now - its `ModelEndpoint`,
`reach_mode: runner-local`, the `requires: {has: ["endpoint:<name>"]}` injection, `server_kind`,
the capability probe, `max_concurrency` and the `gpu_fraction` cost model are consumed here
unchanged), `upcoming/wave5-126-wiring.md` (12.6 - the runner protocol and the deliberately
Docker-agnostic orchestrator seam), the standing rules R1-R8, and the code:
`runner-agent/**` (`lazyaf_runner/{cli,client,config,session,workspace,types}.py`,
`orchestrator/{base,docker_orch,registry}.py`, `Dockerfile`, `README.md`),
`images/base/**`, `images/agent-base/Dockerfile`, `scripts/build_images.py`,
`scripts/run_tier.py`, `.github/workflows/images.yml`,
`.github/scripts/{publish_image,step_images,scan_image_secrets}.py`, `docker-compose.yml`,
`tdd/tier_floors.json`.

---

## 0. Ground truth found during recon (read before arguing with the design)

Measured 2026-08-30 against the live Docker Hub registry API (config blobs only, no image
pulled). Every number below is from that query and is re-checkable with the same request.

- **`ollama/ollama:0.33.2`** (amd64 `sha256:9e7d782e99880c70f9563c51633da875ca605518a8f8d95c2532bda70a027b7a`):
  `ENTRYPOINT ["/bin/ollama"]`, `CMD ["serve"]`, `EXPOSE 11434/tcp`,
  `ENV OLLAMA_HOST=0.0.0.0:11434`, `NVIDIA_VISIBLE_DEVICES=all`,
  `NVIDIA_DRIVER_CAPABILITIES=compute,utility`. **4 layers, 3383 MB compressed.** Ubuntu-based,
  `apt-get` available, no `USER` directive (runs as root).
- **`vllm/vllm-openai:v0.28.0`** (amd64 `sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635`):
  `ENTRYPOINT ["vllm", "serve"]`, no `CMD`, `WORKDIR /vllm-workspace`, no `EXPOSE`.
  **32 layers, 8634 MB compressed.** `CUDA_VERSION=13.0.2` and
  `NVIDIA_REQUIRE_CUDA=cuda>=13.0 ... driver>=535`, so **this pin refuses to start on a host
  whose NVIDIA driver is older than 535**; `vllm/vllm-openai:v0.28.0-cu129` (10922 MB, same
  date) is the escape hatch and the docs must name it. Python inside is uv-managed
  (`UV_PYTHON_INSTALL_DIR=/opt/uv/python`), which is the reason for cross-agent contract 1.
- **The runner agent has exactly ONE orchestrator and it is `docker`.**
  `orchestrator/registry.py`'s `ORCHESTRATORS` is `{"docker": DockerOrchestrator}` and
  `DockerOrchestrator.preflight()` raises `OrchestratorUnavailable` when the daemon is
  unreachable, which `cli.run_agent` turns into `EXIT_FATAL` **before registering**. A runpod
  pod has no docker socket. This is the single largest structural fact about this phase and
  section 3.4 is where it is answered, not wished away.
- **Labels are computed once, at register time.** `RunnerClient.register_payload()` calls
  `merge_labels(config.labels, orchestrator.capabilities())` on every connect; there is no
  frame for updating labels on a live connection, and adding one would be a protocol change
  (wave 5 contracts 1 and 5). **Therefore the ONLY lever this phase has for "stop advertising a
  model that is gone" is the CONNECTION**: start the agent after the server is ready, stop the
  agent when the server dies. `runner_registry.find_available` filters on
  `runner.id in self._connections`, so a closed socket removes the runner from the schedulable
  set immediately. Section 2 is built on exactly this and needs no backend change.
- **`EXIT_FATAL = 2`** in `client.py`, returned for auth rejection, an unsupported protocol
  version, and `ConfigError`. It is the one agent exit code that must NOT be retried, and the
  supervisor's restart policy keys off it.
- **Adding a row to `scripts/build_images.py::IMAGES` has three automatic consequences.**
  `.github/scripts/step_images.py` READS that table, so `images.yml` builds, leak-scans and
  pushes every row on every push to `main`; and `scripts/run_tier.py` runs
  `build_images.py --check` as a **T2 and T3 preflight**, so every developer and every dogfood
  run is told to build every row. A 3.4 GB and an 8.6 GB image in that table would break both.
  Section 6's answer follows from this and from nothing else.
- **`build_images.py` is reusable in pieces.** `stage_context`, `_copy_tree`, `STAGE_EXCLUDE`
  and `tree_hash` are pure and importable; `tree_hash(dir, extra=...)` already folds a parent
  string into a child's hash. The new script imports them rather than copying them (R3).
- **`.github/scripts/scan_image_secrets.py` streams `docker export`** of the whole flattened
  filesystem. It skips `usr/lib/`, `site-packages`, `node_modules`, and any file over 8 MiB, so
  it will not READ much of a vLLM image - but it still walks a ~20-25 GB tar. Checks 1-3 (env,
  labels, build history, `.env` filenames) are cheap and are the ones that actually protect a
  published image. Section 6 budgets for this rather than exempting it.
- **`runner-agent/tests` runs in NO tier.** `run_tier.py` T1 selects `../tdd/unit`,
  `../tdd/demos`, `../tdd/integration` and `../runner-common/tests`; runner-agent's 12 test
  modules are selected by nothing. 12.7 fixed exactly this shape of gap for `runner-common`.
  Any test this phase writes under `runner-agent/tests` is invisible to the ratchet until that
  is fixed, so Agent B fixes it (contract 7).
- **The 12.5 agent wrapper refuses to run as root** and `images/base` drops to uid/gid 1000 via
  `gosu` in `/control/entrypoint.sh`. A runpod pod runs as root. Any in-pod step execution must
  therefore reproduce the privilege drop, not skip it (section 3.4).
- **Wave 8 dependencies this design consumes and does not re-specify:**
  `tdd/support/mock_openai_server.py` (Agent D of wave 8), `runner_common/endpoint_probe.py`
  and `POST /api/model-endpoints/{id}/probe-result` (wave 8 sections 2.3 and 1.5), the
  `requires:` injection for `runner-local` (wave 8 section 6.2), and
  `ENDPOINT_MODEL_PREFIX = "endpoint:"` (wave 8 contract 4). **Wave 9 starts after wave 8's
  Agent A and Agent D have landed**; nothing here edits a wave 8 file.
- **Image size is not a user problem and this design does not treat it as one.** Somebody
  rents a GPU to run a 32B model; they expect a large image. Nothing below trims layers, adds a
  CPU-only variant, or multi-stages for size. The size question that survives is a CI disk
  question and it lives in section 6.

---

## 1. THE IMAGES

Three images, one shared layer. The third exists so that sections 2, 3 and 8 are testable
without pulling either of the first two.

| Local tag | `FROM` | Compressed base | Published as | In `build_images.py::IMAGES`? |
|---|---|---|---|---|
| `lazyaf-runner-ollama:dev` | `ollama/ollama:0.33.2@sha256:9e7d782e...` | 3383 MB | `ghcr.io/<owner>/lazyaf/runner-ollama` | **No** (section 6) |
| `lazyaf-runner-vllm:dev` | `vllm/vllm-openai:v0.28.0@sha256:2286e853...` | 8634 MB | `ghcr.io/<owner>/lazyaf/runner-vllm` | **No** (section 6) |
| `lazyaf-fake-inference:dev` | `lazyaf-base:dev` | (local, ~200 MB) | `ghcr.io/<owner>/lazyaf/fake-inference` | **Yes** |

### 1.1 The shared layer: `images/node-layer/`

One tree, staged into all three build contexts by the build scripts. This is the whole added
surface; if something is not in this list it is not in the image.

```
images/node-layer/
  install.sh                 # the ONE install script all three Dockerfiles RUN
  bin/
    lazyaf-node              # exec-line shim -> /opt/lazyaf/venv/bin/python -m lazyaf_node
    lazyaf-node-health       # HEALTHCHECK + "one command to run inside the pod"
  lazyaf_node/
    __init__.py
    __main__.py              # argv/env -> Supervisor(...).run(); nothing else
    config.py                # NodeConfig.from_env(), validate(), redacted()
    servers.py               # SERVERS: the per-server adapter table (section 1.4)
    gpu.py                   # detect() + verdict() (section 5)
    readiness.py             # the five readiness phases (section 2.3)
    supervise.py             # the state machine (section 2.2)
    logmux.py                # line prefixing, timestamps, the server ring buffer
    advertise.py             # env -> runner labels + the REGISTER THIS ENDPOINT block
    status.py                # /run/lazyaf/status.json writer + reader
```

`install.sh`, verbatim in intent - the same script runs in all three images, which is what
makes `lazyaf-fake-inference` a real proxy for the other two:

```sh
#!/bin/sh
# LazyAF node layer installer (Phase 14.5). Runs as root at build time.
#
# ISOLATION IS THE POINT: we NEVER install into the upstream image's python.
# vllm/vllm-openai ships a uv-managed interpreter with pinned torch/CUDA
# wheels; `pip install websockets` into it is a supply-chain edit to somebody
# else's release engineering. We build our own venv from the distro python and
# put nothing of ours on the upstream PATH except /opt/lazyaf/bin.
set -eu

if ! command -v apt-get >/dev/null 2>&1; then
    echo "install.sh: this layer assumes a Debian/Ubuntu upstream image." >&2
    echo "install.sh: ollama/ollama and vllm/vllm-openai are both Ubuntu." >&2
    exit 1
fi

apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip ca-certificates curl git tini
rm -rf /var/lib/apt/lists/*

# tini: PID 1 duties only (section 2.1). ~200 KB, one job, no configuration.

python3 -m venv /opt/lazyaf/venv
/opt/lazyaf/venv/bin/pip install --no-cache-dir --upgrade pip
/opt/lazyaf/venv/bin/pip install --no-cache-dir /opt/lazyaf/src/runner-agent
/opt/lazyaf/venv/bin/pip install --no-cache-dir requests

# The node package is copied, not pip-installed: it has no dependencies beyond
# requests and keeping it a plain tree makes `docker cp` of a patched file a
# legitimate 2am debugging move on a rented pod.
cp -r /opt/lazyaf/src/node-layer/lazyaf_node /opt/lazyaf/
install -m 0755 /opt/lazyaf/src/node-layer/bin/lazyaf-node        /opt/lazyaf/bin/lazyaf-node
install -m 0755 /opt/lazyaf/src/node-layer/bin/lazyaf-node-health /opt/lazyaf/bin/lazyaf-node-health

# uid/gid 1000 EXACTLY as images/base pins them: the 12.5 agent wrapper refuses
# to run as root, and a step's files must land 1000-owned wherever it runs.
groupadd -g 1000 lazyaf 2>/dev/null || true
useradd -m -s /bin/bash -u 1000 -g 1000 lazyaf 2>/dev/null || true

mkdir -p /run/lazyaf && chmod 0755 /run/lazyaf
/opt/lazyaf/venv/bin/python -c "import lazyaf_runner.cli, sys; sys.path.insert(0,'/opt/lazyaf'); import lazyaf_node.supervise"
```

The final `python -c` is the same trick `images/agent-base` uses: a packaging regression fails
the BUILD, not a step thirty seconds into a rented GPU-hour.

### 1.2 `images/runner-ollama/Dockerfile`

```dockerfile
# LazyAF combined runner + ollama node (Phase 14.5).
# Build: python scripts/build_inference_images.py ollama
#        (NEVER `docker build` by hand - the content hash and the upstream pin
#         label are stamped by that script, and an unstamped image is one
#         nobody can later ask "what is in this?")
#
# WE DO NOT REBUILD OLLAMA. This is a thin layer on the official image and it
# inherits their CUDA/driver release engineering. The pin below carries BOTH
# the human-readable tag and the digest: the tag says what we chose, the digest
# says what we got.
FROM ollama/ollama:0.33.2@sha256:9e7d782e99880c70f9563c51633da875ca605518a8f8d95c2532bda70a027b7a

# Staged by build_inference_images.py: images/node-layer -> src/node-layer,
# runner-agent -> src/runner-agent, images/base/control -> /control.
COPY src/ /opt/lazyaf/src/
COPY control/ /control/
RUN chmod +x /control/run.py /control/entrypoint.sh \
    && sh /opt/lazyaf/src/node-layer/install.sh \
    && rm -rf /opt/lazyaf/src/runner-agent /opt/lazyaf/src/node-layer/lazyaf_node

ENV LAZYAF_SERVER_KIND=ollama \
    LAZYAF_SERVER_PORT=11434 \
    OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MODELS=/root/.ollama/models \
    PATH="/opt/lazyaf/bin:${PATH}"

VOLUME ["/root/.ollama"]
EXPOSE 11434

ARG CONTENT_HASH=dev
ARG UPSTREAM_PIN=unset
LABEL lazyaf.node=1
LABEL lazyaf.server-kind=ollama
LABEL lazyaf.upstream=$UPSTREAM_PIN
LABEL lazyaf.content-hash=$CONTENT_HASH

HEALTHCHECK --interval=30s --timeout=10s --start-period=1800s --retries=3 \
    CMD ["/opt/lazyaf/bin/lazyaf-node-health"]

# tini is PID 1 (section 2.1); the supervisor is its single child.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/lazyaf/bin/lazyaf-node"]
```

`--start-period=1800s` is not padding: it equals `LAZYAF_MODEL_READY_TIMEOUT`, so a first boot
that is downloading 20 GB of weights is not killed by its own healthcheck.

### 1.3 `images/runner-vllm/Dockerfile`

Identical but for four lines, and the differences are the whole story:

```dockerfile
FROM vllm/vllm-openai:v0.28.0@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635

COPY src/ /opt/lazyaf/src/
COPY control/ /control/
RUN chmod +x /control/run.py /control/entrypoint.sh \
    && sh /opt/lazyaf/src/node-layer/install.sh \
    && rm -rf /opt/lazyaf/src/runner-agent /opt/lazyaf/src/node-layer/lazyaf_node

ENV LAZYAF_SERVER_KIND=vllm \
    LAZYAF_SERVER_PORT=8000 \
    HF_HOME=/root/.cache/huggingface \
    PATH="/opt/lazyaf/bin:${PATH}"

VOLUME ["/root/.cache/huggingface"]
EXPOSE 8000

ARG CONTENT_HASH=dev
ARG UPSTREAM_PIN=unset
LABEL lazyaf.node=1
LABEL lazyaf.server-kind=vllm
LABEL lazyaf.upstream=$UPSTREAM_PIN
LABEL lazyaf.content-hash=$CONTENT_HASH

HEALTHCHECK --interval=30s --timeout=10s --start-period=1800s --retries=3 \
    CMD ["/opt/lazyaf/bin/lazyaf-node-health"]
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/lazyaf/bin/lazyaf-node"]
```

The upstream `WORKDIR /vllm-workspace` and `ENTRYPOINT ["vllm","serve"]` are both overridden.
`vllm serve` is invoked BY the supervisor as `vllm serve <model> --port <port> ...`, so the
upstream entrypoint's argv contract is preserved exactly - we replace the process-1 role, not
the invocation.

### 1.4 The server adapter table - the only place the two servers differ

`lazyaf_node/servers.py`. Everything else in the layer is server-agnostic, and a third server
(llama.cpp, LM Studio) is one row plus its tests.

```python
@dataclass(frozen=True)
class ServerAdapter:
    kind: str                    # MUST be a ModelEndpoint.server_kind value (contract 5)
    default_port: int
    model_required: bool         # vLLM cannot start without one; ollama can
    cache_paths: tuple[str, ...] # watched for growth during the readiness wait
    refuses_without_gpu: bool

    def argv(self, cfg: NodeConfig) -> list[str]: ...
    def pull(self, cfg, log) -> None: ...   # no-op where the server downloads itself

SERVERS = {
    "ollama": ServerAdapter(
        kind="ollama", default_port=11434, model_required=False,
        cache_paths=("/root/.ollama/models",), refuses_without_gpu=False,
        argv=lambda cfg: ["/bin/ollama", "serve"],
        # ollama does NOT fetch on demand at /v1/chat/completions in a way we
        # can watch, so the supervisor drives POST /api/pull with stream=true
        # and turns its NDJSON {status, completed, total} into percentages.
        pull=_ollama_pull,
    ),
    "vllm": ServerAdapter(
        kind="vllm", default_port=8000, model_required=True,
        cache_paths=("/root/.cache/huggingface/hub",), refuses_without_gpu=True,
        argv=lambda cfg: [
            "vllm", "serve", cfg.model,
            "--host", "127.0.0.1" if cfg.bind_localhost_only else "0.0.0.0",
            "--port", str(cfg.port),
            # The endpoint row's `model` must equal what /v1/models lists, or
            # wave 8's probe request 1 reports model_listed=false forever.
            "--served-model-name", cfg.model,
            *cfg.server_args,
        ],
        pull=None,   # vLLM downloads from HF itself, on start, into HF_HOME
    ),
}
```

### 1.5 `images/fake-inference/Dockerfile` - the test node

```dockerfile
# LazyAF fake inference node (Phase 14.5) - the T2/T1 stand-in.
#
# It is the SAME node layer and the SAME runner agent as the two real images;
# the ONLY substitution is the server binary, and the contract with that binary
# is HTTP. That is what makes supervision, readiness, label advertisement,
# registration and the runner-local probe testable with zero GB of pulls.
# If this image and the real ones ever stop sharing images/node-layer/, the
# test stops meaning anything - pinned by test_node_images_share_one_layer.
FROM lazyaf-base:dev

COPY src/ /opt/lazyaf/src/
COPY mock/ /opt/lazyaf/mock/
USER root
RUN PIP_USER=0 sh /opt/lazyaf/src/node-layer/install.sh

ENV LAZYAF_SERVER_KIND=fake \
    LAZYAF_SERVER_PORT=11434 \
    PATH="/opt/lazyaf/bin:${PATH}"

ARG CONTENT_HASH=dev
LABEL lazyaf.node=1
LABEL lazyaf.server-kind=fake
LABEL lazyaf.content-hash=$CONTENT_HASH
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/lazyaf/bin/lazyaf-node"]
```

`mock/` is wave 8's `tdd/support/mock_openai_server.py`, staged in - **not a second
implementation** (R3). The `"fake"` adapter's `argv` runs it with the scenario named by
`LAZYAF_FAKE_SCENARIO`; `refuses_without_gpu=False`, `model_required=False`. Because it is
`FROM lazyaf-base:dev` it also carries `/control` and uid 1000, which is what lets the same
image exercise the native orchestrator in T2.

### 1.6 How the pins are maintained

- **Both spellings, always.** `FROM repo:tag@sha256:...`. The tag is for a human reading the
  Dockerfile; the digest is what actually gets pulled. A tag without a digest means two builds
  a week apart are different images with the same provenance claim.
- **The pin lives in exactly one place**, `scripts/build_inference_images.py::INFERENCE_IMAGES`,
  and the Dockerfile's `FROM` is asserted to match it by
  `tdd/unit/control_runtime/test_node_image_contract.py`. A drifted pair is a build that says
  one thing and does another.
- **Bumping is a normal PR, never a bot.** `python scripts/build_inference_images.py --check-pins`
  queries the registry for the newest non-`rc`, non-`nightly`, non-`-rocm` tag of each upstream
  and PRINTS a diff plus the new digest. It exits 0 always: it is a report, not a gate. An
  auto-bumping bot on a 10 GB CUDA image whose driver requirement can change between minor
  releases (see `NVIDIA_REQUIRE_CUDA` in section 0) is a way to discover on a Sunday that your
  GPU node no longer boots.
- **Cadence: on demand, plus a check at every LazyAF release.** The release checklist gains one
  line: run `--check-pins`, and either bump or record why not.
- **A bump PR must state the driver floor.** `NVIDIA_REQUIRE_CUDA` is read out of the new
  config blob by `--check-pins` and printed. Going from `driver>=535` to something higher is a
  breaking change for somebody's home box and it must be in the PR body, not discovered.
- **amd64 only**, matching `images.yml`'s existing stated position. arm64 vLLM exists (9.7 GB)
  and nobody has asked.

---

## 2. PROCESS SUPERVISION

### 2.1 What supervises what, and why

Two long-lived processes, one container, an upstream image with no init system, on hardware the
operator is renting by the minute. The choice is made against failure modes, not aesthetics.

| Option | What kills it |
|---|---|
| **Bare `sh` entrypoint + `trap` + `wait`** | PID 1 in a container is the reaper of every reparented orphan, and a POSIX shell reaps only its own jobs - ollama forks per-model runner subprocesses and vLLM forks workers, so orphans accumulate as zombies until PID exhaustion on a long-lived pod. It also cannot express "genuinely ready" (needs an HTTP+JSON check), cannot stream two children's output with prefixes, and expressing "server died -> stop the agent, restart the server, re-wait readiness, restart the agent, within a budget" in `sh` produces a script nobody can test. |
| **s6-overlay** | Correct, and overkill. It is a real dependency added to two upstream images from different vendors, it brings a service-directory idiom and its own logging semantics that every future reader has to learn, and it still would not know what "the model answered" means - we would write the readiness script anyway. We would be adding an init system to get supervision policy we then have to write on top of it. |
| **The runner agent supervises the server as a child** | Tempting, because the agent is the thing that must stop advertising. Rejected for two reasons. (1) It inverts the failure semantics: an agent crash would take the model server down with it, and the server is the expensive thing to restart (a 30 GB model reload). (2) It couples `runner-agent` releases - a general-purpose component that runs on a Raspberry Pi - to inference-server process management. The seam that made `runner-agent` reusable is the one thing 12.6 was most careful about. |
| **CHOSEN: `tini` as PID 1 + a purpose-built Python supervisor as its single child** | `tini` does exactly one job (reap orphans, forward signals to one child) in ~200 KB with no configuration. The supervisor does policy, in a module that is imported and unit-tested with fake children on a laptop with no GPU and no docker. The split is legible: if zombies appear, that is `tini`; if the wrong thing restarted, that is `supervise.py` and there is a test for it. |

Python is a dependency we are already taking (the runner agent is Python), so the supervisor
costs nothing extra.

### 2.2 The state machine

```
                +-------------+
  start ------> |  PREFLIGHT  |  config validation, GPU verdict, mount checks
                +------+------+  refusal -> exit 78, nothing started
                       |
                       v
                +-------------+
                | PULL/START  |  adapter.pull() if needed, then spawn server
                +------+------+
                       |
                       v
                +-------------+  five phases, heartbeat every 15s (section 2.3)
                |  WAIT_READY |  timeout -> exit 70, with the server's last 50 lines
                +------+------+
                       |
                       v
                +-------------+  agent spawned HERE and not one second earlier.
                |   SERVING   |  status.json phase=serving; HEALTHCHECK green.
                +--+-------+--+
        server dies |       | agent dies
                    v       v
        +-----------------+ +--------------------------------+
        | stop agent      | | exit 2 if EXIT_FATAL,           |
        | (SIGTERM, 20s)  | | else backoff-restart the agent  |
        | restart server  | | (server untouched, model stays  |
        | budget 3/600s   | |  resident)                      |
        | -> WAIT_READY   | +--------------------------------+
        | budget out -> 71|
        +-----------------+
```

```python
# lazyaf_node/supervise.py - the shape, not the whole file.
class Supervisor:
    def __init__(self, cfg, adapter, *, spawn=spawn_child, probe=readiness.probe,
                 clock=time.monotonic, status=status.Writer()):
        # Every collaborator is injected. This is what makes section 8's T1
        # suite possible with no server, no GPU and no container.
        ...

    def run(self) -> int:
        rc = self._preflight()                      # 78 on refusal
        if rc: return rc
        while True:
            self._start_server()
            if not self._wait_ready():              # 70 on timeout
                return EXIT_SERVER_NOT_READY
            self._start_agent()                     # only reached when ready
            who, code = self._wait_for_a_child()    # blocks; SIGCHLD-driven
            if self._stopping:
                return 0
            if who == "agent":
                if code == AGENT_EXIT_FATAL:        # 2 - config/auth, never transient
                    self._log("agent", "refused by the backend; not restarting")
                    return AGENT_EXIT_FATAL
                self._restart_agent_with_backoff()
                continue                            # server keeps the model resident
            # who == "server"
            self._stop_agent(reason="inference server exited; withdrawing this "
                                    "runner so it stops advertising a model it "
                                    "cannot serve")
            if not self._server_restart_budget.take():
                return EXIT_SERVER_RESTART_BUDGET   # 71
```

**Start order is a correctness property, not a nicety.** The runner agent's labels - including
`has=endpoint:<name>` - are fixed at register time (section 0). Starting the agent only after
readiness means the label and the model's availability are the same event. There is no window
in which the backend can schedule a step against a model that has not loaded.

### 2.3 Readiness: five phases, and none of them is "the port is open"

`readiness.py`. A port-open check would call vLLM ready while it is still materializing 30 GB
of weights into VRAM, and the first step to arrive would eat a 20-minute timeout instead of the
supervisor eating a wait.

| Phase | Check | Why it is separate |
|---|---|---|
| P0 | the server process is alive | a server that died during startup must report ITS error, not a connection refusal |
| P1 | TCP connect to `127.0.0.1:<port>` | distinguishes "not listening yet" from "listening and unhappy" |
| P2 | `GET /v1/models` returns 200 | the server's HTTP stack is up |
| P3 | `cfg.model` appears in `data[].id` | **the model, not the server.** This is the phase that vLLM spends downloading and loading, and it is where a cold cache lives |
| P4 | `POST /v1/chat/completions` with `max_tokens: 1`, `temperature: 0` returns 200 with a `choices[0]` | the model can actually generate. ollama reaches P3 the moment a blob is on disk and can still fail to allocate; this is the phase that catches "loaded but OOM at first token". Disable with `LAZYAF_READY_PROBE_COMPLETION=0` |

P2-P4 are deliberately the same three requests wave 8's capability probe makes. **They are not
a second implementation**: `readiness.py` builds the request bodies from
`runner_common.endpoint_probe`'s constants where they overlap, and where the node image cannot
import backend code the shared shape is pinned by a contract test (contract 5).

**The heartbeat is the anti-hang instrument** and it is the answer to "how does a user know a
huge download is in progress rather than a hang". Every `LAZYAF_READY_HEARTBEAT=15` seconds
while in P1-P4, one line to stdout:

```
[node] waiting: phase=P3 model-not-listed elapsed=412s cache=/root/.cache/huggingface/hub
       size=11.4GiB (+842MiB in 15s, ~56MiB/s) last-server-line="Downloading model.safetensors ..."
```

- `+N in 15s` is computed from a `du`-equivalent walk of the adapter's `cache_paths` (cached
  inode walk, capped at 20k entries, skipped and SAID so if it exceeds 250 ms).
- For ollama the supervisor drives `POST /api/pull` with `stream: true` and prints the server's
  own `{status, completed, total}` as a real percentage, which is strictly better than watching
  bytes. It says so in the line.
- **Three consecutive heartbeats with zero cache growth while still in P2/P3 escalates the
  message**: `no cache growth in 45s - the server may be loading weights into VRAM (normal for
  up to several minutes on a large model) or may be stuck; last server line: "..."`. Stating
  the ambiguity is the honest form. A silent wait and a hang are indistinguishable, which is
  R1 applied to a process rather than to a route.
- On timeout (`LAZYAF_MODEL_READY_TIMEOUT`, default **1800s**), the supervisor exits **70**
  after printing the last 50 lines of the server's own output from the ring buffer. That ring
  buffer is the single most valuable diagnostic on a pod you cannot SSH into.

### 2.4 What happens when each process dies

| Event | Action | Rationale |
|---|---|---|
| **Server exits (any code, including 0)** | Stop the agent FIRST (SIGTERM, 20s grace, then SIGKILL), then restart the server, then re-run readiness, then restart the agent. Budget `LAZYAF_SERVER_MAX_RESTARTS=3` inside a `LAZYAF_SERVER_RESTART_WINDOW=600s`; on exhaustion, exit **71** and let the container restart policy (or runpod) recycle the whole pod. | Stopping the agent closes the WebSocket. The backend's `on_runner_disconnect` requeues whatever was in flight, and `find_available` stops selecting this runner because it filters on `runner.id in self._connections`. **The advertisement and the model's existence are the same fact, enforced by one line of ordering.** Restarting the server without stopping the agent would leave a runner claiming `endpoint:local-4090` for the ~90 seconds it takes a 32B model to reload, and every step scheduled in that window would fail on connection refused. |
| **Server exits during `WAIT_READY`** | No restart, exit **70**, print its last 50 lines. | A server that cannot start once will not start three times, and burning three 20-minute readiness waits on a rented GPU to prove it is a bill, not a diagnosis. |
| **Agent exits with 2 (`EXIT_FATAL`)** | Do not restart. Exit **2**, echoing the agent's own message. | `EXIT_FATAL` is returned for a rejected enrollment secret, an unsupported protocol version and a `ConfigError`. None heals. `client.py`'s own docstring calls retrying it "a fleet DDoSing its own backend over a typo'd secret" - the supervisor must not undo that. |
| **Agent exits with anything else** | Restart with backoff `min(60, 5 * attempt)`, unbounded, one log line per restart. The server is left alone. | The agent is a network client with its own reconnect logic; an exit that is not `EXIT_FATAL` is an unexpected crash, and it is not a reason to unload a model that is fine. |
| **Both die inside 2s** | Treated as the server case (server first), because the agent's death is almost certainly downstream of it. | Avoids a restart-storm where each child's handler fights the other's. |

### 2.5 Signals, shutdown, exit codes

`tini` forwards `SIGTERM`/`SIGINT` to the supervisor, which runs a three-phase stop:

1. **Agent first**, `SIGTERM`, up to `min(LAZYAF_SHUTDOWN_GRACE, 20)` seconds. The agent's
   `cli.run_agent` installs `loop.add_signal_handler(SIGTERM, client.stop)`, so this is a clean
   socket close and the backend sees a deliberate disconnect rather than a death timeout.
2. **Server second**, `SIGTERM`, the remainder of `LAZYAF_SHUTDOWN_GRACE` (default 30s).
3. `SIGKILL` to anything still alive, then exit.

Agent-before-server is deliberate: stopping the model first would strand an in-flight step
mid-inference with a connection reset, while stopping the agent first gives the backend a
disconnect it already knows how to requeue.

| Code | Meaning | Who emits it |
|---|---|---|
| 0 | clean stop on SIGTERM/SIGINT, or `LAZYAF_AGENT_MODE=off` and the server exited 0 | supervisor |
| 2 | the backend permanently refused this runner (`EXIT_FATAL` passthrough) | agent, relayed |
| 70 | the server never became ready inside `LAZYAF_MODEL_READY_TIMEOUT`, or died during readiness | supervisor |
| 71 | server restart budget exhausted | supervisor |
| 78 | preflight refusal: no GPU on a `refuses_without_gpu` server, missing `LAZYAF_MODEL` where required, missing `LAZYAF_ENDPOINT_NAME` with the agent enabled, missing enrollment secret, unwritable model cache | supervisor |

These are asserted by name in T1; a bare `sys.exit(1)` anywhere in the layer is a test failure.

### 2.6 Logs

Both children get pipes; the supervisor reads them on two threads and re-emits every line to
**its own stdout**, prefixed and timestamped:

```
2026-08-30T18:02:11Z [node]   preflight ok: 1x NVIDIA GeForce RTX 4090 (driver 550.90.07)
2026-08-30T18:02:11Z [server] time=... level=INFO msg="Listening on [::]:11434"
2026-08-30T18:04:39Z [node]   ready in 148s (P4 ok, 1 token in 620ms)
2026-08-30T18:04:39Z [agent]  Runner agent 0.1.0 starting: {'runner_id': 'workshop-1', ...}
```

- **One stream, because a rented pod's `docker logs` / runpod log pane is the only forensics
  there is.** Writing the server to a file inside the container means the one thing you need
  after a pod is reclaimed is the one thing that is gone.
- The server's last `LAZYAF_SERVER_LOG_RING=50` lines are also kept in memory so a readiness
  failure can print them as the REASON rather than as scrollback.
- The supervisor never logs the enrollment secret, `HF_TOKEN`, or any endpoint key; it logs
  `NodeConfig.redacted()` at startup in the same shape `RunnerConfig.redacted()` already uses,
  and a T1 sentinel test greps every emitted line for planted values (section 8).
- `/run/lazyaf/status.json` is rewritten on every phase transition and every heartbeat:

```json
{
  "phase": "serving",
  "server": {"kind": "vllm", "pid": 12, "port": 8000, "restarts": 0,
             "ready_at": "2026-08-30T18:04:39Z", "ready_seconds": 148.2},
  "agent":  {"mode": "docker", "pid": 34, "restarts": 0, "enabled": true},
  "model": "Qwen/Qwen2.5-Coder-32B-Instruct",
  "endpoint_name": "local-4090",
  "advertise_url": "http://lazyaf-node:8000/v1",
  "gpu": {"present": true, "count": 1, "detail": "NVIDIA GeForce RTX 4090", "driver": "550.90.07"},
  "cache": {"path": "/root/.cache/huggingface/hub", "bytes": 64203288576, "delta_15s": 0},
  "last_error": null,
  "updated_at": "2026-08-30T18:31:02Z"
}
```

`lazyaf-node-health` READS that file and exits 0 only when `phase == "serving"`. **It never
issues its own inference request**: a healthcheck that queues behind a real step's generation
is a healthcheck that reports unhealthy under load, which is precisely backwards.

---

## 3. ENDPOINT REGISTRATION

### 3.1 The decision: the operator registers, the pod advertises. No self-registration.

This is the minimum that works with zero new machinery, and it is also the right answer.

The operator does this once, from their own machine:

```bash
lazyaf endpoints add local-4090 \
    --reach runner-local \
    --base-url http://lazyaf-node:11434/v1 \
    --model qwen2.5-coder:32b \
    --server-kind ollama \
    --rate-usd-hour 0.00 \
    --max-concurrency 1
```

The pod gets one env var:

```
LAZYAF_ENDPOINT_NAME=local-4090
```

`advertise.py` turns that into `has=endpoint:local-4090`, MERGED into whatever
`LAZYAF_RUNNER_LABELS` the operator set (never replacing it), and hands the composed
environment to the agent. Wave 8 section 6.2's injected `requires: {has: ["endpoint:local-4090"]}`
then matches this runner and nothing else. No protocol change, no new frame, no new grammar
key, no write from the pod.

**Why not self-registration.** It is not a convenience question, it is a trust boundary
question, and the boundary is the interesting part of this phase:

- A `ModelEndpoint` row carries `rate_usd_hour`, which feeds `usage_pricing.gpu_node_cost_usd`
  and therefore every cost-to-solve figure Milestone 13 intends to PUBLISH. A compute node that
  can write its own rate can write the benchmark's conclusions.
- It carries `base_url`, which the backend hands to other steps. A node that can PATCH an
  endpoint can point somebody else's step at a server it controls, and read that step's prompts
  - which contain the operator's source code.
- It carries `auth_secret_ref`, which names a backend environment variable. Wave 8's
  prefix allowlist bounds that to `LAZYAF_ENDPOINT_*`, but "bounded" is not "harmless".
- The credential a pod would authenticate with today is `LAZYAF_RUNNER_AUTH_SECRET`, which is
  a **shared fleet enrollment secret**. One compromised rented pod is every pod. Giving that
  secret write access to the control plane's model registry converts "an attacker can run
  steps I send them" into "an attacker can define what models exist and what they cost".

**The convenience gap is real, and it is closed on the operator's side instead.** The thing
that makes the minimum feel like work is not the API call - it is not knowing what `base_url`
to type (section 3.2). So:

1. The supervisor prints, once, at the moment it reaches `serving`, a block containing the
   exact registration for THIS pod, with the `base_url` computed for the network position a
   step will occupy:

```
================ REGISTER THIS ENDPOINT ================
This node is serving and is advertising  has=endpoint:local-4090
It is NOT registered with LazyAF - a compute node does not write rows in your
control plane. Run this on your LazyAF host:

  lazyaf endpoints add local-4090 --reach runner-local \
      --base-url http://lazyaf-node:11434/v1 \
      --model qwen2.5-coder:32b --server-kind ollama --max-concurrency 1

  # then, to prove the URL above is right from where steps actually run:
  lazyaf endpoints probe local-4090
========================================================
```

2. `lazyaf node recipe --server ollama --endpoint local-4090 --model qwen2.5-coder:32b`
   prints the matching `docker run` line. The two halves are generated from one place, so the
   name in the label and the name in the row cannot drift.

That is "just go" without a node ever holding a write credential.

### 3.2 The `base_url` problem, and how the probe reaches a `runner-local` endpoint

Wave 8 section 2.3 is explicit that a `runner-local` endpoint must be probed **from the network
position the step will occupy**, and section 8's inherited-seam table calls step-container ->
endpoint "the biggest new exposure". For a combined image the pod's own `localhost` is
frequently the WRONG answer, and getting this wrong produces a probe that says `unreachable`
with no clue why.

| Agent mode | Where the step runs | Correct `base_url` | How the supervisor derives it |
|---|---|---|---|
| `docker` (home box, DooD) | a SIBLING container on the host's docker daemon, on `LAZYAF_STEP_NETWORK` | `http://<this container's name>:<port>/v1` | `LAZYAF_ENDPOINT_ADVERTISE_HOST` if set, else `$HOSTNAME`. Requires the node container and the step network to be the same user-defined bridge - the recipe sets `--network lazyaf-node --name lazyaf-node --hostname lazyaf-node` and `LAZYAF_STEP_NETWORK=lazyaf-node` |
| `docker`, no shared network | a sibling on the default bridge | `http://172.17.0.1:<port>/v1` | fallback, emitted WITH a warning that it requires `-p <port>:<port>` and the default bridge subnet, and that the shared-network form above is the supported one |
| `native` (runpod) | a process inside THIS container | `http://127.0.0.1:<port>/v1` | literal; this is the only mode where localhost is honest |
| `off` | nowhere - no agent | operator's choice, `reach: direct` | the supervisor prints the `direct` recipe instead (section 3.4) |
| any | - | `LAZYAF_ENDPOINT_ADVERTISE_URL` verbatim | always wins; the escape hatch for a deployment nobody anticipated |

The derived URL is **printed, never sent**. The probe is what makes it true or false, and the
probe already runs from the right place: wave 8's `POST /api/model-endpoints/{id}/probe` on a
`runner-local` endpoint schedules a one-step ad-hoc run pinned by `requires: {has: [endpoint:X]}`,
which lands on THIS runner and executes `python3 -m runner_common.endpoint_probe` in a step
container - in `docker` mode a sibling on the step network, in `native` mode a process in the
pod. Either way the hop it tests is the hop a real step takes. **This phase adds nothing to the
probe; it only has to make sure the pod advertises the label so the probe can be scheduled, and
that the operator was told a URL that has a chance of being right.**

The failure mode is diagnosable without shell access, which was the design goal: an unschedulable
probe fails at `NO_RUNNER_TIMEOUT` with 12.6's message naming the requirements and every
connected runner's labels ("nobody carries `endpoint:local-4090`"), and a schedulable probe that
cannot connect fails with the URL and the OS error in the step log ("the label is right, the URL
is wrong").

### 3.3 Self-registration, specified but NOT shipped

Written down so that "we decided against it" is a decision with a shape rather than an absence.
If the owner later wants it, this is the only form that should be built:

- **A separate credential.** `LAZYAF_ENDPOINT_ENROLL_TOKEN`, never the runner enrollment secret,
  issued per endpoint by the operator (`lazyaf endpoints issue-token local-4090`), stored
  hashed, revocable, and bound to ONE endpoint name.
- **Exactly one write: an idempotent upsert of the row that token names.** `POST /api/model-endpoints/self`.
- **Server-forced fields, ignored from the payload:** `name` (from the token), `reach`
  (`runner-local`), `runner_label` (`endpoint:<name>`), `rate_usd_hour` (`null`), `auth_style`
  (`none`), `enabled` (**`false`**). The row lands DISABLED and wave 8's dispatch already fails
  a disabled endpoint with a clear reason, so an operator flipping one switch is the entire
  remaining ceremony - and no cost figure, no URL used by another step, and no secret reference
  ever came from the node.
- **Node-supplied fields, and only these:** `base_url`, `model`, `server_kind`,
  `max_concurrency`, `context_window`, `description`.
- **No PATCH of an enabled row, no DELETE, ever.** A second upsert against an enabled row
  updates only `description` and `model` and re-disables nothing.
- **A compromised pod is then bounded to:** re-writing the base_url/model of one disabled row it
  already had a token for, which an operator must then look at and enable. That is the whole
  blast radius, and it is small enough to be worth the machinery only if the operator is
  standing up pods often enough for `lazyaf node recipe` to feel slow.

### 3.4 The mode question this phase cannot dodge

Section 0: the runner agent's only orchestrator is `docker`, and a runpod pod has no docker
socket. `LAZYAF_AGENT_MODE` is therefore a real three-valued setting:

| Mode | Deployment | Status in 14.5 |
|---|---|---|
| `docker` | home box / bare metal with `/var/run/docker.sock` mounted. Steps run as sibling containers using the existing `lazyaf-agent-base:dev` images. | **Works with zero new runner-agent code.** This is the phase's spine. |
| `off` | any pod where you only want the server. The supervisor runs the server, prints a `reach: direct` recipe, and never starts an agent. On runpod this pairs with the pod's public proxy URL. | **Works today**, and it is the honest runpod fallback if the next row is cut. Note it gives up the zero-inbound property, which is exactly why it is a fallback and not the design. |
| `native` | runpod, and any socketless pod. Steps run as processes INSIDE this container, as uid 1000, against `http://127.0.0.1:<port>/v1`. | **Requires `NativeOrchestrator`, which 12.6 deliberately did not build** (wave 5 section 10). Agent B builds it here, scoped to exactly what this image needs. |

**This is the wave's cut line and it is stated up front:** cutting Agent B leaves the home box
fully working in `docker` mode and leaves runpod on `off` + a `direct` endpoint. Shipping Agent
B is what makes "a runpod pod is a single-image deploy" literally true.

`NativeOrchestrator`, scoped:

- `capabilities() -> {"orchestrator": "native", "has": []}` - **deliberately no `has=docker`**,
  so a step requiring docker never matches a rented pod (section 7 relies on this).
- Workspace: one `/workspace/{repo,home,.control}` tree, uid 1000, because
  `MAX_CONCURRENT_STEPS = 1` means one step at a time and the control runtime's paths are baked
  at `/workspace/...`. **On a `retain_key` change the tree is wiped and re-cloned**, which is
  the non-docker equivalent of the volume-per-run the docker orchestrator gets from
  `DockerWorkspaceProvisioner`. Same `retain_key` reuses it, so `HOME=/workspace/home` persists
  across a run's steps exactly as it does everywhere else (the 12.3 contract).
- Control files: written into `/workspace/.control/<step_execution_id>.json` at mode 0600 owned
  1000:1000 - the same consume-once delivery `control_archive.build_control_archive` performs,
  minus the tar.
- Execution: `gosu lazyaf env HOME=/workspace/home LAZYAF_CONTROL=1 python3 /control/run.py`,
  in its own process group, cwd `/workspace/repo`. It re-uses `/control` verbatim, which is why
  `images/base/control/` is staged into the node images.
- Cancel: `killpg(SIGTERM)` then `SIGKILL` after grace; exit codes follow
  `docker_orch`'s existing convention (`143` cancelled, `124` timeout, `1` agent error).
- **Refusals, loudly:** an assignment carrying a bind mount, or naming an image whose
  capabilities this pod cannot provide, fails the step with a message naming `native` mode and
  what the step asked for. A native pod that silently ignores the requested image is a step that
  runs in the wrong environment and lies about it.

---

## 4. MODEL DATA

### 4.1 Mounts and env, per server

| | ollama | vLLM |
|---|---|---|
| Mount point | `/root/.ollama` (declared `VOLUME`) | `/root/.cache/huggingface` (declared `VOLUME`) |
| Env that controls it | `OLLAMA_MODELS=/root/.ollama/models` | `HF_HOME=/root/.cache/huggingface` |
| Model selection | `LAZYAF_MODEL=qwen2.5-coder:32b`; the server can hold many, the endpoint row names one | `LAZYAF_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct`; **one model per process**, chosen at server start, `--served-model-name` set equal to it |
| Optional | `LAZYAF_OLLAMA_KEEP_ALIVE=30m` -> `OLLAMA_KEEP_ALIVE` | `LAZYAF_SERVER_ARGS="--max-model-len 16384 --gpu-memory-utilization 0.92"`, `HF_TOKEN` for gated weights |
| Preload | supervisor drives `POST /api/pull` when the model is absent from `/api/tags` | none: vLLM downloads on start; readiness P3 IS the download |
| Recipe mount | `-v lazyaf-ollama:/root/.ollama` or `-v /srv/models/ollama:/root/.ollama` | `-v lazyaf-hf:/root/.cache/huggingface` or `-v ~/.cache/huggingface:/root/.cache/huggingface` |

`LAZYAF_OLLAMA_KEEP_ALIVE` defaults to `30m` and the docs state the trade in one line: ollama
unloads an idle model and reloads it on the next request, which on a 32B model is 30-60 seconds
charged to the first step after a gap. On a dedicated node, keeping it resident is right; the
default is not `-1` because an operator running several models on one box would be surprised by
permanent VRAM occupancy.

### 4.2 Cold cache

A cold cache is the NORMAL first boot, not an error, and the design treats it that way:

- The readiness timeout is **1800s by default** and `HEALTHCHECK --start-period` equals it, so a
  first boot downloading 20 GB is not killed by its own supervision.
- Preflight checks the cache path is a mount point and is writable, and **warns loudly when it
  is not a mount**: `"/root/.cache/huggingface is not a mount - a 30 GB download will be
  discarded when this container is removed. Mount it."` On a rented pod that warning is worth
  real money.
- Preflight prints what is already cached (`GET /api/tags` after start for ollama; a directory
  listing of `hub/models--*` for vLLM) so "cold" versus "warm" is stated before the wait begins,
  not inferred from how long it takes.
- Section 2.3's heartbeat is the in-progress signal: percentage for ollama, cache-growth rate
  for vLLM, an explicit "no growth in 45s, could be VRAM load, could be stuck" escalation, and
  the server's own last line every time.

### 4.3 What the cache is NOT

Weights are never baked into a published image (section 7), and `docker commit` on a warm pod is
called out in the docs as the thing not to do - it produces an image containing licensed weights
and a stale enrollment secret in one move.

---

## 5. GPU

### 5.1 Passthrough differs and the docs never assume

| Host | What the operator does | What the image sees |
|---|---|---|
| runpod | nothing - the platform injects the devices when a GPU pod is selected | `/dev/nvidia*`, `nvidia-smi` on PATH |
| local docker | `docker run --gpus all` **and** the NVIDIA Container Toolkit installed on the host | same |
| local, toolkit missing | `--gpus all` fails at `docker run` with a daemon error | container never starts - not our failure to report |
| local, `--gpus` forgotten | container starts, no devices | **this is the case the preflight exists for** |
| no GPU at all | - | same as above |

### 5.2 Detection, and what it refuses to depend on

`gpu.py::detect()` in order, first hit wins, each recorded in `status.json`:

1. `nvidia-smi --query-gpu=name,driver_version --format=csv,noheader` exits 0 with >= 1 line.
2. **`/dev/dxg` exists** - the WSL2 GPU device. **AMENDED BY SECTION 13.8:** WSL2 presents none of
   the Linux device nodes below, so without this row a Windows host with a perfectly good GPU
   would fall through to signals 3 and 4 and be reported as GPU-less. Signal 1 happens to save
   us, which is exactly the kind of accident that should be a stated row instead.
3. `/proc/driver/nvidia/version` exists (reports driver, not device count).
4. `/dev/nvidia0` exists.

`gpu.platform` (`linux` | `wsl2`) is stamped alongside, from `/dev/dxg` or a `microsoft` in
`/proc/version`, because the FIXES differ by platform (section 13.8) even when the verdict does
not.

It explicitly does **not** import torch: torch does not exist in the ollama image at all, and in
the vLLM image `import torch` costs ~10 seconds and initializes CUDA as a side effect - a
detection that is more expensive and more destructive than the thing it detects.

### 5.3 The verdict table

| Server | GPU present | Verdict |
|---|---|---|
| ollama | yes | proceed. Labels gain `accel=cuda`, `gpu=<count>` |
| ollama | **no** | **proceed, loudly.** ollama genuinely runs on CPU. One WARNING block at preflight naming the expected consequence ("a 32B model on CPU is roughly 1-3 tokens/sec; a single agent step will take tens of minutes and may exceed its timeout"), `accel=cpu` in the labels, `gpu.present=false` in `status.json`. An operator who wants a slow CPU node gets one; nobody gets one by accident |
| vLLM | yes | proceed |
| vLLM | **no** | **REFUSE at preflight. Exit 78, before the server is started at all.** |
| vLLM | no, `LAZYAF_ALLOW_NO_GPU=1` | proceed, with the same warning shape, and the acceptance is recorded in `status.json` |

The vLLM refusal message, because it is the difference between a diagnosis and a crash-loop:

```
[node] PREFLIGHT REFUSED: vLLM needs an NVIDIA GPU and this container has none.
[node]   checked: `nvidia-smi` -> not found
[node]            /proc/driver/nvidia/version -> absent
[node]            /dev/nvidia0 -> absent
[node]   If this is a local docker run: add `--gpus all` AND install the NVIDIA
[node]   Container Toolkit on the host (nvidia-ctk --version).
[node]   If this is runpod: the pod was created without a GPU - pick a GPU type.
[node]   To try CPU anyway (very slow, frequently OOMs): LAZYAF_ALLOW_NO_GPU=1
[node]   Or use the ollama node image, which degrades to CPU by design.
```

Without this, the observed behaviour is vLLM printing a torch CUDA traceback, exiting, being
restarted three times by the supervisor and then by the container restart policy forever - the
exact crash-loop the brief names. **The refusal is checked before the server is spawned**, so
the cost of a misconfigured pod is 200 milliseconds, not three model loads.

`accel` also becomes a schedulable label: `requires: {has: [...]}` plus `accel=cpu` in the
runner panel means an operator can see at a glance that the node they are about to fan out
across is running on a CPU.

---

## 6. RELEASE PROCESS

> **Owner decision, 2026-08-30, which REPLACES an earlier plan in this section:**
> **LazyAF builds the two heavy images itself, on a LazyAF runner**, through a repo-defined
> pipeline - not on GitHub compute. The GitHub-hosted analysis in 6.2 is kept rather than
> deleted, because it is *why* this split exists. Everything about the pin table, the content
> hash and the build-it-yourself path is unchanged.

### 6.1 The shape

| Artifact | Built by | Pushed by |
|---|---|---|
| `lazyaf-runner-ollama`, `lazyaf-runner-vllm` | **`.lazyaf/pipelines/images.yaml`, on a LazyAF runner with disk and a docker socket** | the same pipeline |
| step images in `build_images.py::IMAGES` (incl. `lazyaf-fake-inference`) | `.github/workflows/images.yml` | same |
| service images (backend, frontend, runner-agent) | `images.yml` | same |
| the `lazyaf` CLI wheel | `release.yml` | same |
| PR artifact check, secret scan, version check | `pr-build.yml`, `secret-scan.yml`, `release-please.yml` | nothing |

**`.github/workflows/inference-images.yml` is NOT created.** An earlier draft of this section
specified it; the owner's decision supersedes it, and `.github/scripts/inference_images.py` is
not created either - `build_inference_images.py` reads its own table.

### 6.2 Why the split exists (the GitHub finding stands; it is simply moot for these two)

Two mechanical facts, both still true:

1. **They cannot go in `scripts/build_images.py::IMAGES`.** `.github/scripts/step_images.py`
   READS that table, so `images.yml` would build and push every row on every push to `main`; and
   `run_tier.py` makes `build_images.py --check` a **T2 and T3 preflight**, so every developer
   and every dogfood run would be told to build an 8.6 GB image before running tests.
2. **A standard GitHub runner cannot build the vLLM image.** Measured compressed base sizes are
   3383 MB (ollama) and 8634 MB (vLLM); extracted is typically 2.2-2.5x, so roughly 7-9 GB and
   19-22 GB, and a build plus push needs room for the extracted image and the layers being
   uploaded.

| Image | Estimated disk to build + push | Standard runner (~14 GB free) | With a reclaim step (frees ~20-30 GB) |
|---|---|---|---|
| `runner-ollama` | ~20 GB | no | yes, comfortably |
| `runner-vllm` | ~45 GB | no | **still no** - reclaim lands around 30-35 GB free, on the edge, failing unpredictably mid-push |

The extracted numbers are ESTIMATES; the compressed ones are measured (section 0). **The point of
keeping this table is not that we still intend to fight it** - it is that "build it on a machine
we own" is a decision with a reason, and the reason is a number. The build node's preflight
enforces the same numbers, and the implementer replaces the estimates with what the first real
run reports.

There is also a second, better reason, independent of disk: **LazyAF gates LazyAF.** The project's
standing position is that GitHub packages what the dogfood pipeline blessed. Having LazyAF build
its own node images is that position applied one step further, and it is the largest thing LazyAF
has ever been asked to build - which makes it a real test of the platform rather than a chore
outsourced to somebody else's compute.

### 6.3 `.lazyaf/pipelines/images.yaml`

```yaml
name: "Node Images"
description: "Build and publish the combined runner+inference node images to GHCR. Runs on a
  runner labelled has=image-build with a docker socket and >=60GB free. NOT triggered by push -
  a 45 minute, 45 GB build is not something every commit should start."
triggers:
  - type: manual

steps:
  # 1. PREFLIGHT. Fails in seconds with a number, rather than at 90% of a push.
  #    Also prunes BuildKit cache older than a week: on a WSL2 host the VHDX
  #    does not shrink on its own (section 13.4), so cache growth is Windows
  #    disk that never comes back until somebody notices.
  - id: "preflight"
    name: "Disk, daemon and pins"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires: {has: ["docker", "image-build"]}
      needs: ["docker"]
      command: |
        docker builder prune -f --filter until=168h
        python3 scripts/build_inference_images.py "$LAZYAF_NODE_IMAGE" --require-disk-only
        python3 scripts/build_inference_images.py --check-pins
      environment:
        LAZYAF_NODE_IMAGE: "vllm"
    on_success: next
    on_failure: stop
    timeout: 900

  # 2. BUILD. The long one. No credential is in scope for this step at all -
  #    the same rule images.yml states: while anything is building there is
  #    nothing in the environment for it to capture.
  - id: "build"
    name: "Build the node image"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires: {has: ["docker", "image-build"]}
      needs: ["docker"]
      command: |
        python3 scripts/build_inference_images.py "$LAZYAF_NODE_IMAGE"
        python3 scripts/build_inference_images.py "$LAZYAF_NODE_IMAGE" --print-hash > .build-hash
      environment:
        LAZYAF_NODE_IMAGE: "vllm"
    continue_in_context: true
    on_success: next
    on_failure: stop
    # Measured on the first real run and written back here. 5400 is a ceiling
    # for a cold pull of an 8.6 GB base on a home uplink, not an estimate of
    # the normal case.
    timeout: 5400

  # 3. PUBLISH. The ONLY step the credential exists for, and the shortest.
  - id: "publish"
    name: "Tag, login and push to GHCR"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires: {has: ["docker", "image-build"]}
      needs: ["docker"]
      # Resolved BACKEND-SIDE from the backend's own environment and delivered
      # only inside the 0600 consume-once step config (section 6.4).
      secret_refs: ["LAZYAF_PUBLISH_GHCR_TOKEN"]
      command: |
        # Step-private docker config: `docker login` writes a credential into
        # $DOCKER_CONFIG/config.json, and HOME is /workspace/home - the RUN'S
        # SHARED VOLUME. Without this, the token would outlive this step and be
        # readable by every later step of the run.
        export DOCKER_CONFIG="$(mktemp -d)"
        trap 'docker logout ghcr.io >/dev/null 2>&1; rm -rf "$DOCKER_CONFIG"' EXIT
        # stdin, never argv: argv is visible in `ps` on a machine running other
        # people's steps.
        printf '%s' "$LAZYAF_PUBLISH_GHCR_TOKEN" \
          | docker login ghcr.io -u "$LAZYAF_PUBLISH_GHCR_USER" --password-stdin
        python3 .github/scripts/publish_image.py \
          --local "lazyaf-runner-$LAZYAF_NODE_IMAGE:dev" \
          --repo "ghcr.io/$LAZYAF_PUBLISH_GHCR_OWNER/lazyaf/runner-$LAZYAF_NODE_IMAGE" \
          --retries 3
      environment:
        LAZYAF_NODE_IMAGE: "vllm"
        LAZYAF_PUBLISH_GHCR_USER: "brennan-vanderlaan"
        LAZYAF_PUBLISH_GHCR_OWNER: "brennan-vanderlaan"
    continue_in_context: true
    on_success: next
    on_failure: stop
    timeout: 3600

  # 4. VERIFY. A SEPARATE step on purpose (section 6.6): `docker push` exiting
  #    zero is not proof that the registry now serves what we built.
  - id: "verify"
    name: "Prove the registry serves the image we built"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires: {has: ["docker", "image-build"]}
      needs: ["docker"]
      secret_refs: ["LAZYAF_PUBLISH_GHCR_TOKEN"]
      command: |
        python3 scripts/build_inference_images.py "$LAZYAF_NODE_IMAGE" \
          --verify-pushed "ghcr.io/$LAZYAF_PUBLISH_GHCR_OWNER/lazyaf/runner-$LAZYAF_NODE_IMAGE"
      environment:
        LAZYAF_NODE_IMAGE: "vllm"
        LAZYAF_PUBLISH_GHCR_OWNER: "brennan-vanderlaan"
    on_success: next
    on_failure: stop
    timeout: 900

  # 5. RECLAIM. Explicit, not a cron nobody remembers. It runs even when the
  #    push failed, which is exactly when the disk is fullest.
  - id: "reclaim"
    name: "Prune the build cache"
    type: script
    config:
      image: "lazyaf-base:dev"
      requires: {has: ["docker", "image-build"]}
      needs: ["docker"]
      command: |
        docker builder prune -f --filter until=24h
        df -h /
        docker system df
    on_success: next
    on_failure: continue
    timeout: 600
```

`LAZYAF_NODE_IMAGE` is spelled per step rather than once because the platform's YAML has no
pipeline-level variable block; a second pipeline file (or a second copy of these five steps) for
`ollama` is the honest cost of that, and it is five lines each. When the graph gains parameters,
this collapses to one file with a matrix.

**Which steps need `needs: [docker]`: all of them.** Every step drives the host daemon. The
runner-agent refuses a bind mount that is not on its own allowlist, so the build node must be
started with `LAZYAF_BIND_ALLOWLIST=/var/run/docker.sock` - deliberate friction the README
already documents, and correct: handing a machine's docker socket to a step is the machine
owner's decision.

**Pinned by LABEL, not by `runner_id`.** `requires: {has: ["docker", "image-build"]}` means the
build box can be replaced without editing the pipeline. `matches_requirements` does subset
containment on `has`, so nothing new is needed; the node advertises
`LAZYAF_RUNNER_LABELS=has=image-build`.

### 6.4 The GHCR credential

`secret_refs` is **one small, additive backend change** and it is stated as such. Today
`secret_environment` is computed backend-side only (`agent_secret_environment`) and there is no
way for a repo-defined step to ask for a secret. The addition:

```python
# backend/app/services/pipeline_executor.py
PUBLISH_SECRET_REF_RE = re.compile(r"^LAZYAF_PUBLISH_[A-Z0-9_]{1,48}$")
```

- A step's `config.secret_refs` is a list of backend environment variable NAMES. The backend
  resolves each from its own environment and puts `{name: value}` into `secret_environment`,
  which 12.5's machinery then delivers **only inside the 0600 consume-once step config file** -
  never merged into `run_kwargs["environment"]`, never in `docker inspect`, and on the remote
  path only inside `control_files`.
- **The prefix allowlist is load-bearing, and more so here than in wave 8.** Pipeline definitions
  sync from pushed commits, so the YAML is repo content. Without the allowlist,
  `secret_refs: ["ANTHROPIC_API_KEY"]` or `["LAZYAF_RUNNER_AUTH_SECRET"]` would exfiltrate the
  platform's own credentials into a container. A ref failing the regex is a **validation error
  when the pipeline is materialized**, not a dispatch failure - the push that introduced it says
  so.
- A ref that passes the regex but resolves to nothing is a **dispatch failure naming the
  variable**, 12.5's precedent verbatim.
- **What the allowlist does NOT do**, stated plainly: anyone who can push to this repo can
  already run arbitrary commands in a step container on your runners. `secret_refs` does not
  widen that; it hands that code a credential. The trust boundary is push access, and it is the
  same boundary the dogfood pipeline has always had. What the allowlist prevents is a *typo or a
  careless copy-paste* reaching a credential that has nothing to do with publishing.

**Token scope: `write:packages` and nothing else.** A fine-grained GitHub PAT (or a GitHub App
installation token) scoped to the packages of this owner - no `repo`, no `contents`, no
`workflow`. It is not the `GITHUB_TOKEN` (that only exists inside Actions), it is revocable on
its own, and it never enters git: it lives in the backend's `.env` as
`LAZYAF_PUBLISH_GHCR_TOKEN`, which `scripts/bootstrap_secrets.py` learns to prompt for.
`scan_repo_secrets.py` already fails a build if a live-format credential reaches the tree.

### 6.5 The failure modes that actually happen

| Failure | What it looks like | What the pipeline does |
|---|---|---|
| **Out of disk mid-build** | `no space left on device` 30 minutes in, usually while extracting the base | The `preflight` step's `--require-disk-only` refuses first with the number and the path; `docker builder prune --filter until=168h` runs before the check, so the number reflects reclaimable space. The `reclaim` step runs `on_failure: continue` so a failed run still frees its cache |
| **Push interrupted / partial layer upload** | `docker push` dies on a home uplink hiccup somewhere inside 8.6 GB | `publish_image.py --retries 3` with backoff. A retried push re-uses layers the registry already has, so a retry is cheap, and this is the NORMAL outcome of a large push on a residential connection - not an exception |
| **The build succeeds and the push silently does not** | `docker push` exits 0, the tag is absent or stale, and the first person to find out is somebody pulling next month | **`verify` is a separate step** (6.6) |
| **The node sleeps or reboots mid-build** | the step vanishes (section 13.6) | 12.6 requeues it; because the step is label-pinned it re-runs on the same box, where BuildKit's cache makes the retry cheap. This is also why the build node should have sleep disabled |
| **He starts gaming at minute 20 of a 45-minute build** | - | Nothing. The node stops accepting NEW steps and this one finishes - section 15.6, where the interaction is argued in full. The build is CPU/disk/network work that never touches the GPU |
| **Two runs of the pipeline at once** | two `docker build`s on one daemon, both slow, disk exhausted | `MAX_CONCURRENT_STEPS = 1` per runner agent (wave 5) means one step at a time on that box, and both runs pin the same label. The second run queues visibly |

### 6.6 How the pipeline proves the pushed image is the one it built

`docker push` exiting zero means the client thought it finished. The `verify` step re-reads the
registry as a client with no local cache and checks three things:

```
build_inference_images.py vllm --verify-pushed ghcr.io/<owner>/lazyaf/runner-vllm
```

1. **The tag resolves, and to which digest.** A registry manifest GET for each tag
   `publish_image.py` claimed to push (`sha-<7>`, `content-<12>`, and any version tags). A
   missing tag is a hard failure naming it - this is the "push silently did not happen" case.
2. **The remote digest equals the local one.** `docker image inspect --format '{{index
   .RepoDigests 0}}'` after the push, compared against the manifest's digest. A mismatch means
   something else now occupies that tag.
3. **The remote CONFIG BLOB carries the labels we stamped.** `lazyaf.content-hash` must equal the
   hash computed from the current staged tree, and `lazyaf.upstream` must equal the pin in
   `INFERENCE_IMAGES`. **This is the check that makes the claim "the pushed image is the one it
   built" rather than "something exists at that tag"** - the labels are computed from the source
   tree, so a stale image, a rebuilt-from-a-different-pin image, or a hand-pushed image all fail
   here.

Check 3 additionally refuses to accept a `content-<12>` tag whose remote label disagrees with the
local hash - two different trees claiming one content hash is either a collision or a force-push,
and both deserve a red step rather than a shrug.

### 6.7 Triggering, and how anyone learns the published image is behind

**Trigger: manual.** `triggers: [{type: manual}]`, launched from the UI or by
`lazyaf pipelines run node-images`. Not on push, not on a schedule. A 45-minute, 45 GB build
started by a commit that touched a README is a waste of an evening, and a nightly one is a waste
of an evening he is asleep for.

**Staleness, without making anyone build.** `build_inference_images.py --check-published`:

- computes the content hash of the current staged layer - pure file hashing, milliseconds, no
  docker, no pull;
- makes **one registry API call per image** to list tags;
- reports whether a `content-<12>` tag matching the current tree exists.

```
$ python scripts/build_inference_images.py --check-published
lazyaf-runner-ollama  content-4f2a91c0e3d1  PUBLISHED  (also tagged: main, sha-69f3ef0)
lazyaf-runner-vllm    content-9b1d77aa04e2  BEHIND     published: content-2c55e0198abb
                      -> the node layer or the vLLM pin changed since that build.
                      -> run: lazyaf pipelines run node-images   (about 45 min on buildbox)
```

It runs in three places and **fails in none of them**:

- as a **report step in the dogfood `test-suite.yaml`** with `on_failure: continue`, so the drift
  is visible on every push without a 45 GB build ever being attempted;
- in the release checklist, next to `--check-pins`;
- by hand, by anyone.

**Stated as a deliberate trade rather than hidden (R4):** this is a REPORT, not a gate. Gating a
push on a build that takes 45 minutes on one specific box would either block every push or teach
everyone to ignore a red step. The compensating control is that the drift is printed in the
dogfood output on every push, and that `verify` (6.6) makes any build that DOES run prove itself
completely.

**`build_images.py --check` never learns about these two images.** Contract 7 stands verbatim: no
developer and no T2/T3 preflight is ever told to build a multi-GB image.

### 6.8 What stays on GitHub, and one thing that must never move

| Workflow | Stays on GitHub | Why |
|---|---|---|
| `pr-build.yml` | **Yes, and this is non-negotiable** | Its trigger is `pull_request` on a **PUBLIC** repository, so it runs code from forks. A self-hosted runner behind that trigger would execute a stranger's PR on the owner's desktop, with his docker socket, his model cache and his enrollment secret on the same machine. **Nothing in this design puts his hardware behind an untrusted trigger.** The workflow is also already correct about this: `pull_request`, never `pull_request_target`, `contents: read`, no secrets referenced |
| `images.yml` | Yes | Small images, and it needs `GITHUB_TOKEN`, which only exists inside Actions |
| `release.yml` (CLI wheel) | Yes | Same |
| `secret-scan.yml` | Yes | It gates the others |
| `release-please.yml` | Yes | Version bookkeeping |

GitHub keeps packaging what the dogfood pipeline blessed. It simply no longer attempts the two
builds it cannot fit on disk.

A corollary worth writing down: **the build node must not carry a label that a fork-triggered
anything could target.** It cannot today - LazyAF's runners are reachable only from LazyAF's own
backend, and pipeline definitions sync from pushes to this repo, not from fork PRs - but the
sentence belongs here so that a future "let's have GitHub dispatch a LazyAF build" idea meets it
first.

### 6.9 `scripts/build_inference_images.py` (unchanged, plus three flags)

```python
"""Build the LazyAF combined runner+inference node images (Phase 14.5).

    python scripts/build_inference_images.py ollama vllm
    python scripts/build_inference_images.py --check-pins        # report, never gates
    python scripts/build_inference_images.py --check-published   # report, never gates
    python scripts/build_inference_images.py vllm --require-disk-only
    python scripts/build_inference_images.py vllm --verify-pushed <repo>

These are NOT in scripts/build_images.py's IMAGES table and never will be:
that table drives the per-push GHCR matrix AND the T2/T3 preflight, and a
multi-GB CUDA image in either is a broken developer loop. The two scripts
share their MACHINERY (stage_context, tree_hash, STAGE_EXCLUDE are imported
from build_images.py, never copied) and share nothing else.

This script is executed BY .lazyaf/pipelines/images.yaml inside a step
container, so it stays stdlib-plus-docker-SDK and prints numbers rather than
progress bars.
"""
from build_images import stage_context, tree_hash, local_hash, build_image  # R3

INFERENCE_IMAGES = [
    dict(
        subdir="runner-ollama", name="lazyaf-runner-ollama",
        upstream="ollama/ollama:0.33.2@sha256:9e7d782e99880c70f9563c51633da875ca605518a8f8d95c2532bda70a027b7a",
        # measured 2026-08-30: 3383 MB compressed, 4 layers
        est_disk_gb=20,
        extras=[(REPO_ROOT / "images" / "node-layer", "src/node-layer"),
                (REPO_ROOT / "runner-agent",          "src/runner-agent"),
                (REPO_ROOT / "images/base/control",   "control")],
    ),
    dict(
        subdir="runner-vllm", name="lazyaf-runner-vllm",
        upstream="vllm/vllm-openai:v0.28.0@sha256:2286e8533ca8b6bc777594bae30524f1426ba46ca21797524e06df6a94b06635",
        # measured 2026-08-30: 8634 MB compressed, 32 layers
        est_disk_gb=45,
        extras=[...same three...],
    ),
]
```

**What the content hash covers, and what it must not** (unchanged):
`content_hash = tree_hash(staged, extra=upstream_pin_string)` - every byte of our layer
(node-layer, the staged runner-agent minus `STAGE_EXCLUDE`, the staged control runtime) folded
with the upstream `repo:tag@digest` string.

- It **does** answer "is this image built from the current LazyAF layer against the upstream we
  pinned?" - the only question the chain has ever answered, and now also the input to
  `--check-published` and to `verify`'s check 3.
- It **does not** hash the upstream image's contents. We do not have them without pulling 8.6 GB,
  and pulling to hash would make a report a multi-GB operation. The digest in the `extra` string
  is a cryptographic identity for those contents already, supplied by the registry.
- `LABEL lazyaf.upstream=<pin>` records it in the image, so `docker inspect` answers "what is
  under this?" without reading a Dockerfile.
- **There is no parent-hash chain here.** The node images have no LazyAF parent.
  `lazyaf-fake-inference` DOES (`base`) and participates in the normal chain in `IMAGES` like
  every other row.

`--require-disk-only` runs a `shutil.disk_usage` check and refuses with the number BEFORE pulling
anything - one implementation protecting a laptop, a build node and the pipeline's preflight
alike.

### 6.10 How the pins are maintained: unchanged, see section 1.6

Both spellings in `FROM` (tag AND digest), one pin table, a contract test that the Dockerfile
matches it, `--check-pins` as a REPORT and never a bot, and a required driver-floor note in any
bump PR. The owner's build-it-ourselves decision changes none of that; the only difference is
that the bump PR is followed by a manual pipeline run rather than by a workflow dispatch.

### 6.11 Where it runs

The build node is the same class of machine as the Windows/WSL2 desktop in section 13 - a box
with a docker socket and real disk - and it may literally BE that machine.

| Requirement | Value | Note |
|---|---|---|
| Free disk | **60 GB recommended, 45 GB enforced** for vLLM; 25 GB for ollama | `--require-disk-only` enforces it. On WSL2 remember section 13.4: the VHDX grows and does not shrink, so the build cache is Windows disk that needs `docker builder prune` (step 5) and occasionally a VHDX compact |
| Docker socket | mounted, and `LAZYAF_BIND_ALLOWLIST=/var/run/docker.sock` on the agent | without the allowlist the `needs: [docker]` steps fail with a message naming the variable |
| Labels | `has=image-build` (plus `has=docker` from the orchestrator) | what the pipeline pins on |
| Roles | `LAZYAF_NODE_ROLES=steps` if this is the combined image on a box that is not also serving a model | section 14.2 |
| Yield | `LAZYAF_YIELD_MODE=always-available` on a dedicated box; **`auto` is correct on his desktop** | section 15.10 |
| Sleep | disabled | section 13.6 - a sleeping machine loses a 45-minute build |

**The interaction with GPU-idle yield, honestly:** if the build node is also his desktop serving
models and yielding on GPU busy, then a 45-minute image build is exactly the long-running step a
drain must let finish rather than abandon - and it does. Section 15.6 argues that case in full and
is not restated here; the short version is that the build never touches the GPU, the node stops
accepting new steps the moment he starts gaming, and the build he already paid 20 minutes for
runs to completion.

### 6.12 Tags, cadence, and building it yourself

- **Same `publish_image.py`, same tag policy** - `sha-<7>`, `content-<12>`, `vX.Y.Z`, `latest` on
  a stable tag. The script is stdlib and runs fine inside a step container, so there is **one tag
  policy in the system**, not one for GitHub and another for the pipeline (R3).
- **Cadence: on a pin bump, and on a node-layer change.** Not per LazyAF release - the node layer
  changes far less often than the platform, and republishing 8.6 GB because a frontend store
  changed is not a release process. `--check-published` is what makes "we have not built since
  that change" visible instead of forgotten.
- **The build-it-yourself path stays first-class**, because for vLLM it is what most people will
  do anyway - they are on a machine with a GPU and 100 GB free, which is exactly the machine this
  pipeline needs and exactly what CI is not:

```bash
git clone https://github.com/<owner>/lazyaf && cd lazyaf
python scripts/build_inference_images.py vllm      # ~15 min on a warm cache
docker run --gpus all ... lazyaf-runner-vllm:dev
```

`docs/node-images.md` carries that, the `docker run` recipes for both servers and all three agent
modes, the driver-floor note from 6.10, the build-node runbook from 6.11, and the
endpoint-registration walkthrough.

---

## 7. SECURITY

### 7.1 The runner token on rented hardware is the sharpest edge in this phase

`LAZYAF_RUNNER_AUTH_SECRET` is a **shared fleet enrollment secret**: every runner uses the same
value, and there is no per-runner credential today (12.7 removed the public default but did not
make it per-runner). A pod on hardware you rent by the minute, that other tenants' workloads
have shared a host with, holds it.

What a compromised pod can do, stated plainly:

| Reach | Bound by |
|---|---|
| Enrol additional runners with arbitrary labels | nothing today. This is the gap, and it is named rather than hidden |
| Receive `execute_step` frames | **only for steps whose `requires:` it matches.** A step with no `requires:` is routed LOCAL and never reaches any runner (wave 8 section 6.2's test 4 pins this) |
| Read a step's source, prompts, step JWT and `secret_environment` | the steps it is assigned. For an agent step that `secret_environment` can carry `ANTHROPIC_API_KEY` |
| Call the backend as a step | the step JWT's TTL, which expires with the step |
| Write `ModelEndpoint` rows | **nothing - it has no such credential** (section 3.1) |

Mitigations that exist and must be in the docs as instructions, not as prose:

1. **`wss://` is mandatory off-loopback.** The agent already refuses plaintext to a non-loopback
   host; the node image **does not set `LAZYAF_RUNNER_ALLOW_INSECURE`** and the recipe never
   emits it. A rented pod talking `ws://` broadcasts step JWTs and `secret_environment` across
   the internet.
2. **The token is delivered by FILE, never by env.** The recipe uses
   `LAZYAF_RUNNER_TOKEN_FILE=/run/secrets/lazyaf-runner-token` (a path `config.resolve_token`
   already supports and prefers). Concretely important here: in `native` mode a step runs as uid
   1000 on this same kernel, and an env var on the agent process is one `/proc/<pid>/environ`
   away from a step that manages to become root. A 0600 root-owned file is not.
3. **Do not give a rented pod `has=docker`.** `NativeOrchestrator` reports `has: []` by design,
   so a native pod matches only steps that require its endpoint label. A `docker`-mode pod
   matches any step requiring docker - that is right for a home box you own and wrong for
   rented hardware, and the docs say so at the point where the mode is chosen.
4. **`LAZYAF_BIND_ALLOWLIST` stays empty** on a node image. The agent already refuses non-allowlisted
   bind mounts; the recipe never adds one.
5. **The gap is written down**: per-runner tokens are a backend change (issue, store hashed,
   revoke, bind to a runner id) and are **out of scope for 14.5**. It is named in the risk
   register with the mitigation that exists today (rotate the fleet secret when a pod is
   returned), because an unstated gap is the one nobody rotates for.

### 7.2 Endpoint auth in the pod

- **ollama has no authentication at all.** A `runner-local` ollama endpoint's security is that
  the port is not published to the internet. The recipe binds the server to the node's own
  network (`--network lazyaf-node`) and publishes nothing; `LAZYAF_BIND_LOCALHOST_ONLY=1` (the
  default in `native` mode) makes vLLM listen on `127.0.0.1` only, where "only processes in this
  container" is enforced by the kernel rather than by a firewall rule somebody forgot.
- **vLLM supports `--api-key`.** When `LAZYAF_SERVER_API_KEY_FILE` is set, the supervisor passes
  `--api-key` from the file's contents and the operator registers the endpoint with
  `auth_style: bearer` and a `LAZYAF_ENDPOINT_*` secret ref on the backend. Wave 8's rules then
  apply unchanged: the value lives only in `secret_environment`, never in the row, never in
  `docker inspect`.
- **`HF_TOKEN` is a run-time input only.** Never a build arg, never an `ENV` in a published
  layer - `scan_image_secrets.py` check 1 fails a build that tries, which is the mechanical
  proof rather than the promise.

### 7.3 Licensed weights

The mounted cache can contain gated or licence-restricted weights (Llama's community licence,
gated HF repos, anything requiring acceptance of terms). Rules, in the docs and in the risk
register:

- **Weights are never baked into a published image.** No `COPY` of a model, no warm-cache
  layer, no `--build-arg HF_TOKEN`. A published image containing gated weights is
  redistribution, and the redistribution terms are somebody else's to grant.
- **Never `docker commit` a warm pod.** It captures the weights AND whatever secrets are in the
  running config, and it produces an image with no content hash and no upstream label - an
  artifact nobody can later account for.
- **The `VOLUME` declarations in both Dockerfiles are load-bearing**, not decorative: a build
  step that wrote into `/root/.ollama` or `/root/.cache/huggingface` would be discarded, which
  turns the "do not bake weights" rule into something the image format enforces.
- **Milestone 13's published bundle** records the endpoint's `model` string and the node
  image's `lazyaf.upstream` + `content-<hash>` for provenance. It does not, and must not, ship
  weights.

---

## 8. TESTING WITHOUT A GPU AND WITHOUT MULTI-GB PULLS

The claim this section has to earn: **entrypoint behaviour, supervision policy, readiness,
label advertisement, endpoint registration and the runner-local probe are ALL verifiable in T1
and T2 with zero bytes of ollama or vLLM pulled.** It is earnable because the only thing the
upstream images contribute is a server binary, and the contract with that binary is HTTP over
localhost - which wave 8's `tdd/support/mock_openai_server.py` already speaks.

### 8.1 T1 - the supervisor, in-process, no docker, no server

`tdd/unit/node/` imports `lazyaf_node` directly out of `images/node-layer/` by path, exactly as
`tdd/unit/control_runtime/` already imports `images/base/control/`. Every collaborator on
`Supervisor` is injected (`spawn`, `probe`, `clock`, `status`), so children are fakes with
scripted exits and readiness is a stub or - better, and this is the lever - the REAL
`readiness.probe` pointed at wave 8's mock OpenAI server on a loopback port.

Covered in T1:

- start order (agent is not spawned before readiness returns true - asserted on the fake spawn's
  call order, not on a sleep);
- readiness is not port-open: a socket that accepts and 404s `/v1/models` never reaches P2; a
  server that lists a DIFFERENT model never reaches P3; `LAZYAF_READY_PROBE_COMPLETION=0` skips
  P4 and says so;
- readiness timeout -> exit 70 with the ring buffer's last lines in the message;
- server death -> agent stopped BEFORE the server is respawned (order asserted), budget of 3 in
  600s, exhaustion -> 71;
- agent exit 2 -> no restart, exit 2; agent exit 1 -> restart with backoff, server untouched;
- SIGTERM -> agent then server, grace then SIGKILL, exit 0;
- the GPU verdict table, all five rows, against a fake `detect()`;
- `advertise.py`: `LAZYAF_ENDPOINT_NAME=local-4090` produces `has=endpoint:local-4090` MERGED
  into an operator's existing `LAZYAF_RUNNER_LABELS` (never replacing), and the resulting string
  round-trips through the agent's real `config.parse_labels` to the expected dict - **the
  producer and the consumer meet in one test** (R3);
- the advertise-URL derivation table (section 3.2), all five rows;
- exit codes are the named constants, and no code path calls `sys.exit(1)`;
- the heartbeat: cache growth produces a rate, three zero-growth intervals escalate the wording,
  the ollama `POST /api/pull` NDJSON stream becomes percentages;
- secret hygiene: a sentinel enrollment token, a sentinel `HF_TOKEN` and a sentinel API key
  appear in no emitted line and in no `status.json`;
- `test_node_image_contract.py`: the three Dockerfiles all `RUN` the same
  `images/node-layer/install.sh`; the `FROM` pins match `INFERENCE_IMAGES` exactly (tag AND
  digest); neither big image appears in `build_images.py::IMAGES`; `lazyaf-fake-inference` does;
  LF line endings; no `:latest`; no credential-shaped `ENV`.

### 8.2 T2 - the whole node, for real, in a container

`tdd/integration/services/node/test_fake_inference_node.py`, using `lazyaf-fake-inference:dev`
(built by `build_images.py` from `lazyaf-base:dev`, already in the T2 preflight):

1. **Supervision, end to end.** Start the container. Assert `status.json` reaches
   `phase=serving`, `lazyaf-node-health` exits 0, and `docker logs` contains both a `[server]`
   and an `[agent]` prefixed line.
2. **The registration story.** The runner appears in `GET /api/runners` with
   `labels.has` containing `endpoint:t2-fake`, and its `orchestrator` label matches the mode
   under test.
3. **The runner-local probe, from the step's network position.** Register a `reach=runner-local`
   endpoint whose `base_url` is the advertise URL the supervisor printed, `POST .../probe`, and
   assert the capability record lands with `probed_from == "runner:<id>"`. This is the wave 8
   section 2.3 path exercised against a real node for the first time.
4. **The withdrawal invariant - the crux of section 2.** Kill the fake server process inside the
   container (`docker exec ... pkill -f mock_openai_server`) and assert that within
   `SERVER_DEATH_WITHDRAW_SECONDS` the runner is no longer in the schedulable set (its WS is
   closed / it is not returned by the runner list as connected), then that it comes back once
   the supervisor's restart brings the server through readiness again. **A step dispatched
   during the gap must fail with `NO_RUNNER_TIMEOUT`, not with a connection refusal from a model
   that is not there.**
5. **A real harness step on the node.** With `LAZYAF_AGENT_MODE=docker` and the socket mounted,
   an `openai-harness` agent step pinned to `endpoint:t2-fake` runs to success with
   `StepRun.executor == "remote"` and a `StepUsage` row carrying `provider == "openai-compatible"`.
6. **`native` mode (Agent B).** The same step with `LAZYAF_AGENT_MODE=native` and NO socket
   mounted: the step runs as a process in the container, its files land uid 1000, a second
   `retain_key` wipes and re-clones, and a `requires: {has: [docker]}` step never matches this
   runner.
7. **Secret hygiene in the real container:** `docker inspect` of the node container carries no
   token value (it is a file mount), and the step containers it spawns carry none either.

### 8.3 What is NOT testable without the pull, stated rather than implied (R4)

| Claim | Verified by |
|---|---|
| `install.sh` succeeds on the real ollama / vLLM base (apt available, python3-venv installs, no conflict with the upstream interpreter) | the first real build. **Nothing in T1/T2 proves this** - `lazyaf-fake-inference` is Debian-based like both upstreams, which makes it likely, not proven |
| `/bin/ollama serve` and `vllm serve` argv are still correct for the pinned tag | the real build + a manual boot |
| CUDA initializes; the driver floor is satisfied | real hardware only |
| Time-to-first-token, model load times, the realism of the 1800s default | real hardware only |
| `HEALTHCHECK` start-period behaviour on a genuinely cold 20 GB cache | real hardware only |

### 8.4 Manual bring-up checklist (owner-run, each step producing an artifact)

1. `python scripts/build_inference_images.py ollama` -> image with a `lazyaf.upstream` label.
2. `docker run --gpus all --network lazyaf-node --name lazyaf-node --hostname lazyaf-node \
   -v lazyaf-ollama:/root/.ollama -e LAZYAF_MODEL=qwen2.5-coder:32b \
   -e LAZYAF_ENDPOINT_NAME=local-4090 -e LAZYAF_BACKEND_URL=wss://... \
   -e LAZYAF_RUNNER_TOKEN_FILE=/run/secrets/tok -v ...:/run/secrets/tok:ro \
   -e LAZYAF_STEP_NETWORK=lazyaf-node -v /var/run/docker.sock:/var/run/docker.sock \
   lazyaf-runner-ollama:dev`
   **Artifact:** the REGISTER THIS ENDPOINT block in `docker logs`, and the runner `idle` in the
   runner panel with `has=endpoint:local-4090`, `accel=cuda`.
3. Register the endpoint with the printed command; `POST .../probe`.
   **Artifact:** a capability record with `probed_from = runner:<id>` and a real
   `context_window`.
4. Run one card through it (`agent: openai-harness`, `endpoint: local-4090`).
   **Artifact:** a pushed branch and a `StepUsage` row with `cost_source == "gpu-node"`.
5. `docker kill --signal=KILL` the ollama process inside the container.
   **Artifact:** the runner leaves the panel, the supervisor's restart log, the runner returning
   after readiness. This is the one invariant worth checking by hand because it is the one that
   protects every fan-out.
6. Repeat 1-5 with `vllm` on a runpod pod, `LAZYAF_AGENT_MODE=native`.

`tdd/tier_floors.json` rises for T1 and T2, **re-measured after the wave, not guessed**, with
the reason in `note` as every prior raise did.

---

## 9. RISK REGISTER

| Risk | The failure it produces | Mitigation, and where it is tested |
|---|---|---|
| **Two processes, one container, no init** | Reparented orphans (ollama's per-model runners, vLLM's workers) accumulate as zombies on a pod that runs for days; eventually fork fails and the node dies with an error about nothing | `tini` as PID 1 does exactly one job and does it correctly, and the supervisor's own children are reaped for their exit codes. Rejected `sh + trap` explicitly for this (section 2.1). T1 asserts the entrypoint is `tini` and that the supervisor collects a child's exit code rather than leaving it |
| **A runner advertising a model that is gone** | The server OOMs mid-fan-out; the agent stays connected; K steps are scheduled to a runner whose model is not there and each fails on connection refused after burning its container start | The advertisement IS the connection (section 0). On server death the agent is stopped FIRST, the WS closes, `find_available` stops returning the runner because it filters on `runner.id in self._connections`, and in-flight work is requeued by 12.6's existing `on_runner_disconnect`. No backend change. **T2 test 4 kills the server and asserts the withdrawal**, and asserts a step dispatched in the gap fails as unschedulable rather than as a connection error |
| **A cold cache that looks like a hang** | An operator watching a silent container for twenty minutes kills a 20 GB download at 90%, twice, and concludes the image is broken | The 15s heartbeat naming the phase, the elapsed time, the cache size AND its delta, plus the server's own last line; real percentages for ollama via `POST /api/pull`; an explicit escalation after 45s of zero growth that names both possibilities instead of guessing. `HEALTHCHECK --start-period` equals the readiness timeout so the container is not killed by its own supervision. T1 drives the heartbeat with a fake clock and a fake growing directory |
| **vLLM crash-looping with no GPU** | `--gpus all` forgotten; torch traceback every 40 seconds forever; the actual message ("no NVIDIA device") is 200 lines up in a log nobody scrolls | Preflight refuses BEFORE the server is spawned, exit 78, with the three things it checked and the two fixes (section 5.3). ollama takes the other branch and degrades to CPU loudly with a stated consequence and an `accel=cpu` label. T1 covers all five rows of the verdict table |
| **The heavy images have exactly one build machine** | The box is asleep, out of disk, or gone, and nobody can publish a node image. Or worse: a build dies at 90% of an 8.6 GB push with "no space left on device", intermittently, and the fix is invisible | This is the cost of the owner's build-it-ourselves decision (section 6) and it is accepted with instruments rather than denied. The `preflight` step refuses in seconds with a number; `docker builder prune` runs before that check so the number is real; the `reclaim` step runs `on_failure: continue` so a failed run still frees its cache; the push retries three times because a partial upload is the NORMAL outcome on a home uplink; and the step is label-pinned so a requeue after a reboot lands on the same warm BuildKit cache. **The documented build-it-yourself path is first-class** precisely because it is also the disaster recovery: anyone with a GPU box and 60 GB can produce a byte-identical image from the pin table. The GitHub-runner analysis is kept in 6.2 as the reason this machine exists |
| **A push that silently did not happen** | `docker push` exits 0, the tag is absent or stale, and the first person to find out is somebody pulling next month | `verify` is a SEPARATE step that re-reads the registry with no local cache and checks three things (6.6): the tag resolves; the remote digest equals the local one; and the remote CONFIG BLOB's `lazyaf.content-hash` and `lazyaf.upstream` labels equal what the current tree computes. The third is what turns "something exists at that tag" into "the registry serves the image this pipeline built" |
| **A publish credential outliving its step** | `docker login` writes `$DOCKER_CONFIG/config.json`, and `HOME` is `/workspace/home` - the run's SHARED volume - so a later step of the same run could read the GHCR token | The `publish` step exports a step-private `DOCKER_CONFIG=$(mktemp -d)` and traps `docker logout` plus `rm -rf` on EXIT. The token itself arrives only through 12.5's `secret_environment` (0600, consume-once, never `docker inspect`), is fed to `docker login` on **stdin never argv** (argv is visible in `ps` on a box running other people's steps), is scoped `write:packages` only, and can only be named by a `LAZYAF_PUBLISH_*` ref (6.4) |
| **A rented pod holds the fleet enrollment secret** | One compromised pod can enrol runners and receive any step that matches its labels, including that step's `secret_environment` | `wss://` mandatory (the agent already refuses plaintext and the image never sets the override); the token delivered by 0600 file, never env, so a same-kernel step cannot read it out of `/proc`; `NativeOrchestrator` advertising `has: []` so a rented pod matches only its own endpoint's steps; empty bind allowlist. **The residual gap - no per-runner tokens - is named, not hidden**, with rotation as today's answer |
| **Licensed weights in a published artifact** | An image or a bundle redistributes gated weights nobody had the right to redistribute | Weights never enter a build context; `VOLUME` on both cache paths makes a build-time write discard itself; `HF_TOKEN` is run-time only and `scan_image_secrets.py` check 1 fails a build that bakes it; "never `docker commit` a warm pod" is in the docs next to the recipe that would tempt someone to |
| **Upstream pin rot** | A bump silently raises the CUDA driver floor (the pinned vLLM tag already requires `driver>=535`) and somebody's home box stops booting after a routine update | Both spellings in `FROM` (tag AND digest), one pin table, a contract test that the Dockerfile matches it, `--check-pins` as a REPORT not a bot, and a required driver-floor note in any bump PR read out of the new config blob |
| **The advertise URL is right for the pod and wrong for the step** | The endpoint probes `unreachable` from a runner that is plainly running, and the operator has no way to tell whether the label or the URL is wrong | The derivation table (section 3.2) covers every mode, the supervisor PRINTS the URL rather than assuming it, and wave 8's probe runs from the step's own network position so the answer is authoritative. The two failure modes are separable by their messages: `NO_RUNNER_TIMEOUT` naming the labels means the label is wrong; a connect error naming the URL means the URL is wrong |
| **`native` mode puts a step on the same kernel as the model cache** | A `run_shell` in the harness reads the HF cache, or `/proc` of the agent | The harness's file tools are workspace-confined already (wave 8 section 3.1); the step runs as uid 1000 while the agent and the cache are root-owned; the token is a 0600 root file. Stated as a real reduction in isolation compared with `docker` mode, and the docs say to prefer `docker` mode where a socket exists |

---

## 10. WAVE SPLIT - 3 agents, disjoint file ownership

> **Superseded in part by section 16.5:** the 2026-08-30 addendum adds a fourth agent (D -
> backend availability and `secret_refs`) and extends A, B and C. The ownership below stands as
> written; 16.5 lists what each agent gains and the revised launch order.

Launch order: **A first and alone** (its layer and its constants are imported by B's tests and
staged by C's build script). Then B and C in parallel. **Wave 9 starts after wave 8's Agent A
and Agent D have landed** - it consumes `ModelEndpoint`, the probe, `endpoint_probe.py` and
`tdd/support/mock_openai_server.py`, and it edits none of them.

### Agent A - the node layer and the supervisor

**Owns exclusively:** `images/node-layer/**` (new), `images/runner-ollama/Dockerfile` (new),
`images/runner-vllm/Dockerfile` (new), `images/fake-inference/Dockerfile` (new),
`tdd/unit/node/**` (new), `tdd/unit/control_runtime/test_node_image_contract.py` (new).

**Test contract:**
1. The full section 8.1 T1 list, one test per row of the state machine, the verdict table, the
   advertise-URL table and the exit-code table.
2. Readiness against the REAL wave 8 mock server on loopback, covering P1-P4 and each phase's
   negative case (accepts-but-404s; lists a different model; P4 returns 500).
3. Supervision ordering asserted on a recorded call sequence, never on a sleep: agent-after-ready,
   agent-stopped-before-server-restart, agent-before-server on SIGTERM.
4. `advertise.py`'s label string parsed by the agent's real `config.parse_labels` - producer and
   consumer in one test, including an operator's pre-existing `has=` entries surviving.
5. Secret sentinels absent from every log line and from `status.json`.
6. Image contract: one shared `install.sh` across three Dockerfiles; `FROM` matches the pin
   table; neither big image in `IMAGES`; LF endings; no `:latest`; no credential `ENV`.

### Agent B - `NativeOrchestrator` and the agent-side integration

**Owns exclusively:** `runner-agent/lazyaf_runner/orchestrator/native.py` (new),
`runner-agent/lazyaf_runner/orchestrator/registry.py` (the one `ORCHESTRATORS` entry),
`runner-agent/lazyaf_runner/config.py` (only if a new env is genuinely required - prefer none),
`runner-agent/README.md` (the native section), `runner-agent/tests/test_native_orchestrator.py`
(new), `scripts/run_tier.py` (**the `../runner-agent/tests` selection line only**).

**Test contract:**
1. `capabilities()` is `{"orchestrator": "native", "has": []}`; a `requires: {has: [docker]}`
   step does not match; the endpoint label still does.
2. Workspace: fresh `retain_key` wipes and re-clones; the same `retain_key` reuses and
   `HOME=/workspace/home` persists; the tree and the control file are 1000-owned and the config
   is 0600.
3. Execution: `/control/run.py` is invoked under the privilege drop; a non-zero exit is a
   `StepOutcome`, never an exception; cancel kills the process GROUP and reports 143; a timeout
   reports 124.
4. Refusals: a bind mount is refused naming `LAZYAF_BIND_ALLOWLIST`; an assignment whose image
   this pod cannot provide fails the step with a message naming `native` mode - **it never runs
   silently in the wrong environment**.
5. `test_orchestrator_seam.py` stays green: `orchestrator/base.py` and `types.py` still import
   nothing from `docker`.
6. **`../runner-agent/tests` is added to T1's selection and actually runs** - the 12.7 precedent
   for `runner-common/tests`. If `websockets` or the docker SDK is missing from the backend `uv`
   dev environment, it is added there in the same commit; a suite that silently fails to collect
   is worse than one that was never selected.

### Agent C - release, CI, docs, the operator's recipe, T2

**Owns exclusively:** `scripts/build_inference_images.py` (new),
**`.lazyaf/pipelines/images.yaml` (new - section 6.3)**,
`scripts/build_images.py` (**the one `lazyaf-fake-inference` row only**),
`docs/node-images.md` (new), `cli/lazyaf/cli.py` (**the `lazyaf node` group only**),
`tdd/integration/services/node/test_fake_inference_node.py` (new, T2),
`tdd/unit/scripts/test_build_inference_images.py` (new),
`tdd/unit/config/test_images_pipeline_yaml.py` (new), `tdd/tier_floors.json`.

**Coordinates with Agent D on one thing:** `secret_refs` (section 6.4) is a backend change in
`pipeline_executor.py` and in the pipeline-YAML validation, not a CI change. D owns the code and
the `PUBLISH_SECRET_REF_RE` allowlist; C owns the YAML that uses it and the test that no other
key can reach a credential. **No GitHub workflow is created by this wave** - `inference-images.yml`
and `.github/scripts/inference_images.py` were in an earlier draft and are superseded.

**Test contract:**
1. `build_inference_images.py`: the content hash folds the upstream pin (two different pins over
   an identical tree produce different hashes); `stage_context`/`tree_hash` are IMPORTED from
   `build_images.py`, asserted by an AST check that neither is redefined; `--require-disk`
   refuses with the number and pulls nothing; `--check-pins` never exits non-zero.
2. `inference_images.py --refs/--pairs` reads `INFERENCE_IMAGES`; a row added to the table is
   scanned and published without editing the workflow.
3. `lazyaf-fake-inference` is in `IMAGES` with parent `base`, and `step_images.py` therefore
   lists it - the "read from the table" rule is unbroken.
4. `lazyaf node recipe` emits a `docker run` and a matching `lazyaf endpoints add` whose endpoint
   NAME and label agree by construction; a golden-file test pins both.
5. The full section 8.2 T2 list, including the server-death withdrawal (test 4), which is the
   one this phase exists to prove.
6. Tier floors re-measured, not guessed, with the reason in `note`.

---

## 11. CROSS-AGENT CONTRACTS (pin these first; they are the only shared surfaces)

1. **`/opt/lazyaf` is the ONE layout and `images/node-layer/install.sh` is the ONE installer**,
   run identically by all three Dockerfiles. **Nothing is ever pip-installed into the upstream
   image's python interpreter** - the LazyAF venv at `/opt/lazyaf/venv` is the boundary, and
   `vllm/vllm-openai`'s uv-managed pinned torch stack is the reason. Owner A; pinned by
   `test_node_images_share_one_layer`.
2. **`LAZYAF_ENDPOINT_NAME` -> `has=endpoint:<name>` is the ONE translation**, implemented in
   `advertise.py` and nowhere else. The `endpoint:` prefix must equal wave 8's
   `ENDPOINT_MODEL_PREFIX` (backend-side, wave 8 contract 4); the node image cannot import
   backend code, so a T1 test imports BOTH and asserts they are the same string. Owner A.
3. **The supervisor never writes to the LazyAF API.** The only sockets it opens itself are to
   `127.0.0.1:<server port>`. Registration is the operator's, always (section 3.1). Owner A;
   asserted by an AST/import check that `lazyaf_node` contains no call to a LazyAF API path.
4. **The enrollment secret reaches the agent by FILE only** (`LAZYAF_RUNNER_TOKEN_FILE`). The
   image never sets `LAZYAF_RUNNER_TOKEN`, the recipe never emits it, and
   `LAZYAF_RUNNER_ALLOW_INSECURE` is never set by anything this phase ships. Owners A and C.
5. **`SERVERS` keys are `ModelEndpoint.server_kind` values.** `ollama` and `vllm` here mean
   exactly what they mean in wave 8's row; `fake` is test-only and never registered on a real
   endpoint. Adding a server is one row plus its tests, and readiness reuses
   `runner_common.endpoint_probe`'s request shapes rather than inventing a second set. Owner A.
6. **Exit codes are the section 2.5 table**, defined as named constants in `lazyaf_node/__init__.py`.
   `2` is `EXIT_FATAL` passthrough and must equal `lazyaf_runner.client.EXIT_FATAL`; a test
   imports both. Owner A.
7. **Table membership, and who builds what.** `lazyaf-runner-ollama` and `lazyaf-runner-vllm`
   are NOT in `scripts/build_images.py::IMAGES` - not now, not later, and a test asserts their
   absence with the reason in its docstring. `lazyaf-fake-inference` IS. The heavy images are
   built ONLY by `.lazyaf/pipelines/images.yaml` (section 6); **no GitHub workflow builds them**,
   and a test asserts that no file under `.github/workflows/` references `runner-ollama` or
   `runner-vllm`. `build_inference_images.py` reads its own `INFERENCE_IMAGES` table directly.
   Owner C.
8. **The content hash is `tree_hash(staged_layer, extra=upstream_pin)`**, and
   `LABEL lazyaf.upstream` records the pin. It describes OUR layer against a NAMED upstream and
   claims nothing about the upstream's contents. Owner C; `stage_context` and `tree_hash` are
   imported from `build_images.py`, never reimplemented.
9. **`runner-agent/tests` joins T1 in this wave.** Owner B. A test written into a suite no tier
   runs is R4's "fake green" with extra steps.
10. **Nothing in this wave edits a wave 8 file.** `ModelEndpoint`, the probe,
    `endpoint_probe.py`, the `requires:` injection and `mock_openai_server.py` are consumed as
    they are; if one of them is missing when this wave starts, the wave stops and waits rather
    than forking a second copy.

---

## 12. Seams left open on purpose

- **Per-runner enrollment tokens.** The single biggest security improvement available to this
  deployment shape, and a backend change (issue, hash, revoke, bind to a runner id) rather than
  an image one. Named in section 7.1 and in the risk register so it is a decision, not an
  oversight.
- **Multi-GPU / tensor-parallel vLLM.** `LAZYAF_SERVER_ARGS` passes `--tensor-parallel-size`
  through and nothing in this design objects, but `max_concurrency` and `gpu_fraction`
  (wave 8 section 6.4) still model one endpoint as one queue. Real occupancy accounting on a
  multi-GPU node is deferred with `container_seconds` and `max_concurrency` both recorded, so it
  is recomputable retroactively.
- **Serving more than one model from one ollama node.** The server can; this image advertises
  ONE `LAZYAF_ENDPOINT_NAME`. Two endpoints against one ollama is already legal in wave 8's
  model (two rows, one `base_url`); making one pod advertise two labels is a small change to
  `advertise.py` and is deliberately not made until somebody wants it.
- **arm64 and ROCm.** Both upstreams publish them (`ollama/ollama:0.33.2-rocm` at 1.43 GB;
  arm64 variants of both). `images.yml`'s stated amd64-only position stands until someone asks.
- **Auto-bumping the upstream pins.** Rejected in section 1.6. `--check-pins` reports; a human
  bumps.
- **A "known-good model" matrix.** Same answer wave 8 gave for endpoints: the probe reports what
  THIS server with THIS chat template actually does. `docs/node-images.md` gets a dated "models
  we have actually driven on these images" list with no implied guarantee.
- **Runpod's own API.** Creating, starting and reaping pods from LazyAF is a provider
  integration and a spending authority; this phase ships an image you can paste into runpod's
  UI, and stops there.

---

# ADDENDUM (2026-08-30) - the owner's desktop as a node

Three owner answers landed after sections 0-12 were written, and they add scope rather than
change it. Nothing above is retracted; section 5.2's detection order gains one WSL row (marked
there) and everything else below is additive.

| Question | Answer | Where it lands |
|---|---|---|
| Node role | **both, selectable per endpoint** - one node can serve models AND execute steps, distinguished by labels | section 14 |
| Idle policy | **yield on GPU busy** - LazyAF must not fight him for the GPU | section 15 |
| Shape | **Docker Desktop / WSL2 backend, the 14.5 combined image, `--gpus all`.** Native-in-WSL is NOT first-class | section 13 |

The target is a Windows desktop with idle RTX cards. That is the same artifact as runpod - a
combined image, dialling out, zero inbound - so section 13 stays thin and factual and only
covers what is genuinely different.

---

## 13. THE WINDOWS / WSL2 DESKTOP NODE

### 13.1 What the host actually needs

| Component | Requirement | The thing people get wrong |
|---|---|---|
| Windows | 10 21H2+ or 11, WSL2 enabled (`wsl --install`, then `wsl --update`) | - |
| NVIDIA driver | A **Windows** GeForce Game Ready / Studio / RTX driver with WSL2 CUDA support | **You never install an NVIDIA driver inside WSL or inside the container.** The Windows driver projects the GPU into WSL through `/dev/dxg` and injects its user-space libraries at `/usr/lib/wsl/lib`. Installing a Linux driver in WSL breaks the projection - this is the single most common self-inflicted failure |
| CUDA toolkit | **Not needed on the host.** The image carries what it needs | - |
| Docker Desktop | WSL2 backend, GPU support enabled | `nvidia-container-toolkit` is **not** installed separately - Docker Desktop wires `--gpus` to the WSL projection itself. On plain Docker-in-WSL (not Desktop) you WOULD need it, which is one reason Desktop is the first-class path |
| Driver floor | The pinned vLLM image needs CUDA >= 13.0 and `driver>=535` (section 0). Under WSL, `nvidia-smi` reports the **Windows** driver version and the CUDA version it supports | Check it before choosing the vLLM pin: if `nvidia-smi` reports CUDA < 13.0, use `vllm/vllm-openai:v0.28.0-cu129`. ollama does not care |

One command answers "is the GPU actually reachable", and it should be in the docs as the first
troubleshooting step:

```powershell
docker run --rm --gpus all lazyaf-runner-ollama:dev nvidia-smi
```

### 13.2 The invocation

PowerShell, backtick continuations:

```powershell
docker network create lazyaf-node                      # once; the step containers join it too

docker run -d --name lazyaf-node --hostname lazyaf-node `
  --gpus all `
  --restart unless-stopped `
  --network lazyaf-node `
  -p 11434:11434 `
  -v lazyaf-ollama:/root/.ollama `
  -v C:\lazyaf\secrets:/run/secrets:ro `
  -v /var/run/docker.sock:/var/run/docker.sock `
  -e LAZYAF_SERVER_KIND=ollama `
  -e LAZYAF_MODEL=qwen2.5-coder:32b `
  -e LAZYAF_ENDPOINT_NAME=desktop-4090 `
  -e LAZYAF_NODE_ROLES=inference,steps `
  -e LAZYAF_AGENT_MODE=docker `
  -e LAZYAF_STEP_NETWORK=lazyaf-node `
  -e LAZYAF_BACKEND_URL=wss://lazyaf.example.com `
  -e LAZYAF_RUNNER_ID=desktop-4090 `
  -e LAZYAF_RUNNER_TOKEN_FILE=/run/secrets/lazyaf-runner-token `
  -e LAZYAF_EXPECT_IMAGES=lazyaf-agent-base:dev,lazyaf-base:dev `
  -e LAZYAF_YIELD_MODE=auto `
  lazyaf-runner-ollama:dev
```

Notes that are not decoration:

- **`--restart unless-stopped`, not `always`.** `always` restarts the node when Docker Desktop
  starts, including after he explicitly stopped it. `unless-stopped` respects that.
- **The socket path needs a doubled leading slash from git-bash / MSYS**
  (`//var/run/docker.sock:/var/run/docker.sock`) to defeat path mangling; from PowerShell the
  single-slash form above is correct. Both spellings go in the docs, because the copy-paste that
  fails is always the one taken from the wrong shell.
- **`LAZYAF_RUNNER_ID` is stable** and equals the endpoint name here, because a fresh id per
  restart orphans a registry row every time (12.6, and the compose file says so too).
- `LAZYAF_EXPECT_IMAGES` makes a missing step image show up as `has=[images:stale]` in the
  runner panel instead of as a step failure ten minutes later.
- No `LAZYAF_RUNNER_ALLOW_INSECURE`. The backend URL is `wss://`. Section 7.1 stands.

### 13.3 Where the model cache must live

This is a performance decision disguised as a mount, and it is worth one table:

| Form | Path | Speed | Verdict |
|---|---|---|---|
| Named volume | `-v lazyaf-ollama:/root/.ollama` | ext4 inside the WSL2 VHDX, native | **Recommended.** No translation layer at all |
| WSL filesystem | `-v /home/<user>/models:/root/.ollama` from a WSL shell, browsable as `\wsl$\...` | ext4, native | Fine, and visible from Explorer |
| Windows bind mount | `-v C:\lazyaf\models\ollama:/root/.ollama` | **9p / virtiofs translation on every read** | Works, and it is the syntax people reach for, but a 20 GB model read through the translation layer costs real time on every model load. Use it when the weights must live on a specific Windows drive; otherwise do not |

The docs give the Windows-path form because it is the one people reach for, **with the penalty
named next to it** rather than discovered later.

**One honest caveat about the token mount.** Section 7.1 says the enrollment secret arrives as a
0600 root-owned file. A Windows bind mount comes through the translation layer with permissive
ownership - the 0600 claim does not survive `C:\lazyaf\secrets`. In `LAZYAF_AGENT_MODE=docker`
that is bounded, because steps run in SEPARATE containers that never receive the mount. In
`native` mode on Windows it is not bounded, so the docs say: put the token file inside the WSL2
filesystem (or a named volume seeded once) when running native mode on this host.

### 13.4 `.wslconfig`: the two WSL2 resource facts that bite

`%USERPROFILE%\.wslconfig`, applied with `wsl --shutdown` (which stops Docker Desktop's VM and
therefore this node - drain it first, section 15.10):

```ini
[wsl2]
memory=32GB          # DEFAULT IS 50% of host RAM, or 8GB on some builds
processors=12
swap=16GB

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true       # lets the VHDX shrink; see below
```

- **The memory cap is the one that surprises people.** WSL2 defaults to half the host's RAM (or
  8 GB). Loading a 32B model still needs host RAM to read and map the weights even when the
  layers end up in VRAM, so an 8 GB VM turns "the download worked" into "the load OOMs", and the
  ollama line that says so scrolls past during a wait the operator was told to expect. The
  preflight prints the VM's `MemTotal` next to the model's on-disk size and warns when the ratio
  is obviously wrong.
- **`vmmem` / `vmmemWSL` in Task Manager is the VM, not a leak.** Named in the docs because a
  process eating 30 GB with no obvious owner is exactly what gets a node killed by its owner.
- **The VHDX grows and does not shrink.** Deleting a 40 GB named volume frees space *inside* the
  VM; the Windows disk stays consumed until `sparseVhd` reclaims it or the VHDX is compacted by
  hand (`wsl --manage <distro> --set-sparse true`, or `Optimize-VHD`). This is the disk-space
  surprise that actually happens, and it happens right after somebody pulls three models to
  compare them. It is also why section 6's image-build node needs its headroom stated in numbers
  rather than in adjectives.

### 13.5 Networking: three different views of "this machine"

WSL2 uses NAT by default. The desktop therefore has three addresses that all mean "here", and
picking the wrong one is the most likely way to get an endpoint that probes `unreachable` while
everything is plainly running.

| Who is asking | Correct address | Requires |
|---|---|---|
| **A sibling step container** (this is `reach: runner-local`, the supported shape) | `http://lazyaf-node:11434/v1` | both containers on the `lazyaf-node` user-defined bridge (`--network lazyaf-node`, `LAZYAF_STEP_NETWORK=lazyaf-node`). Identical to Linux - the exchange happens inside one WSL2 VM's docker daemon and WSL's NAT is not involved at all |
| **Windows itself** (a browser, a curl, a sanity check) | `http://localhost:11434/v1` | `-p 11434:11434`; Docker Desktop forwards published ports onto the Windows loopback |
| **Anything else on the LAN** (this is `reach: direct`) | `http://<desktop LAN IPv4>:11434/v1` | `-p 11434:11434` **and** a Windows Firewall inbound allow for Docker Desktop's backend, **on the network profile the desktop is currently using** |

Three traps, each producing a confusing symptom:

1. **`host.docker.internal` is the Windows host, not this container.** From a sibling step
   container it resolves to the Windows side and reaches the model only by bouncing back through
   the published port - which works until somebody drops `-p`, and then fails in a way that looks
   like DNS. **Never write it into a `runner-local` `base_url`.**
2. **The WSL2 VM's own IP (the `172.x` on `eth0` inside WSL) changes on every boot.** It must
   never appear in an endpoint row. Section 3.2's derivation never emits it.
3. **The firewall rule is per network profile.** Docker Desktop prompts once, usually while the
   machine is on a Private profile. Move the desktop onto a network Windows classifies as Public
   and the LAN case silently stops working while `runner-local` keeps working - which is a good
   argument for `runner-local` being the shape the owner uses.

**What the REGISTER THIS ENDPOINT block prints on this host.** Section 3.2's table already
derives the right answer for the mode in force; on Windows the supervisor additionally detects
WSL2 (section 13.8) and prints **both** lines, labelled, because this is the host where the
distinction actually costs somebody an afternoon:

```
================ REGISTER THIS ENDPOINT ================
This node is serving and is advertising  has=endpoint:desktop-4090
Detected: Docker Desktop / WSL2, agent mode=docker, step network=lazyaf-node

  # runner-local (RECOMMENDED here - no inbound, no firewall, no LAN address):
  lazyaf endpoints add desktop-4090 --reach runner-local \
      --base-url http://lazyaf-node:11434/v1 \
      --model qwen2.5-coder:32b --server-kind ollama --max-concurrency 1

  # direct (only if something OFF this machine must call the model):
  #   needs -p 11434:11434 AND a Windows Firewall inbound allow, and the
  #   address is a DHCP lease - reserve it or this row goes stale.
  #   --base-url http://<run ipconfig on Windows>:11434/v1
========================================================
```

The LAN address is printed as a **comment with a caveat and no guessed number**: a container
behind WSL2 NAT cannot reliably enumerate the Windows host's LAN address, and printing a guess
as though it were a fact is worse than printing the caveat.

**Mirrored networking** (`networkingMode=mirrored`, Windows 11 22H2+) collapses cases 2 and 3 and
is a legitimate option, but it changes Docker Desktop's port-forwarding behaviour and is not the
tested path. Named in the docs; NAT is the default this design assumes.

### 13.6 Sleep, hibernate, and a WebSocket that is supposed to live for days

Windows sleep pauses the WSL2 VM. The TCP connection is not reset - it is **frozen**, which is
the worst case for a naive client, because writes neither succeed nor fail for a long time.

What actually happens, all of it existing 12.6 machinery:

1. **Detection is application-level, not TCP-level.** The agent heartbeats on the interval the
   backend dictates in `registered` (`HEARTBEAT_INTERVAL = 10`) and the backend declares death at
   `DEATH_TIMEOUT = 30`. Wave 5 made both server-dictated precisely so they cannot drift. The
   node is therefore marked dead about 30 seconds after the machine sleeps, not whenever Windows'
   TCP stack gets around to noticing.
2. **The backend requeues** the runner's in-flight step through `on_runner_disconnect`.
3. **On resume**, the agent's send or recv raises, becomes `TransportClosed`,
   `RunnerClient.run()` catches it and reconnects with full jitter
   (`uniform(0, min(30, 2 ** attempt))`) - which also means a room full of desktops waking
   together does not synchronize into a reconnect storm.
4. **The registry row is reused, not orphaned**, because `LAZYAF_RUNNER_ID` is stable.
5. If a step was in flight, `register` carries `resume: {step_id}`, the backend replies with an
   action, and the agent reports `143` for a container that did not survive. The work is lost and
   re-run - correct, because a container frozen for six hours holds an expired step JWT and a
   stale workspace.
6. **The supervisor does nothing at all.** A disconnect is not an agent exit, so there is no
   restart, no readiness cycle and no model reload. The model stays resident across a sleep.

Guidance for the docs, not new mechanism: if the node should carry work overnight,
`powercfg /change standby-timeout-ac 0`; hibernate is worse than sleep (the VM returns with a
different network topology - treat it as a restart); and Docker Desktop must be set to start on
login or the node simply is not there after a reboot. **A 45-minute image build (section 6) on a
machine that sleeps is a build that is lost**, which is one more reason that pipeline pins a
specific node rather than taking whatever is idle.

### 13.7 The dynamic-IP problem, if he registers a `direct` endpoint

`reach: runner-local` sidesteps this entirely and is why it is the recommendation. If a `direct`
endpoint by address is genuinely wanted:

- The desktop's LAN address is a DHCP lease. **Reserve it on the router**, or use a name
  (`http://desktop.local:11434/v1` where mDNS works, or the Windows hostname on a network that
  resolves it).
- When the address changes, the fix is a `PATCH` of `base_url` - and wave 8's rule is that
  patching `base_url` **resets the capability record to `unprobed`**, which then **fails at
  dispatch** until it is re-probed. Correct behaviour, and also a surprise, so the docs say it:
  changing a `direct` endpoint's address costs a re-probe.
- A `runner-local` endpoint has no such failure mode, because it is addressed by container name
  on a bridge the operator created.

### 13.8 GPU detection under WSL2 (this amends section 5.2)

WSL2 does not present the Linux NVIDIA device nodes at all. Section 5.2's order happens to
survive because `nvidia-smi` is checked first, but signals 2 and 3 would false-negative, so the
detector gains `/dev/dxg` and a platform stamp.

| Signal | Linux + toolkit | **WSL2 + Docker Desktop, GPU on** | WSL2, `--gpus` missing or driver too old |
|---|---|---|---|
| `nvidia-smi` exits 0 | yes | **yes** (injected under `/usr/lib/wsl/lib`) | no |
| `/dev/dxg` | absent | **present** | absent |
| `/proc/driver/nvidia/version` | present | **absent** | absent |
| `/dev/nvidia0` | present | **absent** | absent |
| `/proc/version` contains `microsoft` | no | **yes** | yes |

`status.json` gains `gpu.platform` (`linux` or `wsl2`) so a support question is answerable from
one file. The refusal message (section 5.3) becomes platform-aware, because the Linux fixes are
the wrong advice here:

```
[node] PREFLIGHT REFUSED: vLLM needs an NVIDIA GPU and this container has none.
[node]   platform: WSL2 (Docker Desktop)
[node]   checked: nvidia-smi -> not found;  /dev/dxg -> absent
[node]   On Windows, in this order:
[node]     1. Update the WINDOWS NVIDIA driver (Game Ready / Studio). Do NOT
[node]        install a driver inside WSL - it breaks the GPU projection.
[node]     2. `wsl --update` from an elevated PowerShell.
[node]     3. Docker Desktop -> Settings -> Resources -> WSL integration on.
[node]     4. Verify: docker run --rm --gpus all <this image> nvidia-smi
[node]   ollama's node image runs on CPU instead, if that is what you want.
```

Two further WSL2 facts that section 15 depends on, stated here once:

- **`nvidia-smi` inside WSL2 sees the WHOLE physical GPU**, including memory held by Windows
  processes. That is exactly the signal we want for "is he using this machine".
- **`utilization.gpu` is frequently `[N/A]` under WSL2**, and per-process listings are
  unavailable. Section 15.2 is built around that limitation rather than around wishing it away.

---

## 14. DUAL ROLE VIA LABELS

The owner wants one node that can serve models AND execute steps, selectable per endpoint. The
routing grammar already expresses all four combinations; this section only fixes what the node
advertises.

### 14.1 The two roles are two independent label facts

| Role | Advertised as | Produced by |
|---|---|---|
| **Inference host** - steps that need THIS model come here | `has=endpoint:<name>` | `advertise.py`, from `LAZYAF_ENDPOINT_NAME` (section 3.1) |
| **Step-execution node** - any step that needs a docker host may come here | `has=docker` | `DockerOrchestrator.capabilities()`, automatically, in `LAZYAF_AGENT_MODE=docker` |

They are already separate advertisements. Nothing has to be invented; what was missing is the
ability to advertise one WITHOUT the other.

### 14.2 `LAZYAF_NODE_ROLES` - the one switch

```
LAZYAF_NODE_ROLES=inference,steps     # default when a docker socket is available
```

| Roles | Server runs | Agent runs | Advertised `has` | What it is for |
|---|---|---|---|---|
| `inference,steps` | yes | `docker` | `endpoint:<name>`, `docker`, plus whatever the operator set | **The desktop.** One box, both jobs |
| `inference` | yes | `docker` (needed to run the endpoint's own steps) | `endpoint:<name>` only - `docker` is **suppressed** | "Serve my model, do not run other people's builds here." The rented-pod posture from section 7.1 |
| `steps` | **no** | `docker` | `docker`, plus the operator's | The combined image degrading to a plain runner-agent, so one artifact covers both jobs and the role is an env var rather than a different image. **This is the role the section 6 image-build node runs in** |
| (empty) | - | - | - | Config refusal, exit 78. A node that advertises nothing is a container burning power |

`roles=inference` still runs the agent in `docker` mode: the endpoint's own steps - wave 8's
capability probe and every `openai-harness` step pinned to this endpoint - are ordinary step
containers and something has to run them. What `roles=inference` removes is the `docker` label,
and with it every step that merely wants *a* docker host.

`roles=steps` implies `LAZYAF_SERVER_ENABLED=0`; readiness, the model cache and the GPU verdict
are all skipped, and `LAZYAF_ENDPOINT_NAME` becomes an error rather than being silently ignored.

### 14.3 Suppression: the one small runner-agent addition

`merge_labels(configured, capabilities)` unions `has` and has no way to subtract, which is
correct for its normal job (an operator adding `has=gpio` must not erase `has=docker`). Roles
need exactly one subtraction, so:

```python
# runner-agent/lazyaf_runner/orchestrator/base.py
def merge_labels(configured: dict, capabilities: dict, *, suppress: tuple = ()) -> dict:
    """... `suppress` removes entries from the UNIONED `has` list after the
    merge. It exists for one reason: a node may host an orchestrator whose
    capability it does not wish to sell. `has=docker` on a machine meant only
    to serve a model is an invitation to run somebody else's build on it, and
    on rented hardware that is a security posture, not a preference.
    Suppression is applied LAST, so it beats both sources - a deliberate
    refusal must not be overridable by a label typo."""
```

Fed by `LAZYAF_RUNNER_SUPPRESS_HAS` (comma-separated), which `advertise.py` sets from
`LAZYAF_NODE_ROLES`. Five lines in `base.py`, one env var, one call site, owned by Agent B. An
operator can also set it directly on the plain `runner-agent` image, which is a small general
win at no extra cost.

### 14.4 No routing-grammar change, and here is the proof

`Runner.matches_requirements` is exact match on `runner_id` / `runner_type`, `normalize_arch`
equality on `arch`, and **subset containment** on `has`. All four combinations are already
expressible:

| Step's `requires:` | `inference,steps` | `inference` | `steps` |
|---|---|---|---|
| (none) | never reaches any runner - routed LOCAL by `ExecutionRouter.decide` | same | same |
| `{has: [endpoint:desktop-4090]}` (wave 8's injection) | matches | **matches** | no |
| `{has: [docker]}` (section 6's build steps) | matches | **no** | matches |
| `{has: [endpoint:desktop-4090, docker]}` | matches | no | no |

No new key, no new operator, no change to `parse_requirements`, no change to
`runner_protocol.py`. Wave 5's contracts 1 and 5 and wave 8's contract 8 all survive untouched.
The only new code is the suppression argument above and the env-to-roles mapping in
`advertise.py`.

**Cost check, as asked:** step execution on this box is `LAZYAF_AGENT_MODE=docker` against the
Docker Desktop socket - the mode section 3.4 already calls the wave's spine and which needs no
new runner-agent code. Dual role therefore costs the five lines in 14.3 and nothing else.

---

## 15. GPU-IDLE YIELD

**Requirement:** when he is using the desktop, LazyAF stops taking new work; when it goes idle
again, it resumes.

### 15.1 The constraint, which is one this design created for itself

Section 0 established that **the connection IS the advertisement**, and section 2.4 uses that
deliberately: a dead inference server closes the socket and the runner leaves `find_available`,
because that function filters on `runner.id in self._connections`.

The naive implementation of yield reuses that lever - disconnect when the GPU is busy - and it is
wrong for three separate reasons:

1. **It abandons the in-flight step.** A disconnect triggers `on_runner_disconnect`, which
   requeues; the local container is killed on reconnect via the `resume` reconciliation. A step
   ninety seconds from finishing is thrown away and re-run from scratch. For a 45-minute image
   build (section 6) that is not a nuisance, it is the whole job.
2. **It flaps.** Disconnect/reconnect on a signal that moves whenever he alt-tabs produces a full
   re-register every few minutes, each one resetting the backoff state that exists to prevent
   exactly that.
3. **It is invisible.** A node that vanishes looks identical to a node that crashed, which is the
   failure R1 exists to forbid: he would have no way to see *why* nothing is running.

So yield is a **drain, not a disconnect**: the socket stays open, the node stays visible, it
simply stops being selected. Disconnection remains reserved for the one case where the node
genuinely cannot serve - a dead inference server.

### 15.2 What is sampled

`lazyaf_node/gpuwatch.py`, every `LAZYAF_YIELD_SAMPLE_INTERVAL=10s`:

```
nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
           --format=csv,noheader,nounits
```

Two signals, because under WSL2 one of them is often unavailable:

- **`utilization.gpu`** - the signal we want. **Frequently `[N/A]` under WSL2** (section 13.8).
  When every sample of a startup window is `N/A`, the watcher logs **once**
  (`GPU utilization is unavailable on this driver under WSL2; falling back to VRAM headroom`)
  and switches to VRAM-only. It never silently reads `N/A` as `0`, which would mean "idle" and
  would hand him a node that never yields.
- **`memory.used`, read as FOREIGN VRAM.** Per-process attribution is unavailable inside WSL2, so
  we cannot subtract our own server's allocation directly. Instead
  `foreign_vram = memory.used - baseline`, where `baseline` is `memory.used` sampled at the
  moment readiness completed, and re-sampled whenever the server restarts or a step finishes -
  i.e. our own steady idle occupancy.

**The one estimator error that matters, and how it is removed structurally.** Our own server's
VRAM grows under load (KV cache), which would read as foreign traffic. So **the watcher only
drives the yield decision while no step is in flight.** When a step is running we are the busy
one, the answer is already "finish it" (section 15.6), and the sample is not consulted. That
removes the largest error source by construction rather than by tuning a threshold.

**The optional external hook, which is the honest best signal.** What we actually care about is
"he sat down", and the authoritative source is Windows - `GetLastInputInfo`, a foreground
fullscreen check, whether a game process is running - none of which a Linux container can see.
So:

```
LAZYAF_YIELD_HOOK=/run/lazyaf/hook/desktop-busy    # exit 0 = BUSY, exit 1 = idle,
                                                   # anything else = unknown -> ignored
```

The hook is ORed with the GPU signal and sampled on the same interval with a 2s timeout. This
ships the **seam**, not the Windows-side script: a scheduled task writing a flag file into a
mounted directory is a five-line PowerShell script the owner can write, and shipping a Windows
agent is a different project. Named as a seam in 15.11.

### 15.3 Thresholds, hysteresis and dwell

| Constant | Default | Why this number |
|---|---|---|
| `LAZYAF_YIELD_SAMPLE_INTERVAL` | 10s | matches the heartbeat interval, so a transition can ride the next heartbeat |
| `LAZYAF_YIELD_BUSY_UTIL_PCT` | 30 | desktop noise (compositor, browser video) sits in single digits; a game or a render pins high. 30 is comfortably above noise and far below anything real |
| `LAZYAF_YIELD_BUSY_VRAM_MB` | 1024 | a browser's GPU process is 200-400 MB; a game is gigabytes |
| `LAZYAF_YIELD_BUSY_FOR` | 30s (3 consecutive samples) | quick, because he should not have to wait for his own machine |
| `LAZYAF_YIELD_IDLE_UTIL_PCT` | 10 | **a genuine hysteresis band, not the same edge** (30 down, 10 up) |
| `LAZYAF_YIELD_IDLE_VRAM_MB` | 512 | the same band on the other signal |
| `LAZYAF_YIELD_IDLE_FOR` | 300s | **deliberately asymmetric.** Five minutes below the release threshold before we take the GPU back, because he probably just paused the game or stepped away for coffee. Resuming eagerly is how a yield feature becomes an annoyance |
| `LAZYAF_YIELD_MIN_DWELL` | 60s | a floor on time-in-state, so nothing can flap faster than once a minute no matter what the samples do |

Entering busy requires **either** signal over its threshold; returning to idle requires **both**
under their release thresholds. Every transition logs the sample that caused it:

```
[node] yield: available -> paused (gpu-busy: util 97% >= 30, foreign VRAM 18.4GiB >= 1.0GiB,
       sustained 30s). Not accepting new steps. In-flight steps will finish.
```

### 15.4 The state machine

```
  available ---- busy sustained BUSY_FOR ----> yielding ---- no step in flight ---> paused
      ^                                            |                                  |
      |                                     (a step is running:                       |
      |                                      stay yielding, accept                    |
      |                                      nothing new, wait)                       |
      +------- idle sustained IDLE_FOR and MIN_DWELL elapsed ------------------------- +
```

- **`yielding`** exists so that "stop accepting" and "the GPU is actually free" are two different
  moments. The node reports itself unavailable the instant the threshold is crossed; the VRAM
  release (15.7) waits until the current step is done.
- `paused` is a normal, healthy, visible state. It is not degraded and it is not an error.

### 15.5 The backend change, stated plainly

**Yes, this needs a small backend change, and it is the right trade** - the alternative is
silently disconnecting, which section 15.1 rejects. Four pieces, all additive:

1. **The `heartbeat` frame gains an optional `availability` block.** No new message type.
   `HeartbeatMessage` currently has no fields, and `RegisterMessage.resume` is the existing
   precedent for an optional additive key read with `.get()`.

```json
{"type": "heartbeat",
 "availability": {"state": "paused",
                  "reason": "gpu-busy",
                  "detail": "foreign VRAM 18.4 GiB, util 97%",
                  "since": "2026-08-30T19:42:11Z"}}
```

   `state` is `available | paused`; `reason` is `gpu-busy | manual | server-unready`.
   **An absent block means `available`**, so `PROTOCOL_VERSION` stays `1` and no agent in the
   field is stranded - the same additive-key rule `agent_config.py` already documents. The agent
   sends an **immediate out-of-band heartbeat** on any transition, so pause latency is
   sub-second rather than up to one interval.

2. **Three columns on `runners`**, one migration:
   `availability` `String(16)` NOT NULL default `'available'`,
   `availability_reason` `String(200)` null,
   `availability_changed_at` `DateTime` null - **stamped backend-side on change, never from the
   wire**, exactly as `last_heartbeat` already is (a runner with a skewed clock must not be able
   to backdate its own state).

3. **`find_available` gains one clause**: `Runner.availability == "available"`. That is the
   entire scheduling change.

4. **`send_runner_status`'s payload gains the three fields.** **No new WS frame type is needed** -
   `runner_status` already exists in `websocket.py` and is already in the frontend's
   `ServerMessageType`, `HANDLED_MESSAGE_TYPES` and switch, so `websocket.test.ts`'s drift guard
   is untouched by this wave.

**`RunnerState` gains nothing.** A `PAUSED` member would put availability inside the connection
state machine, whose transitions are driven by connect/assign/heartbeat events and whose
vocabulary wave 5's contract 4 pins as single-source for the state machine, the column, the API
and the UI. `status` answers "is it alive and what is it doing"; `availability` answers "may we
give it new work". A paused runner is genuinely `idle` - it simply is not selectable. Two
writers, two lifetimes, two columns.

**Two races, both resolved by one rule: once assigned, run it.**

- The dispatcher's compare-and-swap may complete microseconds before the pause lands. The agent
  **must not refuse** the assignment - refusing would waste a dispatch round-trip and produce an
  error the backend would have to interpret. The pause governs `find_available`, not acceptance.
  One extra step runs; he waits at most that step's remaining time.
- **`register` resets `availability` to `available`.** Fail-OPEN, deliberately: an agent that
  reconnects after being downgraded to a build that does not send the block would otherwise stay
  permanently paused with nothing in any log to explain it. An agent that does speak the
  extension re-pauses within one heartbeat (10s or less).

### 15.6 The step that is mid-flight when he starts gaming: it finishes

Stated because "kill it" is the intuitive answer and it is wrong:

- **Killing it wastes what has already been spent** - GPU-minutes, tokens, and for an agent step
  a partially-built change that is committed nowhere.
- **It corrupts the measurement.** M13's cost-to-solve, regression rate and iterations-to-solve
  come from step outcomes. A step killed because somebody launched a game is a failure with
  nothing to do with the loop under test, and it would silently enter the data.
- **It costs MORE GPU, not less.** 12.6 requeues a disconnected runner's step, so killing it
  means the same work runs again somewhere - possibly here, five minutes later.
- **The contention is bounded.** One step, bounded by its own timeout. The observable is a game
  that stutters briefly, not an evening of contention.
- **It is visible.** The log says `In-flight steps will finish`, and the runner panel shows
  `paused - finishing 1 step`.

**This is exactly why section 6's image build is safe on this class of node.** A 45-minute
`docker build` of the vLLM node image is the longest-running step LazyAF has, and it is
CPU/disk/network work that does not touch the GPU at all. If he starts gaming halfway through,
the node stops accepting new steps immediately and the build **finishes**. The alternative -
abandoning it - would throw away 45 minutes and a 20 GB layer cache to protect a GPU the build
was never using. The drain design and the heavy-image pipeline were designed independently and
this is where they meet; nothing extra is needed to make it work.

The escape hatch is explicit and manual, never automatic:
`docker exec lazyaf-node lazyaf-node yield --pause --cancel` cancels the running step through
the existing cancel path and says what it just threw away.

### 15.7 Releasing VRAM - the finding that makes or breaks this feature

**A paused node whose model still holds 22 GB of VRAM has not yielded anything.** He cannot start
a game. Pausing the *scheduler* without releasing the *memory* would be a feature that looks like
it works and does not.

| Server | Can it release? | Policy |
|---|---|---|
| **ollama** | **Yes.** `POST /api/generate {"model": M, "keep_alive": 0}` unloads immediately | `LAZYAF_YIELD_RELEASE=unload` is the **default**. Issued on entering `paused` (i.e. after the in-flight step finishes). The model reloads on the next step - 30-60s on a 32B model, **charged to the first step after resume**, stated in the docs and logged when it happens |
| **vLLM** | **No.** It allocates its KV-cache pool at startup and holds it for the process lifetime; there is no unload | `LAZYAF_YIELD_RELEASE=none` is the default. `stop-server` is offered and honest about its cost: a full readiness cycle (minutes, plus a possible re-read of weights) on resume |

**The recommendation follows from the mechanism, not from taste: a machine you also game on
should run the ollama node; vLLM belongs on the dedicated or rented box.** That is exactly the
split `PLAN.md` already draws ("ollama on bare metal at home ... vLLM the right one on a rented
GPU"), and this is the concrete reason for it.

### 15.8 Readiness while paused, and the healthcheck

Two traps that would each undo the release:

- **P4 must be suspended while paused.** Section 2.3's readiness includes a one-token completion
  request. Re-running it against ollama after an unload would *reload the model* and re-take the
  VRAM we just released - a supervision loop fighting its own yield policy. While `paused` the
  supervisor runs P0-P2 only (process alive, port open, `/v1/models` answers) and skips P3/P4.
- **The healthcheck must stay green.** `lazyaf-node-health` exits 0 for `phase == "serving"` and
  for `phase == "paused"`. A red healthcheck would make Docker Desktop's restart policy recycle a
  container that is working exactly as designed.

`status.json` grows a `yield` block:

```json
"yield": {"state": "paused", "reason": "gpu-busy", "mode": "auto", "source": "sampler",
          "since": "2026-08-30T19:42:11Z", "release": "unload", "model_loaded": false,
          "last_sample": {"util_pct": 97, "foreign_vram_mb": 18841, "baseline_mb": 21504},
          "transitions_24h": 6}
```

`transitions_24h` is there so "is this flapping?" is answerable without reading a log.

### 15.9 What the UI shows

The failure this prevents is him staring at an idle queue wondering what is broken.

- **Runner panel:** the node is present, **amber, never missing and never red**:
  `desktop-4090 - idle, paused (gpu busy since 19:42) - finishing 1 step`. Live, through the
  existing `runner_status` frame with its three new fields.
- **Endpoints page:** the `runners:` count for a `runner-local` endpoint counts only *available*
  runners and shows both numbers: `runners: 1 (0 available - gpu busy)`. A bare `0` here would
  read as "nobody carries this label", which is a different problem with a different fix.
- **A queued step's reason line**, which already exists for the endpoint admission gate (wave 8
  section 6.4 emits `[executor] waiting for endpoint ...`), gains the sibling case: a step that
  cannot be matched because every candidate runner is paused says so rather than sitting in
  `pending` in silence, and `NO_RUNNER_TIMEOUT`'s message - which already names the requirements
  and every connected runner's labels - additionally names their availability.

### 15.10 Manual override

Precedence, highest first, recorded in `status.json` as `yield.source`:

| Lever | Scope | Needs a restart |
|---|---|---|
| `docker exec lazyaf-node lazyaf-node yield --pause / --resume / --auto` | writes `/run/lazyaf/yield.override`, read on every sample | no |
| `LAZYAF_YIELD_MODE=auto / always-available / always-paused` | the container's whole life | yes |
| the sampler | - | - |

`--pause` forces paused regardless of what the GPU says (useful before `wsl --shutdown`, before a
driver update, or when he simply wants his machine back). `--resume` forces available. `--auto`
returns control to the sampler.

`always-available` is the correct setting for a dedicated box, a rented pod, **and the section 6
image-build node**, where yielding to a phantom desktop user would be nonsense - and it is what
those recipes set.

**Out of scope, named:** an operator-facing `POST /api/runners/{id}/pause` from the LazyAF UI.
That is a backend-to-runner command and therefore a new message type in a direction this wave
otherwise does not touch. The container-side lever plus the env var covers the owner's stated
need; the API version is a seam (15.11).

### 15.11 What this is NOT

- **Not `drain`.** 12.6's backend-initiated `drain` means "finish up and go away" and it CLOSES
  the socket (`_drain_then_close`). Yield is runner-initiated, non-terminal, and keeps the
  socket. The two must not be conflated in code or in the UI; a test asserts the yield path never
  calls `transport.close`.
- **Not a scheduler policy.** The backend does not learn about GPUs, gaming or desktops. It
  learns one boolean per runner, and everything interesting stays in the node where the signal
  actually lives.
- **Seams left open here:** the Windows-side busy hook (the seam ships, the script does not); a
  per-endpoint yield policy (one node, one policy today); yielding on CPU, RAM or DISK pressure
  rather than GPU - worth revisiting once the section 6 build pipeline has run a few times on the
  same box; and the operator-facing pause API above.

---

## 16. TESTABILITY OF THE ADDITIONS

The claim from section 8 has to survive the additions: **thresholds, hysteresis, drain and resume
are all provable in T1 and T2 with no GPU and no large pull.** They are, because `GpuSampler` is
injected exactly like `spawn`, `probe` and `clock` already are.

### 16.1 T1 - the yield state machine against a scripted sampler

`tdd/unit/node/test_yield.py`, driving a fake sampler and a fake clock:

1. **Threshold crossing**: two busy samples do nothing; the third (30s sustained) transitions.
2. **Hysteresis is a band, not an edge**: a sample at util 20% (below the 30 busy threshold, above
   the 10 release threshold) changes nothing in either direction, from either state.
3. **Asymmetric timing**: 30s to pause, 300s to resume; a single idle sample at 299s does not
   resume.
4. **Min dwell**: a sampler oscillating every interval produces at most one transition per 60s,
   and `transitions_24h` counts them.
5. **`N/A` utilization** falls back to VRAM, logs the fallback **exactly once**, and never reads
   `N/A` as idle - the test that would have caught the naive bug.
6. **Foreign-VRAM baseline**: re-sampled after a server restart and after a step completes; the
   sampler is **not consulted while a step is in flight**.
7. **Hook contract**: exit 0 = busy, exit 1 = idle, exit 2 or timeout = unknown and ignored; ORed
   with the GPU signal.
8. **Override precedence**: file beats env beats sampler; `--auto` returns control; `yield.source`
   in `status.json` names the winner.
9. **The availability payload**: emitted on transition, shape-for-shape as section 15.5, with an
   out-of-band heartbeat sent immediately.
10. **THE SENTINEL, and the most important test in this section**: across a full
    available -> yielding -> paused -> available cycle, **`transport.close` is never called and no
    second `register` frame is sent**. Withdrawal-by-disconnect stays reserved for a dead
    inference server, and this test is what stops a future refactor quietly merging the two
    mechanisms.
11. **Release policy**: ollama `unload` issues exactly one `POST /api/generate` with
    `keep_alive: 0` on entering `paused` and none while `yielding`; vLLM `none` issues nothing;
    **no completion request (P4) is made while paused** - the test that proves supervision does
    not fight the yield.
12. **Healthcheck**: `lazyaf-node-health` exits 0 in `paused`.
13. **Roles (section 14)**: the four `LAZYAF_NODE_ROLES` rows produce the expected advertised
    `has` set, `merge_labels(suppress=)` removes `docker` from BOTH sources, empty roles is a 78,
    and `roles=steps` together with `LAZYAF_ENDPOINT_NAME` is an error rather than a silent
    ignore.
14. **WSL detection (section 13.8)**: all five signal rows against a faked filesystem and a faked
    `nvidia-smi`; `gpu.platform` stamped; the refusal message contains the Windows fixes and NOT
    the Linux `--gpus all` advice when `/proc/version` says microsoft.

### 16.2 T2 - the real thing, in a container, with a file-driven sampler

Extending `tdd/integration/services/node/test_fake_inference_node.py`. The whole yield loop is
exercised with `LAZYAF_YIELD_HOOK=/opt/lazyaf/test/busy-hook` reading a flag file, so a test flips
one byte and the real state machine, the real agent, the real heartbeat and the real backend do
the rest:

1. Node reaches `serving`; the runner is returned by `find_available` and a step lands on it.
2. Flip the flag to busy. Within `BUSY_FOR` plus a heartbeat, `GET /api/runners` shows the node
   **still connected**, `status == "idle"`, `availability == "paused"`,
   `availability_reason == "gpu-busy"` - and a newly dispatched step pinned to its label does
   **not** land on it (it reaches `NO_RUNNER_TIMEOUT`, or another runner takes it).
3. **The socket never closed**: `Runner.websocket_id` is unchanged across the whole cycle and
   `connected_at` did not move. This is the T2 form of the T1 sentinel and it is the assertion
   that proves drain is not disconnect.
4. **In-flight survival**: start a step, flip to busy mid-step, assert the step **completes
   successfully** and only then does the state reach `paused`.
5. Flip the flag back. After `IDLE_FOR`, `availability == "available"` and a new step lands.
6. **Fail-open on register**: force a reconnect while paused (kill the transport) and assert the
   row reads `available` immediately after `register`, then returns to `paused` within one
   heartbeat.
7. **Roles**: a node started with `LAZYAF_NODE_ROLES=inference` does not match a
   `requires: {has: [docker]}` step but does match its own `endpoint:` step.

Both suites need a GPU exactly nowhere: the sampler is a hook, the server is the mock, and the
image is `lazyaf-fake-inference:dev` from the T2 preflight.

### 16.3 The heavy-image pipeline's own tests (section 6)

The pipeline is repo-defined YAML plus one script, so most of it is testable without ever running
a real build:

1. **T1, `tdd/unit/scripts/test_build_inference_images.py`** (already Agent C's): `--require-disk`
   refuses with the number and pulls nothing; `--verify-pushed` compares the local image id
   against the registry digest and FAILS on a mismatch, on a missing tag, and on a manifest that
   lists fewer layers than were pushed; `--check-pins` never exits non-zero.
2. **T1, `tdd/unit/config/test_images_pipeline_yaml.py`**: the pipeline YAML parses against the
   platform's own pipeline schema; every build step carries `requires: {has: [docker]}` and
   `runner_id`; the GHCR credential appears ONLY under `secret_environment` and nowhere in
   `environment`; no step has `control: false` (which would lose the secret channel); the
   timeouts exceed the measured build durations.
3. **T1 secret hygiene**: a sentinel GHCR token planted in the backend env appears in no rendered
   step config's `environment`, no log line and no `docker inspect`-visible field - the same
   sentinel test shape 12.6 already uses.
4. **T2 dry-run**: the pipeline is executed end to end against `lazyaf-fake-inference` as the
   image under build and a **local registry container** as the push target, with
   `LAZYAF_IMAGE_BUILD_DRY_RUN=1` swapping the upstream `FROM` for `lazyaf-base:dev`. Every step
   runs for real - preflight, build, tag, login, push, verify - on an image measured in hundreds
   of megabytes. **This proves the pipeline's shape, its secret handling and its verify step
   without ever pulling a CUDA base.**
5. **Not tested anywhere, stated (R4):** that the real 8.6 GB base pulls, builds and pushes inside
   the timeout on his actual hardware. The first real run measures it and the numbers replace the
   estimates in the YAML comment.

### 16.4 Still gated on the desktop itself (R4 - stated, not implied)

| Claim | Verified by |
|---|---|
| `nvidia-smi` works, and whether `utilization.gpu` is `N/A` on HIS driver | one command: `docker run --rm --gpus all lazyaf-runner-ollama:dev nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv`. **Run this first, because which of the two signals is live decides whether the VRAM fallback is the primary path** |
| That 30% / 1 GiB actually separates "gaming" from "browsing" on his hardware | one evening of `status.json`'s `last_sample` while he uses the machine normally. The defaults are reasoned, not measured, and the docs say to check them |
| That an ollama unload genuinely frees the VRAM a game needs | `nvidia-smi` before and after a forced `yield --pause` |
| WSL2 memory cap, VHDX growth, sleep/resume reconnect | the desktop, over a few days |
| That `--gpus all` passes the RTX cards through Docker Desktop at all | section 13.1's one-liner |
| That the vLLM node image builds and pushes on this box inside the timeout | the first real run of the section 6 pipeline |

### 16.5 Wave-split and contract deltas

**The wave is now four agents.** The addendum adds a genuinely separate, small backend
deliverable, and hiding a migration inside an agent whose other files are all Dockerfiles would
be the wrong ownership.

| Agent | Additional ownership |
|---|---|
| **A** (node layer) | `images/node-layer/lazyaf_node/{gpuwatch,yield}.py` (new), the WSL branch of `gpu.py`, the roles branch of `advertise.py`, the `yield` block in `status.py`, the `yield` subcommand in `bin/lazyaf-node`, `tdd/unit/node/{test_yield,test_roles}.py` |
| **B** (runner-agent) | `merge_labels(suppress=)` in `orchestrator/base.py`, `LAZYAF_RUNNER_SUPPRESS_HAS` in `config.py`, the optional `availability` block on the outbound heartbeat plus the out-of-band send on transition in `session.py`, `runner-agent/tests/test_availability.py` |
| **C** (release/CI/docs) | **`.lazyaf/pipelines/images.yaml` (new, section 6)**, `build_inference_images.py`'s `--verify-pushed` and `--require-disk-only`, the trimmed `.github/workflows/` changes of section 6.6, `docs/node-images.md` gains the Windows/WSL2 chapter (13.1-13.7), the yield chapter and the build-node runbook; `lazyaf node recipe --windows` emits the PowerShell form; `tdd/unit/config/test_images_pipeline_yaml.py`, the T2 dry-run build |
| **D** (**new** - backend availability **and `secret_refs`**) | `backend/app/services/pipeline_executor.py` (**`secret_refs` resolution + `PUBLISH_SECRET_REF_RE`, section 6.4 - the only backend change section 6 needs**), the pipeline-YAML validation that rejects a non-allowlisted ref at materialization time, `tdd/unit/services/test_publish_secret_refs.py`; `backend/app/models/runner.py` (three columns), `backend/alembic/versions/00NN_runner_availability.py` (claim the head AFTER wave 8's `0011`), the `heartbeat` handler in the runner WS endpoint, `runner_protocol.HeartbeatMessage` + `parse_runner_message`, the one clause in `runner_registry.find_available`, `websocket.send_runner_status`'s payload, the runner-panel and Endpoints-page rendering, `tdd/unit/execution/test_runner_availability.py`, `tdd/integration/api/test_runner_availability_api.py`, `frontend/e2e/` coverage for the amber paused row (R8) |

Order: **A and D first and in parallel** (D's column is what B's heartbeat writes, D's `secret_refs`
is what C's publish step needs, and both are what C's T2
assertions read), then B, then C.

**Contracts 11-16**, continuing section 11's numbering:

11. **`availability` rides the EXISTING `heartbeat` frame** as an optional block. No new runner
    message type, `PROTOCOL_VERSION` stays `1`, **absent means available**. Producer B, consumer
    D, pinned by a shared round-trip test in `tdd/unit/execution/`. `register` resets availability
    to `available` (**fail-open**).
12. **`Runner.availability` is a SEPARATE column from `Runner.status`, and `RunnerState` gains no
    member.** Wave 5's contract 4 (one vocabulary for the connection state machine) survives
    intact. Owner D; asserted by a test that enumerates `RunnerState` and finds no `paused`.
13. **`runner_status` is an EXISTING WS frame type.** Its payload gains three fields and no new
    type is registered, so `websocket.test.ts`'s drift guard is untouched by this wave. Owner D.
14. **The yield path NEVER closes the transport.** Withdrawal-by-disconnect (section 2.4) remains
    reserved for a dead inference server. Owners A and B; pinned by the T1 sentinel (16.1 test 10)
    and the T2 `websocket_id`-unchanged assertion (16.2 test 3).
15. **`LAZYAF_NODE_ROLES` is the ONE role switch**, and label suppression happens only through
    `merge_labels(..., suppress=)`. No second place computes what this node advertises. Owners A
    (mapping) and B (mechanism).
16. **The heavy images are built by `.lazyaf/pipelines/images.yaml` and by nothing else.** No
    GitHub workflow builds them, `scripts/build_images.py` does not know them, and the GHCR
    credential reaches a build step only through 12.5's `secret_environment`. Owner C; pinned by
    16.3's tests 2 and 3, by section 6.8's table, and by contract 7's assertion that no file
    under `.github/workflows/` names either image.
