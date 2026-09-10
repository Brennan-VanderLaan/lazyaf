# Security posture — hosting exposure and execution isolation

**Status:** review complete, plan proposed. No code changed. Nothing committed.
**Owns:** this file.
**Scope:** the whole platform as it stands at `be5e943`. Three review lanes ran
against the source and against a live QA sandbox on `:8790`; the owner stack on
`:8000` was read-only throughout.
**Companion docs:** `upcoming/shadow-ci.md` (§6 here is its security
prerequisite list), `upcoming/wave9-145-runner-images.md` (Phase 14.5 puts
runners on a Windows desktop; §6 says which rungs must land first).

---

## 1. The honest threat model

### 1.1 What this thing is

LazyAF is a self-hosted CI/CD platform whose pipeline steps can be AI agents. It
hosts its own git server (`backend/app/routers/git.py`), so a push to LazyAF
triggers LazyAF. Steps run in ephemeral Docker containers that the backend
creates through a mounted host docker socket (`docker-compose.release.yml:61`,
`backend/app/services/execution/local_executor.py`). One process, one worker,
one SQLite file, one docker daemon.

**Executing code the operator did not write is the product, not a side effect.**
"Do not run arbitrary code" is not on the menu. Every question below is
therefore about *containment* and *who is allowed to ask*, never about whether
untrusted code runs.

### 1.2 What it actually is today, said plainly

**LazyAF today is a single-operator tool for a trusted, private network, and it
should say so on the tin.** There is no user model, no roles, no tenancy, and no
authentication on any human-facing route — `backend/app/main.py:248-256` installs
exactly two middlewares, `UnhandledErrorBoundary` and `CORSMiddleware`, and
neither authenticates anything. Everyone who can open a TCP connection to the
backend port is, as far as the code is concerned, the operator.

That is a legitimate posture. Plenty of good software ships it. What is **not**
legitimate is shipping it while the defaults publish the port on `0.0.0.0`
(`.env.example:49-50`, `docker-compose.release.yml:53`, `docker-compose.yml:5`)
and the README says nothing about it. The gap between "designed for a trusted
network" and "defaults to the whole LAN" is where people get hurt, and closing
that gap costs two lines in a template.

### 1.3 What "secure" can and cannot mean here

It **can** mean:

- Only the operator can ask the platform to run something.
- A step that runs cannot reach the host, the daemon, the operator's files, or
  other runs.
- A step that misbehaves — forkbomb, memory hog, log flood — costs the host a
  bounded amount of CPU, RAM, PIDs and disk, and says so when it is killed.
- A credential the platform holds is not readable by the code the platform runs,
  and does not end up in the database, the logs, or a bug report.
- A repo the operator does not trust cannot define what runs.

It **cannot** mean:

- That an agent step will not do something stupid or hostile if its prompt tells
  it to. Agents run with permission gates off by design
  (`runner-common/runner_common/executors/claude.py:77`
  `--dangerously-skip-permissions`;
  `runner-common/runner_common/executors/gemini.py:45` `--yolo`). That is what
  makes an autonomous step autonomous. The containment story is the container
  and the network, never the model's judgement.
- That a container is a security boundary against a kernel bug. It is one layer.
  On the Phase 14.5 target (Docker Desktop / WSL2) it is measurably the *only*
  layer — `docker info` reports `SecurityOptions=["name=seccomp,profile=builtin",
  "name=cgroupns"]`, with no AppArmor, no SELinux, no userns, no rootless, and
  runtimes `runc` / `nvidia` only, no `runsc`.
- That secrets are encrypted at rest. They are not, there is no key management,
  and M14 already decided the honest answer is *do not store the secret at all*
  (`backend/app/services/model_endpoints/secrets.py:41` — a prefix-allowlisted
  environment-variable *reference*, resolved at use). That doctrine is right and
  §4 says do not replace it with a fake vault.

### 1.4 The three trust planes, and which ones already work

| Plane | Credential today | Verdict |
|---|---|---|
| Runner fleet (`/ws/runner`) | shared secret, `ws_runners.py:136-141`, `hmac.compare_digest` | **Works.** Fail-closed, no default, retired public values rejected. |
| Step callbacks (`/api/steps/*`, endpoint proxy) | per-step JWT, `routers/steps.py:121`, scoped to one `step_id` | **Works,** with two gaps (§2 S12). |
| Human / operator (everything else) | **none** | **Absent.** |

This table is the single most useful fact in the review, because it means human
auth can be added **without a flag day**: the two machine planes carry their own
credentials, so a human-auth layer that exempts them never touches a running
runner or an in-flight step callback (§3.2).

### 1.5 The assumption that is currently doing all the work

Everything holds *only* because nobody hostile can reach the port. That is one
assumption, it is undocumented, and the shipped defaults break it. Rung 0 (§3.1)
is entirely about making the shipped defaults match the posture the code
actually has.

---

## 2. Ranked findings

Ranked by **reachability × impact**. Tier 1 is anything an unauthenticated
caller on the LAN can do. Tier 2 needs the ability to author a step — which,
until Tier 1 is fixed, *is* Tier 1, because pushing to the git server needs no
credential either. Tier 3 needs an upstream compromise or a genuinely remote
runner.

Every row was confirmed against the source at the cited line, and the ones
marked **live** were executed against the QA sandbox.

### Tier 1 — unauthenticated network caller → host compromise

| # | Finding | Where | Live? |
|---|---|---|---|
| **S1** | Forged pipeline → arbitrary shell in a container on the host daemon | `routers/repos.py:53`, `routers/git.py:157-158`, `routers/lazyaf_files.py:230` | **yes** |
| **S2** | `needs: [docker]` mounts the host socket rw, and the socket is the *default* allowlist | `pipeline_executor.py:3207-3226`, `local_executor.py:134-145` | **yes** |
| **S3** | An agent step is handed the platform `ANTHROPIC_API_KEY` | `pipeline_executor.py:192-193, 469-480` | code |
| **S4** | Every published port binds `0.0.0.0` by default | `.env.example:49-50`, `docker-compose.release.yml:53,106`, `docker-compose.yml:5,35,135,157` | **yes** |
| **S5** | No authentication on any human-facing router | `main.py:248-256` + 117 OpenAPI paths | **yes** |

**S1 — forged push → RCE. CONFIRMED END TO END.** Lane A ran the whole chain on
`:8790`: `POST /api/repos` created a repo with no credential
(`routers/repos.py:53`, only `Depends(get_db)`); an anonymous
`git push http://localhost:8790/git/<id>.git` succeeded because
`git_receive_pack` depends on nothing but `db` (`routers/git.py:157-158`);
`on_push` → `sync_repo_pipelines` materialised the pushed
`.lazyaf/pipelines/*.yaml` as the platform pipeline row
(`trigger_service.py:455`, `upsert_materialized_pipeline` — repo YAML is the
source of truth); then `POST /api/repos/<id>/lazyaf/pipelines/<name>/run`
(`routers/lazyaf_files.py:230`) spawned a real container whose
`docker inspect` `Cmd` was the exact injected script. It failed only because the
attacker picked `alpine`, which has no bash; the default step image is
`python:3.12` (`config.py:340`), which does.

That route's own docstring says why it is the sharp one
(`routers/lazyaf_files.py:288-291`): it calls `start_pipeline` **directly** and
skips every gate `POST /api/pipelines/{id}/run` enforces.

**A framing in the brief is wrong and worth correcting:** the auto-trigger path
*is* gated on `repo.is_ingested` (`trigger_service.py:570`), which is False for
a freshly pushed repo — so a bare forged push does not by itself spawn a run.
That gate is not a security control. `POST /api/repos/ingest`
(`routers/repos.py:90,126`) and `POST /api/repos/{id}/test-setup`
(`routers/repos.py:260,314`) both set it to True with no credential, and the
`/lazyaf/pipelines/{name}/run` route skips it entirely. End state identical.

**S2 — the socket is the default.** `bind_mount_allowlist()`
(`local_executor.py:134-145`) reads `getattr(settings, "step_bind_mount_allowlist",
None)` and, when that is falsy, returns `(DOCKER_SOCKET_SOURCE,)`. The attribute
**does not exist** — `grep step_bind_mount_allowlist backend/app/config.py`
returns nothing — so the `getattr` is permanently `None` and the socket is
hardcoded into the default allowlist with no way to turn it off. The docstring
says "settings-driven when present (config.py is owned by a parallel change)";
the parallel change never landed, and because the fallback works nothing ever
failed loudly enough to reveal it.

Lane B proved what that buys, from inside a container holding only the socket:
started a sibling `--privileged` container, mounted `/` and printed
`/hostfs/etc/shadow`; then, *without* `--privileged`, mounted
`/run/desktop/mnt/host/c` and listed the owner's entire Windows `C:` drive
including `C:\Users\Brennan`. There is no container escape to perform here. The
socket **is** the host.

Note also `docker inspect lazyaf-backend-1`: the dev stack binds
`C:\projects\lazyaf\backend\app` into the backend rw (`docker-compose.yml:7`), so
backend RCE edits the owner's working tree.

**S3 — the owner's API key.** `AGENT_SECRET_ENV`
(`pipeline_executor.py:192-193`) maps `claude-code` → `ANTHROPIC_API_KEY`, and
`agent_secret_environment` (`:469-480`) pulls the value out of settings and
raises if unset. So an `agent`-type step in a pushed YAML is handed the
platform's key. Delivery is via the 12.5 file channel, which is the *right*
mechanism (§2.6) — but the step process can read its own config, so a hostile
step author reads the key. This is the cheapest route to the owner's Anthropic
quota in the whole review: no socket, no kernel bug, the platform posts the key
into the attacker's container by design.

**S4 — the multiplier.** Nothing in Tier 1 or Tier 2 matters much on a laptop
with the port on loopback, and all of it matters on a coffee-shop network. The
release stack interpolates `${LAZYAF_BACKEND_PORT:-8000}` straight into the
mapping, and `.env.example:49` ships a bare `8000`. `QUICKSTART.md:184-203`
already documents the fix — put a host IP in the port variable — under the
heading "Binding it to localhost", opening with "Neither compose file does this
for you." **The remedy is written down and is not the default.** That is the
single cheapest change in this document.

**S5 — the open API.** All 117 OpenAPI paths were enumerated live on `:8790`.
Every `/api/*` human router answers with no credential: repos, cards, pipelines,
pipeline-runs, experiments, features, criteria, user-stories, agent-files,
models, model-endpoints (except probe-result/proxy), prompt-templates, jobs,
debug, playground, diagnostics, test-refs. The consequences worth naming
individually, because each is a separate small fix if the big one slips:

- `POST /api/debug/{id}/join-token` (`routers/debug.py:251-274`) mints a
  terminal JWT with **no credential of its own**, for any session id, and
  session ids are enumerable from an equally open `GET /api/debug`
  (`routers/debug.py:233`). The socket itself correctly checks the token —
  the problem is that minting it checks nothing.
- `POST /api/jobs/{id}/callback` (`routers/jobs.py:159-171`) sets a job's
  status and error text with no credential. A passing suite can be reported
  failed, or the reverse.
- Playground `internal/*` (`routers/playground.py:316-347`) — four routes,
  none with an `Authorization` parameter, each returning `{"ok": true}`
  including for sessions that do not exist. The silent success is its own R1
  violation.
- `POST /api/diagnostics/bundle` returned **201 with a 140 KB bundle** to an
  anonymous caller on QA, including a 131 KB backend log. The *redaction* in
  that module is genuinely good and fails closed
  (`routers/diagnostics.py:34-40`); the module's own docstring (`:43-49`) is
  honest that it concentrates exposure rather than creating it. It is an
  argument for auth, not a separate workstream.

### Tier 2 — any step author (today: also anonymous)

| # | Finding | Where |
|---|---|---|
| **S6** | Step containers get full default caps, root on stock images, and passwordless `sudo` on lazyaf images | `local_executor.py:771-790`, `images/base/Dockerfile:33` |
| **S7** | No CPU limit, no PID limit, no fan-out cap, no timeout ceiling, uncapped container logs and uncapped log ingest | `local_executor.py:788-789`, `pipeline_executor.py:2031-2049`, `schemas/lazyaf_yaml.py:72` |
| **S8** | The step network is one flat trust zone: whole open API, git push, every sibling step, unrestricted egress | `local_executor.py:777`, `config.py:329`, `docker-compose.yml:28,38,98,136` |
| **S9** | No image allowlist — a hostile image name receives the step JWT and the platform API key | `pipeline_executor.py:3193-3196` |
| **S10** | Step logs and agent transcripts are written to SQLite unredacted | `routers/steps.py:238-276`, `services/execution/step_logs.py:78` |
| **S11** | The 0600 control file is not a boundary between parallel steps of one run | `local_executor.py:344`, `images/base/control/entrypoint.sh:38`, `workspace/state_machine.py:227-232` |
| **S12** | Step JWT lives 24h; the endpoint proxy and probe-result routes skip the terminal-state check | `control_layer/auth.py:34`, `routers/model_endpoints.py:769,826` |
| **S13** | The debug sidecar is an unhardened shell on the step network with the workspace rw | `services/execution/debug_terminal.py:321-349` |

**S6 deserves its own paragraph, because both prior lanes got half of it.**
`run_kwargs` (`local_executor.py:771-790`) is command / detach / volumes /
working_dir / environment / network / remove / labels, plus `mem_limit` at
`:788-789` *only if the step asked*. A real step container's `docker inspect`
confirms: `User=""`, `CapAdd=null`, `CapDrop=null`, `SecurityOpt=null`,
`ReadonlyRootfs=false`, `PidsLimit=null`, `NanoCpus=0`.

The half nobody flagged: **`images/base/Dockerfile:33` grants the `lazyaf` user
passwordless sudo** (`echo "lazyaf ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers`).
The gosu drop to uid 1000 at `entrypoint.sh:68-75` is therefore **not a
privilege boundary inside the container** — any step can `sudo -i` straight back
to root, and root holds the full default capability set. Two consequences that
shape the plan:

1. Never write "steps run as an unprivileged user" in docs or a UI. It is not
   true, and R1 forbids a control that lies about being on.
2. **Capabilities, not uid, are the boundary.** `cap_drop=ALL` binds root and
   uid 1000 alike, which is exactly why it is the first thing to ship (§3.2).
   It also means the sudo line is not urgent to remove — but adding
   `no-new-privileges` **will break `sudo`** in every lazyaf image, because
   sudo is setuid. That is a real, nameable breakage (§3.2), not a surprise to
   be discovered by a failing dogfood run.

**S7's paired requirement, which is the R1 half:** nothing in the tree reads
`OOMKilled` — grep across `backend/`, `runner-agent/` and `images/` returns
zero hits. Ship a default memory limit without that and an OOM surfaces as a
bare exit 137 with no explanation. A limit that degrades into a mystery failure
is worse than no limit, and it is the kind of thing an operator turns off. The
memory default and the OOM message land in the same change or neither does.

**S9's mechanism:** `pipeline_executor.py:3193-3196` takes `image` straight from
step config. The `lazyaf.agent-runtime=1` preflight
(`pipeline_executor.py:2234-2273`) is an image **author's** capability
declaration — `local_executor.py:73-77` says so — which catches a mis-pinned
image and does nothing to a hostile one, which simply bakes the label.

**S11's mechanism, stated exactly:** every step of a lane drops to the *same*
uid (`entrypoint.sh:66-72`), on the *same* volume
(`state_machine.py:227-232`, `lazyaf-ws-{pipeline_run_id}`), and all entry
points dispatch in parallel (`pipeline_executor.py:2031`). File mode 0600
defends against a different uid, and there is no different uid. A plain
`run: cat /workspace/.control/*.json` step running alongside an agent step
reads that agent's provider key in the window between container-create and
consume-once-delete.

**S12's second half, which Lane C found and is worth one line of code:**
`routers/model_endpoints.py` never calls `_reject_terminal_writes` at all —
neither the proxy broker (`:769`, auth at `:826`) nor `report_probe_result`
(`:528`, auth at `:571`). The five `/api/steps/*` write routes all call it
(`routers/steps.py:188,261,294,337,376`). A finished step has no legitimate
inference to make.

### Tier 3 — upstream compromise, or a genuinely remote runner

| # | Finding | Where |
|---|---|---|
| **S14** | Image builds pipe a remote script to `bash` as root and `npm -g` install with no version pin | `images/claude/Dockerfile:18-20,26`; base is a mutable tag, `images/base/Dockerfile:18` |
| **S15** | The backend container holds every platform credential in inspectable env; provider keys have no `_FILE` support | `docker-compose.release.yml:65-77`, `config.py:374-375` |
| **S16** | Release images are consumed by a moving tag — and `.env.example:42` ships `LAZYAF_VERSION=main`, which moves on every push to the default branch | `.env.example:42`, `docker-compose.release.yml:51,104,127` |
| **S17** | The runner refuses plaintext for the control channel but not for the step container's backend URL | `runner-agent/lazyaf_runner/config.py:307-315` vs `:317-319` |

`S15` is narrower than it looks and should be stated honestly: reading the
backend's env requires the docker socket or host access, and anything with the
socket is root on the host anyway (S2). Fixing it is still worth doing —
`config.py:14-23` already implements `<NAME>_FILE`-first resolution for the two
shared secrets, so giving the provider keys the same treatment is reuse, not new
machinery — but it is a defence-in-depth item, not an urgent one.

### 2.6 What is already right, and must not be redesigned

Recording these so no effort is spent re-litigating them.

- **The 12.5 secret channel.** `local_executor.py:654-660` splits `environment`
  from `secret_environment`; `:746-753` **refuses at dispatch** if a secret
  would have to travel in inspectable container env; `:318-346` builds a 0600
  tar in a 0700 dir delivered by `put_archive` to a created-but-not-started
  container; `images/base/control/run.py:663-676` and
  `runner-common/runner_common/agent_wrapper.py:476` unlink the config on every
  path including parse failure. A real container's `docker inspect` env is
  clean. That is a loud refusal instead of a silent downgrade — R1 done right.
- **The endpoint secret-ref allowlist.**
  `services/model_endpoints/secrets.py:41`,
  `^LAZYAF_ENDPOINT_[A-Z0-9_]{1,48}$`, anchored both ends, re-checked at use
  (`:137-141`) so a hand-written DB row cannot bypass the API gate. This is the
  house doctrine: **a credential is a reference in storage, resolved from a
  prefix-allowlisted variable at use time, `_FILE`-first, never a stored value.**
- **Fail-closed shared secrets.** `config.py` module docstring, `resolve_secret`,
  `MissingSecretError`, `RETIRED_PUBLIC_SECRETS` (`config.py:46-52`), and
  `scripts/bootstrap_secrets.py`, which never prints a value and never
  overwrites one. This is the pattern the human credential should extend, not
  replace.
- **The model-endpoint proxy's gate list** (`routers/model_endpoints.py:757-800`):
  reach check → step JWT → ownership → path allowlist → body cap → concurrency
  slot. The only properly authorised route in the platform, and the written
  template for §3.2.
- **The runner's deny-by-default bind allowlist**
  (`runner-agent/lazyaf_runner/config.py:220`, `bind_allowlist: tuple = ()`),
  with the reasoning written out at
  `runner-agent/lazyaf_runner/orchestrator/docker_orch.py:401-425`: a backend
  must not bind arbitrary host paths on a machine it does not own, and "the
  backend asked for it" is not authorization. **That argument applies verbatim
  to a repo the operator does not own**, and it is the whole case for flipping
  the backend's default (§3.1).
- **The CI secret-scan gate.** Both publish jobs in
  `.github/workflows/images.yml` declare `needs: secret-scan`; image-layer
  scanning runs before the push; every action is SHA-pinned;
  `persist-credentials: false`; no `pull_request_target`. It works and it works
  for stated reasons. One caveat: the canonical patterns now live in
  `backend/app/services/redaction/patterns.py` and reach CI through the
  `.github/scripts/secret_patterns.py` shim — **and both are currently
  uncommitted working-tree changes.** The gate's canonical rules are not yet in
  a commit.
- **The executor never pulls.** `local_executor` calls
  `containers.create(...)` and turns `ImageNotFound` into a step failure
  (`:1004-1011`); the only `.pull(` in the backend is a startup pre-pull of
  settings-driven images (`workspace/population.py:115`). So a hostile YAML can
  only name images already on the daemon. **That is a real containment property
  that arrived for free and nobody knows they have it** — write it down at
  `local_executor.py:1004` and pin it with a test before someone "fixes" the
  confusing error by adding a convenience pull.

---

## 3. The hardening ladder

Four rungs. Each is independently shippable and independently valuable. Nothing
in rungs 0–2 needs Redis, an identity provider, a KMS, or more than one uvicorn
worker.

### 3.1 Rung 0 — what a fresh install should default to

Free. No new mechanisms. Every item here changes a default the operator never
had to think about, in the direction the docs already recommend.

#### 0.1 Bind to loopback by default

**Edit:** `.env.example:49-50`

```
LAZYAF_BACKEND_PORT=127.0.0.1:8000
LAZYAF_FRONTEND_PORT=127.0.0.1:5173
```

and make `docker-compose.yml:5,35` interpolate the same variables with a
loopback default (`"${LAZYAF_BACKEND_PORT:-127.0.0.1:8000}:8000"`), plus hard
`127.0.0.1:` prefixes on the two dev-only publishes at `docker-compose.yml:135`
(mock endpoint, 8099) and `:157` (e2e backend, 8765).

**Do not add a `LAZYAF_BIND` variable.** Both prior lanes proposed one; it is
the wrong call under R3. The release compose already interpolates the port
variable straight into the mapping, so the variable *already* accepts a host IP
— `QUICKSTART.md:184-192` documents exactly that. Adding a second variable
creates two ways to express one thing, and a `LAZYAF_BIND=0.0.0.0` combined with
a `LAZYAF_BACKEND_PORT=127.0.0.1:8000` has no defensible meaning. One variable,
one meaning; rename the comment above it from "Host ports" to "Host bind
address and port", and move `QUICKSTART.md`'s "Binding it to localhost" section
to "Exposing it beyond localhost".

**Cost:** trivial. **What it breaks:** an operator who runs the stack on a
homelab box and browses from a laptop finds the UI unreachable after upgrading,
and will be annoyed. Mitigate with the remedy in the same comment as the
warning. This is the cheapest reachability reduction available anywhere in this
review and it is worth the annoyance.

#### 0.2 Empty the bind-mount allowlist

**Edits:**
- `backend/app/config.py` — add `step_bind_mount_allowlist: tuple[str, ...] = ()`
  near `:340` and wire it in `get_settings()` near `:387` from
  `LAZYAF_STEP_BIND_ALLOWLIST` (comma-separated).
- `backend/app/services/execution/local_executor.py:141-145` — return
  `()` by default, and change the read to `if configured is not None:` so an
  **explicit empty allowlist denies** instead of falling through to the socket.
  The current `if configured:` treats the deny-everything setting an operator
  would most want as "not configured" — a silent fallback, straight against R1.
- `.env.example` — document `LAZYAF_STEP_BIND_ALLOWLIST` next to the existing
  `LAZYAF_BIND_ALLOWLIST` runner line at `:217`, so the two lanes read the same.

`needs: [docker]` then raises the existing loud `ValueError` at
`local_executor.py:216-222`, whose message already names the setting.

**What it breaks — and this is the important one: LazyAF's own CI.**
`.lazyaf/pipelines/test-suite.yaml:125` and `:138` declare `needs: ["docker"]`
for the T2 and T3 tiers, and the YAML comment at `:113-119` already concedes the
tradeoff ("host-root-equivalent for these two steps only"). So this change makes
the owner set one variable in his own `.env` before his dogfood pipeline runs
again. **That is the feature.** The owner's own repo is the one repo that
genuinely needs the socket, because its T2 tests spawn containers to test the
executor. Every other repo does not, and today every other repo has it.

The existing test at
`tdd/unit/services/execution/test_local_executor_hardening.py:236-264` asserts
the socket **is** default-allowlisted. Inverting it is the acceptance test.

#### 0.3 Gate or delete the internal push-event route

`POST /git/{repo_id}.git/_internal/push-event` (`routers/git.py:258-262`) takes
no credential and calls `trigger_service.on_push`, so an anonymous caller can
forge a push and spawn containers **without pushing anything**. `git-receive-pack`
already fires `on_push` on the default path (`routers/git.py:201-212`), so the
route is redundant there. Per R2, delete only after acceptance: gate it behind
the runner/step secret now, delete it when nothing calls it.

#### 0.4 Stop the template tracking a moving tag

`.env.example:42` ships `LAZYAF_VERSION=main`, and the comment two lines above
says `main` is rebuilt on every push to the default branch. That is worse than
the compose default of `latest`, because it is the shape an operator copies.
Ship a pinned release tag in the template and make `main` the opt-in. While
there: print the running image digest in `GET /api/diagnostics/system` next to
the container ages — one `docker inspect` field, and it makes "what am I
running" answerable in the place an operator already looks.

### 3.2 Rung 1 — the cheap high-value controls

#### 1.1 One hardening spec, applied at every spawn site

**The R3 problem first, because it decides whether the rest stays fixed.** Four
sites build container config independently: `local_executor.py:771-790`,
`runner-agent/lazyaf_runner/orchestrator/docker_orch.py:427-466`,
`workspace/population.py:199-217` (the clone helper, root, no limits), and
`services/execution/debug_terminal.py:321-349` (the debug sidecar). They already
disagree — the runner denies binds by default, the backend allows the socket;
the runner refuses `network_mode=host` with a written justification
(`docker_orch.py:10-12`), the backend never had to. Ship `cap_drop` in
`local_executor` only and the remote lane silently stays wide, which is the
worst outcome available: release notes say steps are confined and half of them
are not.

**Put the spec in `runner-common`** — both sides already depend on it
(`runner_common/agent_config.py` is imported by both) — as one dataclass holding
caps, `security_opt`, pids/cpu/memory defaults, `read_only`, tmpfs and
`log_config`. All four sites build kwargs through it.

**The spec, tested on the owner's own daemon by Lane B:**

```
cap_drop     = ["ALL"]
cap_add      = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"]
security_opt = ["no-new-privileges"]
pids_limit   = 512
nano_cpus    = 2.0 CPUs
mem_limit    = "2g"          # default, per-step override upward
log_config   = {"type": "json-file", "config": {"max-size": "64m", "max-file": "3"}}
```

Verified working:
`docker run --rm -v lb-b:/workspace --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID --cap-add DAC_OVERRIDE --security-opt no-new-privileges lazyaf-agent-base:dev sh -c 'id; python3 -c "print(1+1)"'`
→ `uid=1000(lazyaf)`, `CapEff 0000000000000000`, workspace writable.

The four caps are the **minimum**, not a guess: `--cap-drop ALL` alone fails the
entrypoint with `mkdir: cannot create directory '/workspace/home/.cache':
Permission denied` because the image bakes `/workspace/home` owned by uid 1000
and the root entrypoint needs `DAC_OVERRIDE` to write into it, plus
`SETUID`/`SETGID` for the gosu drop and `CHOWN` for `entrypoint.sh:23-35`.

**What it breaks, named up front:**

- **`sudo` stops working inside every lazyaf-* image**, because
  `no-new-privileges` blocks setuid escalation and `images/base/Dockerfile:33`
  grants passwordless sudo. Any step script that runs `sudo apt-get install`
  fails. Decide deliberately: either keep sudo and drop `no-new-privileges` for
  `script` steps (the cap drop still binds root, so most of the value survives),
  or ship `no-new-privileges` and remove the sudoers line in the same change so
  the failure reads as "sudo is not available in step containers" rather than a
  mystery. Do not leave a sudo binary that fails for unexplained reasons.
- **`apt-get install` inside a step is uncertain under the four-cap set.** apt
  wants `FOWNER` and `FSETID` for some packages. **Verify with a real
  `apt-get install -y build-essential` in `lazyaf-agent-base` before defaulting
  the drop for `script` steps**; if it needs them, add `FOWNER`/`FSETID` and say
  so in the spec's comment. This is the one number in the plan I am not
  confident about.
- **`read_only` rootfs is NOT in the Rung 1 spec** on purpose — it breaks
  `apt-get` and the socket group-join at `entrypoint.sh:49-57` (which runs
  `usermod`, writing `/etc/group`). It moves to Rung 2 as a per-step flag.
- **The memory default requires the OOM message in the same change.** Read
  `.State.OOMKilled` on the finished container and report
  `killed: exceeded memory_limit 2g` in the step result. Sizing rationale,
  stated honestly: 512 PIDs is comfortable for `npm ci` and `make -j`, 256 is
  tight enough to bite a real build; 2 CPUs is where a big pytest suite stops
  feeling slower than before; 2 GB is the floor a real build needs — 512 MB
  OOMs webpack and most JVM builds.

**Acceptance:** extend the existing T2 test that greps a created container's
`docker inspect` (the one referenced at `local_executor.py:656-659`) to assert
the hardening fields, rather than writing a new harness.

#### 1.2 One human credential, audit-then-enforce

**Design: extend the house shared-secret pattern. Do not invent a new one.**

- `backend/app/config.py` — `api_auth_secret: str`, resolved via
  `resolve_secret("LAZYAF_API_SECRET")` next to the other two at `:390-391`.
  Required, no default, same fail-closed startup.
- `scripts/bootstrap_secrets.py:78` — add it to `MANAGED_SECRETS`. The helper
  already generates, never overwrites, never prints, and is already mandatory in
  `QUICKSTART.md:32,57`. **The operator-facing cost of this entire feature is
  zero new steps.**
- One ASGI dependency in front of the human routers, accepting either
  `Authorization: Bearer <secret>` (CLI, curl) or a cookie signed with that
  secret (browser). A minimal login page POSTs the token once and sets the
  cookie. `itsdangerous` or the JWT lib already in the tree — no DB, no Redis,
  fine under single-worker uvicorn.
- **Git smart-HTTP uses git's native HTTP Basic** against the same secret
  (`http://lazyaf:<token>@host/git/...`), so `git push` is covered by the same
  value rather than a second concept.
- **Exempt the two machine planes** — the step-JWT routes (`routers/steps.py`,
  the endpoint proxy and probe-result) and the runner-secret routes (`/ws/runner`).
  They authenticate themselves.

**Migration, and why it is not a flag day (§1.4).** Stage 1: `LAZYAF_REQUIRE_AUTH`
defaults to `audit` — the middleware logs `would reject: <path>` and allows the
request, while the operator provisions the token and the CLI learns
`lazyaf config set token`. Stage 2, next release: the default flips to `enforce`.
Because the runner and step planes carry their own credentials, turning human
auth on never breaks a running runner or an in-flight step callback.

**Adoption:** this is the option a solo operator actually turns on — one `.env`
value that mirrors `LAZYAF_RUNNER_AUTH_SECRET`, generated for them, fail-closed
so it cannot be half-configured. Ranked against the alternatives: a local
password hash is slightly better UX and slightly more code; reverse-proxy basic
auth is zero backend code but does not uniformly cover git HTTP and offloads the
config to the operator, so document it as an optional extra layer, not the
primary control; **OIDC is rejected** (§4).

**This one credential closes S1, S5, and every route in the S5 bullet list at
once.** It is the spine of the plan.

#### 1.3 Redact on the step-log write path

`services/execution/step_logs.py:78` `append_step_logs` is already the R3 single
writer for both the HTTP route and the runner WebSocket frame — **one call
site**. Build a `Redactor` once per process from
`app.services.redaction.discovery.discover_known_secrets()` (pass-1 known
literals only — a substring scan over four values, not a full pattern sweep) and
apply it to the batch before the SQL append. Fail closed the way
`routers/diagnostics.py:34-40` does: if the redactor cannot be built, refuse the
append rather than writing raw.

**Why it matters more than it sounds:** an agent step's stdout is its entire
transcript — the rendered prompt, every file it read, every shell command and
its output. `runner_common/harness/transcript.py` holds that in memory and it
reaches the database **only** through this path. One `env` in a step script
writes `ANTHROPIC_API_KEY` into `lazyaf.db` in cleartext, permanently, and into
every plain-file backup, and then serves it to any unauthenticated caller. Fix
this one call site and the SQLite-leak story goes from *catastrophic* to
*embarrassing*.

Pair it with a per-step byte budget on `POST /api/steps/{id}/logs` that returns
413 — `LogsRequest` (`routers/steps.py:94-98`) has no length bound today, so a
chatty loop fills SQLite using only its own legitimate token, which survives
every auth fix in §1.2.

#### 1.4 Token hygiene — three small edits

- **TTL:** `control_layer/auth.py:34` `DEFAULT_EXPIRATION_SECONDS = 86400`
  against a default step timeout of 300s. Pass `expires_in_seconds` from the
  step's own timeout plus the executor grace at the call site
  (`pipeline_executor.py:3731`); the dispatcher already knows both
  (`local_executor.py:653`, `:113-118`).
- **Terminal check:** add `_reject_terminal_writes(execution)` after
  `verify_step_auth` in `routers/model_endpoints.py` at `:826` (proxy) and
  `:571` (probe-result). Do **not** build a revocation list — SQLite plus
  single-worker means the `StepExecution` row *is* the revocation list, and this
  is how you read it.
- **`typ` claims:** `runner_token.py:36-38,99-106` already added
  `TOKEN_TYPE = "runner"` precisely so a validly-signed token of the wrong kind
  is refused. The debug join token (`routers/debug.py:99`) has no `typ`. Add
  `"typ": "debug-join"` and check it. Three token types, one house pattern,
  currently two of three implementing it.

#### 1.5 Put the orphan machine-routes on a machine credential

`POST /api/jobs/{id}/callback` (`routers/jobs.py:159`) and the four playground
`internal/*` routes (`routers/playground.py:316-347`) are runner→backend calls
wearing no credential. Give them the step JWT / runner secret like every other
machine route — **not a fifth mechanism**. If the playground is genuinely on the
12.6 deletion list, R2 says delete it after the default path no longer needs it;
in the meantime the one-line honesty fix is that an unknown session id returns
404 instead of `{"ok": true}`.

#### 1.6 Move the control file off the shared volume

S11 has no fix via file modes, because there is no second uid to lean on. The
small fix: `put_archive` the step/agent config to a container-private path
(`/run/lazyaf/<id>.json` on a per-step tmpfs) instead of
`/workspace/.control/`, and point the existing `CONFIG_PATH` /
`AGENT_CONFIG_PATH_ENV` constants at it. The config is consume-once anyway, so
nothing needs it to persist, and a tmpfs removes the sibling-read window
entirely. Alternative if that seam is contested: give a step declaring
`secret_environment` its own workspace lane —
`generate_volume_name` already takes a worker key
(`workspace/state_machine.py:227-245`). The tmpfs option is smaller and does not
change workspace semantics.

**Acceptance test:** a sibling step that greps the shared volume finds nothing,
run alongside the existing `docker inspect` env test.

### 3.3 Rung 2 — real isolation work

#### 2.1 Split the step network

Today every step joins `settings.container_network` (`local_executor.py:777`,
`config.py:329`) — the same bridge as the backend, the frontend, the git server
and the mock endpoint (`docker-compose.yml:28,38,98,136`), with ICC on. Lane B
confirmed live from a container on that network: `GET /api/repos` → 200,
`GET /api/debug` → 200, `https://example.com` → 200, and a plain-HTTP
`git push` to the internal server → exit 0. **A step is currently inside the
admin API.**

Put steps on their own bridge that does **not** contain the backend, the
frontend or the git server, and expose only what a step legitimately needs — the
step API and the git server — through a narrow reverse proxy on that network
that requires the step JWT. One compose change plus a small proxy. Create the
step network with `com.docker.network.bridge.enable_icc=false` so parallel steps
cannot reach each other; nothing legitimate breaks, because steps integrate
through git (M13-1).

This is the single biggest reduction in what an injected instruction can reach,
and it does not depend on §1.2 landing.

#### 2.2 Image allowlist

Settings-driven `LAZYAF_STEP_IMAGE_ALLOWLIST`, prefix or exact match, defaulting
to the `lazyaf-*` images the platform ships **plus** the common official
language bases (`python:*`, `node:*`, `golang:*`). Refuse anything else loudly,
naming the setting — the same shape as the bind-allowlist refusal at
`local_executor.py:216-222`. This is M14's doctrine applied to images: the
platform decides what names are legal, the repo only picks from them.

**Adoption cost is real:** `image: node:20` is a normal thing to write. Default
generously, make it one variable to extend, and make the refusal say which
variable to edit. The prize is that a repo can no longer choose the process that
receives the platform's API key.

#### 2.3 Read-only rootfs and tmpfs, per step

`read_only=True` plus `tmpfs={"/tmp": "rw,size=64m"}`, **defaulting on for agent
steps and off for `script` steps** until the base images carry what builds need.
Breaks `apt-get install` inside a step and the socket group-join at
`entrypoint.sh:49-57`. Separately toggleable from the cap drop, which is why it
is not in Rung 1.

#### 2.4 Fan-out cap and timeout ceiling

`pipeline_executor.py:2031-2049` dispatches all entry points in parallel with no
semaphore; grep finds per-model-endpoint and per-experiment caps but nothing over
step containers. `timeout` is `Field(300, ...)` with no `ge`/`le`
(`schemas/lazyaf_yaml.py:72`), so a pushed YAML can say 86400, and there is no
`MAX_STEPS`. A hostile YAML with 200 entry-point steps at `timeout: 86400` is
200 unlimited containers held for a day, from one push.

Add a global in-flight step semaphore (default 4, settings-backed, **queue
visible in the UI** so it does not look like a hang) and a `STEP_MAX_TIMEOUT`
ceiling (default 3600) that clamps loudly. The semaphore is the item most likely
to be raised by an operator who set up parallel steps to go faster — that is
fine, as long as raising it is one variable.

#### 2.5 Decompose `needs: [docker]` instead of fencing it

What a step actually wants the socket **for** is two things:

- **`docker build`** → a **rootless BuildKit sibling**, one per run, reachable
  only by that run's steps. Verified working on the owner's daemon with **no
  `--privileged`**:
  `docker run --rm -d --security-opt seccomp=unconfined --security-opt apparmor=unconfined --device /dev/fuse moby/buildkit:rootless --oci-worker-no-process-sandbox`
  came up clean. This is the only option that **removes** the socket rather than
  fencing it.
- **`docker compose up` of test dependencies** → a `services:` key in the step
  YAML (the GitHub Actions shape), spawned **by the backend**, which is where the
  privilege belongs.

**LazyAF's own T2/T3 tiers are the exception that proves the rule**
(`.lazyaf/pipelines/test-suite.yaml:125,138`): they spawn containers *in order to
test the executor*, so neither decomposition covers them. That is fine — the
owner's own repo opts in via §0.2's variable. It is also the cleanest argument
for the per-repo trust tier in Rung 3: the repo that needs the socket is the one
repo whose author is the operator.

For whatever residual case remains, the only socket proxy worth building is a
**body-validating** one. An API-path allowlist (tecnativa-style) buys nothing
here: the backend must be able to `POST /containers/create`, and that call with
an arbitrary `HostConfig` **is** the escape. The proxy must reject the body —
`Privileged`, `CapAdd`, `SecurityOpt`, host `PidMode`/`IpcMode`/`NetworkMode`,
`Devices`, and any `Bind` whose source is not a `lazyaf-ws-*` volume. That is
roughly 150 lines and it is the only version with a point.

#### 2.6 `_FILE` support for provider keys, and pinned image builds

- `config.py:374-375` — give `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` the same
  `<NAME>_FILE`-first resolver the shared secrets already use (`config.py:14-23`
  — reuse it, do not write a second), and ship the release compose with a docker
  `secrets:` block as the documented shape while inline env stays supported.
  **Do not make `_FILE` mandatory**; that drops adoption to zero.
- `images/base/Dockerfile:18` — digest-pin `python:3.12-slim`, tag in a trailing
  comment, matching the SHA-pin convention `.github/workflows` already uses.
- `images/claude/Dockerfile:26` — pin `@anthropic-ai/claude-code@X.Y.Z` and add
  `--ignore-scripts` if the package tolerates it. This pays twice: an unpinned
  agent CLI is also a reproducibility problem for M13 experiment attribution.
- `images/claude/Dockerfile:18-20` — replace the
  `curl … | bash -` nodesource pipe with the distro `nodejs`, or download,
  checksum against a recorded value, then run. If any of these three is going to
  rot, keep the first two and skip this one.

### 3.4 Rung 3 — what would be needed before running untrusted repos

This rung is the entry condition for `upcoming/shadow-ci.md`. Nothing below is
worth building until rungs 0–2 are in.

1. **A per-repo trust tier, set by the operator, never by the repo.** At minimum
   `trusted` (may define pipelines from its own YAML, may request capabilities,
   may consume the platform API key) and `untrusted` (may not define pipelines
   — the operator's platform pipeline runs *at* it; may not request `needs:`
   capabilities; may not consume the platform key). `shadow-ci.md` §4.1 already
   refuses to run a mirrored repo's own YAML — this is that refusal generalised
   and given a home in the data model.
2. **Egress control for untrusted steps.** A third network with `internal: true`
   plus a per-step `network: none | internal | egress`. **Ship it as opt-in with
   `egress` as today's default** — `pip install` and `npm ci` need egress, and a
   default-blocking control gets blamed for the first broken build and switched
   off globally. Flip the default for `untrusted` repos only, once the tier
   exists and a package cache/proxy exists.
3. **Budget and quota per repo:** max concurrent steps, max steps per run, max
   wall-clock, max tokens/spend for agent steps. Untrusted repos spending the
   owner's Anthropic quota is the failure mode that ends the experiment.
4. **A separate key path for untrusted agent steps** — a per-repo endpoint or a
   per-repo key, never `settings.anthropic_api_key`
   (`pipeline_executor.py:469-480`).
5. **The body-validating socket proxy (§2.5), or moving container spawn out of
   the backend into a small non-root broker.** With untrusted repos in the
   picture, backend RCE stops being a theoretical concern.
6. **A decision on secret-at-rest, written down.** Either the M14 doctrine
   generalises (nothing sensitive is stored; everything is a reference) or the
   data directory is documented as being exactly as sensitive as the secrets
   configured. **Do not build a fake vault** (§4).

---

## 4. What NOT to do, and why

**gVisor / `runsc`.** Measured, not assumed: the owner's daemon reports runtimes
`io.containerd.runc.v2`, `nvidia`, `runc`. No `runsc`, and Docker Desktop does
not ship it. Installing it under WSL2 is not something a solo operator will do.
Drop it from the plan rather than listing it as future work.

**userns-remap.** Not enabled on this daemon (`docker info` `SecurityOptions`
has no `name=userns`), not supported by Docker Desktop, and it would break more
than it fixes even where available: the platform depends on a stable uid map —
`entrypoint.sh:23-35` chowns to uid 1000 and `workspace/population.py:137-145`
pins ownership — and userns-remap breaks named-volume ownership.

**Custom AppArmor / SELinux profiles.** No policy is loaded on Docker Desktop
(`SecurityOptions` has neither). `runc`'s advertised feature list claims support,
but the runtime being capable and the daemon having a policy loaded are
different things, and only `SecurityOptions` is evidence. A profile that is
silently inert on the primary target is a control that lies about being on —
R1 forbids exactly that. Document it as an advanced Linux-only note; never
default it, and never let the UI claim it is active.

**Firecracker / microVMs.** A different product. Not a Docker Desktop option.

**Rootless podman / rootless Docker as the containment story.** Not present on
this daemon. Fine as a Linux-only note; not the plan.

**OIDC or any external identity provider.** A design requiring an IdP to run a
hobby CI stack will not be adopted, and an unadopted control is worth nothing.
The shared-secret + signed-cookie design (§1.2) is one `.env` value the operator
already understands, generated for them by a script they already run.

**Per-user accounts, roles, RBAC.** There is no user model anywhere in the data
model. Adding one to solve "the API is open" is a large half-built feature where
a single credential does the whole job. Revisit if and when LazyAF has more than
one human operator.

**An API-path-allowlist docker socket proxy.** The backend needs
`containers create/start/kill/stop/wait/logs/remove/list/put_archive/exec`,
`images get/pull`, `volumes create/get/list/remove`, `networks get/create`,
`events` — enumerated from the call sites. It must be able to
`POST /containers/create`, and that call with an arbitrary `HostConfig` is the
escape. Only body validation narrows anything (§2.5).

**Docker-in-Docker (`docker:dind`) as the `needs: [docker]` replacement.** It
requires `--privileged`. Same power through a longer pipe.

**Digest-pinning the release compose file.** It breaks `docker compose pull` as
an upgrade path and a solo operator will not maintain it. Pin the *template's
version tag* instead (§0.4) and surface the running digest in diagnostics.

**Removing `--dangerously-skip-permissions` / `--yolo`.** That is what makes an
autonomous agent step work at all. Removing it turns the product off. Contain
the container and the network instead, and say so in the docs.

**Default-on egress blocking.** The first failed `pip install` gets blamed on it
and it is disabled globally, taking the cases where it *would* have helped with
it. Opt-in per step now; default-on per *untrusted repo* later (§3.4).

**Encrypting the SQLite file with a key stored next to it.** Theatre. It would
also contradict the honest M14 refusal to store model API keys at all
(`models/model_endpoint.py`, `services/model_endpoints/secrets.py:41`), which is
the better answer and the house precedent.

**An SBOM pipeline.** Not the marginal risk here and nobody will maintain it.
Three pinned lines (§2.6) get most of the value.

**Trusting `repo.is_ingested` as a security gate.** It is a "has content" flag
that two unauthenticated routes set to True (`routers/repos.py:126,314`) and
that a third route bypasses entirely (`routers/lazyaf_files.py:230`). Do not
build on it.

---

## 5. Draft `SECURITY.md`

Proposed for the repo root (there is none today). Written to be edited and
committed, not admired. It is deliberately blunt: every claim below is one this
review verified, and the "does not defend" section is the part that keeps people
safe until Rung 1 lands.

---

```markdown
# Security

## What LazyAF is

A self-hosted CI/CD platform whose pipeline steps can be AI agents. It hosts its
own git server, and a push to it triggers it. Steps run in Docker containers
that the backend creates through a mounted docker socket.

Executing code you did not write is the product, not a side effect.

## The deployment shape it assumes

One operator, one trusted network, one host. A single-worker uvicorn process, a
SQLite database, and a docker socket. **Everyone who can reach the port is
assumed to be the operator.** There is no user model, no roles, and no tenancy,
and nothing in the codebase pretends otherwise.

## What LazyAF defends against

- **Credential leakage into places credentials get pasted.** The bug-report
  bundle redacts before you attach it. Model-endpoint API keys are never stored
  in the database — only a reference to an allowlisted environment variable.
  Endpoint error fields are scrubbed on write.
- **Provider keys appearing in `docker inspect` of a step container.** They are
  delivered through a 0600 file that the step runtime consumes and deletes; if a
  secret would have to travel in inspectable container environment, dispatch
  refuses instead of downgrading.
- **Plaintext runner enrollment.** A runner refuses `ws://` to a non-loopback
  backend without an explicit override, and refuses the retired public
  development secret by value.
- **Half-configured secrets.** The shared secrets have no defaults. The process
  does not start without them, and known-public historical values are rejected
  exactly as an empty value is.
- **Publishing a leaked credential.** CI refuses to publish an image or a
  release if the tree or the built image layers contain a live-format
  credential.

## What LazyAF explicitly does NOT defend against

**The API has no authentication.** Every human-facing route is open to anyone
who can reach the port: read every repo, card, pipeline and model endpoint;
create a diagnostics bundle summarising the whole system; mint a debug terminal
token; push to the git server; trigger a pipeline.

> Do not expose this to the internet. Do not expose it to a network you do not
> control. If you need it reachable, put it behind a reverse proxy that
> authenticates, and understand that you are the one providing the
> authentication.

**A pipeline file in a repo is arbitrary code on your machine.** Anyone who can
push can run a step, and a step can request the docker socket, which is root on
the host. Only host repositories you would grant a shell to.

**Agent steps are not sandboxed from their own input.** Agents run with
permission prompts disabled — that is what makes them autonomous — so an
instruction hidden in a card description or a repo file is executed with the
same authority as one you wrote. That authority includes the step's own API key,
the repository, the backend, and the internet.

**There is no secret-at-rest story.** The database is a plain SQLite file,
backups are plain files, and step logs are stored verbatim. If a step prints a
credential, that credential is in the database. Treat the data directory as
being as sensitive as the secrets you configured.

## If you ignore one thing here, do not let it be this one

**Bind the ports to localhost.**

```
# .env
LAZYAF_BACKEND_PORT=127.0.0.1:8000
LAZYAF_FRONTEND_PORT=127.0.0.1:5173
```

Confirm it took:

```
docker compose -f docker-compose.release.yml config | grep -A1 host_ip
```

No `host_ip` line means the port is open to your whole network.

## Reporting a vulnerability

Email <CONTACT>. Please do not open a public issue with a diagnostics bundle
attached until you have read it — automatic redaction removes credentials, not
source code, prompts, or a customer's name.
```

---

## 6. Sequencing against what is already planned

### 6.1 Right now, before anything else

Rung 0 in full (§3.1). It is four small edits, it requires no design, and it
moves the platform from "defaults expose an unauthenticated RCE to the LAN" to
"defaults expose it to the local host". `0.2` is the one that will be felt,
because it makes the owner opt his own dogfood pipeline back into the socket —
one variable, and the right thing to be conscious of.

### 6.2 Phase 12.8 (in progress) — no interaction

12.8 retires the v1 array format. Nothing in this plan touches the wire format.
Land 12.8 first if it is close; Rung 0 can go in alongside it.

### 6.3 Phase 14.5 — runners on a Windows desktop

`PLAN.md:173` and `upcoming/wave9-145-runner-images.md`: 14.5 puts runner agents
on a Windows desktop with idle RTX cards, over Docker Desktop / WSL2.

**Prerequisite: Rung 1.1 (the shared hardening spec) must land before 14.5
ships.** The reason is `runner-agent/lazyaf_runner/orchestrator/docker_orch.py:427-466`
— the second spawn site. 14.5 multiplies the number of hosts running step
containers; shipping it while only `local_executor` is hardened means the
platform's containment posture becomes *whichever executor happened to route the
step*, which is the silent-degrade shape R1 exists to prevent.

**Also relevant to 14.5, already measured:** every control in Rung 1.1
(`cap_drop`, `cap_add`, `no-new-privileges`, `pids_limit`, `nano_cpus`,
`mem_limit`, `log_config`) works on Docker Desktop / WSL2. gVisor,
userns-remap, AppArmor and rootless do not (§4). **Do not design 14.5's
isolation story around anything in that second list.**

**Rung 1.2 (human auth) is not a 14.5 blocker** — runner enrollment is on its
own credential — but 14.5 makes the loopback default (§0.1) awkward, because a
runner on another desktop must reach the backend. That is the moment
`LAZYAF_BACKEND_PORT=0.0.0.0:8000` gets set, and it is exactly the moment the
API must not be open. **Practically: 14.5 and Rung 1.2 should ship in the same
release**, or 14.5's docs must say "set the bind wide only behind a reverse
proxy you authenticate".

`S17` (`runner-agent/lazyaf_runner/config.py:317-319`, no scheme check on the
step container's backend URL) is a 14.5 item too: the control channel refuses
plaintext to a non-loopback host and the data channel — which carries the step
JWT and the step's secret environment — does not. Same rule, same flag,
`is_loopback` helper already exists. One line, and 14.5 is when it starts
mattering.

### 6.4 Shadow CI — mirroring repos the operator does not own

`upcoming/shadow-ci.md` is the milestone that changes the threat model rather
than the exposure. Its own §4.1 already refuses to run a mirrored repo's YAML,
and its `refs/upstream/*` namespacing argument — that a security boundary whose
enforcement is "a string field happens not to equal another string field" is not
a boundary — is exactly right and generalises to everything in Rung 3.

**Prerequisites, stated as a gate:**

| Shadow CI needs | Rung |
|---|---|
| Human auth, enforced (not audit) | 1.2 |
| Empty bind allowlist, socket opt-in per operator | 0.2 |
| Container hardening at both spawn sites | 1.1 |
| Redaction on the log write path | 1.3 |
| Step network split off the admin network | 2.1 |
| Image allowlist | 2.2 |
| Fan-out cap, timeout ceiling, per-repo quota | 2.4 + 3.4(3) |
| Per-repo trust tier | 3.4(1) |
| Platform API key never reachable by an untrusted repo's steps | 3.4(4) |

Shadow CI is where "single-operator tool on a trusted network" stops being a
sufficient posture, because the *code* stops being the operator's even when the
*network* still is. Everything in Rung 3 exists for that transition and for
nothing else — which is why Rung 3 should not be built early.

### 6.5 If only three things get done

1. **Rung 0.1** — bind to loopback in `.env.example`. Two lines. It changes the
   reachability of every Tier 1 and Tier 2 finding at once.
2. **Rung 1.2** — one shared-secret human credential, audit-then-enforce. It
   closes S1, S5 and every open route in the S5 list, and it is the prerequisite
   for both remaining milestones.
3. **Rung 1.3** — redact on `append_step_logs`. One call site, no operator
   surface, and it is the difference between a leaked SQLite file being
   embarrassing and being a credential compromise.

Rung 1.1's cap drop is a close fourth and is the only item on this list that
helps even if the authentication work never lands, because it is the one control
a step's own author can trip without any credential at all.

---

## 7. Verification notes

**What was executed, and where.** All live probing ran against the QA sandbox on
`:8790` (test-mode on) or as standalone containers. The owner stack on `:8000`
saw exactly one read-only `docker inspect` of `lazyaf-backend-1` and nothing
else; nothing there was restarted, rebuilt or deleted. The forged-push chain
(S1) was run to a real container spawn on QA and cleaned up: the probe repo was
deleted (204) and the spawned container removed. The socket-escape proof (S2)
mounted the host filesystem **read-only** by choice; `:rw` is the same command.

**Artifacts left on the host by the review:** docker images `docker:cli`,
`alpine` and `moby/buildkit:rootless` were pulled and are new. Harmless; prune
if unwanted. Scratch volumes `lb-a`, `lb-b`, `lb-probe1`, `lb-probe2` were
removed.

**Confirmed by code reading only, not by execution** — each is worth one T2 test,
and those tests are the acceptance criteria for their fixes:

- S11's parallel-step read window (the window is bounded by container start and
  consume-once; it was reasoned, not raced).
- S12's zombie-token replay against the proxy broker.
- S3's key delivery to an agent step (the mapping and the settings read were
  read at `pipeline_executor.py:192-193,469-480`; no agent step was dispatched
  with a real key).

**The one number in this plan I am not confident about:** whether the four-cap
set (`CHOWN`, `SETUID`, `SETGID`, `DAC_OVERRIDE`) is sufficient for
`apt-get install` inside a `script` step. It is sufficient for the entrypoint,
the gosu drop and a Python workload — that was tested. Package installs may want
`FOWNER`/`FSETID`. **Run a real `apt-get install -y build-essential` under the
spec in `lazyaf-agent-base:dev` before defaulting the drop for `script` steps**,
and record the answer in the spec's comment rather than guessing generously.

**A working-tree observation, not a finding:** the redaction package
(`backend/app/services/redaction/`) and the CI shim
(`.github/scripts/secret_patterns.py`) that §1.3 and the secret-scan gate both
depend on are **uncommitted** at the time of review. The canonical rules the CI
gate loads are not yet in a commit.
