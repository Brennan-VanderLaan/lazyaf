# Wave 8 - Milestone 14 Wiring Design: self-hosted OpenAI-compatible endpoints and the LazyAF agent harness

Status: DESIGN - implementers build from this verbatim.

Inputs: `PLAN.md` "## Milestone 14" (the owner's decisions of 2026-08-30 are BINDING and are
not relitigated here), `PLAN.md` "## Milestone 13" (this milestone unblocks its central
hypothesis), the standing rules R1-R8, `upcoming/wave4-125-wiring.md` (12.5 - the agent-step
config contract, `secret_environment`, and the usage sidecar this design EXTENDS; its
vocabulary is reused verbatim), `upcoming/wave5-126-wiring.md` (12.6 - the runner protocol,
the `requires:` grammar and the deliberately Docker-agnostic orchestrator seam),
`upcoming/wave6-1265-wiring.md` (12.6.5 - the experiment matrix), and the code:
`runner-common/runner_common/executors/{base,claude,gemini,mock}.py`,
`runner-common/runner_common/{agent_wrapper,agent_config,usage,git_helpers}.py`,
`backend/app/services/control_layer/workspace.py`, `backend/app/models/usage.py`,
`backend/app/schemas/usage.py`, `backend/app/services/{usage_ingestion,usage_pricing}.py`,
`backend/app/services/execution/{local_executor,runner_protocol,runner_registry,runner_dispatcher}.py`,
`backend/app/services/pipeline_executor.py`, `backend/app/services/workspace/execution_router.py`,
`backend/app/services/agent_run.py`, `backend/app/schemas/experiment.py`, `runner-agent/`.

---

## 0. Ground truth found during recon (read before arguing with the design)

- **This design is written against the POST-wave-7 tree.** At the time of writing a 6-agent
  wave holds `backend/app/routers/cards.py`,
  `backend/app/services/{pipeline_executor,agent_run,experiment_service,agent_prompt,spec_context}.py`,
  `backend/app/main.py`, `backend/app/schemas/**`, `frontend/src/lib/**`, `cli/**` and
  `tdd/**`. Every line number here is indicative; every SYMBOL named here
  (`resolve_agent_type`, `agent_secret_environment`, `AGENT_BY_RUNNER_TYPE`,
  `generate_agent_config`, `build_execute_step_config`, `parse_requirements`,
  `node_rate_usd_hour`) is load-bearing and must still exist, or the wave stops and
  re-reads before editing.
- **Alembic head is `0010`** (`0010_experiments.py`, chain `0007 -> 0009 -> 0010`; there is no
  `0008`). This milestone claims **`0011`**, `down_revision = "0010"`, written idempotently
  with `inspector.has_table` / `has_index` guards per the 0002/0004/0005/0006 convention.
  `tdd/integration/test_migrations.py` pins `ALEMBIC_HEAD_REVISION`.
- **The usage model already anticipates this milestone and needs no new columns.**
  `UsageProvider` already has `OPENAI_COMPATIBLE = "openai-compatible"` and
  `SELF_HOSTED = "self-hosted"`; `UsageCostSource` already has `GPU_NODE = "gpu-node"`;
  `StepUsage` already carries `gpu_node_id`, `gpu_fraction`, `container_seconds`,
  `determinism` and `role`. `usage_pricing.gpu_node_cost_usd(rate, container_seconds,
  gpu_fraction)` is already written and already correct. **Nothing about the cost story is
  new machinery; 14.x is the phase that finally SETS the inputs.**
- **`LAZYAF_GPU_NODE_ID` / `LAZYAF_GPU_FRACTION` are already read.**
  `local_executor.execute_step` stamps them into container env from
  `execution_context["gpu_node_id"]` / `["gpu_fraction"]`, and `run.py` copies them into the
  fallback usage record. Wave 5 section 10 named this as "a three-line change deliberately
  not made against zero real hardware". This is that hardware. The three lines are in
  `pipeline_executor`, section 5.3.
- **`secret_environment` is a solved problem and is reused unchanged.**
  `local_executor` raises when `secret_environment` is present without control mode, never
  merges it into `run_kwargs["environment"]`, and delivers it only inside the 0600
  consume-once step config file. `runner_protocol.build_execute_step_config` puts it only
  inside `control_files`. An endpoint API key is just another entry in that dict.
- **The executor seam takes construction arguments already.** `agent_wrapper.EXECUTORS` maps
  agent name -> a BUILDER lambda taking the whole `AgentConfig`
  (`ClaudeExecutor(output_format=...)`, `MockExecutor(mock_config=...)`). **The harness
  therefore adds ZERO fields to `ExecutorConfig` and `ExecutorResult`** - 12.5's cross-agent
  contract #4 ("nothing else may add fields to that dataclass") survives this milestone
  intact, and a test asserts it.
- **`runner-common` already depends on `requests`.** The harness uses it (including
  `iter_lines()` for SSE). No new dependency, so `images/agent-base`'s install line is
  unchanged and only the staged source tree moves the content hash.
- **The agent vocabulary has exactly three chokepoints.** `pipeline_executor.DEFAULT_AGENT_IMAGE`
  (which `resolve_agent_type` validates against), `agent_run.AGENT_BY_RUNNER_TYPE` (which
  `schemas/experiment.AGENT_VOCABULARY` derives from, so cards, playground and the matrix all
  inherit it), and `agent_wrapper.EXECUTORS` container-side. Adding a fourth agent means
  editing exactly those three plus `AGENT_SECRET_ENV` / `AGENT_USAGE_PROVIDER`.
- **`agent_config.py` already documents the additive-key rule.** Its `spec_context` handling is
  the precedent: "ABSENT is fine and means a pre-12.6.6 backend ... PRESENT-BUT-WRONG is a
  refusal". `endpoint` and `harness` follow it exactly, so `version` stays `1` and no runner
  agent in the field is stranded mid-phase.
- **`ExecutionRouter.decide` routes ANY step carrying a `requires:` block to `remote`**
  (12.6 rule 2), agent steps included; `parse_requirements` is the one parser and `has:` is
  subset containment against `labels["has"]`. **`runner-local` therefore needs no grammar
  change, no protocol change, and no new message type** - it needs one injected requirement.
- **`MatrixModelEntry` is `{agent, model, label, step_config}` with `agent` required and
  validated against `AGENT_VOCABULARY`, and `model: null` legal.** A self-hosted matrix cell
  is `{"agent": "openai-harness", "model": "endpoint:local-4090"}`. **The 12.6.5 matrix needs
  no schema change at all to mix API and self-hosted models.**
- **A new WS frame type is not free.** `frontend/src/lib/stores/websocket.ts`
  (`ServerMessageType` + `HANDLED_MESSAGE_TYPES` + the switch) is drift-guarded by
  `websocket.test.ts`, which greps the backend source. `model_endpoint_status` is added on
  both sides in one commit or the guard fails.
- `runner_registry.find_available` filters on `Runner.status == IDLE` **and**
  `runner.id in self._connections`, and `MAX_CONCURRENT_STEPS = 1` per agent process
  (wave 5 section 10). That constant is why section 6.4's concurrency answer is what it is.

---

## 1. THE MODEL ENDPOINT ENTITY (14.1)

### 1.1 The model (`backend/app/models/model_endpoint.py`, new)

One row is one addressable (server, model) pair. Not one row per server: ollama on one box
serving `qwen2.5-coder:32b` and `llama3.1:8b` is two endpoints, because every decision the
platform makes - tool support, context window, rate, concurrency - is a property of the
MODEL on that server, not of the server. Two rows sharing a `base_url` is normal and cheap.

| Column | Type | Notes |
|---|---|---|
| `id` | `String(36)` PK | uuid4 |
| `name` | `String(40)` NOT NULL, UNIQUE | `^[a-z0-9][a-z0-9-]{0,38}$`. The handle every other surface uses (`model: "endpoint:local-4090"`). Capped at 40 so `endpoint:<name>` fits `gpu_node_id`'s `String(64)` |
| `description` | `Text` | operator prose |
| `base_url` | `String(512)` NOT NULL | the OpenAI-compatible ROOT **including** the version segment, e.g. `http://192.168.1.50:11434/v1`. Stored normalized (trailing `/` stripped); a URL not ending in `/v1` is accepted with a WARNING on the record, never rewritten |
| `model` | `String(200)` NOT NULL | the id sent in the request body (`qwen2.5-coder:32b`) |
| `server_kind` | `String(24)` NOT NULL | `ollama \| vllm \| llamacpp \| lmstudio \| other`. **Forensics and probe HINTS only - never behavior.** The only place it is read is the context-window discovery order (section 2.2) |
| `auth_style` | `String(16)` NOT NULL | `none \| bearer \| header` (section 1.2) |
| `auth_secret_ref` | `String(64)` null | the NAME of a backend env var. **Never a value** |
| `auth_header_name` | `String(64)` null | required when `auth_style == "header"`, e.g. `x-api-key` |
| `reach` | `String(16)` NOT NULL | `direct \| runner-local \| proxy`, default `direct` |
| `runner_label` | `String(64)` null | `runner-local` only; defaults to `endpoint:<name>` |
| `rate_usd_hour` | `Numeric(18,6)` null | `null` = unpriced. `0.000000` is a LEGAL, meaningful value (owned hardware, marginal cash cost) |
| `gpu_node_id` | `String(64)` NOT NULL | defaults to `endpoint:<name>`. Joins `step_usages.gpu_node_id` and `usage_pricing` |
| `max_concurrency` | `Integer` NOT NULL | default **1** (section 6.4) |
| `request_timeout_seconds` | `Integer` NOT NULL | default 300; per HTTP request, not per step |
| `context_window` | `Integer` null | operator override; authoritative when set |
| `max_output_tokens` | `Integer` null | default 1024 applied at use time when null |
| `supports_tools` | `Boolean` null | **probed.** `null` = never probed |
| `supports_streaming` | `Boolean` null | probed |
| `reports_usage` | `Boolean` null | probed - does the server return a `usage` block at all |
| `probe_status` | `String(16)` NOT NULL | `unprobed \| ok \| degraded \| unreachable`, default `unprobed` |
| `probe_detail` | `Text` NOT NULL | JSON, default `{}`. Scrubbed (section 1.2) and capped at 4 KiB |
| `probed_at` | `DateTime` null | |
| `probed_from` | `String(64)` null | `backend` or `runner:<runner_id>` |
| `probe_harness_version` | `String(64)` null | git-describe of LazyAF at probe time (M13 provenance) |
| `consecutive_failures` | `Integer` NOT NULL | default 0; bumped by probes AND by real step outcomes |
| `last_success_at` | `DateTime` null | |
| `last_error` | `Text` null | scrubbed, 512 chars |
| `enabled` | `Boolean` NOT NULL | default true. A disabled endpoint fails at dispatch with a clear reason; existing runs are untouched |
| `created_at` / `updated_at` | `DateTime` | |

Derived, never stored (computed in the schema layer so there is one definition):

```python
@property
def probe_age_seconds(self) -> float | None: ...

@property
def probe_stale(self) -> bool:
    """True when the capability record is older than PROBE_TTL_SECONDS.
    A stale record still WORKS (section 2.4); it is amber, not red."""

@property
def health(self) -> str:
    """healthy | stale | degraded | unhealthy | unprobed. DERIVED from
    probe_status + probe_age + consecutive_failures, because a second
    stored health column is a second writer that drifts from the first."""
```

`supports_tools` deliberately has **three** states. `None` is not "assume no": it is
"we have not asked", and section 6.1 refuses to dispatch on it. A default of `False` would
silently route every new endpoint down the fallback protocol, which is exactly the kind of
invisible downgrade R1 exists to forbid.

### 1.2 Auth: a REFERENCE in the row, the value only in the 12.5 secret channel

| `auth_style` | Request header | Requires |
|---|---|---|
| `none` | (none) | nothing. **The default and a first-class case** - LAN ollama and vLLM behind a firewall genuinely have no key, and a schema that makes "no auth" the exceptional branch is a schema that will grow a fake key |
| `bearer` | `Authorization: Bearer <secret>` | `auth_secret_ref` |
| `header` | `<auth_header_name>: <secret>` | `auth_secret_ref` + `auth_header_name` |

**The database never stores a secret value.** `auth_secret_ref` names an environment variable
on the BACKEND, resolved at dispatch exactly as `agent_secret_environment` already resolves
`ANTHROPIC_API_KEY` from settings. Rationale, stated because it is the security decision of
this phase: LazyAF has no secret-at-rest story (no encryption key, no KMS, SQLite backups are
plain files, and `GET /api/model-endpoints` is unauthenticated like the rest of the operator
API). A stored key would be a new class of exposure introduced for the convenience of one
form field.

```python
# backend/app/services/model_endpoints/secrets.py
ENDPOINT_SECRET_PREFIX = "LAZYAF_ENDPOINT_"
ENDPOINT_SECRET_REF_RE = re.compile(r"^LAZYAF_ENDPOINT_[A-Z0-9_]{1,48}$")

#: The FIXED env var the harness reads inside the container, whatever the
#: backend-side ref is called. One name container-side means the harness
#: never has to be told where to look.
HARNESS_API_KEY_ENV = "LAZYAF_ENDPOINT_API_KEY"
```

The prefix allowlist is load-bearing: without it, `auth_secret_ref: "LAZYAF_STEP_AUTH_SECRET"`
or `"ANTHROPIC_API_KEY"` would exfiltrate the platform's own credentials into a container the
operator does not control. A ref failing the regex is a **422 at CREATE time**, not a dispatch
failure. A ref that passes the regex but resolves to nothing is a **dispatch failure** naming
the variable (12.5's precedent, verbatim): burning 30 seconds of container start to reach an
opaque 401 is the outcome that rule exists to prevent.

`GET`/`PATCH` return `auth_secret_ref` and a computed `secret_present: bool`. They never
return, echo, or log the value.

**Scrubbing.** Any upstream text this phase persists or logs -`probe_detail`, `last_error`,
proxy error bodies, harness log lines - passes through
`scrub_secrets(text, known_values) -> str`, which replaces the resolved secret value (when
known), anything matching `(?i)bearer\s+\S+`, and `sk-[A-Za-z0-9_-]{8,}` with `***`. A 401
body that echoes the key back is a real failure mode and it must not be the thing that puts
the key in the database.

### 1.3 Reach modes

| `reach` | Who makes the HTTP call | `base_url` is written in the terms of | Routing consequence |
|---|---|---|---|
| `direct` (default) | the step container | the step container's network position | none - the step routes local (or remote if the operator also wrote a `requires:`) |
| `runner-local` | the step container, on the runner's host | that host (`http://172.17.0.1:11434/v1`, `http://host.docker.internal:11434/v1`, or a LAN IP) | the step is FORCED remote by an injected `requires: {has: ["<runner_label>"]}` (section 6.2). Zero inbound connectivity to the operator's house; no tunnel |
| `proxy` | the BACKEND, on the container's behalf | the backend's network position | the step routes local and its `base_url` is rewritten to the broker path (section 6.3) |

`runner-local` is the mode that makes NAT'd home hardware work, and it works because 12.6
already pushes work to a runner over an outbound WebSocket the runner opened. The endpoint's
URL never has to be reachable from anywhere except the box that hosts the model.

### 1.4 Migration `backend/alembic/versions/0011_model_endpoints.py`

`revision = "0011"`, `down_revision = "0010"`. Idempotent throughout
(`inspector = sa.inspect(op.get_bind())`, `has_table` / `has_index` / column-name guards), so
a pre-alembic dev DB healed by `create_all` and then stamped still upgrades cleanly.

1. `create_table("model_endpoints", ...)` with the columns above. Indexes:
   `ix_model_endpoints_name` (unique), `ix_model_endpoints_gpu_node_id`,
   `ix_model_endpoints_enabled_reach`.
2. `with op.batch_alter_table("step_executions")`: ADD `model_endpoint_id`
   (`String(36)`, null, FK `fk_step_executions_model_endpoint` -> `model_endpoints.id`).
   Index `ix_step_executions_endpoint_status` on `(model_endpoint_id, status)` - the
   admission gate's only query (section 6.4).
3. **Nothing is added to `step_usages`.** The endpoint join goes through the existing
   `step_usages.gpu_node_id` (which is why `gpu_node_id` defaults to `endpoint:<name>` and is
   NOT NULL on the endpoint row). A materialized `model_endpoint_id` on the usage table would
   be a second writer for a fact the join already carries - the same discipline 12.6.5 applied
   when it refused to copy `cost_usd` onto `experiment_runs`.

Down-revision drops the index, the column and the table, in that order.

### 1.5 API surface (`backend/app/routers/model_endpoints.py`, new)

| Method + path | Auth | Notes |
|---|---|---|
| `GET /api/model-endpoints` | operator (open, like the rest) | list + derived health + live `in_flight`. Never returns a secret value |
| `POST /api/model-endpoints` | operator | creates, then probes SYNCHRONOUSLY unless `?probe=false`. Returns the row WITH the probe record, so the operator learns "this model cannot tool-call" at the moment of registration, not at the first 30-minute step |
| `GET /api/model-endpoints/{id}` | operator | |
| `PATCH /api/model-endpoints/{id}` | operator | changing `base_url`, `model`, `server_kind` or `auth_*` **resets the capability record to `unprobed`** and nulls the three capability booleans. A capability observed against a different model is not evidence about this one |
| `DELETE /api/model-endpoints/{id}` | operator | **409 while `in_flight > 0`**, naming the step run ids. Otherwise soft: the row is deleted and `step_executions.model_endpoint_id` is set null by the FK's `ondelete="SET NULL"`; historical `step_usages` keep their `gpu_node_id` string and stay priceable from `settings.gpu_node_rates` |
| `POST /api/model-endpoints/{id}/probe` | operator | section 2. Returns **200 with the record even when the endpoint is down** |
| `POST /api/model-endpoints/{id}/probe-result` | **step JWT** (`verify_step_auth`) | the runner-local probe run reports here (section 2.3). Additionally requires `step_execution.model_endpoint_id == id` - the split-brain fence, borrowed from 12.6 |
| `ANY /api/model-endpoints/{id}/proxy/v1/{path:path}` | **step JWT** | the broker (section 6.3). `reach == "proxy"` only; anything else is 404 |
| `GET /api/model-endpoints/{id}/usage` | operator | rollup over `step_usages WHERE gpu_node_id = endpoint.gpu_node_id`: tokens, `by_source` counts, `cost_coverage`, step count, median wall clock |

Schemas live in `backend/app/schemas/model_endpoint.py`: `ModelEndpointCreate`,
`ModelEndpointUpdate`, `ModelEndpointRead`, `EndpointCapabilities`, `ProbeResult`,
`EndpointInFlight`. Vocabularies are `Literal[...]` so an unknown `reach` or `auth_style` is a
422, matching the 12.5 usage-schema idiom.

WS: `manager.publish_model_endpoint_status(endpoint_id, payload)` emits a
`model_endpoint_status` frame on probe completion, health change and in-flight change. The
frontend adds it to `ServerMessageType` + `HANDLED_MESSAGE_TYPES` in the same commit
(cross-agent contract 10).

---

## 2. THE CAPABILITY PROBE

`backend/app/services/model_endpoints/probe.py`. One function does the work and it is
transport-agnostic on purpose, because the same code runs backend-side and, for
`runner-local`, inside a container on the operator's box:

```python
PROBE_TIMEOUT_SECONDS = 20        # per request
PROBE_TOTAL_TIMEOUT_SECONDS = 60  # all four requests
PROBE_TTL_SECONDS = 86_400        # 24h: capability record is stale after this
PROBE_MIN_INTERVAL_SECONDS = 30   # in-flight/recent probes return the cached record
DEFAULT_ASSUMED_CONTEXT = 8192    # used ONLY with a loud log line
DEFAULT_MAX_OUTPUT_TOKENS = 1024

def run_probe(spec: ProbeSpec) -> ProbeResult:
    """Four requests, no side effects, never raises. Pure enough to run in a
    container with nothing but `requests` and to unit-test against a stub."""
```

### 2.1 What it actually sends

**Request 1 - liveness and model presence.** `GET {base_url}/models`.
200 -> `model_listed = configured model appears in `data[].id``. Also harvests
`data[].max_model_len` (vLLM ships it) into the context-window candidates.
404/501 -> `model_listed = null` and NOT a failure; some brokers do not implement it.
Connection error / TLS error / timeout -> **unreachable, stop here** (requests 2-4 are
pointless and would triple the operator's wait).

**Request 2 - tool calling.** `POST {base_url}/chat/completions`:

```json
{
  "model": "<endpoint.model>",
  "messages": [
    {"role": "system", "content": "You call tools. Never answer in prose."},
    {"role": "user", "content": "Call the tool `probe` with value 7. Do not reply in text."}
  ],
  "tools": [{
    "type": "function",
    "function": {
      "name": "probe",
      "description": "Echo a number back to the caller.",
      "parameters": {
        "type": "object",
        "properties": {"value": {"type": "integer", "description": "The number to echo."}},
        "required": ["value"]
      }
    }
  }],
  "tool_choice": "auto",
  "max_tokens": 64,
  "temperature": 0,
  "stream": false
}
```

**Request 3 - streaming.** The same body without `tools`, with `"stream": true` and
`"stream_options": {"include_usage": true}`, `"max_tokens": 8`.

**Request 4 - context window, `server_kind == "ollama"` only.**
`POST {base_url without the trailing /v1}/api/show` with `{"model": "<model>"}`; reads
`model_info["*.context_length"]` (the key is family-prefixed, so the probe scans for a key
ENDING in `.context_length` and takes the max). This is a NAMED vendor extension attempted for
exactly one `server_kind`, not a guess applied everywhere.

### 2.2 How it decides

| Capability | Decided by | Recorded when it fails |
|---|---|---|
| `supports_tools` | request 2 returns **200** AND `choices[0].message.tool_calls` is a non-empty list AND `tool_calls[0].function.name == "probe"` AND `tool_calls[0].function.arguments` parses as a JSON object | `probe_detail.tools_reason` in `{"http_400","no_tool_calls","wrong_tool","bad_arguments_json","http_5xx","timeout"}` plus the scrubbed response body, truncated to 512 chars |
| `supports_streaming` | request 3 yields at least one SSE `data:` frame carrying `choices[].delta` before `[DONE]` | `probe_detail.stream_reason` |
| `reports_usage` | request 2 (or 3) returns a `usage` object with an integer `prompt_tokens` **or** `completion_tokens` | `probe_detail.usage_reason` - this is the capability that decides whether ANY cost number is possible |
| `context_window` | first hit in this order: (1) operator override on the row, (2) request 4's ollama `*.context_length`, (3) request 1's `max_model_len`, (4) `null` | `null` means the harness assumes `DEFAULT_ASSUMED_CONTEXT` and says so, loudly, in the step log |
| `max_output_tokens` | operator override, else `null` -> `DEFAULT_MAX_OUTPUT_TOKENS` at use time | |

The probe deliberately uses `tool_choice: "auto"`, not `"required"`. Several servers accept
`required` and then emit prose anyway; a probe that trusts the parameter is testing the
server's ADVERTISING. This probe checks the actual response shape, which is the only thing the
harness can rely on.

`probe_status` is then:

- `ok` - request 1 succeeded and at least one of `supports_tools` / `supports_streaming` is
  true and `reports_usage` is true.
- `degraded` - reachable, but tool calling failed, or `reports_usage` is false, or the model
  is not listed. **Degraded is USABLE**: `supports_tools=False` routes the fallback protocol,
  and `reports_usage=False` routes an honest token-blind usage row. The status exists so the
  UI can say why the endpoint will behave the way it will.
- `unreachable` - request 1 failed.

### 2.3 Where the probe runs, and what it does when the endpoint is down

| `reach` | Probed from | Mechanism |
|---|---|---|
| `direct` | the backend, in-process (`httpx.AsyncClient`) | `probed_from = "backend"`. The backend and the step container share `lazyaf-network` in every supported deployment, so backend reachability is a good - not perfect - proxy for step reachability. Section 8 names the gap |
| `proxy` | the backend, in-process | reachability from the backend is the mode's PREMISE, so the probe tests exactly the right hop |
| `runner-local` | a real pipeline run, on the runner | see below |

A `runner-local` endpoint is unreachable from the backend by definition; probing it therefore
uses the machinery that already reaches that host. `POST /api/model-endpoints/{id}/probe`
starts a one-step ad-hoc run through `agent_run.start_adhoc_agent_run`'s sibling
`start_endpoint_probe_run(db, endpoint)` with `trigger_type = "endpoint_probe"`:

```yaml
type: script
config:
  image: "lazyaf-agent-base:dev"
  requires: {has: ["endpoint:local-4090"]}
  command: "python3 -m runner_common.endpoint_probe"
  environment: {LAZYAF_PROBE_ENDPOINT_ID: "<id>"}
timeout: 120
```

`runner_common/endpoint_probe.py` is the SAME `run_probe` logic (it imports
`runner_common.harness.client`, which is the same HTTP client the harness uses - one client,
one bug surface) and POSTs the `ProbeResult` to
`/api/model-endpoints/{id}/probe-result` with the step JWT it already has. `probed_from` is
stamped server-side from `step_execution.runner_id`, never from the payload.

This is worth the extra plumbing for one reason: it probes from **the exact network position
the real step will occupy**, which the backend cannot do for this mode. And the failure to
schedule it is itself information - if no runner carries the label, the probe run fails at
`NO_RUNNER_TIMEOUT` with "no runner carries label endpoint:local-4090", which is the true
reason the endpoint is unusable.

**When the endpoint is down:**

- `probe_status = "unreachable"`, `probed_at` stamped, `last_error` set (scrubbed, 512 chars),
  `consecutive_failures += 1`.
- **The capability booleans are LEFT AT THEIR PREVIOUS VALUES.** Nulling a good record because
  the box was rebooting is strictly worse than carrying a stale one; the record is timestamped
  and the UI shows the age.
- The API returns **200** with the record. A probe is an observation, and "it is down" is a
  successful observation. Returning 502 would make the operator's UI show a request error
  where it should show a red endpoint.
- A never-probed endpoint that is unreachable keeps `supports_tools = None`, which section 6.1
  refuses to dispatch on. That is the correct outcome: we do not know how to drive it.

### 2.4 Staleness

Both mechanisms, because they answer different questions.

- **A re-probe endpoint** exists and is the operator's manual lever
  (`POST .../probe`), rate-limited to one in flight per endpoint via a per-id `asyncio.Lock`
  plus `PROBE_MIN_INTERVAL_SECONDS = 30`; a call inside that window returns the cached record
  with `"cached": true`.
- **A TTL of 24h** governs automatic behavior at dispatch:

| State at dispatch | Behavior |
|---|---|
| `probe_status == "unprobed"` | **FAIL AT DISPATCH**: `"endpoint 'local-4090' has never been probed; POST /api/model-endpoints/<id>/probe first"`. Not a probe-on-first-use: a 30-minute agent step is not the place to discover the model cannot tool-call, and an implicit probe would put a 60s stall inside a step's timeout budget |
| `probe_stale` (older than TTL) | **RUN**, log `[executor] endpoint local-4090 capability record is 31h old; re-probing in the background`, schedule a background re-probe, and stamp `raw.endpoint_probe_age_s` on the usage row. Blocking on staleness would make a working endpoint stop working overnight; running blind would hide it. Warn plus refresh is the only honest option |
| `probe_status == "unreachable"` and `consecutive_failures >= 3` | **FAIL AT DISPATCH** naming `last_error`. Three consecutive failures is not a blip |
| `enabled == false` | **FAIL AT DISPATCH**: `"endpoint 'local-4090' is disabled"` |

A successful step also refreshes `last_success_at` and zeroes `consecutive_failures`
(section 5.4), so a healthy endpoint never drifts into the stale-and-failing state through
disuse of the probe button alone.

---

## 3. THE AGENT HARNESS (14.2) - the core of this milestone

`runner-common/runner_common/harness/`:

```
harness/
  __init__.py        # HarnessExecutor is the only public name
  constants.py       # every budget and limit, named, no inline literals
  client.py          # OpenAICompatClient - requests-based, streaming, retries
  tools.py           # the tool table, schemas, and the sandboxed implementations
  transcript.py      # message list + token estimation + elision
  fallback.py        # the no-tools text protocol: prompt, parser, corrections
  loop.py            # run_loop() - the state machine and its stop conditions
  executor.py        # HarnessExecutor(AgentExecutor) - the EXECUTORS entry
```

Registered container-side as one more builder in the existing map:

```python
# runner_common/agent_wrapper.py
EXECUTORS = {
    "claude-code": lambda cfg: ClaudeExecutor(output_format="stream-json" if cfg.stream else "json"),
    "gemini":      lambda cfg: GeminiExecutor(),
    "mock":        lambda cfg: MockExecutor(mock_config=cfg.mock_config),
    "openai-harness": lambda cfg: HarnessExecutor(endpoint=cfg.endpoint, harness=cfg.harness),
}
```

`HarnessExecutor.runner_type` is `"openai-harness"`. `build_command(config)` returns
`["<lazyaf-harness>", endpoint["name"], endpoint["model"]]` - **used only for the `$ ...`
log line the base class emits**; no subprocess is ever spawned for the model. `execute()` is
overridden entirely and never calls `super().execute()`.

Everything else about the step is 12.5, unchanged: the wrapper loads and consumes the agent
config, refuses to run as root, installs the SIGTERM handler, materialises spec context,
calls `executor.execute(ec, log_callback=_emit, streaming=cfg.stream)`, then `_finish(cfg,
result)`, then writes the usage manifest in a `finally`. **The harness is a new executor, not
a new step type.**

### 3.1 The tool set - the minimum that can complete a real card

Six tools. Every additional tool costs schema tokens in **every** request, which on an
8k-context model is a real budget line, so each one has to earn its place.

| Tool | Arguments | Result | Why it is not removable |
|---|---|---|---|
| `list_files` | `path: str = "."`, `depth: int = 2`, `max_entries: int = 200` | newline-separated relative paths with a `[N more elided]` marker | The model cannot read what it cannot find. `run_shell("ls -R")` exists but is unbounded, and one unbounded listing costs a small model its whole context |
| `read_file` | `path: str`, `start_line: int = 1`, `max_lines: int = 400` | numbered lines plus `total_lines` | RANGED, because a 4000-line file does not fit in an 8k window and a model that can only read whole files simply cannot work on a real repo |
| `write_file` | `path: str`, `content: str` | `{"bytes": N, "created": bool}` | The one write primitive every model gets right. Creating a new file has no other spelling |
| `apply_patch` | `path: str`, `find: str`, `replace: str`, `count: int = 1` | `{"occurrences": N, "applied": N}` | Editing one function in a 2000-line file by rewriting the file is unaffordable at small context. **Exact-string find/replace, NOT unified diff**: small models produce malformed hunks constantly, and a diff applier that fails half the time burns turns without teaching the model anything. `occurrences == 0` is a tool ERROR naming the nearest matching line, which is the feedback that makes the retry work |
| `run_shell` | `command: str`, `timeout: int = 120` | `{"exit_code": N, "stdout": "...", "stderr": "..."}` | This is the tool that makes the loop able to VERIFY. Tests, build, grep, `git status`, `git diff`. **A non-zero exit is a RESULT, not an error** - "the tests failed" is the single most useful observation the loop can make |
| `finish` | `status: "success"\|"failed"\|"blocked"`, `summary: str` | terminates | Termination has to be unambiguous. A magic string in prose is a parsing problem; a tool call is a fact (section 3.3) |

Deliberately absent, with reasons:

- **`search` / `grep`** - `run_shell("grep -rn ...")` covers it. A seventh schema for a
  one-line shell command is schema tax on every turn.
- **`delete_file`** - `run_shell("rm ...")` covers it, rarely, and making deletion require the
  shell tool means it appears verbatim in the log where an operator can see it.
- **`git_commit` / `git_push`** - the platform commits. See section 3.6.

**Sandbox rules, enforced in `tools.py` and pinned by unit tests:**

1. Every `path` is `os.path.realpath`-resolved and must live under
   `cfg.repo["workdir"]` (`/workspace/repo`). Escape -> tool error
   `"path escapes the workspace"`. Not an exception, not a crash: a tool error is a message
   the model can learn from.
2. `/workspace/.control` is denied even though it is outside the workdir anyway - belt and
   braces, because that directory holds a sibling step's config and the usage manifest.
3. `run_shell` runs `bash -lc <command>` with `cwd=workdir`, the step's own environment
   **minus** `HARNESS_API_KEY_ENV` and minus every `LAZYAF_*` variable that is not
   `LAZYAF_PIPELINE_RUN_ID` / `LAZYAF_STEP_RUN_ID`. The model has no business holding the
   endpoint key or the step's identity.
4. `run_shell` denies a small, explicit, stated command denylist:
   `git push`, `git remote`, `git config --global`, and anything containing the value of
   `repo["remote_url"]`'s credential form. Denial returns a tool error saying
   `"the platform commits and pushes this step's work; do not push"`. Rationale: a model
   pushing to the run's own trigger branch re-fires the push trigger that started the run -
   the exact loop `resolve_agent_work_branch` exists to prevent. `git add`/`commit`/`status`/
   `diff` remain allowed and are harmless (the wrapper's later commit finds nothing to add
   and says "no changes to commit").
5. Output caps: `TOOL_OUTPUT_MAX_BYTES = 8192`, applied head+tail with a
   `...[N bytes elided]...` marker in the middle. A tool result is always truncatable; the
   transcript is not.

### 3.2 The loop

```python
def run_loop(ctx: HarnessContext) -> HarnessOutcome:
    tx = Transcript(system=SYSTEM_PROMPT, user=ctx.prompt, ctx=ctx)
    consecutive_textonly = 0
    consecutive_tool_errors = 0
    consecutive_malformed = 0

    for turn in range(1, ctx.max_iterations + 1):
        if ctx.past_deadline():           return stop("time_budget", turn)
        if ctx.tokens_spent >= ctx.max_total_tokens: return stop("token_budget", turn)

        tx.fit()                          # section 3.7 - elide before sending, never after
        try:
            response = ctx.client.chat(tx.messages, tools=ctx.tool_schemas_or_none())
        except EndpointFatal as exc:      return stop("endpoint", turn, error=str(exc))
        ctx.record_usage(response)        # EVERY turn, section 5.1
        ctx.log_turn(turn, response)

        calls, prose, malformed = ctx.interpret(response)   # tools mode or fallback

        if malformed is not None:
            consecutive_malformed += 1
            if consecutive_malformed > ctx.max_malformed_retries:
                return stop("unparseable", turn, error=malformed.reason)
            tx.append_correction(malformed)
            continue
        consecutive_malformed = 0

        if not calls:
            consecutive_textonly += 1
            if consecutive_textonly >= NO_TOOL_PATIENCE:
                return stop("model_stopped_calling_tools", turn, prose=prose)
            tx.append_nudge()
            continue
        consecutive_textonly = 0

        for call in calls[:ctx.max_tool_calls_per_turn]:
            if call.name == "finish":
                return stop("finish", turn, finish=call.args)
            result = TOOLS[call.name].run(ctx, call.args)   # never raises
            consecutive_tool_errors = (consecutive_tool_errors + 1) if result.is_error else 0
            tx.append_tool_result(call, result)
            if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                return stop("tool_error_loop", turn, error=result.text)

    return stop("iteration_budget", ctx.max_iterations)
```

**Stop conditions, exhaustively.** This table is the answer to "model output that never
terminates":

| # | Condition | Constant / default | Outcome |
|---|---|---|---|
| 1 | `finish` tool called | - | terminal; status from its `status` argument |
| 2 | iteration budget | `max_iterations = 40` | FAILED, exit 3 |
| 3 | token budget | `max_total_tokens = 400_000` summed over turns | FAILED, exit 3 |
| 4 | **soft deadline** | `time_budget_seconds = step_timeout - HARNESS_TIME_RESERVE (60)` | FAILED, exit 3 |
| 5 | model stops calling tools | `NO_TOOL_PATIENCE = 2` consecutive prose-only turns | status from `_looks_final(prose)`; FAILED unless it reads as a completion claim, in which case section 3.5's change check decides |
| 6 | tool-error loop | `MAX_CONSECUTIVE_TOOL_ERRORS = 5` | FAILED, exit 1 - small models thrash on the same failing `apply_patch` indefinitely |
| 7 | unparseable responses (fallback mode) | `MAX_MALFORMED_RETRIES = 3` consecutive | FAILED, exit 5 (section 3.8) |
| 8 | endpoint fatal | `MAX_ENDPOINT_RETRIES = 3` per request on 429/5xx/timeout with jittered backoff; any other 4xx is fatal on the first response | FAILED, exit 4 |
| 9 | SIGTERM | - | current tool call finishes, usage is written, exit 143 |
| 10 | context floor unmeetable at turn 0 | - | FAILED, exit 6, **before spending a single token** (section 3.7) |

Condition 4 is the load-bearing one and it is a deliberate refinement of 12.5's
"ONE timeout owner" rule, not a violation of it. `images/base/control/executor.py` remains the
only component that KILLS anything. The harness sets a SOFT deadline strictly inside that hard
one and treats crossing it as an ordinary stop, so it still gets to commit its partial work,
write the usage manifest and exit with a meaningful code - instead of being SIGKILLed with
nothing to show for 30 minutes of GPU time. `HARNESS_TIME_RESERVE = 60` is the commit-plus-push
budget; a step whose timeout is under 120s gets `time_budget_seconds = timeout // 2` and a
warning.

`constants.py` in full:

```python
NO_TOOL_PATIENCE              = 2
MAX_CONSECUTIVE_TOOL_ERRORS   = 5
MAX_MALFORMED_RETRIES         = 3      # consecutive; reset by any successful parse
MAX_TOOL_CALLS_PER_TURN       = 4
MAX_ENDPOINT_RETRIES          = 3
ENDPOINT_RETRY_BASE_SECONDS   = 1.5    # full jitter, capped at 20s
HARNESS_TIME_RESERVE          = 60
TOOL_OUTPUT_MAX_BYTES         = 8192
TOOL_SHELL_TIMEOUT            = 120
MAX_EVENT_LINE                = 2000   # same value the 12.5 wrapper uses
KEEP_RECENT_TURNS             = 6
CONTEXT_RESERVE_FRACTION      = 0.15
DEFAULT_MAX_ITERATIONS        = 40
DEFAULT_MAX_TOTAL_TOKENS      = 400_000
```

### 3.3 The system prompt (tools mode)

Short on purpose - every token here is paid on every turn:

```
You are a software engineer working inside a git repository at {workdir}.
Complete the task using the tools provided. Work in small steps: read before
you write, and run the project's tests to check yourself.

Rules:
- Only the tools change anything. Describing an edit does not make it.
- When the task is done, call finish(status="success", summary=...).
- If the task cannot be done, call finish(status="blocked", summary=<why>).
- Do not commit or push. The platform commits your work for you.
- You have at most {max_iterations} turns.
```

`finish` is a tool rather than a magic phrase because termination is the one decision that
must not depend on parsing prose. "I think that's everything!" is not a signal; a call is.

### 3.4 Progress into the existing log stream

The harness writes to the step's stdout through the wrapper's existing `log_callback`
(`_emit`), so `run.py`'s log pump and `POST /api/steps/{id}/logs` carry it exactly as they
carry claude's events today. Every line keeps the `[agent] ` prefix, so the UI, the
`SCRAPE_FAILED_LOG_MARKER` grep and `verify_executor` are unchanged.

```
[agent] harness: endpoint=local-4090 model=qwen2.5-coder:32b mode=tools ctx=32768 reach=runner-local
[agent] harness: budgets iterations=40 tokens=400000 deadline=1740s tools=6
[agent] turn 1/40 in=1204 out=88 (total in=1.2k out=88) 3.1s
[agent]   text: I'll start by reading the router to see how routes are registered.
[agent]   tool list_files(path=backend/app/routers, depth=1) -> 14 entries
[agent] turn 2/40 in=1902 out=141 (total in=3.1k out=229) 4.4s
[agent]   tool read_file(path=backend/app/routers/repos.py, lines=1-400) -> 11.9 KB (of 640 lines)
[agent] turn 6/40 in=7734 out=302 (total in=22.4k out=1.4k) 9.8s
[agent]   tool run_shell(`pytest -q tdd/unit/api`) -> exit 1 in 12.4s
[agent]   tool ERROR apply_patch: `find` matched 0 occurrences in backend/app/routers/repos.py
[agent] context: elided 8 earlier messages (est 29,100 -> 14,800 tokens)
[agent] stop: finish(status=success) after 11 turns, 34.2k in / 2.1k out, 4m12s
```

Rules:

- ONE line per event, truncated at `MAX_EVENT_LINE` - the same rule and the same constant
  the 12.5 wrapper already uses for claude's stream-json events.
- Tool ARGUMENTS are rendered (elided to 120 chars each); tool RESULTS are summarized to size
  and exit code. Dumping results would double the log volume of the step and put file contents
  into `StepRun.logs` for no benefit.
- The model's prose IS emitted (truncated). R1: a 20-minute dark step is unacceptable, and
  prose is the only window into what a model that has stopped calling tools is thinking.
- **The Authorization header, `HARNESS_API_KEY_ENV`'s value and `endpoint.auth_secret_ref`'s
  resolved value never appear in any line.** Pinned by a sentinel test over every emitted
  line, mirroring 12.6's `test_secret_hygiene.py`.
- The full JSONL transcript is written to
  `$HOME/.lazyaf/harness/<step_execution_id>.jsonl` **only** when
  `harness.debug_transcript` is true. Off by default; named as a seam (section 12) because
  M13 will want it as a first-class artifact and that is a channel, not a flag.

### 3.5 Success versus failure

| Stop reason | `ExecutorResult.success` | exit | `error` |
|---|---|---|---|
| `finish(status="success")` **and** the working tree changed | True | 0 | None |
| `finish(status="success")` and **nothing changed** and `require_changes` | **False** | 1 | `"the agent reported success but changed no files"` |
| `finish(status="failed"\|"blocked")` | False | 1 | the model's own `summary` |
| model stopped calling tools, prose reads final, tree changed | True | 0 | None |
| model stopped calling tools, no change | False | 1 | `"the agent stopped without calling finish and changed no files"` |
| iteration / token / time budget | False | 3 | names which budget and the counters |
| endpoint fatal | False | 4 | the scrubbed HTTP status and reason |
| unparseable (fallback) | False | 5 | reason + last raw response, 500 chars |
| context floor unmeetable | False | 6 | estimated prompt tokens vs the window |
| SIGTERM | False | 143 | `"cancelled"` |

The success-with-no-change refusal is the most opinionated line in this design and it is
deliberate. In a benchmark, a no-op that reports success is the most expensive possible
failure, because it looks like a cheap win: it costs almost nothing, scores as solved by the
harness, and only the oracle catches it. Making it a red step means the failure is visible in
the pipeline, not just in M13's aggregate. `require_changes` defaults to `commit.enabled`, so
an analysis-only step (`commit: false`, "review this and report") can turn it off explicitly.

"The tree changed" is `git status --porcelain` producing output, computed by the harness
before it returns - the same command `agent_wrapper._finish` already runs.

### 3.6 Committing: the 12.5 wrapper does it, and there is no second implementation

`HarnessExecutor.execute()` returns an `ExecutorResult` and **touches git zero times**.
`agent_wrapper._finish(cfg, result)` then does exactly what it does for claude and gemini
today: `configure_git`, `checkout -B <branch>`, `add -A`, `commit -m <message>`,
`push(workdir, branch, set_upstream=True)` via `runner_common.git_helpers`, honoring
`commit.enabled` / `commit.push` / `commit.allow_empty`.

That is why the shell denylist (section 3.1 rule 4) exists: the harness owns nothing about
landing work, and letting the model push would create a second, unpoliced path to the remote.
A unit test asserts `HarnessExecutor` contains no `git` invocation and does not import
`git_helpers`.

### 3.7 Context-window management (the owner's second open question, answered)

**Budget and elide. Never summarize. Refuse only when the floor cannot be met.**

- `Transcript` tracks an estimated token count: `len(text) // 4` per message, **corrected
  after every response** using the server's own `usage.prompt_tokens` to compute a live
  chars-per-token ratio. That correction is what turns a crude heuristic into a real feedback
  loop after turn 1, and it costs nothing.
- Working budget = `context_window - max_output_tokens - CONTEXT_RESERVE_FRACTION * context_window`.
  With `context_window == None` the harness uses `DEFAULT_ASSUMED_CONTEXT = 8192` and logs
  `[agent] WARNING: endpoint declares no context window; assuming 8192 tokens`. Assuming 128k
  silently is how a step dies at turn 12 with an opaque 400.
- `tx.fit()` runs **before every request**. When over budget it drops from the MIDDLE: the
  system message and the original task message are never dropped, the last
  `KEEP_RECENT_TURNS = 6` messages are never dropped, and the elided span is replaced by one
  synthetic user message: `"[12 earlier messages elided to fit the context window: 7 tool
  calls, 5 results. Re-read files if you need them.]"`. It logs the before/after estimate.
- **Summarization is rejected.** It costs an extra generation on the slowest, scarcest
  resource in the system (the local GPU), it burns output tokens against the same budget it is
  trying to protect, and a small model summarizing its own transcript is precisely the model
  least able to do it faithfully. An honest elision marker is cheaper and does not fabricate.
- A single tool result larger than half the working budget is truncated at
  `min(TOOL_OUTPUT_MAX_BYTES, budget // 2)` before it enters the transcript. A tool result is
  always truncatable.
- **The refusal**: if at turn 0 `system + prompt` alone exceeds the working budget, the step
  fails immediately with exit 6 and
  `"prompt (est 9,200 tokens) exceeds endpoint local-4090's context window (8,192); use a
  larger model, trim the spec context, or set context_window on the endpoint"`. Turn 0, before
  any spend. This is also the natural interaction with 12.6.6: an over-large curated spec
  bundle is caught here with a message naming the cause.

### 3.8 THE NO-TOOLS FALLBACK PROTOCOL

Entered when `harness.mode == "text"`, or when `mode == "auto"` and
`endpoint.capabilities.supports_tools` is false, or when a tools-mode request returns a 400
whose body mentions `tool`/`function` (a runtime demotion, logged, and recorded as
`raw.probe_drift = true`).

**What the model is asked to emit.** The system prompt replaces the tool schemas with:

````
You are a software engineer working inside a git repository at {workdir}.
You cannot act directly. To act, emit EXACTLY ONE fenced block per reply, in
this format and nothing else after it:

```lazyaf
{"tool": "read_file", "args": {"path": "src/main.py", "start_line": 1, "max_lines": 200}}
```

Available tools:
  list_files  {"path": str, "depth": int, "max_entries": int}
  read_file   {"path": str, "start_line": int, "max_lines": int}
  write_file  {"path": str, "content": str}
  apply_patch {"path": str, "find": str, "replace": str, "count": int}
  run_shell   {"command": str, "timeout": int}
  finish      {"status": "success"|"failed"|"blocked", "summary": str}

Rules:
- One block per reply. Prose before the block is allowed; nothing after it.
- The block must be valid JSON with exactly the keys "tool" and "args".
- Do not commit or push. The platform commits your work for you.
- When the task is done, emit a finish block.
- You have at most {max_iterations} turns.
````

**How it is parsed.** `fallback.parse_action(text) -> Action | Malformed`, in this exact
order:

1. Find every fenced block whose info string is `lazyaf` (case-insensitive), with
   `re.compile(r"^```[ \t]*lazyaf[ \t]*\r?\n(.*?)^```", re.DOTALL | re.MULTILINE)`.
2. **Zero blocks** -> `Malformed(reason="no_block")`.
3. **More than one block** -> take the **first**, and set `warn="multiple_blocks"` which is
   appended to the next correction. Deterministic beats clever: models routinely emit a plan
   block and a call block, and refusing that would burn a turn on a response that contains a
   perfectly good action.
4. `json.loads(block)`. On failure, ONE repair attempt: scan from the first `{` to its
   balanced closing `}` and parse that span (this fixes the overwhelmingly common
   trailing-prose-inside-the-fence case). Still failing -> `Malformed(reason="bad_json",
   detail=<json error>)`.
5. Top level must be an object with a string `tool` in the tool table ->
   else `Malformed(reason="unknown_tool: <name>")`.
6. `args` must be an object (absent is treated as `{}`); every required argument of that tool
   must be present and of the right JSON type -> else
   `Malformed(reason="missing_arg: path")` / `"bad_arg_type: max_lines expected integer"`.

**How malformed responses are handled without hanging and without silently passing:**

- Each malformed response appends ONE correction message (`role: "user"`) quoting the exact
  reason and re-stating the format with a one-line example:
  `"Your last reply could not be used: no ```lazyaf block found. Reply with exactly one
  ```lazyaf block containing JSON with keys "tool" and "args"."`
- `MAX_MALFORMED_RETRIES = 3` **consecutive**. The counter resets to zero on any successful
  parse, so a model that stumbles once every ten turns is not punished.
- On the **fourth consecutive** malformed response the step STOPS with exit 5 and
  `"endpoint local-4090 (model qwen2.5-coder:32b) produced no parseable action in 4
  consecutive turns; last reason: bad_json"`, plus the last raw response truncated to 500
  chars in the step log. **It never silently passes**: the step is FAILED, the usage row still
  lands with every token spent, and the endpoint's `consecutive_failures` is NOT bumped
  (this is a model-capability failure, not an endpoint failure - conflating them would make a
  working endpoint look down).
- Malformed turns count against `max_iterations` and against the token budget. A model that
  only ever emits prose therefore burns its budget and dies with a clear reason; it cannot
  loop forever.

**Tool results in fallback mode** are fed back as `role: "user"` messages, because the server
has no tool role:

```
TOOL RESULT read_file (ok)
   1 | from fastapi import APIRouter
   2 | ...
```
```
TOOL ERROR apply_patch: `find` matched 0 occurrences in backend/app/routers/repos.py
nearest line 88: `    return await list_repos(db)`
```

The two modes therefore produce the same observable transcript shape and the same log lines,
which is what lets one experiment vary ONLY `harness.mode` and attribute the difference.

**The bridge for a probe that lied.** After a **tools-mode** turn that returns no
`tool_calls`, the harness runs the fallback parser over `message.content` before treating the
turn as prose. If it parses, the action is executed, `raw.probe_drift = true` is recorded, and
the harness continues in tools mode. Two such turns in one step and the harness switches to
fallback mode for the remainder and logs the switch. Section 5.4 then demotes the endpoint's
stored `supports_tools`.

---

## 4. THE AGENT-CONFIG ADDITIONS (exact JSON) AND THE SHARED CONTRACT TEST

### 4.1 The wire, extending 12.5's contract additively

`version` stays `1`. Two new optional top-level keys. This follows the precedent
`agent_config.py` already documents for `spec_context`: an additive optional key that an old
consumer ignores and a new one defaults does NOT justify a version bump, because bumping
strands every runner agent in the field mid-phase.

```json
{
  "version": 1,
  "agent": "openai-harness",
  "model": "qwen2.5-coder:32b",
  "stream": true,
  "prompt": "You are implementing a feature for this project.\n\n## Feature Request\n...",
  "agents_json": null,
  "task": {
    "card_id": "c1d2...",
    "card_title": "Add rate limiting to /api/repos",
    "card_description": "...",
    "step_index": 3,
    "step_name": "implement"
  },
  "context": {
    "previous_step_name": "plan",
    "previous_step_logs": "...",
    "previous_step_logs_truncated": false
  },
  "repo": {
    "repo_id": "r9f8...",
    "workdir": "/workspace/repo",
    "base_branch": "main",
    "branch": "lazyaf/agent-8a44c1b2",
    "remote_url": "http://backend:8000/git/r9f8....git"
  },
  "commit": {
    "enabled": true,
    "message": "feat: Add rate limiting to /api/repos\n\nImplemented by LazyAF agent",
    "author_name": "LazyAF Agent",
    "author_email": "agent@lazyaf.local",
    "push": true,
    "allow_empty": false
  },
  "mock_config": null,
  "role": "worker",
  "spec_context": null,

  "endpoint": {
    "id": "e7c1a4b2-...",
    "name": "local-4090",
    "base_url": "http://172.17.0.1:11434/v1",
    "model": "qwen2.5-coder:32b",
    "server_kind": "ollama",
    "reach": "runner-local",
    "auth_style": "bearer",
    "auth_env": "LAZYAF_ENDPOINT_API_KEY",
    "auth_header": null,
    "request_timeout_seconds": 300,
    "capabilities": {
      "supports_tools": true,
      "supports_streaming": true,
      "reports_usage": true,
      "context_window": 32768,
      "max_output_tokens": 4096,
      "probe_status": "ok",
      "probed_at": "2026-08-30T09:14:22Z",
      "probed_from": "runner:workshop-1",
      "probe_age_seconds": 3821,
      "stale": false
    },
    "pricing": {
      "gpu_node_id": "endpoint:local-4090",
      "gpu_fraction": 1.0,
      "priced": true
    }
  },

  "harness": {
    "mode": "auto",
    "max_iterations": 40,
    "max_total_tokens": 400000,
    "time_budget_seconds": 1740,
    "max_tool_calls_per_turn": 4,
    "shell_timeout_seconds": 120,
    "tool_output_max_bytes": 8192,
    "temperature": 0,
    "top_p": null,
    "seed": 7,
    "require_changes": true,
    "debug_transcript": false
  }
}
```

Field notes, all load-bearing:

| Field | Why |
|---|---|
| `endpoint.auth_env` | names the FIXED container-side variable (`LAZYAF_ENDPOINT_API_KEY`) the harness reads. **The agent config never carries the key.** The value arrives through `secret_environment` in the STEP config file, which `run.py` merges into the child process's env and then deletes (12.5 section 1.4). `auth_style: "none"` -> `auth_env: null` and no secret is produced at all |
| `endpoint.model` | duplicated with top-level `model` on purpose: the top-level field is the 12.5 contract every executor reads, and the endpoint block is self-contained so `HarnessExecutor` needs exactly one argument. The producer sets both from the same source and a contract test asserts they are equal |
| `endpoint.capabilities` | a SNAPSHOT taken at dispatch, not a live reference. A step must behave identically if someone re-probes the endpoint mid-run; a snapshot is also what M13 needs to attribute a result to the capabilities that were actually in force |
| `endpoint.pricing.gpu_fraction` | `1.0 / max_concurrency` (section 5.2). On the wire so `run.py` can stamp `LAZYAF_GPU_FRACTION` without a DB lookup |
| `harness.mode` | `auto` (decide from `capabilities.supports_tools`) is what the backend writes unless the operator pinned `tools` or `text`. **Pinning is how M13 makes loop shape an independent variable**: forcing `text` on a tool-capable model measures the cost of the fallback protocol directly |
| `harness.temperature` / `seed` / `top_p` | the first agent LazyAF has where determinism is actually exposed. They go into `UsageManifest.determinism`, which has been an honest empty object for all three CLIs; M13's determinism-disclosure control finally has real values |
| `harness.time_budget_seconds` | computed backend-side as `timeout - HARNESS_TIME_RESERVE` so the soft deadline and the watchdog's hard one have exactly one source |

### 4.2 Producer and consumer

**Producer** - `control_layer.workspace.generate_agent_config` gains two keyword arguments,
`endpoint: dict | None = None` and `harness: dict | None = None`, emits them as the last two
top-level keys, and `agent_config_keys()` gains `"endpoint"` and `"harness"`. Validation lives
next to `validate_spec_context`:

```python
def validate_endpoint_block(agent: str, endpoint: dict | None) -> None:
    """Raise ValueError when the agent and the endpoint block disagree.

    - agent == "openai-harness" REQUIRES a block with non-empty base_url and
      model, and capabilities.probe_status != "unprobed".
    - any other agent MUST NOT carry one: an endpoint on a claude-code step is
      an authoring mistake whose silent acceptance would be a step that looks
      self-hosted in the UI and bills Anthropic.
    """
```

**Consumer** - `runner_common/agent_config.py` gains `endpoint: Optional[Dict] = None` and
`harness: Dict = field(default_factory=dict)`, plus the same three-way strictness the module
already applies to `spec_context`:

- absent -> `None` / `{}` (a pre-14 backend; every other agent is unaffected);
- present but not an object -> printed reason, `None` return, exit 1;
- `agent == "openai-harness"` with no endpoint block, or with an empty `base_url` or `model`
  -> printed reason, `None` return, exit 1.

Plus one convenience property used by the executor builder and nothing else:

```python
@property
def harness_mode(self) -> str:
    """'tools' | 'text', resolved from harness.mode and the probed
    capability. 'auto' with supports_tools None is a REFUSAL, not a
    guess - the backend already refuses to dispatch an unprobed
    endpoint, so reaching this branch means the wire lied."""
```

### 4.3 The shared contract test (R3)

`tdd/unit/control_runtime/endpoint_contract.py` - a shared module, the same instrument
`usage_contract.py` and `manifest_contract.py` already are, imported by **both** sides' tests
in one process (the `control_runtime` conftest already puts `images/base` and `runner-common`
on `sys.path`):

```python
ENDPOINT_BLOCK_KEYS = ("id", "name", "base_url", "model", "server_kind", "reach",
                       "auth_style", "auth_env", "auth_header",
                       "request_timeout_seconds", "capabilities", "pricing")
CAPABILITY_KEYS = ("supports_tools", "supports_streaming", "reports_usage",
                   "context_window", "max_output_tokens", "probe_status",
                   "probed_at", "probed_from", "probe_age_seconds", "stale")
PRICING_KEYS  = ("gpu_node_id", "gpu_fraction", "priced")
HARNESS_KEYS  = ("mode", "max_iterations", "max_total_tokens", "time_budget_seconds",
                 "max_tool_calls_per_turn", "shell_timeout_seconds",
                 "tool_output_max_bytes", "temperature", "top_p", "seed",
                 "require_changes", "debug_transcript")

def make_endpoint_payload(**overrides) -> dict: ...
def make_harness_payload(**overrides) -> dict: ...
```

Pinned by `tdd/unit/control_runtime/test_endpoint_config_contract.py`:

1. `generate_agent_config(agent="openai-harness", endpoint=..., harness=...)` output loads
   through `runner_common.agent_config.load_agent_config` with **zero key loss**, in one
   process.
2. Producer key set == `ENDPOINT_BLOCK_KEYS` exactly (no extras, no omissions); same for
   capabilities, pricing and harness.
3. `payload["model"] == payload["endpoint"]["model"]`.
4. The secret is nowhere: the rendered agent config contains no value from
   `secret_environment`, only the NAME in `auth_env`.
5. A block with `probe_status == "unprobed"` raises in the producer.
6. An `endpoint` block on a `claude-code` step raises in the producer.
7. `HarnessExecutor` constructed from a loaded config resolves the same mode the backend
   computed - one function decides `tools` vs `text` and both sides call it.

---

## 5. USAGE REPORTING

### 5.1 Token accumulation (the difference from every executor so far)

The three CLI executors scrape ONE final report. The harness makes N requests and must sum
them - this is the only genuinely new accounting logic in the milestone.

| Manifest field | Source |
|---|---|
| `provider` | `"openai-compatible"` - constant for this executor |
| `model` | `endpoint.model` (the real id, not the `endpoint:<name>` coordinate) |
| `model_version` | the response body's own `model` string when it differs from `endpoint.model`, else null. ollama returns the resolved tag, which is exactly M13's "the provider's exact version, not just the family" |
| `input_tokens` | `sum(r.usage.prompt_tokens for r in responses if r.usage)` |
| `output_tokens` | `sum(r.usage.completion_tokens ...)` |
| `cache_read_tokens` | `sum(r.usage.prompt_tokens_details.cached_tokens ...)` when present, else null |
| `cache_write_tokens` | always null - no OpenAI-compatible server exposes it |
| `cost_usd` | **always null from the harness** |
| `cost_source` | **always `"unknown"` from the harness** (section 5.2) |
| `determinism` | `{"temperature": .., "top_p": .., "seed": ..}` - the first non-empty one LazyAF has produced |
| `raw` | the accounting record below, capped at 8 KiB by `usage._cap_raw` |

Streaming requests send `"stream_options": {"include_usage": true}` so the final SSE frame
carries `usage`. Servers that ignore it produce a turn with no usage, which is counted:

```json
"raw": {
  "harness": {
    "endpoint_id": "e7c1a4b2-...",
    "endpoint_name": "local-4090",
    "endpoint_reach": "runner-local",
    "endpoint_max_concurrency": 2,
    "endpoint_probe_age_s": 3821,
    "mode": "tools",
    "turns": 11,
    "turns_without_usage": 0,
    "stop_reason": "finish",
    "finish_status": "success",
    "tool_calls": {"read_file": 6, "apply_patch": 3, "run_shell": 4, "finish": 1},
    "tool_errors": 1,
    "malformed_responses": 0,
    "context_elisions": 1,
    "endpoint_http_errors": 0,
    "probe_drift": false,
    "files_changed": 3
  }
}
```

Partial-report rule, stated so a partial is never read as a total: if SOME turns reported
usage and some did not, the sums are the sums of the reporting turns and
`turns_without_usage > 0` says so. If **no** turn reported usage, `input_tokens` and
`output_tokens` are **null** and the harness prints the existing 12.5 marker
`SCRAPE_FAILED_LOG_MARKER` (`"[agent] WARNING: usage scrape failed"`) with reason
`"endpoint reported no usage block in any of 11 turns"` - reusing that exact string means
`scripts/verify_executor.py`'s existing grep catches it on the dogfood lane for free. Zeroes
are never substituted for nulls: a zero is a claim, a null is an absence.

### 5.2 Dollars: the server prices, the harness never does

The harness returns `cost_usd = null` and `cost_source = "unknown"` unconditionally. Dollars
come from the 12.5 machinery that already exists:

1. `pipeline_executor` puts `gpu_node_id` and `gpu_fraction` into `execution_context`
   (section 5.3).
2. `local_executor` (and `runner_protocol.build_execute_step_config` for the remote path)
   stamps `LAZYAF_GPU_NODE_ID` / `LAZYAF_GPU_FRACTION` into non-secret container env -
   already implemented.
3. `run.py` copies both onto the manifest - already implemented.
4. `usage_ingestion.ingest_usage` applies its existing precedence: manifest cost ->
   `cli-reported`; else `gpu_node_id` with a resolvable rate ->
   `usage_pricing.gpu_node_cost_usd(rate, container_seconds, gpu_fraction)` and
   `cost_source = "gpu-node"`; else `unknown` with `cost_usd = None`.

The ONE change to `usage_pricing.py` is a DB-first rate resolution:

```python
async def resolve_node_rate(db, node_id: str | None) -> Decimal | None:
    """The hourly rate for a node, endpoint row FIRST, env table second.

    A ModelEndpoint whose gpu_node_id matches and whose rate_usd_hour is
    non-null wins: the operator who set a rate on the endpoint they created
    should not also have to edit LAZYAF_GPU_NODE_RATES. Falls back to the
    existing pure `node_rate_usd_hour(node_id)` so nodes that are not model
    endpoints (a runpod pod running a script step) keep working unchanged.
    Never raises: a pricing lookup must not 500 a telemetry POST.
    """
```

`node_rate_usd_hour` stays sync and pure and keeps its own tests. `ingest_usage` already has a
session.

**`gpu_fraction = 1.0 / endpoint.max_concurrency`.** This is a judgement call and it is made
deliberately. The node bills by the hour regardless of how many steps share it, so with K
steps concurrent on one GPU, charging each `1.0` multiplies the node's real cost by K. That
would inflate exactly the measurement M14 exists to enable - "expensive planner, K cheap
workers" would be reported as K times more expensive than it is, and the hypothesis would be
rejected by an artefact of the accounting. Dividing by the CONCURRENCY CAP under-attributes
when the node is idle, which is a smaller and stated error. `raw.endpoint_max_concurrency` and
`container_seconds` are both recorded, so any figure here is re-derivable later under a
different model without re-running anything. The write-up discloses it; the UI labels a
`max_concurrency > 1` endpoint's cost as **shared**.

`rate_usd_hour = 0.000000` is legal and means "owned hardware, marginal cash cost" - it
produces `cost_usd = 0.000000` with `cost_source = "gpu-node"`, which is a REAL cost figure
(the honest claim "this cost no cash"), not an absence. `rate_usd_hour = null` produces
`cost_source = "unknown"` and `cost_usd = null`, which is "we do not know". Keeping those two
distinguishable is the whole point of decision 4.

**A harness step's `StepUsage` may never carry `cost_source = "cli-reported"`.** A unit test
pins it. That value is what the board reads as "the provider billed us this amount", and no
self-hosted endpoint can make that claim.

### 5.3 The three lines wave 5 named

In `pipeline_executor._build_local_execution_config` / `_build_remote_execution_config`, for
an `openai-harness` step:

```python
exec_context["gpu_node_id"]  = endpoint.gpu_node_id
exec_context["gpu_fraction"] = 1.0 / max(endpoint.max_concurrency, 1)
exec_step_config["usage_provider"] = "openai-compatible"   # AGENT_USAGE_PROVIDER
```

`AGENT_USAGE_PROVIDER["openai-harness"] = "openai-compatible"` means even a step SIGKILLed
before the wrapper wrote a manifest gets `run.py`'s fallback record attributed to the right
provider and the right node - so an OOM-killed local step still produces a priced row rather
than vanishing from the cost coverage.

### 5.4 Endpoint health from real outcomes

`backend/app/services/model_endpoints/health.py`:

```python
async def record_step_outcome(db, endpoint_id: str, raw_harness: dict) -> None:
    """Fold one step's harness record into the endpoint's health.

    Called from usage_ingestion inside a try/except that logs and swallows:
    the never-fail-a-step rule reaches here too, and a health update is not
    worth a 500 on a telemetry POST.

    - endpoint_http_errors == 0 -> last_success_at = now, consecutive_failures = 0
    - stop_reason == "endpoint"  -> consecutive_failures += 1, last_error set
    - probe_drift on two consecutive steps of an endpoint whose stored
      supports_tools is True -> demote to False, probe_status = "degraded",
      probe_detail.demoted_reason = "tools advertised but never emitted"
    """
```

The demotion is the teeth behind "a probe that lies": an endpoint that passes the probe and
then never actually emits `tool_calls` in real work gets its capability record corrected by
the work itself, visibly, within two steps.

---

## 6. ROUTING AND SCHEDULING

### 6.1 How a step selects an endpoint

`backend/app/services/model_endpoints/resolve.py`:

```python
ENDPOINT_MODEL_PREFIX = "endpoint:"

async def resolve_step_endpoint(db, step_config: dict, step_name: str) -> ModelEndpoint:
    """The ONE resolver. Precedence:

    1. step_config["endpoint"]  -> a name or a uuid (the explicit spelling)
    2. step_config["model"] starting with "endpoint:" -> the name after it
    3. neither -> ValueError. There is NO default endpoint, for the same
       reason there is no default agent: guessing which GPU to bill is not a
       recoverable mistake.

    Raises ValueError naming every enabled endpoint when the name does not
    resolve, when the endpoint is disabled, when probe_status is "unprobed",
    or when consecutive_failures >= 3.
    """
```

Rule 2 is what makes 14.3 cheap. `step_config["model"]` is the field that **all four**
selection surfaces already populate - the card's model picker, the playground's `model`, the
pipeline editor's step form, and `MatrixModelEntry.model`. Spelling a self-hosted model as
`"endpoint:local-4090"` therefore reaches the dispatcher from every one of them with **zero
schema changes anywhere**, and a 12.6.5 matrix mixing API and self-hosted models is:

```json
{"models": [
   {"agent": "claude-code",    "model": "claude-haiku-4-5",        "label": "haiku"},
   {"agent": "openai-harness", "model": "endpoint:local-4090",     "label": "qwen32b-local"},
   {"agent": "openai-harness", "model": "endpoint:runpod-a100",    "label": "qwen72b-pod"}
 ],
 "prompts": [{"prompt_template_id": null, "label": "default"}],
 "repeat": 5}
```

The matrix coordinate keeps the `endpoint:<name>` spelling as the variant identity (which is
what the leaderboard groups and keys history on); `StepUsage.model` records the real model id.
Those are two different questions and they get two different answers on purpose.

Vocabulary edits, all three chokepoints, in one commit:

```python
# pipeline_executor.py
DEFAULT_AGENT_IMAGE["openai-harness"] = "lazyaf-agent-base:dev"   # python + runner-common
AGENT_SECRET_ENV["openai-harness"]    = None                      # resolved per-endpoint
AGENT_USAGE_PROVIDER["openai-harness"] = "openai-compatible"
# agent_run.py  -> propagates to schemas/experiment.AGENT_VOCABULARY automatically
AGENT_BY_RUNNER_TYPE["openai-harness"] = "openai-harness"
```

`agent_secret_environment` gains an `endpoint` parameter and, for `openai-harness`, returns
`{}` when `auth_style == "none"` (the first-class no-auth case: no secret, no
`secret_environment` key, no control-mode requirement beyond the one agent steps already
have) and `{HARNESS_API_KEY_ENV: <resolved value>}` otherwise, raising at dispatch with the
ref named when the backend env var is unset.

### 6.2 `runner-local` and 12.6's `requires:` matching

A `runner-local` endpoint injects one requirement, before `ExecutionRouter.decide` runs:

```python
# pipeline_executor, where the step config is normalized for dispatch
if endpoint is not None and endpoint.reach == "runner-local":
    requires = dict(step_config.get("requires") or {})
    has = list(requires.get("has") or [])
    if endpoint.runner_label not in has:
        has.append(endpoint.runner_label)          # default "endpoint:local-4090"
    requires["has"] = has
    step_config = {**step_config, "requires": requires}
```

Everything downstream is 12.6, untouched: `decide()` sees a `requires:` block and returns
`("remote", "runner-pin", parsed)`; `parse_requirements` normalizes it; the requirements
persist on `StepExecution.runner_requirements`; `Runner.matches_requirements` does subset
containment on `labels["has"]`; the dispatcher CASes an assignment. **No new message type, no
new grammar key, no change to `runner_protocol.py`** - wave 5's cross-agent contracts 1 and 5
survive intact.

The operator's side is one env var on the box that hosts the model:

```
LAZYAF_RUNNER_ID=workshop-1
LAZYAF_RUNNER_LABELS=arch=amd64,has=docker,has=endpoint:local-4090
```

Observability (R1): `StepRun.executor == "remote"`, `StepExecution.runner_id` names the
runner, `StepExecution.runner_requirements` shows the injected label, and the step's first
`[runner]` line names the resolved backend URL. A `runner-local` endpoint with no runner
carrying its label fails at `NO_RUNNER_TIMEOUT = 300` with 12.6's existing message, which
already names the requirements and every connected runner's labels.

The Endpoints UI shows `runners: 0` in red for such an endpoint, which is the same fact
before anyone tries to use it.

### 6.3 `proxy`

`ANY /api/model-endpoints/{id}/proxy/v1/{path:path}`, `verify_step_auth`-authenticated, and
gated so that:

- `endpoint.reach != "proxy"` -> 404. The broker is not a general egress hole.
- the calling step's `step_execution.model_endpoint_id != id` -> 403. Same fence shape as
  12.6's step gate.
- `path` must be in `{"chat/completions", "completions", "models", "embeddings"}` ->
  anything else 404.
- request body capped at `PROXY_MAX_BODY_BYTES = 4 * 1024 * 1024`.
- upstream auth is injected server-side; **no endpoint secret ever reaches the container in
  proxy mode** - the container authenticates with the step JWT it already holds. That is the
  one genuine advantage of the mode and it is worth stating.
- streaming is passed through with `StreamingResponse` and no buffering, so
  `supports_streaming` still means something.
- the endpoint's `max_concurrency` is enforced here too, with an `asyncio.Semaphore` per
  endpoint id; over-limit requests wait up to `PROXY_QUEUE_TIMEOUT = 120s` then return 503
  with `Retry-After`, which the harness's retry policy handles as a normal 5xx.
- dispatch logs `[executor] WARNING: endpoint 'x' uses reach=proxy; inference traffic flows
  through the backend and the backend is a bottleneck for it`. Opt-in, never default, and the
  warning is on every use rather than only in the docs.

### 6.4 Max concurrency: the owner's open question, answered

**Yes. The endpoint carries `max_concurrency`, it defaults to 1, and the scheduler respects
it - through a DB-backed admission gate for `direct` and `proxy`, and through the runner's own
single-step limit for `runner-local`.**

**Why the default is 1.** One ollama process serving four requests on one GPU does not go 4x
faster; it goes roughly 1x with 4x the latency, 4x the KV-cache pressure and a real chance of
an OOM that kills all four. Defaulting to unlimited would make M13's wall-clock and
speedup numbers - the whole reason wall-clock is a co-headline metric - fiction. An operator
with a multi-GPU vLLM raises it deliberately, and the UI explains what it means.

**The gate, for `direct` and `proxy`.**
`backend/app/services/model_endpoints/scheduler.py`:

```python
ENDPOINT_WAIT_TIMEOUT = 900          # 15 min, then fail the step LOUDLY
ENDPOINT_WAIT_POLL     = 5

async def admit(db, step_execution_id: str, endpoint: ModelEndpoint) -> None:
    """Block until this step holds one of the endpoint's slots.

    Admission is a compare-and-swap arbitrated by the DB, exactly like 12.6's
    runner assignment - one transaction, and rowcount is the only acceptable
    contention detector:

        SELECT ... FROM model_endpoints WHERE id = :eid FOR UPDATE   -- the mutex
        SELECT count(*) FROM step_executions
         WHERE model_endpoint_id = :eid
           AND status IN ('assigned','preparing','running','completing')
        -- if count < max_concurrency:
        UPDATE step_executions SET model_endpoint_id = :eid
         WHERE id = :sid AND model_endpoint_id IS NULL

    SQLite has no FOR UPDATE and serializes writers, which is sufficient for
    the single-process backend LazyAF runs today; the statement is written so
    that Postgres gets a real row lock the day it is used. The invariant is
    the same either way: in-flight count is READ FROM THE DATABASE, never
    from an in-memory counter that a restart loses.
    """
```

- Waiting steps sit in `pending` with a **visible** reason. `_consume_local_events` emits
  `[executor] waiting for endpoint local-4090 (2 of 2 slots busy, position 3)` once at entry
  and every 30s thereafter, so a fan-out that is serializing looks like a queue rather than a
  hang. R1: silent waiting and hanging are indistinguishable.
- Slots are released when the step reaches a terminal `StepExecution.status`, which the
  existing terminal-write path already does; a `asyncio.Condition` per endpoint id is notified
  there so the next step wakes immediately instead of polling.
- `ENDPOINT_WAIT_TIMEOUT` then a step failure naming the endpoint, the cap, and the ids of the
  steps holding the slots - the same "a pin nobody can satisfy must not hang a pipeline
  forever" rule as 12.6's `NO_RUNNER_TIMEOUT`.
- The startup sweep clears `model_endpoint_id` on any `step_executions` row that is already
  terminal, so a crash cannot leak slots permanently.

**`runner-local` skips the endpoint gate entirely.** Two gates that can block each other
(runner availability and endpoint slots) is a deadlock waiting to be discovered in production,
and it buys nothing: `MAX_CONCURRENT_STEPS = 1` per runner agent (wave 5 section 10) already
means one step per agent process. An operator who wants K concurrent workers against a local
GPU runs K runner-agent processes on that box - each with its own `LAZYAF_RUNNER_ID`, each
visible in the runner panel, each individually killable. The effective concurrency is
therefore `count(runners carrying the label)`, which is a number the operator can see and
change. `max_concurrency` on a `runner-local` endpoint is **advisory**: the Endpoints page
shows `runners: 3 / max_concurrency: 2` in amber with "more runners carry this endpoint's
label than it declares capacity for".

**Interaction with M13's fan-out.** K workers on one endpoint with `max_concurrency = 2`
produce a real, measured, honest serialization: two run, K-2 queue visibly, wall-clock reflects
it, and `speedup = serial_equivalent / wall_clock` reports approximately 2 rather than K. That
is the number the write-up needs. 12.6.5's dry-run estimator gains one line to say so:
`"variant qwen32b-local: 8 cells against endpoint local-4090 (max_concurrency 2) will
serialize into approximately 4 waves"`.

---

## 7. THE UI (14.3)

### 7.1 The Model Endpoints page

Route `/endpoints`, `frontend/src/lib/pages/EndpointsPage.svelte`, with
`frontend/src/lib/stores/endpoints.ts` (snapshot fetch on mount + `model_endpoint_status` WS
deltas - the 12.6 runner-store pattern, not polling) and
`frontend/src/lib/components/EndpointModal.svelte`.

The table, one row per endpoint:

| Column | Content |
|---|---|
| Name | `local-4090`, with `server_kind` and `model` beneath |
| Reach | pill: `direct` / `runner-local` (+ `runners: N`, red at 0) / `proxy` (amber: "inference flows through the backend") |
| Health | dot + text from the derived `health`: healthy / stale / degraded / unhealthy / unprobed, with `last_seen` relative time |
| Capabilities | three checks and a number: `tools`, `stream`, `usage`, `ctx 32k`. A false `tools` reads "no tools - fallback protocol"; a null `context_window` reads "ctx unknown (assumes 8k)" |
| Cost basis | `$1.89/hr` / `$0.00/hr (owned)` / **`unpriced`** in amber |
| Concurrency | `1 / 2 busy` live from the in-flight query |
| Enabled | toggle |

Row actions: **Probe now** (spinner, then the record updates in place - for `runner-local` it
shows "probing on runner workshop-1..." and links to the probe pipeline run), Edit, Disable,
Delete (disabled with a tooltip while in-flight > 0).

The create/edit form: name, base_url, model, server_kind, reach, auth style (a three-way
radio with **None selected by default**), secret ref (a NAME field with the
`LAZYAF_ENDPOINT_` prefix rendered as a fixed affix, and a red "not set in the backend
environment" hint driven by `secret_present`), rate $/hr, max concurrency, context window
override, request timeout. **No field in this form ever holds a secret value**, and the form
says so.

### 7.2 Every place an endpoint must become selectable

All four are one-liners because of the `model: "endpoint:<name>"` sugar (section 6.1).

| Surface | Change |
|---|---|
| **Agent step config** (`PipelineEditorPage.svelte` step form) | the agent dropdown gains `openai-harness`; choosing it swaps the model dropdown for an endpoint dropdown fed by `stores/endpoints`, and reveals a collapsed "harness budgets" section (iterations, token budget, mode: auto/tools/text). Emits `{agent: "openai-harness", endpoint: "<name>", harness: {...}}` |
| **Card creation** (`CardModal.svelte`) | the runner-type selector gains `openai-harness`; the model selector merges endpoints into a **"Self-hosted"** optgroup below the Anthropic and Google groups, emitting `endpoint:<name>` as the value. `stores/models.ts` gains the merge, so both the card and the playground pick it up from one place |
| **Playground** (`PlaygroundPage.svelte`) | the same merged selector; `playground.runner_type = "openai-harness"` + `model = "endpoint:<name>"` flow through `agent_run.resolve_agent` unchanged |
| **Experiment matrix** (`components/experiments/*`) | the model-axis editor gains an "Add self-hosted model" row producing `{agent: "openai-harness", model: "endpoint:<name>", label: <name>}`. **This is what lets one matrix mix API and self-hosted models in one run**, and it needs no backend schema change at all |

Plus one addition to the results board, which is decision 4's UI half:

- Each variant row gains a **cost-basis pill**: `billed` (every row `cli-reported`),
  `node-priced` (every row `gpu-node`), `mixed`, or **`unpriced`** (any row `unknown`), driven
  by the `by_source` counts the 12.5 rollup already returns.
- A comparison containing an `unpriced` variant renders its cost column struck through with
  "no cost data" rather than a number, and the existing overlapping-interval warning gains a
  sibling: `"this comparison mixes billed and node-priced costs; see METHOD"`.

R8 deliverables in the same phase: `frontend/e2e/endpoints.spec.ts` (register a no-auth
endpoint against the mock server, probe it, watch capabilities populate from the WS delta,
then open the experiment matrix editor and select it) and vitest for `stores/endpoints.ts`
(snapshot insert, delta update, delta for an unknown id is an insert, `enabled: false` keeps
the row but greys it).

---

## 8. THE DOGFOOD RATCHET AND 14.4

### 8.1 CI must not need a GPU

`tdd/support/mock_openai_server.py` - a stdlib `http.server` OpenAI-compatible endpoint,
started as a fixture in T1/T2 and as a compose service `mock-endpoint` on `lazyaf-network` for
the dogfood lane. It implements `/v1/models`, `/v1/chat/completions` (tools and no-tools,
streaming and not), returns real `usage` blocks, and is driven by a scenario name so responses
are deterministic:

| Scenario | Behavior | What it covers |
|---|---|---|
| `happy_tools` | list -> read -> write -> finish(success) | the tools path end to end |
| `happy_text` | the same four actions as ```lazyaf blocks | the fallback protocol |
| `never_finishes` | echoes `read_file` forever | stop condition 2 |
| `malformed` | prose, prose, prose, then a valid block | the malformed retry counter AND its reset |
| `malformed_forever` | prose only | stop condition 7 and exit 5 |
| `no_usage` | omits the `usage` block | the null-tokens path and the scrape marker |
| `lying_tools` | advertises tools at probe, emits a `lazyaf` block in `content` | the probe-drift bridge and the demotion |
| `slow` | 3s per turn | the soft deadline |
| `flaky_5xx` | two 503s then success | the retry policy |

### 8.2 What `.lazyaf/pipelines/test-suite.yaml` gains (R7)

Two agent steps, both against the mock endpoint, both zero cost and zero GPU, both on
**every push**:

```yaml
  # 14.2 harness coverage (R7): the REAL harness through the REAL control
  # runtime against a REAL OpenAI-compatible server. If the loop, the tool
  # sandbox, the token accumulator, the gpu-node pricing path or the endpoint
  # registry break, this step FAILS THE PUSH.
  - id: "harness-probe"
    name: "14.2 agent harness (tools mode, mock endpoint)"
    type: agent
    config:
      agent: openai-harness
      endpoint: "dogfood-mock"
      commit: false
      task: "Create .lazyaf-dogfood/harness-ran naming the endpoint you used"
      harness: {max_iterations: 8, time_budget_seconds: 120}
    on_success: next
    on_failure: stop
    timeout: 300

  # The fallback protocol is the part most likely to rot, because nothing
  # exercises it once a tool-capable model is plugged in. It runs every push.
  - id: "harness-probe-notools"
    name: "14.2 agent harness (no-tools fallback, mock endpoint)"
    type: agent
    config:
      agent: openai-harness
      endpoint: "dogfood-mock-notools"
      commit: false
      task: "Create .lazyaf-dogfood/harness-fallback-ran"
      harness: {mode: text, max_iterations: 8, time_budget_seconds: 120}
    on_success: next
    on_failure: stop
    timeout: 300
```

The two dogfood endpoints are seeded by the test-mode reset endpoint.
`dogfood-mock` is seeded with **`rate_usd_hour: "0.01"`** on purpose: it puts the `gpu-node`
pricing branch on the dogfood lane, closing 12.5's stated gap that the branch "is reached only
by API tests with a hand-built manifest".

### 8.3 `scripts/verify_executor.py` assertions 13-18

Stdlib-only, still `lazyaf-base:dev` with `control: false`. Existing numbering preserved.

13. The `harness-probe` StepRun has `executor == "local"` and a `StepUsage` row with
    `provider == "openai-compatible"` and non-null `input_tokens`/`output_tokens` **greater
    than the largest single turn's tokens** - i.e. the accumulator actually summed across
    turns rather than recording the last response.
14. That row has `cost_source == "gpu-node"` and a non-null `cost_usd`.
15. `SCRAPE_FAILED_LOG_MARKER` appears in **no** step's logs for this run.
16. The `harness-probe-notools` StepRun succeeded and its usage `raw.harness.mode == "text"`
    with `malformed_responses` present (0 or more, but the key exists - proving the fallback
    path recorded itself).
17. `GET /api/model-endpoints` reports `dogfood-mock` with `probe_status == "ok"` and
    `probe_age_seconds < PROBE_TTL_SECONDS`.
18. No `StepUsage` row in this run carries `cost_source == "cli-reported"` together with
    `provider == "openai-compatible"` - the invariant from section 5.2, asserted on the lane.

Each gets a unit test in `tdd/unit/scripts/test_verify_executor.py` **including its negative
case** (a run whose usage equals one turn's tokens must FAIL 13; a run with an unpriced
endpoint must FAIL 14; and so on).

`tdd/tier_floors.json` rises for T1, T2 and T3 - **re-measured after the wave, not guessed**,
with the reason written into the `note` field as every prior raise did.

### 8.4 14.4: proving it on real hardware (manual, owner-run)

The loopback lane cannot prove a NAT'd home GPU works; only a real one can. The bring-up
checklist, in order, each step producing a visible artifact:

1. On the box hosting ollama: `docker run ... lazyaf/runner-agent` with
   `LAZYAF_RUNNER_ID=workshop-1`, `LAZYAF_RUNNER_LABELS=has=docker,has=endpoint:local-4090`,
   `LAZYAF_BACKEND_URL=wss://<backend>`, `LAZYAF_RUNNER_TOKEN=...`.
   **Artifact:** the runner appears `idle` in the runner panel.
2. Register the endpoint: `reach=runner-local`, `base_url=http://172.17.0.1:11434/v1`,
   `auth_style=none`, `rate_usd_hour=0.00`, `max_concurrency=1`.
   **Artifact:** `POST .../probe` schedules a probe run pinned to `workshop-1`; the capability
   record populates with a real `context_window`.
3. Run one card through it end to end (`agent: openai-harness`, `endpoint: local-4090`).
   **Artifact:** a branch pushed to the internal git server, a `StepUsage` row with
   `provider == "openai-compatible"`, real tokens, `cost_source == "gpu-node"`,
   `cost_usd == 0.000000`.
4. The M13 headline run: a 12.6.5 matrix with a high-end API planner role and K=4
   `openai-harness` workers on the local endpoint, `repeat >= 5`.
   **Artifact:** `cost_by_role` on the board with real dollars in the planner row and
   node-priced dollars in the worker rows - the number the "expensive planner, cheap workers"
   hypothesis has been waiting for since 2026-08-29.

Failures at step 1 or 2 are network/label problems and are diagnosable from the runner panel
and the probe run's logs without shell access to the operator's box. That is the design goal
of putting the probe on the runner.

---

## 9. RISK REGISTER

### 9.1 The six the owner named

| Risk | The failure it produces | Mitigation, and where it is tested |
|---|---|---|
| **Model output that never terminates** | An agent loop with no CLI to bound it runs until the container watchdog SIGKILLs it at 1800s, leaving no commit, no usage row and a step that cost 30 minutes of GPU to learn nothing | **Ten enumerated stop conditions** (section 3.2), of which four are budgets and three are loop-detectors. The load-bearing one is the SOFT deadline at `timeout - 60s`: the harness stops itself INSIDE the watchdog's hard deadline so it can still commit and write telemetry. `MAX_CONSECUTIVE_TOOL_ERRORS = 5` catches the specific small-model pathology of retrying an identical failing `apply_patch` forever. T1: the `never_finishes` and `slow` mock scenarios assert exit 3 and a usage row with real tokens; T2 asserts the container exits before the watchdog fires |
| **Context-window overflow on small models** | Turn 12 returns an opaque 400 and the step dies having spent everything, or worse the server silently truncates the transcript and the model starts answering a question it can no longer see | Budget-and-elide with a **live** chars-per-token correction from the server's own `prompt_tokens` (section 3.7). Never summarize. Elisions are logged with before/after estimates. A prompt that cannot fit at turn 0 fails at turn 0 with exit 6, before any spend. A null `context_window` assumes 8192 and SAYS SO. T1: a transcript driven past a 4096-token window keeps the system message and the task message, drops the middle, and the elision marker appears |
| **A probe that lies** | The endpoint passes the tool probe, then never emits `tool_calls` in real work; every step burns its whole budget on prose and fails, and the capability record keeps saying it is fine | Three layers. (1) The probe checks BEHAVIOR (`tool_calls[0].function.name == "probe"`), not the server's acceptance of the `tools` parameter, and uses `tool_choice: "auto"` rather than trusting `required`. (2) The runtime bridge: a tools-mode turn with no `tool_calls` is re-parsed with the FALLBACK parser before being treated as prose, so a server that emits calls in `content` still works, with `probe_drift` recorded. (3) The demotion: `record_step_outcome` flips `supports_tools` to False and `probe_status` to `degraded` after two consecutive drifting steps. T1: the `lying_tools` scenario asserts the step still succeeds AND the endpoint is demoted |
| **Endpoint auth leaking into logs** | A 401 body echoes the key; the probe persists it in `probe_detail`; `GET /api/model-endpoints` serves it to anyone on the LAN | The value is never in the database at all - only an env-var NAME, prefix-allowlisted to `LAZYAF_ENDPOINT_*` so a row cannot reference `ANTHROPIC_API_KEY` or `LAZYAF_STEP_AUTH_SECRET`. The value travels only through 12.5's `secret_environment` (config FILE, 0600, consume-once, never `docker inspect`, never `container.environment` on the remote path). `scrub_secrets` runs over every persisted or logged upstream string. `proxy` mode never sends the key to the container at all. Pinned by a sentinel test over every harness log line and every `log` WS frame (12.6's `test_secret_hygiene.py` shape), by a T2 test grepping the created container's `inspect` output, and by an API test asserting no response body contains the value |
| **`runner-local` endpoints that vanish** | The GPU box is rebooted mid-fan-out; K steps hang forever waiting for a runner that is not coming back | 12.6 already handles it and this milestone adds nothing but visibility: the socket closes, `on_runner_disconnect` requeues, the dispatcher re-matches, and an unmatchable step fails at `NO_RUNNER_TIMEOUT = 300` with a message naming the requirements and every connected runner's labels. New: the Endpoints page shows `runners: 0` in red for a `runner-local` endpoint whose label nobody carries - the fact is visible BEFORE a step is dispatched. A probe of such an endpoint fails with "no runner carries label endpoint:X", which is the true reason. T2: `test_loopback_failover.py`'s sibling kills the labelled agent mid-harness-step and asserts the step is requeued and re-runs |
| **Cost figures that are estimates mistaken for bills** | A board compares an Anthropic invoice against a made-up local number and someone quotes the ratio in public | Four instruments. (1) `cost_source` is a first-class column and a harness step may NEVER carry `cli-reported` - unit-pinned. (2) `rate_usd_hour = null` produces `cost_usd = null` and `cost_source = "unknown"`, never a guessed rate; `0.00` is a DIFFERENT, meaningful value ("owned hardware, marginal cash cost") and the two stay distinguishable. (3) The board's cost-basis pill (`billed` / `node-priced` / `mixed` / `unpriced`) and the struck-through cost column for unpriced variants (section 7.2). (4) `gpu_fraction`, `container_seconds` and `raw.endpoint_max_concurrency` are all recorded, so any node-priced figure is re-derivable under a different cost model without re-running a trial - which is what makes the published bundle honest rather than merely confident |

### 9.2 Seams inherited from 12.3/12.5/12.6, now with a model endpoint attached

| Seam | Status | What pins it |
|---|---|---|
| **uid ownership** | Unchanged. The harness runs as uid 1000 under the wrapper's existing root check, and it writes only inside `/workspace/repo` (already chowned) and `$HOME/.lazyaf` (already on the workspace volume) | Existing entrypoint + the wrapper's `_running_as_root` refusal. T2 asserts written files land 1000-owned |
| **network reachability** | The BIGGEST new exposure and a genuinely new hop: step container -> model endpoint, which for `direct` may be a LAN IP the backend can reach and the container cannot | The harness logs the resolved `base_url` on its FIRST line of every step, before any request. A connect failure produces exit 4 with the URL and the OS error in the step's terminal status, so one grep answers "why can't the step reach the model". The probe records `probed_from`, so "the backend could reach it" is never silently read as "the step can" |
| **timeouts under DooD** | Amplified: a cold ollama loading a 32B model can take 60s on the FIRST request. `request_timeout_seconds` defaults to 300 for exactly that reason | ONE killer (the container watchdog); the harness's soft deadline sits inside it; per-request timeout is separate and endpoint-configurable. A first-request timeout is retried under `MAX_ENDPOINT_RETRIES`, so a cold model costs a retry rather than a step |
| **secret leakage over the wire (12.6)** | Unchanged shape, one new secret: the endpoint key rides inside `execute_step.config.control_files` for `runner-local` | `wss://` required off-loopback (`LAZYAF_RUNNER_ALLOW_INSECURE` to opt out, default off); the agent logs `sorted(config.keys())` only. The step JWT's TTL still expires with the step |
| **single-worker backend** | The endpoint admission gate reads its count from the DATABASE, not from an in-memory counter, so it is already multi-process-correct in a way `RunnerRegistry` is not | Stated, and the `FOR UPDATE` is written for the day Postgres arrives |
| **`raw` cap** | The harness record is the largest `raw` payload any executor produces | `usage._cap_raw` (8 KiB, existing) applies unchanged; `tool_calls` is a count map, not a list, precisely so it cannot grow with the transcript |

---

## 10. WAVE SPLIT - 5 agents, disjoint file ownership

Launch order: **A first and alone** (its model, schemas and migration are imported by B's
contract module, C's resolver and E's types). Then B, C and D in parallel. E starts on the
store and page skeleton immediately and lands the selection surfaces once C's vocabulary edit
is in.

### Agent A - the endpoint entity, migration, probe, API

**Owns exclusively:** `backend/app/models/model_endpoint.py` (new),
`backend/app/models/__init__.py` (export only),
`backend/app/schemas/model_endpoint.py` (new),
`backend/alembic/versions/0011_model_endpoints.py` (new),
`backend/app/services/model_endpoints/{__init__,probe,secrets,health,resolve}.py` (new),
`backend/app/routers/model_endpoints.py` (new),
`backend/app/main.py` (the `include_router` line only),
`backend/app/services/websocket.py` (the `publish_model_endpoint_status` helper only),
`tdd/unit/models/test_model_endpoint.py`, `tdd/unit/services/test_endpoint_probe.py`,
`tdd/integration/api/test_model_endpoints_api.py`.

**Test contract:**
1. Probe decision table against a stub server, one test per row of section 2.2: tools ok;
   tools 400; tools 200 with no `tool_calls`; tools 200 with the wrong tool name; arguments
   that are not JSON; streaming ok; streaming unsupported; `usage` absent; ollama
   `/api/show` context discovery; vLLM `max_model_len` discovery; and the operator override
   beating both.
2. Unreachable: `probe_status == "unreachable"`, `consecutive_failures` incremented,
   **the previous capability booleans unchanged**, and the API returns **200**.
3. Rate limiting: two probes inside `PROBE_MIN_INTERVAL_SECONDS` produce one upstream call
   and the second response carries `cached: true`.
4. Secret refs: `ANTHROPIC_API_KEY` and `LAZYAF_STEP_AUTH_SECRET` are 422 at create;
   a valid ref that resolves to nothing yields `secret_present: false` and is NOT an error at
   create; no response body, log record or `probe_detail` ever contains the value (a sentinel
   value planted in the env and grepped for across all of them).
5. `scrub_secrets` removes the known value, `Bearer <x>`, and `sk-...` shapes.
6. `PATCH` of `base_url` / `model` / `server_kind` / `auth_*` resets the capability record to
   `unprobed`; `PATCH` of `description` / `rate_usd_hour` does not.
7. `DELETE` is 409 while an in-flight `step_executions` row references the endpoint.
8. Migration: `0011` upgrades from `0010`, is re-runnable, and `tdd/integration/test_migrations.py`
   parity is updated in the same commit.
9. `health` derivation table: unprobed / ok+fresh / ok+stale / degraded /
   unreachable+failures.

### Agent B - the harness

**Owns exclusively:** `runner-common/runner_common/harness/**` (new),
`runner-common/runner_common/endpoint_probe.py` (new),
`runner-common/runner_common/agent_config.py` (the `endpoint` / `harness` fields only),
`runner-common/runner_common/agent_wrapper.py` (the one `EXECUTORS` entry only),
`runner-common/runner_common/usage.py` (`PROVIDER_BY_AGENT` entry + the accumulator helper),
`runner-common/tests/test_harness_*.py`, `runner-common/tests/fixtures/openai/**`.
Imports nothing from `backend/app`.

**Test contract:**
1. `test_harness_loop.py` - one test per stop condition in the section 3.2 table, each
   asserting the exit code AND that a usage record was produced with the tokens actually
   spent. The `finish`-with-no-change case asserts **failure**.
2. `test_harness_tools.py` - path escape (`../../etc/passwd`, an absolute path, a symlink out
   of the workspace) is a tool ERROR not an exception; `/workspace/.control` is denied;
   `apply_patch` with 0 occurrences reports the nearest line; `run_shell` non-zero exit is a
   RESULT; the `git push` denylist fires and its message names the platform; output over
   `TOOL_OUTPUT_MAX_BYTES` is head+tail elided.
3. `test_harness_fallback.py` - the parser table: zero blocks, two blocks (first wins plus a
   warning), trailing prose inside the fence (repaired), unknown tool, missing arg, wrong arg
   type. Three consecutive malformed responses recover when the fourth parses; **four
   consecutive** produce exit 5 with the reason and the last raw response in the log. A
   malformed turn counts against `max_iterations`.
4. `test_harness_usage.py` - tokens sum across turns (the fixture's per-turn numbers are all
   distinct so a last-response-wins bug cannot pass); a mix of reporting and non-reporting
   turns yields the partial sum plus `turns_without_usage`; **no** reporting turn yields null
   tokens plus `SCRAPE_FAILED_LOG_MARKER`; `cost_usd` is always null and `cost_source` is
   always `"unknown"`; `determinism` carries temperature/seed/top_p.
5. `test_harness_context.py` - live chars-per-token correction after turn 1; elision keeps
   system + task + last 6; a turn-0 overflow exits 6 with zero upstream requests made.
6. `test_harness_secrets.py` - a sentinel API key appears in no emitted log line, no
   transcript line, and not in the `run_shell` child's environment.
7. `test_harness_no_git.py` - `HarnessExecutor` neither imports `git_helpers` nor invokes
   `git` (AST parse plus a subprocess spy), proving commit ownership stayed with the wrapper.
8. `test_dataclass_stability.py` - `ExecutorConfig` and `ExecutorResult` field sets are
   **unchanged** from 12.5 (contract #4 of that wave, still true after this one).
9. `test_agent_config_endpoint.py` - absent block -> None; present-but-not-an-object ->
   refusal; `openai-harness` with no block / no `base_url` / no `model` -> refusal;
   `harness_mode` resolution table including the `auto` + `supports_tools is None` refusal.

### Agent C - dispatch, routing, scheduling, the proxy

**Owns exclusively:** `backend/app/services/control_layer/workspace.py`
(the `endpoint`/`harness` producer args + `validate_endpoint_block` + `agent_config_keys`),
`backend/app/services/pipeline_executor.py` (vocabulary entries, `agent_secret_environment`'s
endpoint parameter, `_attach_agent_payload`'s endpoint/harness blocks, the `requires:`
injection, the `gpu_node_id`/`gpu_fraction` context, the admission-gate call site),
`backend/app/services/agent_run.py` (`AGENT_BY_RUNNER_TYPE` entry only),
`backend/app/services/model_endpoints/scheduler.py` (new),
`backend/app/services/usage_pricing.py` (`resolve_node_rate` only),
`backend/app/services/usage_ingestion.py` (the `record_step_outcome` call only),
the proxy route in `backend/app/routers/model_endpoints.py` (coordinate with A: A owns the
module and lands the CRUD first; C appends the proxy and `probe-result` handlers),
`tdd/unit/services/test_harness_step_dispatch.py`,
`tdd/unit/services/test_endpoint_scheduler.py`,
`tdd/unit/control_runtime/{endpoint_contract.py,test_endpoint_config_contract.py}`,
`tdd/integration/api/test_endpoint_proxy.py`.

**Test contract:**
1. `resolve_step_endpoint` precedence table, plus each refusal (unknown name naming the known
   ones; disabled; unprobed; `consecutive_failures >= 3`).
2. The producer/consumer round trip of section 4.3, all seven assertions.
3. Secret containment: the rendered agent config contains only `auth_env`; the endpoint key
   appears only in `secret_environment`; `auth_style: "none"` produces NO
   `secret_environment` entry at all and the step still dispatches (the first-class no-auth
   case); a missing backend env var fails at dispatch naming the ref and not the value.
4. `runner-local` injection: `requires.has` gains the label, an operator's existing
   `requires:` is merged not replaced, `ExecutionRouter.decide` returns
   `("remote", "runner-pin", ...)`, and `StepRun.executor == "remote"` is persisted. A
   `direct` endpoint with no operator `requires:` stays LOCAL - a global accidental flip to
   remote is as much a regression as the reverse.
5. Admission CAS: two concurrent `admit()` calls against a `max_concurrency=1` endpoint on a
   real session - exactly one is admitted, the loser waits; the slot is released on terminal
   status and the waiter wakes; `ENDPOINT_WAIT_TIMEOUT` fails the step with a message naming
   the endpoint, the cap and the holding step ids; a `runner-local` endpoint **skips** the
   gate entirely.
6. `gpu_node_id` / `gpu_fraction` reach `execution_context` and therefore container env;
   `resolve_node_rate` prefers the endpoint row over `settings.gpu_node_rates` and falls back
   when the endpoint has no rate; a `null` rate yields `unknown`, a `0.00` rate yields
   `gpu-node` with `0.000000`.
7. Proxy: non-proxy endpoint 404; wrong step 403; disallowed path 404; oversized body 413;
   streaming passes through unbuffered; the upstream key is injected server-side and appears
   in no response header; over-concurrency waits then 503 with `Retry-After`.
8. **A harness `StepUsage` row can never have `cost_source == "cli-reported"`** - asserted
   through `ingest_usage` with a manifest that tries.

### Agent D - the mock endpoint, integration, the ratchet

**Owns exclusively:** `tdd/support/mock_openai_server.py` (new),
`docker-compose.yml` (the `mock-endpoint` service only),
`backend/app/routers/test_api.py` (the endpoint seeding + the `model_endpoints` resettable),
`.lazyaf/pipelines/test-suite.yaml`, `scripts/verify_executor.py`,
`tdd/tier_floors.json`, `tdd/unit/scripts/test_verify_executor.py`,
`tdd/integration/services/test_harness_step_container.py` (new, T2),
`tdd/integration/execution/test_runner_local_endpoint.py` (new, T2),
`tdd/e2e/test_harness_card_loop.py` (new, T3).

**Test contract:**
1. The mock server serves every scenario of section 8.1 and its own unit test pins each
   response shape against the OpenAI schema the harness parses.
2. T2 full round trip on a **named volume** with `lazyaf-agent-base:dev`: the harness step
   runs in a real container, logs arrive via `POST /api/steps/{id}/logs`, files land in the
   repo, a `StepUsage` row appears with `provider == "openai-compatible"`, summed tokens and
   `cost_source == "gpu-node"`, and the created container's `inspect` output contains the
   endpoint key nowhere.
3. T2 `runner-local`: a loopback runner agent labelled `has=endpoint:t2-mock`, an endpoint
   with `reach=runner-local`, a probe run that lands a real capability record with
   `probed_from == "runner:<id>"`, then a harness step that executes on that runner with
   `StepRun.executor == "remote"`.
4. T3 US-2-shaped: card -> harness agent -> branch pushed -> `card.status == "in_review"`,
   against the mock endpoint, zero cost.
5. `verify_executor` assertions 13-18 with the **negative case for each**.
6. Tier floors re-measured (not guessed) with the reason in `note`.

### Agent E - the frontend

**Owns exclusively:** `frontend/src/lib/pages/EndpointsPage.svelte` (new),
`frontend/src/lib/components/EndpointModal.svelte` (new),
`frontend/src/lib/stores/endpoints.ts` (new), `frontend/src/lib/stores/models.ts`
(the self-hosted merge), `frontend/src/lib/stores/websocket.ts`
(`model_endpoint_status` in `ServerMessageType` + `HANDLED_MESSAGE_TYPES` + the switch),
`frontend/src/App.svelte` (the `/endpoints` route + nav item),
`frontend/src/lib/api/{client.ts,types.ts}` (endpoint surface only),
the agent/model selectors in `PipelineEditorPage.svelte`, `CardModal.svelte`,
`PlaygroundPage.svelte` and `components/experiments/*`,
the cost-basis pill in the results board,
`frontend/e2e/endpoints.spec.ts` (new), `frontend/src/lib/stores/endpoints.test.ts` (new).

**Test contract:**
1. Vitest: snapshot populates the store; a `model_endpoint_status` delta for a known id
   updates in place, for an unknown id inserts; `enabled: false` keeps the row and marks it;
   the derived health label matches the backend's for all five states (a shared fixture, so
   the two definitions cannot drift).
2. Vitest: `models.ts` merges endpoints into a `Self-hosted` group and emits
   `endpoint:<name>` values; an endpoint with `probe_status == "unprobed"` is rendered
   disabled with "probe required".
3. Playwright (R8, workers=1, real tier): register a no-auth endpoint against the mock
   server, click Probe, assert the capability row populates **from the WS delta** without a
   reload; reload mid-probe and assert the page is populated from the snapshot; then open the
   experiment matrix editor and assert the endpoint is selectable on the model axis.
4. Playwright: an unpriced variant's cost column renders struck through with "no cost data",
   and a mixed-basis comparison shows the METHOD warning.
5. The drift guard (`websocket.test.ts`) stays green - the new frame type is present on both
   sides in the same commit.

---

## 11. CROSS-AGENT CONTRACTS (pin these first; they are the only shared surfaces)

1. **The `ModelEndpoint` row and its `ModelEndpointRead` schema.** Owner A. B never imports
   it (the container gets a dict); C, D and E consume it. Nobody but A adds a column, and a
   column addition requires a migration in the same PR.
2. **The `endpoint` and `harness` blocks of the agent config**, section 4.1, key-for-key.
   Producer C (`generate_agent_config`), consumer B (`agent_config.py`), pinned by the shared
   module `tdd/unit/control_runtime/endpoint_contract.py`. **The shared module is owned by C
   and imported by B's tests**; nobody adds a key without editing it.
3. **`HARNESS_API_KEY_ENV = "LAZYAF_ENDPOINT_API_KEY"`** is the ONE container-side variable
   name. Defined in A's `secrets.py`, written by C into `secret_environment`, read by B in
   `harness/client.py`. It is never spelled as a literal anywhere else.
4. **`ENDPOINT_MODEL_PREFIX = "endpoint:"`** is the ONE sugar spelling. Defined by A in
   `resolve.py`, parsed only by `resolve_step_endpoint` (C), produced by E's four selectors
   and by D's dogfood YAML. No second parser.
5. **The agent vocabulary is `openai-harness`**, added to `DEFAULT_AGENT_IMAGE`,
   `AGENT_SECRET_ENV`, `AGENT_USAGE_PROVIDER` (C), `AGENT_BY_RUNNER_TYPE` (C, which
   propagates to `AGENT_VOCABULARY` automatically) and `agent_wrapper.EXECUTORS` (B). Five
   sites, one commit, one test asserting they agree.
6. **`ExecutorConfig` and `ExecutorResult` gain nothing.** 12.5's contract #4 stands. The
   harness takes its configuration through the `EXECUTORS` builder, exactly as
   `ClaudeExecutor(output_format=...)` and `MockExecutor(mock_config=...)` already do. Owner
   B, asserted by `test_dataclass_stability.py`.
7. **`usage_pricing.resolve_node_rate(db, node_id)`** is the ONE rate resolver, endpoint row
   first and `settings.gpu_node_rates` second. Owner C; `node_rate_usd_hour` stays pure and
   keeps its own tests. `gpu_fraction = 1.0 / max_concurrency` is computed in exactly one
   place (C's dispatch) and travels on the wire.
8. **`requires:` injection for `runner-local` happens before `ExecutionRouter.decide`, and
   nothing else in 12.6 changes.** No new message type, no new requirement key, no edit to
   `runner_protocol.py`. Owner C; wave 5's contracts 1 and 5 are explicitly preserved.
9. **The endpoint admission gate reads its in-flight count from
   `step_executions.model_endpoint_id`, never from memory**, and `rowcount != 1` is the only
   acceptable contention detector. Owner C; A provides the column and the index.
10. **`model_endpoint_status` is a new WS frame type** and lands on both sides in one commit:
    `websocket.py` publisher (A) and `stores/websocket.ts` `ServerMessageType` +
    `HANDLED_MESSAGE_TYPES` + switch (E). The drift guard in `websocket.test.ts` is the
    enforcement.

---

## 12. Seams left open on purpose

- **The harness transcript is not a first-class artifact.** `harness.debug_transcript` writes
  JSONL to `$HOME/.lazyaf/harness/` on the workspace volume and is off by default. Making it a
  real artifact means a fifth control-layer channel (`POST /api/steps/{id}/artifacts`), which
  is a protocol addition, not a flag. M13's per-iteration forensics is where that earns its
  place.
- **Embeddings and non-chat endpoints.** The `ModelEndpoint` shape and the proxy's path
  allowlist already accommodate `/embeddings`; nothing uses it. A retrieval step is a
  different milestone.
- **Per-token pricing for self-hosted endpoints.** Deliberately absent, for the same reason
  12.5 refused a token-price table: a second pricing source drifts. `cost_source="estimated"`
  remains in the vocabulary and remains written by nothing.
- **True occupancy-based `gpu_fraction`.** `1/max_concurrency` is an upper-bound convention
  (section 5.2). `reach="proxy"` is the mode where the backend actually knows the concurrent
  request count and could report real occupancy; deferred until a node bills enough for the
  difference to matter, and `container_seconds` + `max_concurrency` are both recorded so it
  can be recomputed retroactively.
- **Multi-endpoint routing / failover.** A step names one endpoint. "Try local, fall back to
  the API" is a STRATEGY (M13's independent variable), expressible as a graph with a
  conditional edge, and it does not belong in the resolver where it would be an invisible
  policy.
- **`MAX_CONCURRENT_STEPS = 1` on the runner agent** still stands (wave 5 section 10), which
  is why `runner-local` concurrency is "run K agents". Raising it is a per-assignment state
  machine, a real design change.
- **The harness does not do subagents.** `agents_json` is null for `openai-harness` and the
  producer refuses a non-null value. Multi-agent shapes belong in the graph, where they are
  visible and costed per role, not inside one step's loop where they are not.
- **Vision, audio, and structured-output modes** of the OpenAI-compatible API are out of
  scope; `server_kind` exists so a future capability probe can branch on them without a
  schema change.
- **A known-good model matrix in the docs** (the owner's first open question) is deliberately
  NOT shipped as a static table. The probe is the answer: it reports what THIS server with
  THIS chat template actually does, which a table cannot, and the Endpoints page is the
  matrix. `docs/` gets a short "endpoints we have run" note listing what the owner has
  actually driven, dated, with no implied guarantee - a claim about hardware we tested, not a
  compatibility promise.
