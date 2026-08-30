# Testing Milestone 14 by hand, today

Two paths. **(A) needs no GPU at all** and runs against a mock OpenAI server that
ships with the repo. **(B) uses the ollama already running on this machine.**
Both end the same way: a real step, executed by the LazyAF agent harness, against
a self-hosted OpenAI-compatible endpoint, producing a real `git diff` and a
`StepUsage` row with real token counts and a node-priced cost.

Every command below is copy-pasteable. Commands marked **VERIFIED** were actually
run on this host (Windows 11 + Docker Desktop, dev stack on `:8000`) against the
live stack on 2026-08-30; commands marked **NOT VERIFIED** could not be run and
say why. Section 5 is the honest ledger of both.

**Read section 1.3 before you try to create a card** - the card route is the one
surface that does not work yet, and there are two that do.

---

## 0. Preflight - read this first

### 0.1 The wiring edits have LANDED (verified 2026-08-30)

This section used to say two edits were missing. They are applied:

* `backend/app/main.py` imports and mounts `model_endpoints.router`
* `backend/app/models/__init__.py` exports `ModelEndpoint` (plus
  `ENDPOINT_FAILURE_THRESHOLD` and `IN_FLIGHT_STEP_STATUSES`)
* `frontend/src/App.svelte` carries the `/endpoints` route and its nav item

Check them (note the module is `model_endpoint`, SINGULAR - an earlier version
of this document grepped for `model_endpoints` against `models/__init__.py` and
always reported `0`, which sent the reader chasing a phantom):

```bash
grep -c "model_endpoints" backend/app/main.py          # -> 2
grep -c "model_endpoint" backend/app/models/__init__.py # -> 1
grep -c "EndpointsPage" frontend/src/App.svelte         # -> 2
```

### 0.2 Restart the backend once, to apply migration 0011 and mount the router

The dev backend bind-mounts `./backend/app` and `./backend/alembic`, runs
`alembic upgrade` at startup, and has no `--reload`. So the code is already
there but the PROCESS has to be restarted once:

```bash
docker compose restart backend
docker compose logs backend --tail 30 | grep -i alembic
```

**Do not** `docker compose down`. Restarting one service is enough.

Confirm the table and the routes:

```bash
curl -s http://localhost:8000/openapi.json | python -c "import sys,json; print([p for p in json.load(sys.stdin)['paths'] if 'model-endpoint' in p])"
```

Expected - five path patterns carrying the eight operations (list, create, get,
patch, delete, probe, probe-result, usage):

```
['/api/model-endpoints', '/api/model-endpoints/{reference}',
 '/api/model-endpoints/{reference}/probe',
 '/api/model-endpoints/{reference}/probe-result',
 '/api/model-endpoints/{reference}/usage']
```

`{reference}` accepts the endpoint's **name or its uuid**, so every command below
can use the readable handle.

The `.../proxy/v1/{path}` broker route (`reach: proxy`) IS registered on the app
but is hidden from `openapi.json`, so it does not appear above. Nothing in this
document needs it. Confirm it if you care:

```bash
docker compose exec -T backend /app/.venv/bin/python -c   "from app.main import app; print([r.path for r in app.routes if 'proxy' in getattr(r,'path','')])"
# -> ['/api/model-endpoints/{reference}/proxy/v1/{path:path}']
```

Use `/app/.venv/bin/python`, not `python` - the container's system interpreter
has no `fastapi`. On Git Bash for Windows prefix the whole command with
`MSYS_NO_PATHCONV=1` or the leading `/app/...` is rewritten into a Windows path
and the exec fails with `no such file or directory`. (**VERIFIED**, both traps.)

### 0.3 What is proven to work, end to end, on the running dev stack

Everything in this table was executed against `http://localhost:8000` on the
real dev stack on 2026-08-30 (**VERIFIED**):

| Call | Result |
|---|---|
| `POST /api/model-endpoints` against the mock | `201`, `probe_status: "ok"`, `supports_tools: true`, `context_window: 32768` from `max_model_len` |
| `POST /api/model-endpoints` against **this host's ollama**, `llama3.1:8b` | `201`, `probe_status: "ok"`, tools/stream/usage all true, `context_window: 131072` via ollama's `/api/show` |
| `POST .../{name}/probe` twice inside 30s | second returns `cached: true` |
| **A real pipeline step through the harness (tools mode)** | passed; a real file written; `StepUsage` `provider=openai-compatible`, `2100/420` tokens, `cost_source=gpu-node` |
| **The same in forced no-tools fallback mode** | passed; `raw.harness.mode == "text"`; identical token totals |
| **A real card-shaped run against real ollama** | ran, produced real tokens and a node-priced `$0.000000` row |
| `GET .../{name}/usage` | rollup with `by_source` counts and `cost_coverage` |

The one thing still **NOT** provable from a browser is creating a **card** with
`runner_type: "openai-harness"` - see section 1.3, which has the working
substitutes.

## 1. Path A - no GPU: the mock OpenAI server

### 1.1 Start it

`mock-endpoint` is a new compose service: a stdlib OpenAI-compatible server
(`tdd/shared/mock_openai/`) that serves **all nine test scenarios at once**, one
per URL prefix.

```bash
docker compose up -d mock-endpoint
docker compose ps mock-endpoint
```

**VERIFIED** - comes up `healthy` in about 5 seconds, no build, no GPU. It is
**already running on this machine**; the command is idempotent, and it starts
only that one service, so the rest of the dev stack is untouched.

```bash
curl -s http://localhost:8099/health
```

**VERIFIED** output:

```json
{"status":"ok","scenarios":["flaky_5xx","happy_text","happy_tools","lying_tools","malformed","malformed_forever","never_finishes","no_usage","slow"]}
```

The scenarios, and what each is for:

| URL prefix | Behaviour | Use it to see |
|---|---|---|
| `/happy_tools/v1` | six real `tool_calls`, then `finish(success)` | the tools loop end to end |
| `/happy_text/v1` | the same six actions as ```` ```lazyaf ```` blocks; **fails the tool probe on purpose** | the no-tools fallback protocol |
| `/never_finishes/v1` | `list_files` forever | the iteration budget stopping a runaway loop |
| `/malformed/v1` | prose, prose, prose, then valid blocks | the malformed-retry counter AND its reset |
| `/malformed_forever/v1` | prose only | exit 5, "produced no parseable action" |
| `/no_usage/v1` | omits the `usage` block | null tokens + the usage-scrape warning |
| `/lying_tools/v1` | tool-calls at probe time, prose blocks in real work | the probe-drift bridge and the endpoint demotion |
| `/slow/v1` | 3s per turn | the soft deadline |
| `/flaky_5xx/v1` | two 503s, then normal | the endpoint retry policy |

Two useful facts about it:

* **Token counts grow with the turn.** Turn *N* reports `prompt_tokens = 100*N`
  and `completion_tokens = 20*N`. Over the six-turn happy script a correct
  accumulator lands on **2100 / 420**; a harness that kept only the last
  response would land on **600 / 120**. That difference is what
  `scripts/verify_executor.py` assertion 13 checks, and it is why the numbers
  are shaped this way.
* **It is stateless.** The turn number is derived from the number of `assistant`
  messages in the request, so retries and replays are deterministic. The one
  exception is `flaky_5xx`; reset it with
  `curl -s -X POST http://localhost:8099/_control/reset -d '{}' -H 'Content-Type: application/json'`.

### 1.2 Register the two endpoints

One command does both, idempotently, and probes them.

**First, the one thing to get right.** For `reach: direct` the *probe* is made
by the **backend container** and the *inference* is made by the **step
container** - both on `lazyaf-network`. So the registered `base_url` must be
`http://mock-endpoint:8099/...`. `http://localhost:8099` works only from your
shell, and registering it produces `probe_status: "unreachable"` (**VERIFIED** -
the seeder fails loudly rather than registering a broken row).

For the real dev stack, register the service name:

```bash
LAZYAF_BACKEND_URL=http://localhost:8000 \
LAZYAF_MOCK_ENDPOINT_URL=http://mock-endpoint:8099 \
LAZYAF_MOCK_HEALTH_URL=http://localhost:8099 \
python scripts/seed_dogfood_endpoints.py
```

`LAZYAF_MOCK_HEALTH_URL` exists because there are honestly two network
positions: the URL that gets **registered** (what the containers use) and the URL
this script **polls for readiness** (what your shell can reach). Neither is a
fallback for the other.

**VERIFIED** against a backend with the router mounted:

```
[seed] readiness checked on http://localhost:8099; registering http://mock-endpoint:8099 (the address the step container will use)
[seed] dogfood-mock: base_url=.../happy_tools/v1 model=mock-model probe_status=ok tools=True usage=True ctx=32768
[seed] dogfood-mock-notools: base_url=.../happy_text/v1 model=mock-model-notools probe_status=degraded tools=False usage=True ctx=32768
[seed] 2 model endpoint(s) registered and probed
```

Re-running it is safe: an existing row is PATCHed back into shape and re-probed
(**VERIFIED**, identical output on the second run).

The dogfood pipeline runs this same script as its `seed-endpoints` step, from
inside a step container, where both URLs are the service name and no env is
needed.

The raw equivalent, if you would rather see the request:

```bash
curl -s -X POST 'http://localhost:8000/api/model-endpoints?probe=true' \
  -H 'Content-Type: application/json' -d '{
    "name": "dogfood-mock",
    "description": "M14 dogfood: tool-calling mock OpenAI server",
    "base_url": "http://mock-endpoint:8099/happy_tools/v1",
    "model": "mock-model",
    "server_kind": "vllm",
    "auth_style": "none",
    "reach": "direct",
    "rate_usd_hour": "0.010000",
    "max_concurrency": 1
  }' | python -m json.tool
```

```bash
curl -s -X POST 'http://localhost:8000/api/model-endpoints?probe=true' \
  -H 'Content-Type: application/json' -d '{
    "name": "dogfood-mock-notools",
    "description": "M14 dogfood: a model that CANNOT tool-call",
    "base_url": "http://mock-endpoint:8099/happy_text/v1",
    "model": "mock-model-notools",
    "server_kind": "vllm",
    "auth_style": "none",
    "reach": "direct",
    "rate_usd_hour": "0.010000",
    "max_concurrency": 1
  }' | python -m json.tool
```

**What to look for in the response.** `POST /api/model-endpoints` and
`POST .../probe` BOTH return the same wrapper, not a bare endpoint row
(**VERIFIED** - an earlier version of this document showed `capabilities` at the
top level, which is one level too shallow and makes every copy-pasted
`python -c` snippet raise `KeyError`):

```json
{
  "endpoint": { ... the full ModelEndpointRead ... },
  "cached": false,
  "detail": null,
  "probe_run_id": null
}
```

So read it as `.endpoint`:

```bash
curl -s http://localhost:8000/api/model-endpoints/dogfood-mock | python -c "
import sys, json
e = json.load(sys.stdin)                 # GET returns the row BARE
c = e['capabilities']
print(c['probe_status'], c['supports_tools'], c['context_window'], e['health'])
"
```

`GET /api/model-endpoints` and `GET /api/model-endpoints/{reference}` return the
row (or a list of rows) **bare**. Only the two POSTs wrap. Inside `.endpoint` the
shape is:

```json
"capabilities": {
  "supports_tools": true, "supports_streaming": true, "reports_usage": true,
  "context_window": 32768, "probe_status": "ok",
  "probed_from": "backend", "probe_age_seconds": 0.01, "stale": false
},
"pricing": {"gpu_node_id": "endpoint:dogfood-mock", "gpu_fraction": 1.0, "priced": true},
"health": "healthy"
```

With `?probe=false` the wrapper's `detail` tells you what you just built:
`"created without probing; dispatch will refuse this endpoint until POST
/api/model-endpoints/<id>/probe succeeds"` (**VERIFIED**).

`dogfood-mock-notools` **should** come back `"supports_tools": false` and
`"probe_status": "degraded"`. That is the correct answer, not a failure:
degraded is *usable* - it is what routes the no-tools fallback protocol. An
endpoint that came back `unprobed` is the one that will refuse to dispatch.

### 1.3 Run a real step through it

```bash
curl -s http://localhost:8000/api/repos | python -c "import sys,json;print([(r['id'],r['name']) for r in json.load(sys.stdin)])"
```

#### 1.3a The two routes that WORK today

**A CARD DOES NOT WORK YET.** `POST /api/repos/{repo_id}/cards` with
`"runner_type": "openai-harness"` returns **422** (**VERIFIED**):

```
Input should be 'any', 'claude-code', 'gemini' or 'mock'
```

`app.models.card.RunnerType` is a fifth vocabulary site that the wave's
five-site list did not name, so `AGENT_BY_RUNNER_TYPE` knows `openai-harness`
but the card schema does not. The Board's "Self-hosted endpoint" option in
`CardModal.svelte` therefore 422s on submit. The fix is one enum member (the
column is already `String(50)`, so no migration):

```python
# backend/app/models/card.py
class RunnerType(str, Enum):
    ANY = "any"
    CLAUDE_CODE = "claude-code"
    GEMINI = "gemini"
    MOCK = "mock"
    OPENAI_HARNESS = "openai-harness"   # <- add this
```

Until it lands, use either of these two - **both VERIFIED end to end**.

**Route 1: the Playground (works in the browser, right now).**

```bash
curl -s -X POST http://localhost:8000/api/repos/<REPO_ID>/playground/test   -H 'Content-Type: application/json' -d '{
    "prompt": "Create .lazyaf-dogfood/harness-ran naming the endpoint you used",
    "branch": "main",
    "runner_type": "openai-harness",
    "model": "endpoint:dogfood-mock"
  }'
# -> {"session_id": "...", "status": "running", ...}

curl -s http://localhost:8000/api/playground/<SESSION_ID>/result | python -m json.tool
```

`playground.runner_type` is a plain `str`, not the card enum, so this path is
open. **VERIFIED** output:

```
status: completed   files_changed: ['.lazyaf-dogfood/harness-ran']
--- diff ---
--- a/.lazyaf-dogfood/harness-ran
+++ b/.lazyaf-dogfood/harness-ran
@@ -0,0 +1,2 @@
+lazyaf harness reached endpoint mock-model
+written by tdd/shared/mock_openai (no GPU involved)
```

`branch` is REQUIRED on this body; omitting it is a 422.

**Route 2: a one-step pipeline (what the dogfood lane itself uses).**

```bash
curl -s -X POST http://localhost:8000/api/repos/<REPO_ID>/pipelines   -H 'Content-Type: application/json' -d '{
    "name": "M14 smoke: harness tools mode",
    "steps": [
      {"name": "harness-tools", "type": "agent",
       "config": {"agent": "openai-harness", "endpoint": "dogfood-mock",
                  "commit": false,
                  "task": "Create .lazyaf-dogfood/harness-ran naming the endpoint you used",
                  "harness": {"max_iterations": 8, "time_budget_seconds": 120}},
       "timeout": 300},
      {"name": "show-diff", "type": "script",
       "config": {"image": "lazyaf-base:dev",
                  "command": "git -C /workspace/repo status --porcelain; echo ---DIFF---; git -C /workspace/repo add -A -N .; git -C /workspace/repo diff"},
       "timeout": 120}
    ]}'

curl -s -X POST http://localhost:8000/api/pipelines/<PIPELINE_ID>/run   -H 'Content-Type: application/json' -d '{}'
```

The step form spells the agent `agent:` and names the endpoint with `endpoint:`;
`"model": "endpoint:dogfood-mock"` is the equivalent sugar and resolves the same
way. The second step is optional - it exists so you can SEE the diff, because
`commit: false` leaves the work in the workspace volume and the volume is
cleaned within a second of the run finishing.

For the forced no-tools fallback, swap the endpoint and pin the mode:

```json
{"agent": "openai-harness", "endpoint": "dogfood-mock-notools", "commit": false,
 "task": "Create .lazyaf-dogfood/harness-fallback-ran",
 "harness": {"mode": "text", "max_iterations": 8, "time_budget_seconds": 120}}
```

### 1.4 What to check after it runs

```bash
# every step's lane and status
curl -s http://localhost:8000/api/pipeline-runs/<RUN_ID>   | python -c "import sys,json;[print(s['step_name'], s['executor'], s['status']) for s in json.load(sys.stdin)['step_runs']]"

# the log stream a user watches (per step INDEX, not step id)
curl -s http://localhost:8000/api/pipeline-runs/<RUN_ID>/steps/0/logs   | python -c "import sys,json;print(json.load(sys.stdin)['logs'])"

# the usage row - the whole point
curl -s http://localhost:8000/api/pipeline-runs/<RUN_ID>/usage | python -m json.tool
```

**VERIFIED** log stream for the tools-mode step (this is exactly what came back):

```
[executor] endpoint dogfood-mock: model=mock-model reach=direct url=http://mock-endpoint:8099/happy_tools/v1 node=endpoint:dogfood-mock gpu_fraction=1.0
[agent] harness: endpoint=dogfood-mock model=mock-model mode=tools ctx=32768 reach=direct url=http://mock-endpoint:8099/happy_tools/v1
[agent] harness: budgets iterations=8 tokens=400000 deadline=120s tools=6
[agent] turn 1/8 in=100 out=20 (total in=100 out=20) 0.1s
[agent]   tool list_files(path=., depth=1, max_entries=50) -> 50 entries (+58 elided)
[agent] turn 2/8 in=200 out=40 (total in=300 out=60) 0.0s
[agent]   tool run_shell(command=mkdir -p .lazyaf-dogfood, timeout=30) -> exit 0
[agent] turn 3/8 in=300 out=60 (total in=600 out=120) 0.0s
[agent]   tool write_file(path=.lazyaf-dogfood/harness-ran, ...) -> 105 bytes created
[agent] turn 4/8 in=400 out=80 (total in=1.0k out=200) 0.0s
[agent]   tool apply_patch(...) -> 1 of 1 occurrence(s) replaced
[agent] turn 5/8 in=500 out=100 (total in=1.5k out=300) 0.0s
[agent]   tool read_file(path=.lazyaf-dogfood/harness-ran, start_line=1, max_lines=20) -> lines 1-2 of 2
[agent] turn 6/8 in=600 out=120 (total in=2.1k out=420) 0.0s
[agent] stop: finish(status=success) after 6 turns, 2.1k in / 420 out, 0s
[agent] commit disabled for this step; leaving the workspace as-is
[lazyaf] exit code: 0
```

In `mode: text` the same six actions arrive as ```` ```lazyaf ```` blocks and the
harness echoes each one on a `text:` line before executing it - same shape, same
totals, which is what lets an experiment vary only `harness.mode`.

**VERIFIED** usage row for that run:

| Field | Observed | Why it matters |
|---|---|---|
| `provider` | `openai-compatible` | the step really went to a self-hosted endpoint |
| `input_tokens` / `output_tokens` | **2100 / 420** | proves the accumulator SUMMED all six turns; `600`/`120` would be the last-response-wins bug |
| `cost_source` | `gpu-node` | the endpoint's `rate_usd_hour` priced it; `cli-reported` here would be a lie |
| `cost_usd` | `0.000002` | `0.01/hr` x `0.543` container-seconds x `gpu_fraction 1.0` |
| `determinism` | `{"temperature": 0, "top_p": null, "seed": null}` | the first non-empty determinism record LazyAF has produced |
| `raw.harness` | `turns: 6`, `stop_reason: "finish"`, `tool_calls: {...}`, `malformed_responses: 0`, `files_changed: 1` | the loop's own account of itself |

Endpoint-level rollup:

```bash
curl -s http://localhost:8000/api/model-endpoints/dogfood-mock/usage | python -m json.tool
```

## 2. Path B - the real ollama already on this machine

### 2.1 The addressing decision, made once

This is the only genuinely confusing part, so decide it before typing anything.

**`base_url` is written in the terms of WHOEVER MAKES THE HTTP CALL**, and who
that is depends on `reach`:

| `reach` | Who calls the model | Write `base_url` as |
|---|---|---|
| `direct` (default) | **the step container**, on `lazyaf-network` | an address a container on `lazyaf-network` can reach |
| `runner-local` | the step container **on a remote runner's host** | an address that runner's host can reach (`http://172.17.0.1:11434/v1`, a LAN IP, ...) |
| `proxy` | **the backend**, on the container's behalf | an address the backend can reach |

ollama here runs **on the Windows host**, and the backend and step containers run
**in Docker**. So `localhost:11434` is wrong for every one of them. The address
that works is **`host.docker.internal`**, which Docker Desktop resolves from
user-defined networks - and which this project already relies on: see
`tdd/integration/conftest.py`'s `advertise_addr()`, whose docstring is the
authority on this exact problem ("on the host (the dev path):
`host.docker.internal`, which Docker Desktop resolves from user-defined
networks. A Linux-Engine host may need `--add-host
host.docker.internal:host-gateway`").

`docker-compose.yml` needs no `extra_hosts` entry on Docker Desktop.

**VERIFIED** from a throwaway container on the real network:

```bash
docker run --rm --network lazyaf-network python:3.12-slim python -c \
  "import socket; print(socket.gethostbyname('host.docker.internal'))"
# -> 192.168.65.254

docker run --rm --network lazyaf-network python:3.12-slim python -c \
  "import urllib.request,json; print([m['id'] for m in json.load(urllib.request.urlopen('http://host.docker.internal:11434/v1/models'))['data']])"
# -> ['llama3.1:8b', 'qwen3.5:9b', 'qwen3.6:27b', 'gemma4:latest', 'llama3.1:latest',
#     'gemma3:27b', 'deepseek-r1:8b', 'deepseek-coder-v2:latest', 'deepseek-r1:32b', 'llama3.1:70b']
```

If ollama refuses connections from Docker, it is bound to loopback only. Set
`OLLAMA_HOST=0.0.0.0` and restart it.

**One asymmetry worth knowing before it confuses you.** `host.docker.internal`
resolves *from the host* too (Docker Desktop writes it into the Windows hosts
file), but it resolves to this machine's **LAN address** - and connecting to a
published container port through it **timed out** here, almost certainly the
Windows firewall (**VERIFIED**: `host.docker.internal` -> `192.168.10.224` from
the host, `192.168.65.254` from a container; the second reaches published ports,
the first does not).

The practical rule: **one `base_url` per network position, and check the one the
caller actually occupies.** For `reach: direct` on the normal dev stack, the
backend and the step containers are both on `lazyaf-network`, so a single
service-name URL is correct for both and none of this bites. It only bites if
you run a backend on the host and expect it to share a URL with a container.
The probe records `probed_from` for exactly this reason: "the backend could
reach it" is never silently read as "the step can".

**On a Linux engine** (not Docker Desktop), `host.docker.internal` does not
exist by default. Use the bridge gateway `http://172.17.0.1:11434/v1`, or add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the backend service.
**NOT VERIFIED** - this host is Docker Desktop.

### 2.2 WARM THE MODEL BEFORE YOU PROBE

This bit the author, so it is here rather than in a footnote.

The probe's per-request budget is `PROBE_TIMEOUT_SECONDS = 20`. A cold ollama
loading a model takes longer than that, so the **first** probe of a cold model
reports `probe_status: "degraded"` with `"tools_reason": "timeout"` - which
looks exactly like "this model cannot tool-call" and is not.

**VERIFIED, both halves:**

```bash
# cold: probe says degraded, tools=False, reason timeout
# warm it - one throwaway completion, and ollama keeps it resident
curl -s http://localhost:11434/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' >/dev/null
# now the same probe: probe_status ok, tools True, streaming True, usage True, ctx 131072
```

A 27B or 70B takes considerably longer to load; warm it and wait for the curl to
return before registering. If you would rather not warm by hand, raise
`request_timeout_seconds` on the endpoint - but note the probe uses
`min(request_timeout_seconds, 20)` per request, so that alone does not lift the
probe's ceiling. Warming is the reliable move.

### 2.2b REASONING MODELS PROBE AS `supports_tools: false`, AND THEY ARE NOT

This is the single biggest gotcha found while verifying this milestone, and on
this host it affects **four of the ten installed models**.

The tool probe sends `"max_tokens": 64`. A reasoning model (qwen3.x,
deepseek-r1, and friends) spends its first hundreds of tokens on `reasoning`
before it emits anything else, so it hits the cap and returns
`finish_reason: "length"` with an EMPTY `content` and NO `tool_calls`. The probe
correctly records what it saw - `tools_reason: "no_tool_calls"` - and the
endpoint lands `probe_status: "degraded"`, `supports_tools: false`, which routes
every step down the slower no-tools fallback protocol.

**VERIFIED, both halves, against `qwen3.6:27b` on this host:**

```bash
# the probe's exact request, with the probe's max_tokens
curl -s http://localhost:11434/v1/chat/completions -H 'Content-Type: application/json' -d '{
 "model":"qwen3.6:27b",
 "messages":[{"role":"system","content":"You call tools. Never answer in prose."},
             {"role":"user","content":"Call the tool `probe` with value 7. Do not reply in text."}],
 "tools":[{"type":"function","function":{"name":"probe","description":"Echo a number back to the caller.",
   "parameters":{"type":"object","properties":{"value":{"type":"integer"}},"required":["value"]}}}],
 "tool_choice":"auto","max_tokens":64,"temperature":0,"stream":false}'
# -> finish_reason: length,  tool_calls: null,  content: "",  reasoning: 214 chars

# the same request with max_tokens 1024
# -> finish_reason: tool_calls
# -> [{"function": {"name": "probe", "arguments": "{\"value\":7}"}}]
```

Same model, same prompt, same server: **tool-capable, reported as not**.

Until the probe's tool request gets a larger budget (it is pinned at 64 in
`backend/app/services/model_endpoints/probe.py`, and raising it is a design
change, not a config), do this:

1. Register the reasoning model and probe it. Expect `degraded`.
2. Check `probe_detail.tools_reason`. If it is `no_tool_calls` **and** the
   `tools_body` shows a non-empty `reasoning` field with `finish_reason:
   "length"`, the model is a reasoning model being cut off, not a model that
   cannot tool-call.
3. Either run it in the fallback protocol on purpose (`harness: {mode: text}`),
   or wait for the probe budget change. **Do not** conclude the model is
   incapable.

A non-reasoning model that genuinely cannot tool-call shows the same
`no_tool_calls` reason but with real prose in `content` - that is how you tell
them apart. `dogfood-mock-notools` is the honest example of the second kind.

### 2.3 Register it

```bash
curl -s -X POST 'http://localhost:8000/api/model-endpoints?probe=true' \
  -H 'Content-Type: application/json' -d '{
    "name": "local-ollama",
    "description": "ollama on this workstation",
    "base_url": "http://host.docker.internal:11434/v1",
    "model": "llama3.1:8b",
    "server_kind": "ollama",
    "auth_style": "none",
    "reach": "direct",
    "rate_usd_hour": "0.000000",
    "max_concurrency": 1,
    "request_timeout_seconds": 300
  }' | python -m json.tool
```

Three fields worth explaining:

* **`server_kind: "ollama"`** is the *only* thing `server_kind` changes: it lets
  the probe attempt ollama's `POST /api/show` extension to discover the context
  window. **VERIFIED**: that is how `131072` came back for `llama3.1:8b`. With
  `server_kind: "vllm"` the probe reads `max_model_len` from `/v1/models`
  instead. Nothing else in the system branches on it.
* **`rate_usd_hour: "0.000000"`** means *owned hardware, no marginal cash cost* -
  a real cost figure. It is a **different** value from `null`, which means "we do
  not know" and produces `cost_source: "unknown"` with `cost_usd: null`. Keep
  them distinct: that distinction is the whole reason the field is nullable.
* **`auth_style: "none"`** is a first-class case, not a workaround. LAN ollama
  genuinely has no key. If yours does, set `auth_style: "bearer"` and
  `auth_secret_ref: "LAZYAF_ENDPOINT_MYBOX"` - the **name** of a backend
  environment variable. The value is never stored in the database and never
  appears in any response.

  **But putting it in `.env` is NOT enough today, and this will bite you on a
  runpod endpoint.** `docker-compose.yml`'s `backend` service enumerates the
  environment variables it forwards, and there is no `LAZYAF_ENDPOINT_*`
  passthrough, so a value in `.env` never reaches the backend process. The
  symptom is `secret_present: false` on the row and, at dispatch:

  ```
  endpoint 'X' references backend environment variable LAZYAF_ENDPOINT_MYBOX,
  which is not set (neither LAZYAF_ENDPOINT_MYBOX nor LAZYAF_ENDPOINT_MYBOX_FILE).
  Set it on the backend and retry; the value is never stored in the database.
  ```

  (That refusal message is **VERIFIED** and it is the right behaviour - it fails
  at dispatch naming the variable rather than burning a container start on an
  opaque 401.) Until compose forwards the prefix, add the variable to the
  `backend` service's `environment:` list yourself, or start the stack with an
  override file:

  ```yaml
  # m14-endpoint-secrets.override.yml
  services:
    backend:
      environment:
        - LAZYAF_ENDPOINT_MYBOX=${LAZYAF_ENDPOINT_MYBOX:-}
  ```
  ```bash
  docker compose -f docker-compose.yml -f m14-endpoint-secrets.override.yml up -d backend
  ```

  **VERIFIED** with a sentinel key by exactly this override: the probe
  authenticated, a real harness step ran against an endpoint that 401s without
  the key, and the value appeared in **no** database cell, **no** API response,
  **no** WebSocket frame, **no** log line, and nowhere in the step container's
  `docker inspect` - it travels only in 12.5's 0600 consume-once config file.

**VERIFIED** response for exactly this endpoint on this machine (remember the
wrapper - these keys live under `.endpoint`):

```json
"capabilities": {"supports_tools": true, "supports_streaming": true,
                 "reports_usage": true, "context_window": 131072,
                 "probe_status": "ok", "probed_from": "backend"},
"context_window_source": "ollama",
"health": "healthy"
```

### 2.4 Picking a model

There is deliberately no compatibility table in this repo. **The probe is the
table**: it reports what *this* server with *this* chat template actually does,
which a static list cannot. Register a candidate, probe it, read three fields:

```bash
curl -s http://localhost:8000/api/model-endpoints | python -c "
import sys, json
for e in json.load(sys.stdin):
    c = e['capabilities']
    print(f\"{e['name']:22} {c['probe_status']:10} tools={c['supports_tools']!s:5} \"
          f\"usage={c['reports_usage']!s:5} ctx={c['context_window']}\")
"
```

**VERIFIED** output on this host, which is the whole argument for "the probe is
the table" - and for section 2.2b:

```
dogfood-mock           ok         tools=True  usage=True  ctx=32768
dogfood-mock-notools   degraded   tools=False usage=True  ctx=32768
local-ollama           ok         tools=True  usage=True  ctx=131072
local-qwen27           degraded   tools=False usage=True  ctx=262144
```

The last row is the trap: `local-qwen27` is `qwen3.6:27b`, it reads
`tools=False`, and it is **tool-capable**. Read section 2.2b.

* `supports_tools: true` -> the harness runs the **tools loop**. Best case.
* `supports_tools: false`, `probe_status: degraded` -> still usable; the harness
  runs the ```` ```lazyaf ```` **fallback protocol**. Expect more turns and more
  malformed replies.
* `reports_usage: false` -> the endpoint will produce **null** token counts and a
  loud scrape warning. The step still runs; the cost axis just goes dark for it.
* `context_window: null` -> the harness assumes 8192 and says so in the log. Set
  `context_window` on the endpoint if you know better.

* `supports_tools: false` on a model whose `probe_detail.tools_body` shows a
  `reasoning` field and `finish_reason: "length"` -> **the probe is wrong about
  this one**, read section 2.2b before believing it.

Warm each candidate first (section 2.2) or you will be reading timeouts as
capabilities. Measured on this host (**VERIFIED**):

| Model | Probe says | Reality |
|---|---|---|
| `llama3.1:8b` | `ok`, tools true, ctx 131072 | tool-calls for real; does not reliably call `finish` (section 2.5) |
| `qwen3.6:27b` | `degraded`, tools **false**, ctx 262144 | **tool-capable** - cut off by the probe's 64-token budget (section 2.2b) |

The other eight installed models were not probed.

### 2.5 Run a real step through it - and what actually happened

Identical to section 1.3 (Playground or a one-step pipeline), with
`"model": "endpoint:local-ollama"` or `"endpoint": "local-ollama"`.

**VERIFIED, and the result is worth reading in full**, because it is the honest
answer to "will my 8B model do this?".

`llama3.1:8b`, tools mode, task "create HELLO-M14.txt containing one line":

```
[agent] harness: endpoint=local-ollama model=llama3.1:8b mode=tools ctx=131072 reach=direct url=http://host.docker.internal:11434/v1
[agent] turn 1/12 in=873 out=36 (total in=873 out=36) 0.8s
[agent]   tool write_file(path=/workspace/repo/HELLO-M14.txt, content=hello from llama3.1) -> 19 bytes created
[agent] turn 2/12 ...
[agent]   text: {"name": "run_tests", "parameters": {}}
[agent] turn 3/12 ...
[agent]   tool write_file(...) -> 19 bytes written
[agent] turn 4/12 ...
[agent]   text: {"name": "run_tests", "parameters": {}}
[agent] turn 5/12 ...
[agent]   text: {"name": "run_tests", "parameters": {}}
[agent] stop: model_stopped_calling_tools after 5 turns, 3.7k in / 111 out, 2s
[agent] result: FAILED (the agent stopped calling tools without calling finish)
```

It **did** tool-call, it **did** write the file, and then it hallucinated a
`run_tests` tool that does not exist, emitted it as prose three times, and never
called `finish`. Two consecutive prose-only turns is `NO_TOOL_PATIENCE`, so the
harness stopped - which is stop condition 5 doing exactly its job.

The step is RED and the usage row still landed (**VERIFIED**):

```
provider openai-compatible | model llama3.1:8b | in 3711 out 111 | cost 0.000000 gpu-node
```

`cost_usd: "0.000000"` with `cost_source: "gpu-node"` is the honest *"this cost
no cash"* claim, and it is a different value from `null` + `unknown`, which
would mean *"we do not know"*.

The same model in `mode: text` fails differently and just as visibly - it emits
a bare ```` ``` ```` fence instead of ```` ```lazyaf ````, so the parser reports
`no_block` four times running and the step exits **5** (**VERIFIED**):

```
[agent]   text: ```{"tool": "write_file", "args": {...}}```
[agent]   unparseable (no_block); correcting the model
... x4
[agent] stop: unparseable after 4 turns, 2.3k in / 104 out, 2s
[agent] result: FAILED (endpoint local-ollama (model llama3.1:8b) produced no
        parseable action in 4 consecutive turns; last reason: no_block)
```

The JSON inside the fence is perfectly good; only the ```` lazyaf ```` info
string is missing, and the parser requires it (by design - see the wiring doc
section 3.8). If your model does this, say so in the task text: *"the fence must
be marked ```lazyaf"*.

**The takeaway for picking a model today:** an 8B model drives the tools loop
but does not reliably terminate it. Budget for a bigger model, and read section
2.2b before you write off a reasoning model that probed `degraded`.

### 2.6 If the GPU box is a different machine (`runner-local`)

Not needed here - ollama is on this host, so `direct` works. For a NAT'd box
elsewhere, the mode exists so the endpoint URL never has to be reachable from
anywhere except the machine hosting the model:

```bash
# on the box with the GPU
docker run -d --name lazyaf-runner \
  -e LAZYAF_RUNNER_ID=workshop-1 \
  -e LAZYAF_RUNNER_LABELS=has=docker,has=endpoint:local-4090 \
  -e LAZYAF_BACKEND_URL=wss://<your-backend> \
  -e LAZYAF_RUNNER_TOKEN=<LAZYAF_RUNNER_AUTH_SECRET> \
  -v /var/run/docker.sock:/var/run/docker.sock \
  lazyaf/runner-agent
```

then register with `"reach": "runner-local"` and
`"base_url": "http://172.17.0.1:11434/v1"` (that host's own view of ollama).
The label `endpoint:<name>` is what pins the step - and the probe - to that
runner. **NOT VERIFIED**: no second machine here.

---

## 3. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `POST /api/model-endpoints` -> `{"detail":"Not Found"}` | the router is not mounted | section 0.1, then `docker compose restart backend` |
| `no such table: model_endpoints` | migration 0011 not applied | `docker compose restart backend` (alembic runs at startup) |
| `probe_status: "unreachable"`, `last_error` naming a connect error | the URL is written from the wrong point of view | section 2.1 - `localhost` from inside a container is the container |
| `probe_status: "degraded"`, `tools_reason: "timeout"` | cold model, 20s probe budget | warm it (section 2.2) and re-probe |
| `probe_status: "degraded"`, `model_not_listed` | the `model` string is not in `GET /v1/models` | copy the id from `curl localhost:11434/v1/models` verbatim, tag included |
| second probe returns instantly with `"cached": true` | `PROBE_MIN_INTERVAL_SECONDS = 30` floor | wait 30s, or accept the cached record |
| dispatch refuses: `"has never been probed"` | `probe_status: unprobed` | `POST /api/model-endpoints/<name>/probe`. This is deliberate - a 30-minute agent step is not where you want to discover a model cannot tool-call |
| dispatch refuses: `"is disabled"` / 3 consecutive failures | endpoint health | `PATCH {"enabled": true}` and re-probe |
| `DELETE` returns 409 | steps still in flight on that endpoint | wait, or cancel them; the response names the step run ids |
| mock endpoint 404s with a list of scenario names | wrong URL prefix | the path is `/<scenario>/v1/...`, e.g. `/happy_tools/v1/chat/completions` |
| the probe succeeds but the STEP cannot reach the endpoint | `probed_from: "backend"` and the step container sit in different network positions | keep the backend and the step containers on `lazyaf-network` and use a service name; do not share a `base_url` between a host-run backend and a container |
| `POST /api/repos/.../cards` -> 422 `Input should be 'any', 'claude-code', 'gemini' or 'mock'` | `RunnerType` has no `openai-harness` member | section 1.3 - use the Playground or a pipeline step until the enum lands |
| `KeyError: 'capabilities'` from a copy-pasted snippet | the two POSTs wrap the row as `{endpoint, cached, detail, probe_run_id}` | read `.endpoint`; the GETs return the row bare |
| a reasoning model probes `degraded` / `tools: false` | the probe's `max_tokens: 64` cuts it off mid-`reasoning` | section 2.2b - it is almost certainly tool-capable |
| step exits **5**, `no_block` x4, and the raw reply is a bare ``` fence | the parser requires the ```` ```lazyaf ```` info string | tell the model to mark the fence, or use a model that follows the format |
| step exits **1**, `model_stopped_calling_tools` | two consecutive prose-only turns, and the prose did not read as a completion claim | small-model pathology; the work it did do is still in the workspace, and the usage row still landed |
| `secret_present: false` on a `bearer` endpoint whose key IS in `.env` | compose does not forward `LAZYAF_ENDPOINT_*` to the `backend` service | section 2.3 - add it to the service's `environment:` or use an override file |
| `docker compose exec -T backend python ...` -> `No module named 'fastapi'` | the container's system python is not the venv | use `/app/.venv/bin/python`, and prefix with `MSYS_NO_PATHCONV=1` on Git Bash |
| `endpoints.spec.ts` matrix test fails on `add-model-row-btn` not found | the spec goes to `/#/experiments` without clicking `experiment-create-btn`, so the MatrixBuilder never renders | a test bug, not a product bug - the selector works once the builder is open (verified) |
| `POST /api/repos/.../playground/test` -> 422 `branch: Field required` | `branch` is required on that body | add `"branch": "main"` |

---

## 4. Running the automated coverage

```bash
# the mock server, judged by the code that consumes it
cd backend && uv run pytest ../tdd/unit/shared/test_mock_openai_server.py -q

# the dogfood exit-gate, including the six new harness assertions
cd backend && uv run pytest ../tdd/unit/scripts/test_verify_executor.py -q

# the harness in a real container against a real endpoint (T2, needs Docker)
cd backend && uv run pytest ../tdd/integration/services/test_harness_step_container.py -q

# the whole no-Docker tier, then the Docker tiers
python scripts/run_tier.py T1
python scripts/run_tier.py T2
python scripts/run_tier.py T3

# the Endpoints page (Playwright, needs the e2e stack + the mock server)
docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml   --profile e2e up -d backend-e2e runner-agent-e2e mock-endpoint
cd frontend && npx playwright test endpoints.spec.ts --reporter=list
```

**`backend-e2e` bakes its source into its image** - unlike the dev `backend`, it
does NOT bind-mount `./backend/app`. After any backend change you must
`docker compose ... --profile e2e build backend-e2e` before `up -d`, or the
spec runs against stale code and every `/api/model-endpoints` call 404s
(**VERIFIED** - this bit while writing this section).

**Tear the e2e services down BY NAME**, never with `--profile e2e down`:

```bash
docker compose stop backend-e2e runner-agent-e2e
docker compose rm -f backend-e2e runner-agent-e2e
```

**VERIFIED 2026-08-30**, in this order:

| Command | Result |
|---|---|
| `test_mock_openai_server.py` | **56 passed** |
| `test_verify_executor.py` | **72 passed** |
| `scripts/run_tier.py T1` | **4523 passed, 1 baselined skip, 0 failed** in 10m33s; `CI GATE [T1]: OK` |
| `scripts/run_tier.py T2` | **77 passed, 1 baselined skip, 0 failed** in 6m54s; `CI GATE [T2]: OK`. Includes all three M14 container tests: tools mode, forced text mode, and "the endpoint key is in no container inspect" |
| `scripts/run_tier.py T3` | **22 passed, 3 baselined skips, 0 failed** in 25s; `CI GATE [T3]: OK` |
| `cd runner-common && pytest` | **461 passed, 1 baselined skip** |
| `cd runner-agent && pytest` | **189 passed** |
| `cd frontend && npm run test:unit` | **276 passed** (15 files) |
| `cd frontend && npm run check` | 4 errors / 56 warnings - the pre-existing baseline; **zero in any M14 file** |
| `python scripts/build_images.py --force` then `--check` | 6 images rebuilt, all fresh |

One trap worth naming: run `runner-common`'s own suite with the package on the
path, or three tests fail for a reason that is not a defect -

```bash
cd runner-common && PYTHONPATH=$PWD python -m pytest -q     # 461 passed
cd runner-common && python -m pytest -q                     # 3 failed
```

`test_pytest_lazyaf.py` spawns a NESTED pytest that must import
`runner_common`; without `PYTHONPATH` the child cannot. `scripts/run_tier.py`
sets it for you, which is why T1 is green on the same tests.

The mock suite is worth a moment's attention because of *how* it checks things.
It does not assert against a hand-written idea of the OpenAI format - it imports
the code that actually consumes the mock and runs it over the mock's real bytes:

* the backend's shipping probe judges (`judge_tools`, `judge_streaming`,
  `judge_usage`, `judge_models`, `judge_ollama_context`) and a full `run_probe`
  over HTTP;
* the harness's real HTTP client (`OpenAICompatClient`), non-streaming **and**
  streaming, reading the tool calls and the usage block;
* the harness's real fallback parser, over every ```` ```lazyaf ```` block the
  mock scripts.

A mock that satisfies only its own author drifts. One that satisfies the
shipping consumers cannot.

The dogfood pipeline (`.lazyaf/pipelines/test-suite.yaml`) carries three new
steps that run on every push - `seed-endpoints`, `harness-probe` (tools mode)
and `harness-probe-notools` (forced text mode) - and `verify_executor.py` gained
assertions 13-18 over them. Assertion 13 is the interesting one: it reads
`raw.harness.turns` from the usage row and requires the recorded tokens to be
**strictly greater** than the largest single turn, which is the only thing that
can catch a token accumulator that replaced instead of added.

## 5. What is verified, and what still is not

Stated plainly rather than glossed (R4).

### Verified end to end on the running dev stack, 2026-08-30

1. **Registering and probing an endpoint over HTTP** against both the mock
   server and this host's real ollama, through the mounted router on `:8000`.
2. **Dispatching an `openai-harness` step** - the vocabulary, the agent-config
   producer/consumer, the `endpoint:` sugar, the admission gate
   (`step_executions.model_endpoint_id` is stamped), the `gpu_node_id` /
   `gpu_fraction` container env, and `usage_ingestion`'s node pricing.
3. **The tools loop and the no-tools fallback**, each producing a real file, a
   real `git diff`, a real log stream, and a `StepUsage` row with tokens summed
   across all six turns (`2100/420`) and `cost_source: "gpu-node"`.
4. **A real model on real hardware** (`llama3.1:8b` through ollama): real
   tokens, `cost_usd: "0.000000"` with `cost_source: "gpu-node"`, and two
   genuine model-capability failures caught by stop conditions 5 and 7 rather
   than by a hang. See section 2.5.
5. **The security invariants**: a row cannot reference a non-allowlisted env var
   (422 at create *and* at PATCH); a planted sentinel key appears in **zero** of
   12,738 string cells across all 24 database tables, zero API response bodies,
   zero of 18 captured WebSocket frames, zero backend log lines, and nowhere in
   the step container's `docker inspect` (its `Config.Env` carries
   `LAZYAF_GPU_NODE_ID`, `LAZYAF_GPU_FRACTION` and `LAZYAF_USAGE_PROVIDER` and
   no key - the key travels in 12.5's 0600 consume-once config file); and an
   **unprobed** endpoint refuses dispatch with
   `"endpoint 'X' has never been probed; POST .../probe first"` instead of
   silently taking the fallback path.

### Still not verified

1. **Creating a CARD with `runner_type: "openai-harness"`** - 422, see section
   1.3. One enum member fixes it; the Playground and the pipeline step form both
   work today.
2. **`runner-local` reach**, which needs a second machine.
3. **`proxy` reach.** The broker route exists on the app
   (`ANY /api/model-endpoints/{id}/proxy/v1/{path}`) but is hidden from
   `openapi.json`, so it does not appear in section 0.2's output. Nothing in
   this document needs it.
4. **Models larger than `llama3.1:8b` completing a card.** `qwen3.6:27b` was
   probed and is genuinely tool-capable once the probe budget allows it
   (section 2.2b); it was not driven through a full card.
