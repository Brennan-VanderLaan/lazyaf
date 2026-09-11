# Shipping and the happy path — can a stranger install this today?

**Status:** review complete. No code changed. Nothing committed, pushed, tagged
or triggered (one exception, disclosed in §8).
**Owns:** this file.
**Scope:** the published artifacts at `ac777bc` — the repo, the `lazyaf` CLI,
and the nine GHCR images — judged against the question "can a person who has
never seen this project get a *working* LazyAF from what is published."
**Method:** four lanes walked the path for real against published images, then
an adversarial pass tried to refute every finding. Several did not survive;
three were killed outright and are recorded in §7 so nobody re-files them.
**Companion docs:** `upcoming/security-posture.md` (Rung 0 landed at `ac777bc`
and changed the happy path; §2 step 3 here is the regression it left behind),
`.github/WORKFLOWS.md` (the release-CI reference this report keeps pointing at).

---

## 1. The verdict

**Yes. A stranger can install LazyAF today and reach a green pipeline run using
only published artifacts — no source build required.** A lane walked it cold:
clone, `python scripts/bootstrap_secrets.py`, `docker compose -f
docker-compose.release.yml pull` (32s), `up -d` (backend healthy first try,
migrations `0001`→`0015` ran from inside the image), pull-and-retag the six step
images, `pip install ./cli`, `lazyaf ingest`, `git push lazyaf`, and the push
trigger fired a step container that came back `passed`. Nothing was built from
source. **There are zero BLOCKS_INSTALL gaps.**

Three qualifications, and they are the report:

1. **The path works on exactly one tag.** `LAZYAF_VERSION=main` resolves on all
   nine images and is current (`main` == `edge` == `sha-ac777bc` by digest).
   `latest` 404s on all nine — verified live today, `backend:latest -> HTTP 404`
   against the anonymous GHCR token endpoint — yet `latest` is the built-in
   default in `docker-compose.release.yml:51,107,130` and in
   `scripts/preflight.py:35`. No version tag exists at all.
2. **The third documented step tells a correctly-configured user they are not
   ready, and its advice reopens the hole Rung 0 just closed** (§3, B1). This is
   the one regression I would fix before anything else.
3. **Nothing in CI has ever started the release stack**, so gaps 1 and 2 — and
   the backend image's inability to boot without PyPI — were invisible to every
   green run that published them (§3, A2).

The honest one-line answer to the owner's question: *the artifacts are all there
and they work; the guardrails around them are the part that is not finished.*

---

## 2. The happy path as it actually runs

Steps are `QUICKSTART.md`'s own numbering. `[OK]` means a lane did it against
published images and it worked.

| # | Step | Result |
|---|------|--------|
| 1 | `git clone` | `[OK]` |
| 2 | `python scripts/bootstrap_secrets.py` | `[OK]` — best part of the install |
| 3 | `python scripts/preflight.py` | **BREAK — B1.** Exits 1, `NOT READY`, on a perfect config |
| 4b | `compose -f docker-compose.release.yml pull` | `[OK]` 32s — **but see A1, A5** |
| 4b | `up -d` | `[OK]` backend healthy, migrations to head |
| 5 | pull + retag six step images | `[OK]` — **but see D2 on upgrade day** |
| 6 | `pip install ./cli` | `[OK]` on Python 3.10.11, the declared floor |
| 7 | `lazyaf ingest` → `git push lazyaf` | `[OK]` git server accepts the push |
| 8 | push trigger → step container | `[OK]` **green run** |
| 9 | agent card, no API key | `[OK]` — best refusal message in the product |
| 10 | step with `needs: [docker]` | **BREAK — B2.** Refusal is perfect; its remedy is unreachable |

### Step 2 — `bootstrap_secrets.py` is the thing that makes this work

It seeds `.env` verbatim from `.env.example`
(`scripts/bootstrap_secrets.py:371-385`), which is why the user inherits
`LAZYAF_VERSION=main` at `.env.example:42` instead of falling through to the
`latest` that does not exist. It generates both mandatory secrets, prints
neither value, is idempotent, works standalone in a flat directory, and ends by
naming the next action. Two secrets have no defaults and the process refuses to
boot without them — the answer to the brief's question "is the user told before
they hit it" is **yes**, `bootstrap_secrets.py` is mandatory step 2 and it
handles them before the user can reach the wall.

### Step 3 — the break: preflight rejects the ports `.env.example` ships

Run on a clean clone with the `.env` that step 2 just wrote:

```
[FAIL] LAZYAF_BACKEND_PORT is not a number
       Set it to a port number in .env, or remove it to use 8000.
[FAIL] LAZYAF_FRONTEND_PORT is not a number
NOT READY: 2 problem(s) to fix          (exit 1)
```

`scripts/preflight.py:441` does `int(raw)` on the whole string. `.env.example:75-76`
ships `LAZYAF_BACKEND_PORT=127.0.0.1:8000` — the `[HOST_IP:]PORT` form the
compose file documents at `docker-compose.release.yml:53-55`. Rung 0 changed the
default and nothing re-checked the parser. Details and the security consequence
in §3.

### Step 4b — the pull works, and the image is not as self-contained as it looks

`docker-compose.release.yml` is genuinely checkout-free: zero `build:` stanzas,
zero bind mounts except `/var/run/docker.sock`. Migrations run from inside the
image with no extra command. The UI serves, nginx proxies `/api` (200) and
upgrades `/ws` (101), no CORS setup needed.

What is not visible: the backend image re-resolves its own package from PyPI
every time a container is **created**. First `up` after a `pull`, and every
upgrade. It works on a normal network (26-39s measured, against a 110s health
budget) and fails totally behind a proxy or during a PyPI outage. See A1.

### Step 9 — the agent refusal, quoted because it is the standard the rest should meet

A card started with no key failed in 0.9s, before spawning any container:

> `agent step 'Document the project' needs ANTHROPIC_API_KEY to run the`
> `'claude-code' CLI, but no key is configured — set ANTHROPIC_API_KEY in the`
> `backend's environment`

Names the variable, what it drives, and where it goes. R1 exactly as written.

### Step 10 — the second break: a perfect refusal with an unreachable remedy

> `bind mount source '/var/run/docker.sock' is not permitted from pipeline step`
> `config (allowed: none) ... the operator must opt in by naming it in`
> `LAZYAF_STEP_BIND_ALLOWLIST; mounting the docker socket into a step is`
> `host-root-equivalent, so it is not a default.`

Rung 0's refusal is correct and self-explaining. But the variable it names never
reaches the container through any documented route (§3, B2).

### The path a user takes if they never read QUICKSTART

They install, open `http://localhost:5173`, and follow the on-screen wizard at
`frontend/src/lib/pages/BoardPage.svelte:100-104`. It tells them to copy the
`git remote add` commands from the sidebar. **Those commands are dead** — 150
seconds of hang, then connection refused (§3, E1). The documented path works;
the in-product path does not.

---

## 3. BLOCKS_INSTALL gaps

**None.** Every path a lane walked reached a running system. The rest of this
report is about what breaks after that, what lies, and what is not gated.

---

## 4. Everything else, ranked

### BREAKS_LATER

#### B1 — preflight rejects the shipped port format, and its advice reopens the Rung 0 hole

`scripts/preflight.py:437-457`. `resolve_port()` does `int(raw)`; `.env.example:75-76`
ships `127.0.0.1:8000`. Two consequences, both bad:

- **The advice is harmful.** The message offers two remedies: *"Set it to a port
  number in .env, or remove it to use 8000."* The second is safe — compose's
  `${LAZYAF_BACKEND_PORT:-127.0.0.1:8000}` default still renders
  `host_ip: 127.0.0.1`. The first is not, and it is the natural reading for
  someone who deliberately set a port. Proven with `compose config`: with the
  shipped value the rendered config has `host_ip: 127.0.0.1`; after following
  the advice it has **no `host_ip` line at all** — 0.0.0.0, publishing an
  unauthenticated, docker-socket-holding API to the whole network. Preflight is
  the tool whose job is catching that, and it is the thing that talks them into it.
- **A second check goes dark.** `check_ports` at `scripts/preflight.py:471` does
  a bare `continue` when `resolve_port` returns None, so the port-in-use check
  never runs for either port. A user who already has something on 8000 gets no
  warning from the check that exists to give it. R1, silent skip.

Severity is BREAKS_LATER not BLOCKS_INSTALL because QUICKSTART.md:126 heads the
step "Preflight (optional, strongly recommended)" and following the bad advice
still yields a running stack — just an exposed one.

**Fix:** parse `[HOST_IP:]PORT` — split on the last colon, validate the port
half, keep the host half for display. Report a non-loopback bind as a WARN
naming SECURITY.md, never as a FAIL. Keep the "remove it" remedy; drop the
"set it to a port number" one. `tdd/unit/scripts/` has no `test_preflight.py`,
which is why Rung 0 sailed past this — land the fix with a test pinning
`127.0.0.1:8000`, `0.0.0.0:8000`, bare `8000`, and a genuinely bad value.
**Effort: small.**

#### B2 — `LAZYAF_STEP_BIND_ALLOWLIST` is in no compose file, so the documented opt-in is inert

`.env.example:60` tells the user to uncomment
`LAZYAF_STEP_BIND_ALLOWLIST=/var/run/docker.sock`. The backend reads it at
`backend/app/config.py:418`. It never arrives.

Root cause is one line more general than a missing entry: **no compose file in
the repo has an `env_file:` stanza** — `grep -n env_file docker-compose*.yml`
returns nothing, verified again for this report. `.env` is interpolation input
only; the sole vars that reach the backend are the six enumerated at
`docker-compose.release.yml:68-91`. Any future setting documented as "put it in
.env" has this bug by default.

Worse, compose does not even recreate the backend when the value changes:
`compose config --hash backend` returns the identical digest
`215451b8c2ac...` with and without it set. So `up -d backend` is a provable
no-op. The user edits `.env`, restarts, retries, fails identically, and the
failure names a variable they already set.

Same hole in the runner lane (`docker-compose.release.yml:129-159` has no
`LAZYAF_BIND_ALLOWLIST` despite `runner-agent/README.md:96-110` requiring it)
and in `deploy/k8s/backend-deployment.yaml:41-75`.

This lands on LazyAF's own CI: `.lazyaf/pipelines/test-suite.yaml:133,146` use
`needs: ["docker"]` for T2/T3, and the owner's backend has no allowlist var set.
**The dogfood run is what blesses a revision for packaging, and it has not run
since Rung 0 landed.**

**Fix:** add `- LAZYAF_STEP_BIND_ALLOWLIST=${LAZYAF_STEP_BIND_ALLOWLIST:-}` to
the backend environment in `docker-compose.release.yml` and
`docker-compose.yml`, and `- LAZYAF_BIND_ALLOWLIST=${LAZYAF_BIND_ALLOWLIST:-}`
to the runner-agent service. Then re-run the dogfood pipeline before trusting
the next green. **Effort: trivial.**

> Note: the two spellings (`LAZYAF_STEP_BIND_ALLOWLIST` backend,
> `LAZYAF_BIND_ALLOWLIST` runner) are **not** a bug — see §7.

#### A1 — the backend image re-resolves itself from PyPI on every container creation

`backend/Dockerfile:15-29` copies `pyproject.toml`/`uv.lock`, runs `uv sync`
(line 18), *then* copies `app/` (line 21) — so the project itself is never
installed at build time. `CMD ["uv", "run", "uvicorn", ...]` (line 29)
re-resolves at runtime.

Proven:

```
docker run --rm --network none ghcr.io/brennan-vanderlaan/lazyaf/backend:main
  Building lazyaf-backend @ file:///app
  Failed to resolve requirements from build-system.requires
  No solution found when resolving: setuptools>=61.0
  Failed to fetch: https://pypi.org/simple/setuptools/
```

The same image with network boots fine. And the baked venv alone boots offline —
`--entrypoint /app/.venv/bin/python ... -m uvicorn app.main:app` reached
`Started server process [1]` with `--network none`. The venv is complete; only
the CMD is wrong.

**Scoped honestly** (the first draft of this finding overstated it, twice): the
round-trip happens once per container **creation**, not per start — an existing
container restarts offline in ~3s. And it is not slow on a working network: 26s
and 39s measured against a 110s budget. The failure is not degradation, it is
binary. When PyPI is unreachable, `uv` exits nonzero, `restart: unless-stopped`
(`docker-compose.release.yml:100`) restart-loops the backend, it never goes
healthy, and the frontend's `condition: service_healthy` (`:111-112`) means the
UI never comes up at all.

It stays BREAKS_LATER rather than PAPERCUT because it **re-arms on every
`pull && up` upgrade** — a stack that installed fine can fail to come back
months later behind a new corporate proxy — and because the error names
`setuptools` and `pypi.org` and never mentions LazyAF. That is R1.

The in-repo sibling already does it right: `runner-agent/Dockerfile:22-24` does
`pip install --no-cache-dir .` at build time and its published image runs under
`--network none`. This is an inconsistency with an established house pattern,
not an open design question. No doc anywhere — README, QUICKSTART,
`.env.example`, SECURITY.md, preflight — mentions offline, PyPI, or first-boot
timing, so a user watching a supposedly prebuilt image print
`Building lazyaf-backend @ file:///app` for 30s gets no signposting either.

**Fix:** one line. `backend/Dockerfile:29` →
`CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host",
"0.0.0.0", "--port", "8000"]` (verified to boot with `--network none`). Or keep
uv and use `uv run --no-sync`. Better still, move `COPY app ./app` above the
`uv sync` so the project installs at build time. **Effort: trivial.**

#### A2 — nothing in CI ever starts the release stack

Not "nothing boots" — the first draft claimed that and it is false. Three of the
six step images are booted on every push to main by the project's own declared
gate (`.lazyaf/pipelines/test-suite.yaml` runs every step on `lazyaf-base:dev` /
`lazyaf-test-runner:dev`, and `agent: mock` + `openai-harness` both resolve to
`lazyaf-agent-base:dev` per `backend/app/services/pipeline_executor.py:171-176`).
`images.yml:5-17` says outright that it packages and does not gate, and that is
a correct, deliberate R4 posture — a workflow that refuses to pretend to be a
test gate.

The true statement is sharper: **`docker-compose.release.yml` — the only path a
stranger has — has never been brought up by anything, ever.** `grep -rn
"docker-compose.release"` finds docs, release-note echoes, and one hand-run
`docker compose config` at `PLAN.md:934`, which is a parse, not a boot. Seven
green `images.yml` runs published eight revisions of nine images without that
lane being started once. Genuinely unbooted anywhere: `claude`, `gemini`,
`debug-sidecar`, and the release compose lane. That is the hole A1 walked
through, and it is where B1, B2 and B5 hid too.

**Two obvious fixes are both wrong, and I tested them:**

- `docker run --rm --network none <backend> true` → **exit 0**. `true` replaces
  CMD, so the broken startup at `backend/Dockerfile:29` is never reached.
- compose-up-and-poll-`/health` → **also passes**. GHA runners have internet, so
  `uv run` reaches PyPI and starts; `/health` returns 200 in ~20s. The proposed
  smoke test goes green on the exact bug it is sold as catching — itself an R4
  fake-green.

Per-image isolated runs are worse than useless in both directions: they
false-pass the backend, and they **false-fail** the frontend, whose nginx
legitimately exits 1 on `host not found in upstream "backend"` because that
upstream only resolves on the compose network.

**Fix:** one job that stands up `docker-compose.release.yml` against the freshly
built tags and polls `/health`, **plus** an explicit egress-blocked assertion,
because "boots with a live PyPI" and "boots" are different claims and only the
second is what the user gets. Cheapest honest form of the second half:
`docker run --rm --network none <backend> .venv/bin/python -c "import app.main"`
— exercises the installed venv without the uv re-resolve and without needing the
compose network. Add a liveness line each for `claude`, `gemini` and
`debug-sidecar`, which nothing in the project has ever started. **Effort: small.**

#### C2 — the wheel gate has been dark since 2026-08-30, and it is currently RED

`gh run list --workflow=pr-build.yml`: last successful run is 33322245423 on
2026-08-30. Every run since (34537232651, 34503240557, 33502613517,
33404654600, 33343161644) is `action_required` at **0s** — they never executed.
`release.yml` has never run at all.

Root cause is worse than a missing `push: main` trigger. `pr-build.yml:46-54` is
`pull_request` only, and **it has never gated a single line of LazyAF's own
source** — its only two executions were a dependabot bump and a release-please
PR. The project's real workflow is direct pushes to main and local `lazyaf/*`
merges, neither of which produces a GitHub PR. The trigger was structurally
unreachable from day one.

The comment justifying `pull_request`-only says *"images.yml already builds
every image on a push to main"* — true, and irrelevant: `images.yml` does not
build the CLI wheel at all.

**The gate is not merely dark, it is red.** Running the excluded lane:

```
tdd/unit/packaging/test_wheel_build.py::TestWheelContents::
  test_wheel_contains_exactly_the_package_and_metadata FAILED
AssertionError: wheel contents changed.
    unexpected: ['lazyaf/debug_cmd.py', 'lazyaf/debug_protocol.py']
```

`test_wheel_build.py:65-88` asserts exact set equality against a hardcoded
manifest. Phase 12.7 added two modules and nobody updated it, because no lane
runs it — `pytestmark = pytest.mark.slow` at `test_wheel_build.py:35`, and
`scripts/run_tier.py:101` (T1) and `:126` (T3) both pass `-m "not slow"` while
T2 only targets `../tdd/integration/services`. Real drift landed behind the dark
gate during exactly the identified window. This is no longer hypothetical.

**An R3 violation explains why nobody noticed.** `test_wheel_build.py:11-12`
states *"The release workflow runs this same selection before publishing."*
That is false — `grep -niE "pytest|run_tier|-m slow|tdd/" .github/workflows/release.yml`
returns zero matches. `release.yml` builds the wheel and shell-smoke-tests it
(`--version`/`--help`, `:125-133`) but never invokes pytest. The author believes
in a backstop that does not exist.

Blast radius is larger than "pip install works": the same dark file holds
`test_no_repo_content_in_the_wheel` and `test_sdist_carries_the_project_only`
(FORBIDDEN_PATH_PATTERNS at `:37-52` — `.env`, `*.pem`, `*.key`, `backend/`,
`tdd/`, `docker-compose`). Those are the guard keeping a credential out of a
public artifact, and `release.yml:267` has a `pypi: publish to PyPI (opt-in)`
job downstream of a wheel job that runs no such check.

**Fix, and the order matters:** correct the manifest at
`tdd/unit/packaging/test_wheel_build.py:69-77` to include `debug_cmd.py` and
`debug_protocol.py` **first**, or re-arming the lane just turns main red. Then
either add `cli-wheel` to a push-to-main trigger (it is ~2m44s, no registry
writes) or add a `-m slow` packaging step to
`.lazyaf/pipelines/test-suite.yaml`. Separately, fix the false docstring at
`test_wheel_build.py:11-12` — wire the selection into `release.yml` or delete
the claim. And find out why release-please PRs sit in `action_required`; that is
the one PR that must have a green wheel before it becomes a tag. **Effort: small.**

#### C1 — `lazyaf land` pushes to a real remote based on CWD, after admitting it cannot confirm the destination

`cli/lazyaf/cli.py:715` — `cwd = Path.cwd()` is the only way the local repo is
chosen. There is no `--path`; `ingest` by contrast takes `REPO_PATH` as an
argument. `cli.py:707-711` warns that the repo *"has no remote_url recorded, so
LazyAF cannot confirm that origin is the right destination"* and `cli.py:766-767`
**pushes anyway**. That is the exact pattern the project's own dogfood pipeline
condemns.

This was found the hard way: a lane ran `lazyaf land <repo-id> --branch trunk -s
http://localhost:8790` intending a scratch repo, from a shell whose CWD was
`C:\projects\lazyaf`, and it pushed to the public GitHub repo. Cleanup in §8.

Two amendments that widen it:

- The warning fires **only** when `remote_url` is null. When `remote_url` *is*
  recorded there is no warning and still no check — `cli.py:703` reads it and
  `:707` is its only consumer, with no comparison against the local clone's
  `origin`. The quieter, more common path has no signal at all.
- `git remote remove lazyaf` is unconditional at `cli.py:732` **and** at
  `cli.py:602` in `ingest` (commented `# Remove if exists`). Fixing only `land`
  leaves the clobber reachable from the other documented entry point. A user who
  dogfoods one repo against two servers gets their remote silently repointed by
  whichever `--server` they last used.

`upcoming/shadow-ci.md:324` documents `remote_url` as land's push destination,
which the code does not implement — R3, one of the two must change.

**Fix:** make an unconfirmable destination a refusal, not a warning; require an
explicit `--remote` when the server cannot confirm. Add `--path` so the local
clone is named rather than inherited from CWD. Print the resolved local path and
destination URL before pushing. Do not remove an existing `lazyaf` remote that
differs — say so and require `--force`. **Effort: small.**

#### D4 — the schema-drift refusal hands an operator a destructive command naming a volume that does not exist

`backend/app/database.py:23-26`:

```
_RECREATE_HINT = (
    "Recreate the dev database (docker compose down && docker volume rm lazyaf-data) "
    "or migrate it manually."
)
```

Appended to the drift RuntimeError (`:242-246`) and the unknown-revision
RuntimeError (`:274-278`).

**The command resolves in zero topologies, not one.** The release volume is
`lazyaf-release_lazyaf-data` (`docker-compose.release.yml:40` sets
`name: lazyaf-release`; `QUICKSTART.md:421-424` says so correctly). The dev
volume is `lazyaf_lazyaf-data` — `docker-compose.yml` has no `name:` key, so the
project is the directory name, and `docker compose ls` shows the owner's live
stack as project `lazyaf`. `docker volume inspect lazyaf-data` → no such volume.

Three failures in one string: it calls a release user's production database
"the dev database"; the literal command errors, inviting a user to go guessing
at volume names while holding a delete command; and "or migrate it manually"
gives no procedure.

Harm is narrower than a first read suggests — the command **fails safe**,
nothing matches, so nothing is deleted, and recreating is the intended remedy
for genuine drift anyway. The real harm is a stranded operator: crash-looping
backend, a remedy that cannot run, and zero doc fallback (`grep -rn -i
"rollback|downgrade|older version|revert to"` across README/QUICKSTART/
CONTRIBUTING/SECURITY returns nothing).

**The reachable trigger is the unknown-revision guard, not drift.** A release
user cannot get an unversioned DB — migrations run from inside the image on
every boot. The live path is `database.py:274-278` after a version rollback, and
it is armed on published images today: GHCR carries `sha-aae20fa` (chain ends at
0014) alongside `main`/`sha-ac777bc` (has 0015), and `.env.example:41` tells the
user to *"Pin a real version for anything you care about reproducing"* when
`sha-` tags are the only pins that exist.

**Fix:** do not hardcode any project prefix — `lazyaf-release_lazyaf-data` would
still be wrong for source users and for anyone setting `COMPOSE_PROJECT_NAME`.
Lead with the non-destructive branch and let the user discover their own name:

> Back up first: `docker run --rm -v <project>_lazyaf-data:/d -v $PWD:/b alpine
> tar czf /b/lazyaf-backup.tar.gz /d`. Find `<project>` with `docker volume ls |
> grep lazyaf-data` (source stack: `lazyaf_lazyaf-data`; release stack:
> `lazyaf-release_lazyaf-data`). If you got here by pinning an older
> `LAZYAF_VERSION`, the fix is to go back to the newer tag — this codebase
> cannot downgrade a database. Deleting the volume erases every repo, card and
> pipeline run.

Drop the word "dev". And fix
`tdd/integration/test_migrations.py:387`, which asserts only
`"lazyaf-data" in str(excinfo.value)` — a substring that passes on a name that
does not exist. That is the fake-green that let this ship. **Effort: trivial.**

#### E1 — the UI's own onboarding hands out a git URL that hangs for 150 seconds, then fails

Found while refuting C9 (§7). `frontend/nginx.conf:13` uses
`proxy_set_header Host $host` — nginx's `$host` **strips the port**;
`$http_host` keeps it. So `backend/app/routers/repos.py:170` computes
`request.base_url` as `http://localhost/` and the clone URL loses its port:

```
curl http://localhost:5173/api/repos/<id>/clone-url   →  {"clone_url":"http://localhost/git/<id>.git"}
curl http://localhost:8000/api/repos/<id>/clone-url   →  {"clone_url":"http://localhost:8000/git/<id>.git"}
```

`git ls-remote` on the three candidates:

- `http://localhost/git/<id>.git` — **exactly what the copy button gives you**:
  `Failed to connect to localhost port 80 after 150213 ms: Connection refused`.
  A 2.5-minute hang. Nothing listens on :80.
- `http://localhost:5173/git/<id>.git` — the user hand-fixes the port:
  `not valid: is this a git repository?` — `nginx.conf` has locations for `/`,
  `/api` and `/ws` and **no `/git`**, so it falls through `try_files`
  (`nginx.conf:7-9`) and serves `index.html` with HTTP 200. A silent wrong
  answer. R1.
- `http://localhost:8000/git/<id>.git` — what `lazyaf ingest` uses. **Works.**

This is not a dev-stack artifact: `frontend/Dockerfile:14` copies the same
`nginx.conf`, and the file extracted from the published `frontend:main` image is
byte-identical. `git log` shows it unchanged since commit `22dc1f2` "Phase 1" —
this has never worked, in any port configuration.

Who hits it: the user who installs, opens the UI, and follows the numbered
empty-state wizard at
`frontend/src/lib/pages/BoardPage.svelte:100-104` — *"The exact `git remote add`
and `git push` commands appear under Repository Details in the sidebar"* —
without ever reading QUICKSTART. Also the three `git push {cloneUrl} --all`
buttons in `BranchManager.svelte:301,312,431`.

R4 note: `tdd/integration/api/test_repos_ingest_api.py:152-156` asserts only
`startswith("http")`, `"/git/" in`, `endswith(".git")` against an ASGI client
whose `base_url` is `http://test/`. It never asserts the URL is reachable and
never exercises it through nginx.

**Fix:** two lines in `frontend/nginx.conf` — change `Host $host` to
`Host $http_host` on `:13`, and add
`location /git { proxy_pass http://backend:8000; proxy_set_header Host $http_host; }`.
Then the copy buttons hand out `http://localhost:5173/git/<id>.git` and it
works. **Effort: trivial.**

---

### CONFUSING

#### D2 — the documented upgrade refreshes 3 of 9 images, and preflight then reports the 6 stale ones green

`QUICKSTART.md:407-411` documents the upgrade as exactly `compose pull` + `up -d`.
`docker-compose.release.yml` declares three services (`:50`, `:106`, `:129`); the
six step images are deliberately not compose services
(`scripts/build_images.py:45`). The backend resolves agent steps through a
hardcoded map of **local** tags (`pipeline_executor.py:171-177` →
`lazyaf-claude:dev`, `lazyaf-gemini:dev`, `lazyaf-agent-base:dev`, plus
`lazyaf-debug-sidecar:dev` at `execution/debug_terminal.py:201`). Nothing in the
upgrade path refreshes a `:dev` tag.

`QUICKSTART.md:396` already knows the risk — *"Keep the step images (step 5) on
the same tag as the services. Mixing versions is not tested"* — but sits 13 lines
above an upgrade block that never re-runs step 5.

The sharper half: `scripts/preflight.py:509-511,597-601` checks step images by
`docker image inspect` presence only and prints
`[ OK ] All 6 step images present as :dev` regardless of vintage, while
`QUICKSTART.md:391` advertises preflight as the registry-versus-tag checker.
That is the R4 shape here.

Mitigating, and better than the first draft credited: `scripts/build_images.py --check`
**does** compare the recomputed content hash against each image's
`lazyaf.content-hash` label, the GHCR images carry that label (verified
identical across local and published: `base` `e1e59bed1aea`, `claude`
`962bdf50f57d`), it exits 1 on drift, and it is documented at `QUICKSTART.md:228`.
So the tool exists and works — it just appears only in the build-from-source
block (4a), never in the pull block (4b), and never in the upgrade block.

**Fix:** lift the step-5 pull+retag loop from `QUICKSTART.md:266-271` into the
upgrade block at `:407-411`, add one line pointing at
`build_images.py --check` as the verification, and make preflight's step-image
OK line say it checked presence only and name `--check`. **Effort: trivial.**

#### D1 — a running install cannot tell you what it is

Three legs, two of which hold exactly:

- `backend/app/main.py:159` is `version="0.1.0"`, a literal that has never moved
  and will keep reporting 0.1.0 through every future release. `/health` returns
  `{"status":"ok","app":"LazyAF"}` with no version; `/api/version` and `/version`
  404.
- The three published service images carry **no OCI labels**. I pulled the
  config blob for `backend:main` from GHCR; `Labels` is empty. `frontend` carries
  only nginx's inherited `maintainer`. So `docker inspect` cannot answer it
  either. The step images already prove the pattern works — they carry
  `lazyaf.content-hash`, `lazyaf.agent-runtime` and `lazyaf.control-layer`.
- "No signal at all" is **false**, and the first draft was wrong here:
  `/api/diagnostics/system` already reports each container's image ref and a
  12-hex image id (`diagnostics.py:728-734`), the UI renders it
  (`SystemHeader.svelte:251`), and the bug-report bundle tabulates it
  (`diagnostics.py:1651-1664`).

The real defect is narrower and worth stating precisely: **`image_id` is the
config digest** (`sha256:489a2890…` for `backend:main`) **while GHCR's packages
page and `docker pull @sha256:` use the manifest digest** (`sha256:d7bd96b1…`).
Different values, so the number the UI shows cannot be looked up on the page
`QUICKSTART.md:392` sends you to. Immutable `sha-<7>` tags do exist and are the
only real pin available today — but that convention appears only in
`.github/scripts/publish_image.py:19` and in no user-facing doc.

The refusal to bake a SHA at `diagnostics.py:432-437` — *"with a bind mount that
value is a lie by construction"* — is correct for the dev compose and simply
over-applied. `docker-compose.release.yml` mounts only `lazyaf-data:/app/data`
(`:59`) and no code, so in the release topology the code *is* the image and a
baked SHA would be true.

**Fix:** (1) add `--label org.opencontainers.image.revision=${{ github.sha }}`
and `.version=<computed tag>` to the service-images build in `images.yml`;
(2) add the image's RepoDigest alongside `image_id` in `_container_info` — one
extra inspect, and it is the value that is greppable on the packages page (also
what `upcoming/security-posture.md:436` already asks for); (3) return the
revision from `/health` and stop hardcoding `0.1.0`; (4) pass `LAZYAF_VERSION`
into the backend `environment:` — `probe.py:1218` already reads it and currently
gets nothing; (5) one line in QUICKSTART's version table naming the `sha-<7>`
tags. **Effort: small.**

#### A5 — every published image is amd64-only and no user-facing doc says so

`docker manifest inspect --verbose ghcr.io/brennan-vanderlaan/lazyaf/backend:main`
returns a single manifest, `platform: {architecture: amd64, os: linux}` — no
multi-arch index. The choice is deliberate and well argued at `images.yml:31-36`,
and restated at `.github/WORKFLOWS.md:379-382` (*"An arm64 user builds locally
today"*). But `.github/WORKFLOWS.md` is a release-engineering doc linked from one
place in the repo — `CONTRIBUTING.md:154,170`, in a section about not adding test
jobs. Nothing an installer reads reaches it. `grep -n "arm64|Apple
Silicon|aarch64|amd64|platform" QUICKSTART.md README.md` returns no hit relating
to CPU architecture.

A Mac user pulls and either runs everything under emulation or fails outright.
And the quiet case is the expensive one: `scripts/preflight.py:514-522` already
shells out to `docker manifest inspect`, whose output carries `architecture`, and
tells the arm64 user everything is fine.

**Fix:** a note in QUICKSTART 4b *and* section 5 (step images are amd64-only too,
and they run a Node toolchain), saying path 4a builds from source and works
unconditionally — plus an arch comparison in preflight against
`platform.machine()`, a handful of lines in a function already doing the network
round-trip. **Effort: trivial.**

#### B5 — preflight looks for `.env` one directory up, so it is wrong from its first run in the shipped-asset layout

`scripts/preflight.py:30` — `REPO_ROOT = Path(__file__).resolve().parent.parent`,
which assumes the script lives in `<repo>/scripts/`.
`scripts/bootstrap_secrets.py:68` gets this right via `_default_root()`.

Reproduced the shipped-asset layout from `release.yml:146`
(`docker-compose.release.yml`, `.env.example`, `preflight.py`,
`bootstrap_secrets.py` in one flat dir). `bootstrap_secrets.py` worked and
created `.env` there. `preflight.py` then produced **three phantom FAILs**:

1. `[FAIL] docker-compose.release.yml not found in <parent>` — the file is
   sitting right there.
2. `[FAIL] .env not found` — likewise.
3. `[FAIL] 3 release image(s) do not exist at that name/tag` — because it cannot
   read the `.env` that says `main`, it falls back to `DEFAULT_VERSION = "latest"`
   (`preflight.py:35` → `:536`, `:613`) and condemns three images the user was
   never going to fetch.

Every remediation string names a `scripts/...` path that does not exist there.
FAIL 3's remediation actively steers the user to *"build from source instead:
`docker compose build && docker compose up -d`"* — impossible in a flat directory
with no `docker-compose.yml` and no source tree. A user who trusts the tool
abandons a working pull path for a dead end.

The strongest evidence is the workflow's own comment, `release.yml:22-24`:

> `* docker-compose.release.yml + .env.example + preflight.py — what a`
> `... compose header tells them to run preflight, so it has to ship WITH it.`

The maintainer explicitly reasoned that the flat-download user runs preflight and
shipped it for that purpose. The implementation contradicts the intent.
Reinforced by `docker-compose.release.yml:6-8` and `:29`.

There is also no escape hatch: `preflight.py:660-667` defines only `--offline`
and `--dev` — no `--env-file`, no `--root`, unlike `bootstrap_secrets.py`.

This does not bite today (zero releases) but **PR #2 is open and merging it ships
these assets.**

**Fix:** give preflight `bootstrap_secrets.py`'s `_default_root()` marker walk —
import or share it rather than duplicate (R3) — and make remediation strings name
`bootstrap_secrets.py` relative to whichever root was found. Add `--root` /
`--env-file` for parity. Add a regression test that runs both scripts from a flat
temp dir and asserts a clean exit. **Effort: small.**

#### C3 / A3 — the CLI's own README tells users to `pip install lazyaf-cli`, and CI enforces the broken instruction

`cli/README.md:22` `pip install lazyaf-cli`, `:28` `uv tool install lazyaf-cli`,
`:29` `pipx install lazyaf-cli`. `https://pypi.org/pypi/lazyaf-cli/json` → HTTP
404 (and `lazyaf` → 404). In a clean venv: `ERROR: Could not find a version that
satisfies the requirement lazyaf-cli (from versions: none)`, RC=1. Three of four
install lines fail, with an error naming no remedy.

`tdd/unit/packaging/test_wheel_metadata.py:124` asserts
`'pip install lazyaf-cli' in text` — so T1 runs on every push and will actively
fight any correction. The test is honestly named and scoped to long-description
hygiene ahead of a publish, so calling it fake-green overstates it; but it
*will* revert the fix if the fix does not touch it.

This is a genuine R3 split, and the CI-pinned story is the false one:
`README.md:179` reads *"`pip install ./cli` # not on PyPI, and no release has
been tagged yet"*, and `QUICKSTART.md:282-288` says the same. The nearest honest
text inside `cli/README.md` itself is 127 lines away under a contributors'
heading and is a build command, not an install.

Blast radius is narrow — no markdown link in the repo points at `cli/README.md`;
the only live exposure is GitHub auto-rendering it when someone clicks into
`cli/`. The "becomes the PyPI long description" concern is theoretical until a
package exists.

**Fix:** state the truth today at the top of the Install section (not on PyPI;
`pip install ./cli` from a checkout) and keep the `lazyaf-cli` block clearly
marked as the post-release path. Relax
`tdd/unit/packaging/test_wheel_metadata.py:124` in the same change.
**Effort: trivial.**

> The wider A3 claim — *"the release lane has never executed, therefore it is
> unexercised on the day it matters"* — is only half true. I exercised it by
> hand: the wheel builds, `check_release_version.py` is correct in both
> directions, and the wheel installs and runs in a clean venv. What remains
> genuinely untested is the `gh release create` upload and the onboarding-asset
> bundle, and `release.yml:146-152` deliberately makes missing assets warn
> rather than fail. The zero-tag situation itself is one of the best-documented
> facts in the repo (`README.md:179`, `QUICKSTART.md:141-147,282-288`,
> `.env.example:36-42`) — it is signposted in four places. See §6.

#### C6 — the backend's version is a hardcoded literal release-please will never bump

`backend/app/main.py:159` is a literal. `.github/release-please-config.json`
`extra-files` lists **only** `cli/lazyaf/__init__.py`, which carries the
`x-release-please` markers at `:19-21`.
`.github/.release-please-manifest.json` is `{".": "0.1.0"}`. So the first release
bumps the CLI and leaves the backend advertising 0.1.0 forever. Today both read
0.1.0 by coincidence, which hides it.

Not a compatibility break — there is no CLI/server handshake to break (C5) — but
an honesty defect: after the first release, "what version is my server?" has no
answer at all.

**The fix is bigger than the first draft said.** Adding only
`backend/app/main.py` leaves the same bug in
`runner-agent/lazyaf_runner/__init__.py:14`, which is *more* user-visible — it
reaches the UI as "agent 0.1.0" via `client.py:304` →
`RunnerPanel.svelte:178-179`. Also consider `backend/pyproject.toml:10`,
`runner-agent/pyproject.toml:3`, `runner-common/pyproject.toml:3`.

Worth pairing: `RuntimeFacts` in `backend/app/services/diagnostics.py:985` has no
app-version field, and published images carry no `.git`
(`backend/Dockerfile:21-23`), so bug-report bundles from image users identify no
version at all. And `CONTRIBUTING.md:124` (*"The version number lives in exactly
one place"*) becomes actively false unless updated in the same change.
**Effort: trivial.**

#### C7 — `.env.example` invites `LAZYAF_SERVER` into a file the CLI never reads

`.env.example:255-259` has a section headed *"CLI — read by the `lazyaf` command
on YOUR machine, not by the containers"* with `# LAZYAF_SERVER=` and *"Match
LAZYAF_BACKEND_PORT if you changed it."* The CLI does no dotenv loading — it
reads only `os.environ` (`cli/lazyaf/cli.py:65`); grep for `dotenv` in `cli.py`
returns nothing. Proven: with `.env` containing
`LAZYAF_SERVER=http://localhost:8790` in the CWD and the shell var unset,
`lazyaf list` printed `http://localhost:8000 (from the built-in default …)`.

The mitigation is stronger than it first looks and in a different place than
expected: `lazyaf list` prints the provenance line on the **success** path,
unconditionally, at `cli/lazyaf/cli.py:830`, and its docstring (`:820-821`) says
that is deliberate. `lazyaf list` is exactly the smoke test `QUICKSTART.md:294`
hands the new user. Real R1 signposting.

But one troubleshooting entry cancels it. `QUICKSTART.md:464-465`: *"If `lazyaf
list` cannot connect, the backend is not up: check `docker compose logs
backend`."* For the port-changed user the backend **is** up, the logs are clean,
and the stated cause is wrong. They go re-check `.env` and find `LAZYAF_SERVER`
set exactly as instructed. That loop is the cost.

**Fix:** at `.env.example:257` add *"This file is read by docker compose, not by
the CLI — setting it here does nothing. Export it in your shell (`export
LAZYAF_SERVER=http://localhost:<port>`) or pass `--server`."* And amend
`QUICKSTART.md:464-465` so the "cannot connect" entry says to check the URL the
CLI printed and where it came from before blaming the backend. **Effort: trivial.**

#### E2 — the 12.7 interactive debug terminal is not wired into the shipped CLI

Found while refuting C4 (§7). `cli/lazyaf/cli.py` never imports `debug_cmd` and
never calls `debug_cmd.register()` — grep over all 1584 lines returns only three
prose mentions (`:180`, `:1305`, `:1319`); the sole callers of `register()` are
tests at `tdd/unit/scripts/test_cli_debug.py:660,708`. The `lazyaf debug attach`
a user actually runs is the placeholder at `cli/lazyaf/cli.py:1453-1462`,
verified at runtime (`attach is debug_cmd.attach` → False; callback module
`lazyaf.cli`; `--help` shows no `--print-credential`).

So the interactive terminal promised at `README.md:342` is unavailable in the
shipped CLI, and the entire `[terminal]` extra path is dead code from a user's
perspective. **Fix:** wire `debug_cmd.register()` into the CLI, or correct
`README.md:342`. **Effort: trivial to wire; needs a decision on which.**

---

### PAPERCUT

**A4 / B6 / D7 — `latest` is the default in four places and exists nowhere.**
These three findings are one defect; recording it once. `docker-compose.release.yml:51,107,130`
and `scripts/preflight.py:35` default to `latest`; live check today confirms
`backend:latest -> HTTP 404` while `main`, `edge` and `sha-ac777bc` all return
200 on the same digest. Reachability is much narrower than any draft claimed: with
no `.env` at all, compose dies first at
`${LAZYAF_STEP_AUTH_SECRET:?…}` (`:90`) with a message naming
`bootstrap_secrets.py` and never resolves an image tag — I ran it. The reachable
doors are a `.env` that exists but has no `LAZYAF_VERSION` line (the
download-without-`.env.example` route, or a hand-written two-line `.env`), and
preflight's independent fallback. The condition is signposted in six places
(`.env.example:38-41`, `QUICKSTART.md:130-136,165,167-178,390,460-462`,
`docker-compose.release.yml:35-36`) and the failure is loud, not silent.
**Fix:** do *not* hardcode `main` — `publish_image.py:92-99` pushes `latest` on
the first stable semver tag, so `latest` is the correct long-term default and
pinning `main` trades one mismatch for a worse one. Use the file's own house
style instead: `${LAZYAF_VERSION:?not set. Run: python scripts/bootstrap_secrets.py,
or add LAZYAF_VERSION=main to .env}` at all three sites — the same `:?` shape
already used twenty lines below — and give `preflight.py:35` the same treatment,
leaving `.env.example:42` as the one source of truth. Or just cut the release
(§6) and the problem evaporates. **Effort: trivial.**

**B8 — the browser tab says "frontend", next to the Vite logo.** `frontend/index.html:7`
is `<title>frontend</title>` and `:5` points at `/vite.svg`, which is the stock
Vite lightning bolt (served 200, 1497 bytes). Two scaffold leftovers, not one, and
they reproduce on both the published image and the from-source path since both
build from the same file. **Fix:** `<title>LazyAF</title>` and a real favicon in
`frontend/public/`. **Effort: trivial.**

**C5 + E3 — no CLI/server handshake, and `lazyaf --version` crashes on click 8.1.7.**
The CLI sends no identity (a stub backend logging headers saw only
`python-httpx/0.28.1`) and the server offers none (`/health` has no version,
`/api/version` 404s). This is an R3 outlier: the runner↔backend link has the full
thing — `PROTOCOL_VERSION` mirrored on both sides
(`runner-agent/lazyaf_runner/client.py:35-37`,
`backend/app/services/execution/runner_protocol.py:40-41`), `agent_version` sent
on register, persisted to a column, unsupported versions refused at
`ws_runners.py:343-348`, and a named skew warning. The house knows how to do
this and did it twice; the CLI is the omission. It is a PAPERCUT today because no
*published* artifact can produce the failure — every GHCR backend tag is a
descendant of `dc340a0`, the commit that added the last router the CLI calls, so
there is no pullable backend that 404s a route the current CLI uses. It graduates
to CONFUSING the first time `v*` tags exist and people pin them.
**E3 is a separate, live bug found while testing the fix:** `cli/lazyaf/cli.py:432`
uses bare `@click.version_option()` with no `package_name`, while the
distribution is `lazyaf-cli` and the module is `lazyaf`. With click 8.5.0 it
works; pinned to `click==8.1.7` — allowed by `cli/pyproject.toml:28`
(`click>=8.1.0,<9`), and what the owner's own machine has — `lazyaf --version`
crashes with `RuntimeError: 'lazyaf' is not installed. Try passing
'package_name' instead.` `release.yml`'s `lazyaf --version` smoke step catches
this only if the runner resolves a new click: resolver-dependent green.
**Fix:** `@click.version_option(package_name="lazyaf-cli")` — one word. Then add
the version to `/health` and send `User-Agent: lazyaf-cli/<version>`.
**Effort: trivial (E3) / small (C5).**

**C8 — `lazyaf list` breaks the piping contract its own README promises.**
`cli/README.md` states *"Diagnostics go to stderr, results go to stdout. `lazyaf
list | …` pipes clean data"*. Measured: `lazyaf list 2>/dev/null` still emits the
provenance line and `Found 16 repo(s):`; `2>&1 >/dev/null` emits nothing — stderr
is empty. `cli/lazyaf/cli.py:830,836` use `console.print` (stdout) while
`err_console` at `:52` is used only by `fail()`/`warn()`. So
`lazyaf list | awk '{print $1}'` yields **two** junk rows:
`http://localhost:8790` and `Found`. `lazyaf branches` has the same header
problem. The contract holds only on the failure path — `err_console` has 3 call
sites, against 58 stdout `console.print` calls. `cli/tests/` does not exist, so
nothing will hold the fix in place. **Fix:** route success-path headers and
provenance banners to `err_console` across the result-listing commands.
**Effort: trivial.**

**B4 — a fresh instance's orphan audit claims labelled workspace volumes from
another stack.** `backend/app/services/workspace_service.py:824-856` (sweep 3)
lists host volumes by label `lazyaf.workspace=true` and removes any not in *its
own* Workspace table that pass `_volume_is_old`, where a missing or unparseable
`lazyaf.created_at` label counts as old (`:898-913`). A second instance's
database is empty, so every labelled volume on the shared daemon looks like
garbage to it. Observed live during this review (disclosure in §8). Blast radius
today is limited by the 15-minute age guard and by most volumes predating the
label (1 of 68 on this host carried it) — but on a new user's machine every
volume their stack creates carries it. `QUICKSTART.md:183-186` documents the
dev↔release switch and says *"Bring one down before starting the other"*, which
is what keeps this at PAPERCUT; the BREAKS_LATER variant requires running two
stacks concurrently, which that line forbids.
**Fix:** stamp an instance id label at volume creation and restrict sweep 3 to
this instance's id; treat an unlabelled-instance volume as not-ours (log once,
never remove). That also retires the `# exact match: host safety` workaround at
`tdd/integration/services/test_workspace_lifecycle.py:484`. Two adjacent items to
fold in: downgrade the in-use 409 to a debug-level "busy, skipping" like the
`LockTimeoutError` branch above it at `:854` (today it emits a full ERROR
traceback every sweep that reads like a crash — reproducible on a single stack,
so fix it separately and trivially); and call the already-written
`_sync_volume_exists` (`workspace_service.py:239`, currently dead code) in
`get_or_create`'s READY/IN_USE fast path so a vanished volume fails loudly
instead of letting Docker auto-create an empty one. **Effort: medium.**

**D5 — no backup procedure exists anywhere.** `grep -rn -i "backup|back up"`
across README, QUICKSTART, SECURITY, CONTRIBUTING and `docs/` returns exactly one
hit, `SECURITY.md:66`, about secrets in backups — not a procedure. Nothing
anywhere says migrations run at boot and are not reversible.
**But the predicted breakage does not happen**, and this is worth recording
because the first draft got it wrong: rolling `LAZYAF_VERSION` back never invokes
a downgrade — there is no `command.downgrade` call anywhere in the product. I ran
it: the old image's startup guard sees `alembic_version` 0015, cannot resolve it,
and raises **without touching the schema**. The DB is byte-identical afterward
and rolling forward works. The `0015`/`0014` downgrade docstrings only apply if
an operator manually types `alembic downgrade`, which no doc or script suggests.
**Fix:** two lines above the upgrade block saying migrations run at boot and are
not reversible, that rolling the tag back makes the backend *refuse to start*
(nothing is lost; roll forward again), and giving the snapshot one-liner
`docker run --rm -v lazyaf-release_lazyaf-data:/d -v "$PWD":/b alpine tar czf
/b/lazyaf-backup.tgz -C /d .`. The genuinely dark edge in this area is D4, not
this. **Effort: small.**

**D6 — a startup refusal crash-loops, and its remedy needs a container that is
dead.** `backend/app/main.py:59` calls `await init_db()` in the lifespan with no
try/except; a RuntimeError propagates, uvicorn exits, and
`docker-compose.release.yml:100` `restart: unless-stopped` loops it. The user
watches a good message scroll past repeatedly and cannot act on it —
`compose exec` needs a running container. `QUICKSTART.md:445-483` has no "backend
keeps restarting" entry, and no user-facing doc contains the word "alembic".
**Three corrections that shrink this,** all from running it: a fresh empty
database upgrades clean to `['0015']`, so no new user can reach it. The refusal
operators actually land on is **0014's**, not 0015's — `_RETIRED_COLUMNS` at
`backend/app/database.py:66-70` maps `("pipelines","steps") -> "0013"`,
explicitly not 0014, and I confirmed with a scratch DB that `upgrade head` raises
from 0014. And 0015's own remedy string (`alembic upgrade 0014`) is a **no-op** —
I reached that refusal by hand-stamping a DB at 0014, ran the remedy verbatim, it
exited 0 and changed nothing, and the next `upgrade head` refused identically.
So giving *that* command a pasteable `compose run --rm` wrapper would hand the
operator a command that exits 0 and leaves them in the same loop — worse than no
remedy (R1). **Fix:** a QUICKSTART troubleshooting entry for "the backend
restarts over and over" pointing at `docker compose logs backend` plus the
one-off form `docker compose -f docker-compose.release.yml run --rm backend uv
run alembic …`, which appears nowhere in the repo. This applies to every startup
refusal (`database.py:238-244`, `:276-281`), not just schema ones. Also fix the
stale prose in 0015's module docstring, which names "0014" where the code says
0013. **Effort: small.**

**D3 — preflight's step-image OK line does not cross-reference the tool that
checks freshness.** Folded into D2 above; the standalone version overstated it.
`scripts/build_images.py --check` already does the content-hash comparison,
exits 1 on drift, and is documented at `QUICKSTART.md:228` and `README.md:531`.
Preflight says "present" and proves presence — the word is accurate. The residual
gap is a missing cross-reference, not a check that lies. **Effort: trivial.**

**D8 — frontend/backend skew has no handshake, but the detection already exists
and simply never reaches the screen.** The backend already computes a
container-age skew warning (`diagnostics.py:747,806-822,1066-1068`) — *"Services
were recreated at different times, so they may not be running the same
generation of the code"* — and the SPA already renders every container's service,
start time and image (`SystemHeader.svelte:241-251`). It also handles a
diagnostics 404 explicitly as "this backend predates the feature"
(`SystemHeader.svelte:84-100`). The trigger is not a partial `compose pull` (all
three services share one `${LAZYAF_VERSION}`); it is
`.github/workflows/images.yml:230-233`, `fail-fast: false`, whose own comment
blesses *"publishing backend but not frontend"*, leaving a skewed `:main` in the
registry that `scripts/preflight.py:525-565` will not notice because it checks
existence, never agreement. **Fix, in order:** (1) gate the moving-tag push on
all three service builds succeeding, or have preflight compare the three digests
and FAIL on disagreement — that is the only path a stranger reaches; (2) map
`headline_problems`, `problem_titles`, `containers.spread_warning` and
`containers.spread_headline` into `SystemFacts`
(`frontend/src/lib/stores/diagnostics.ts:120-134`) and render them — the
detection is built and tested but the bundle and the UI currently disagree about
what the product knows (R3); (3) only then a build stamp, and delete
`backend/app/main.py:159` in the same change. Also worth a QUICKSTART line: no
user-facing doc mentions the Logs tab or the diagnostics strip, even though
`App.svelte:150` puts it in the nav. **Effort: small.**

**A7 — the service image list is hardcoded in four CI/compose sites plus the k8s
manifests.** `.github/workflows/images.yml:234-241`,
`.github/workflows/pr-build.yml:190-196` (a fourth copy the first draft missed),
`docker-compose.release.yml:51,107,130`, `scripts/preflight.py:38`, plus
`deploy/k8s/*-deployment.yaml` and prose at `.github/WORKFLOWS.md:183` and
`PLAN.md:246`. Contrast the step images, genuinely single-sourced via
`.github/scripts/step_images.py:46-61` and `scripts/preflight.py:493-507`, both
importing `IMAGES` from `scripts/build_images.py`.
This is a **weaker** hazard than the step-image drift it invites comparison with,
and the direction matters: a service in compose but missing from GHCR fails
loudly twice (`preflight.py:552-565` FAILs by name, and `compose pull` says
`manifest unknown`); only the opposite direction is silent — a service published
and in compose but absent from `SERVICE_IMAGES` is simply unchecked, verified by
appending a fictional `scheduler:` service to a scratch compose copy and seeing
preflight's output unchanged at three images. Consequence is a slightly weaker
preflight, not a broken install. **Fix:** the cheap completion —
`images.yml:20-21` already documents the coupling; add a reciprocal comment at
`scripts/preflight.py:37` and one in `pr-build.yml`, which has none.
**Effort: small.**

**A6 / uv pinning — recorded as hygiene only, and the original finding is
withdrawn.** "No base-image digest pinning, so `sha-<7>` is not reproducible"
does not survive: `publish_image.py:19-21` promises tag *immutability*, not
rebuild reproducibility (*"the exact commit. Never reused, never moved. This is
the tag to quote in a bug report"*), which I verified holds — `sha-ac777bc`,
`main` and `edge` all resolve to `sha256:d7bd96b1…` and the `sha-` tag will never
move off it. The documented bug-report mechanism is pull-the-digest, which does
not require a rebuild, and no file in the repo asks anyone to rebuild a revision.
Worse, the proposed fix is explicitly declined in-repo: `images.yml:251-252`
builds with `--pull` *on purpose* (*"a release is built against the current
upstream base image rather than whatever the runner happened to cache"*), and
`.github/dependabot.yml` states the scoping rationale at length. Pinning by
digest and building with `--pull` are mutually exclusive.
**What survives, and it is small:** `backend/Dockerfile:12` does
`COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/` — third-party code on a
`:latest` tag, in a repo whose own `.github/WORKFLOWS.md` argues *"A tag can be
moved by whoever owns the action; a SHA cannot"*, and which `build_images.py:11`
declares a grep-able "NO `:latest` anywhere" rule against. Pinning uv to a
version tag costs nothing and fights no machinery, since it is a `COPY --from`
source rather than a base layer the `--pull` policy governs.
**Effort: trivial.**

**B7 — the release compose header tells a download-only user to run a path they
will not have.** `docker-compose.release.yml:7`: *"Clone (or just download this
one file plus .env.example), run `scripts/bootstrap_secrets.py`…"* — a user who
downloaded two files has no `scripts/`. Same assumption in the `:?` strings at
`:90-91` and `:152`. Unreachable today (zero releases, and `README.md:473-474`
says so), and when a release *is* cut, `release.yml:180-183` already emits the
correct wording — *"Download `docker-compose.release.yml`, `.env.example` and
`bootstrap_secrets.py`"* plus `python bootstrap_secrets.py`. So the real defect
is narrower: the compose header contradicts the release notes about file count
and path (R3 drift), and will mislead only someone who skipped the release page.
**Fix:** align the header and the three `:?` strings to the release-notes wording
— three files, referred to as `bootstrap_secrets.py`, noting it sits under
`scripts/` in a checkout. **Effort: trivial.**

---

## 5. What is already good

This is not a rescue job. The parts that work, work unusually well, and several
of them are the reason the verdict in §1 is "yes".

**The images are real, public, and current.** Anonymous GHCR token + tags list
returns HTTP 200 for all nine — no credential, no confusing auth error, which is
the actual new-user experience. For all nine packages the `main` digest is
byte-identical to `sha-ac777bc`, current HEAD. Zero staleness.

**The secret-scan gate is real and blocking, not fake green.** Proven two ways:
run 34503186221 on `be5e943` shows `secret-scan / scan for credentials =>
failure` with both the step-images job and the service matrix `skipped`, and
correspondingly there is no `sha-be5e943` tag in any package. Locally,
`scan_repo_secrets.py` passes clean on 724 tracked files and exits 1 with a clear
remediation when a live-format key is planted.

**`bootstrap_secrets.py` is the best part of the install** — see §2.

**Rung 0 is correctly implemented and genuinely well signposted.** As shipped,
`compose config` renders `host_ip: 127.0.0.1` for both ports. It is explained
three times before a user can trip on it: `QUICKSTART.md:16-24` above everything
else, `.env.example:62-76` at the variable, `README.md:220-225` — and
`QUICKSTART.md:207` gives the exact `grep -A1 host_ip` command to verify it
rather than asking for trust. The bind refusal message (§2 step 10) is the same
quality. The *only* thing wrong with Rung 0 is that two downstream tools
(preflight, compose wiring) were not updated with it.

**The docs are unusually honest about their own gaps.** The absence of tags, the
non-existence of `latest`, cards not supporting `openai-harness`, `reach:
runner-local` being unverified — all stated plainly in `README.md:475` and
`QUICKSTART.md:142-147,389-391`. Nothing I found contradicted a claim the docs
made about maturity, with the single exception of `cli/README.md` (C3).

**Step-image single-sourcing works exactly as designed.**
`python .github/scripts/step_images.py --pairs` emits the six expected pairs, and
`preflight.py:493-507` imports the same `IMAGES` table, so a new step image is
published, scanned *and* preflight-checked automatically. The service list (A7)
is the exception, not the rule.

**Provenance tagging on the step images is solid.** Both `sha-<7>` and
`content-<12>` tags exist in the registry, `base:main` carries
`lazyaf.content-hash: e1e59bed1aea`, and `images.yml:143` re-runs
`build_images.py --check` to refuse publishing a content tag that would lie. I
compared local `:dev` labels against published `:main` config blobs for all
five and got a clean match on every one, despite the local images being 11 days
older by build date — the chain is doing its job. (I initially misread that age
as drift; it is not.)

**Two of the three contract boundaries are properly versioned.** Runner↔backend
(`runner_protocol.py:40-41`, `ws_runners.py:343-350`, and
`unsupported_version_message` at `:406-409` which names *both* sides) and
backend↔step-image (`runner_common/agent_config.py:38-39,216-226`, refusing an
unknown version rather than guessing, with an explicit note that the bool check
exists so `{"version": true}` cannot sneak through). The CLI is the omission, not
the norm.

**`step_default_image` is not the trap it looked like.** `backend/app/config.py:353,414`
set it to `python:3.12`, a public pullable image, and
`services/workspace/population.py:98` pre-pulls it at startup. A release user
writing a plain step with no image can run it with nothing built locally.
`PLAN.md:1213` states the intent explicitly.

**`images.yml`'s refusal to pretend to be a test gate** (`:5-18` — packaging only,
the dogfood pipeline decides whether a revision is good, only "could not build"
and "contains a credential" may block) is the correct R4 posture and should not
be changed by A2's fix.

**The CLI's failure reporting is a model.** Scheme-less URLs are refused rather
than guessed, with an explanation of *why* guessing `http://` is how a request
that should have been encrypted goes out in the clear. Connection failures name
the URL, name where the URL came from, quote the OS error verbatim and indented,
and give `curl <url>/health` as the remedy. `lazyaf tests reconcile` with no
input source refuses and explains the consequence — that defaulting to one
tier's results would orphan every test that tier did not run. Exit codes match
the documented contract exactly (0/1/2). Server-URL precedence is transparent on
every command.

**The branch-detection fix holds.** A repo whose trunk is literally named `trunk`
ingested correctly: the CLI printed *"Using current branch as the default:
trunk"* and the server recorded `default_branch: trunk`, not a hardcoded `main`
(`cli/lazyaf/cli.py:589-594`).

**`tdd/unit/packaging/test_wheel_metadata.py` is strict and useful** — single
version source, bounded dependencies on both ends, explicit non-discovered
package lists, no intra-repo deps, no test/credential files in the build context,
and it *imports* the leak-gate's shared pattern table rather than restating it.
These run in T1 on every push, so the declarations are continuously guarded even
while the build itself is not.

**Release CI security posture matches its own header comments.** Every GitHub
Action pinned to a commit SHA, `GITHUB_TOKEN` fed to docker login on stdin,
`packages: write` scoped per job, never triggered by `pull_request_target`.
`release.yml` gates on the leak scan before building, checks the wheel version
against the tag, smoke-tests in a throwaway venv, and keeps PyPI opt-in,
dispatch-only and tokenless via OIDC trusted publishing. It is careful where it
counts. It has simply never run.

**Windows is a first-class platform here.** The entire walkthrough ran on
Windows 11 with Docker 29.4.0 / Compose v5.1.2 with no workaround.
`QUICKSTART.md:480-481` warns about `core.autocrlf` for ingested repos, and step
5 ships a PowerShell variant of the pull/retag loop alongside bash.

---

## 6. The release question

### The state, verified

Zero tags (`git tag` empty locally, `git ls-remote --tags origin` empty). Zero
releases and zero `release.yml` runs via the GitHub API. `release-please.yml`
succeeds on every push, and **PR #2 "chore: release main" has been open since
2026-08-30T16:21:51Z proposing v0.2.0**. Its branch is correct:
`cli/lazyaf/__init__.py` on `release-please--branches--main` already reads
`__version__ = "0.2.0"`, so `check_release_version.py` would pass.

So the release lane is one merge from firing, and has been for eleven days.

### Should you cut one? Yes — but not today, and not by clicking merge

Cutting `v0.2.0` fixes real things:

- `publish_image.py:92-99` pushes `latest` on a stable (non-prerelease) semver
  tag — verified in the source for this report — so `latest` becomes real on all
  nine packages and the A4/B6/D7 default stops pointing at nothing.
- It makes `docker-compose.release.yml`'s and `.env.example`'s documented
  defaults true.
- It gives users an immutable pin that is not a git SHA, which `.env.example:40-41`
  already advises (*"Pin a real version for anything you care about
  reproducing"*) and which is currently impossible in the form it implies.
- It runs `release.yml` for the first time, which is the only way that lane stops
  being theory.

**But "ready to merge" is not true, and the cost is real.** `gh pr checks 2`
returns *"no checks reported on the 'release-please--branches--main' branch"*;
`mergeStateStatus` is `UNSTABLE`; `pr-build.yml` has five runs on that branch,
every one concluded `action_required` — it has never executed. Merging PR #2
today means merging a branch whose build check has never run, into a repo whose
wheel gate is currently red (C2).

And cutting a tag **arms three findings that are dormant today**:

| Finding | Dormant because | Armed by the tag |
|---|---|---|
| B5 | no release assets exist | ships `preflight.py` into a flat dir where it emits three phantom FAILs |
| B7 | nobody downloads assets | compose header contradicts the release notes |
| C6 | CLI and backend both read 0.1.0 by coincidence | they diverge permanently, and `CONTRIBUTING.md:124` becomes false |

### Recommendation, concrete

**Tag `v0.2.0`. Keep the `v` prefix and stay pre-1.0.** `SEMVER_TAG` in
`publish_image.py` already expects `vX.Y.Z`, release-please is configured for it,
and the manifest is `{".": "0.1.0"}`, so 0.2.0 is what the conventional-commit
history has earned. Pre-1.0 is also the honest signal: the docs are already
candid that the shape can move, and 1.0 would contradict `README.md:475`.

**What has to be true first — five items, all small:**

1. **C2** — fix the wheel manifest at `tdd/unit/packaging/test_wheel_build.py:69-77`
   (add `debug_cmd.py`, `debug_protocol.py`) and find out why release-please PRs
   sit in `action_required`. This is the one PR that must have a green wheel
   before it becomes a tag.
2. **B5** — give `preflight.py` a marker-walk root, or the very first thing the
   very first release-asset user sees is three failures that are all false.
3. **B7** — one sentence in the compose header, so it agrees with
   `release.yml:180-183`.
4. **C6** — add `backend/app/main.py` *and*
   `runner-agent/lazyaf_runner/__init__.py` to release-please `extra-files`
   before the first bump, or they are stuck at 0.1.0 forever and every later fix
   is retroactive.
5. **A1** — not strictly a release blocker, but a tagged release is the thing
   people will pull in six months on a network you do not control. Fix the CMD
   before you put a permanent name on the image.

**What it costs:** roughly a day of the five items above, plus the first-ever run
of `release.yml`, whose upload and asset-bundle steps are the only genuinely
unexercised parts (`release.yml:146-152` deliberately makes missing assets warn
rather than fail, so the downside is a thin release page, not a failed release).
The wheel builds, the version gate is correct in both directions, and the wheel
installs and runs in a clean venv — I verified all three by hand.

**One thing to decide separately:** the PyPI job (`release.yml:267`) is opt-in
and dispatch-only. Do not opt in until C2's forbidden-path guard is running in a
lane, because that guard is what keeps a credential out of a public artifact and
it is currently dark.

---

## 7. Three findings that did not survive — do not re-file these

Recorded so the next reviewer does not spend a day on them.

**"Backend and runner use two different env var names for the same allowlist."**
False premise. `LAZYAF_STEP_BIND_ALLOWLIST` and `LAZYAF_BIND_ALLOWLIST` are two
*gates*, not two names for one gate, with disjoint enforcement sites —
`local_executor.py:777` (local path only) and `docker_orch.py:418` (the runner's
own host only). The rationale is in the source at `docker_orch.py:404-407`:
*"a backend must not be able to bind arbitrary host paths on a machine it does
not own, and 'the backend asked for it' is not authorization on someone else's
hardware."* Unifying them would imply one backend-side setting governs remote
hosts — a security regression dressed as tidiness. Also, the claim that the
runner spelling *"appears nowhere in `.env.example`"* is simply wrong: it is at
`.env.example:243`. The real bug in this area is B2, which this was groping at.

**"The `[terminal]` extra's error message hands out an install command that
cannot work."** Refuted twice. `TERMINAL_EXTRA_INSTALL` is unreachable (E2 —
`register()` is never called). And even when reached the line works: the guard
can only fire where `lazyaf-cli` is already installed, so pip satisfies the root
requirement from installed metadata and installs only the extra — verified,
`pip install "lazyaf-cli[terminal]"` returned RC=0,
`Successfully installed websockets-15.0.1`, and `_load_websockets()` then
succeeded. The real defect here is E2, filed above.

**"Docs never say the CLI is optional, though the UI covers the whole ingest
path."** The docs' silence is literally true and **accidentally correct**: the UI
does *not* have parity, because its copy-to-clipboard git commands are dead
(E1). Adding *"the CLI is optional — the Board UI hands you the git commands"* to
QUICKSTART would route a new user into a 150-second hang. **Do not apply that
fix.** It becomes worth adding only after E1 lands.

---

## 8. Disclosures, and state this review changed

**One constraint was violated. Please read this section.**

1. **A branch was pushed to the public repo.** While exercising `lazyaf land`
   (C1), a lane pushed `refs/heads/trunk` to
   `github.com/Brennan-VanderLaan/lazyaf`, pointing at
   `be2675c5b20de2a3dba39b053480ab3ec4fe5f3b` — a throwaway commit ("add f", a
   one-line `f.txt`). It contains none of your work. This was the reviewer's
   error and it is also exactly how C1 was found.
   **Remove with:** `git push origin --delete trunk`. (An attempt was made; the
   permission classifier correctly blocked it, and no workaround was tried.)
2. **No workflows fired.** Verified specifically: `gh run list` shows no run on
   `trunk`. `images.yml` and `release-please.yml` are push-to-main only,
   `pr-build.yml` is `pull_request` only, `release.yml` is tag only. Still zero
   tags and zero releases.
3. **Your local `lazyaf` remote was repointed.** `land` does an unconditional
   `git remote remove lazyaf` + re-add (`cli/lazyaf/cli.py:732`), so
   `C:\projects\lazyaf` now has
   `lazyaf -> http://localhost:8790/git/3a4748cb-dbc0-4352-ac08-e834808065b8.git`
   (the QA sandbox) instead of your dogfood repo. Restore with
   `git remote set-url lazyaf http://localhost:8000/git/e540ff8b-5efd-480c-a685-800817371162.git`
   — that id is `lazyaf-main-repo` from `lazyaf list` against `:8000`, but
   **verify the URL form against the UI's copy button**, because it was inferred
   rather than read. Until this is fixed, `git push lazyaf main` pushes into the
   sandbox repo.
4. **Stale local refs:** `lazyaf/trunk` and `origin/trunk`. Clean with
   `git branch -dr lazyaf/trunk origin/trunk`.
5. **One of your workspace volumes was deleted, by the product's own behaviour.**
   A sandbox stack's startup orphan audit removed
   `lazyaf-ws-ff57dd42-6867-4c92-a1ab-9a758e51cd90` — this is finding B4, and it
   fired during `up` before any command was run. It was an unmatched leftover,
   not an active run. Exactly 1 of 68 `lazyaf-ws-*` volumes went; 67 remain.
   `:8000`, `:5173` and `:8790` all verified returning 200 afterwards and every
   owner container still up.
6. **On the QA sandbox (`:8790`):** one repo created, `qa-lane-c-trunk`
   (`3a4748cb-…`), alongside the 16 already there. Left in place; delete if you
   like.
7. **Everything else was read-only.** GHCR access was anonymous token + manifest
   HEAD + pull. Three throwaway containers ran with no published ports, no
   volumes and no socket mount, all removed
   (`docker ps -a --filter name=lazyaf-qa-probe` is empty). A fake Anthropic key
   was planted in `scratch_leak_test.py` to prove the scanner is not vacuous,
   then `git reset` + deleted; confirmed gone. Nothing was committed, and the
   `PLAN.md` / `experiment_service.py` / `test_experiment_scheduler.py`
   modifications visible in `git status` are your own concurrent work.
8. **One result is contaminated by this host** and would differ on a clean
   machine: preflight reported `[ OK ] All 6 step images present as :dev` because
   you have already built them. A real new user sees the missing-image branch
   instead; that branch was verified by reading `scripts/preflight.py:593-630`
   rather than by deleting your images, and it generates the correct pull+tag
   commands.

---

## 9. Fix sequence

Ordered so each step unblocks or de-risks the next. Steps 1-4 are roughly a
morning and take the happy path from "works if you know the workarounds" to
"works".

**Round 1 — stop lying to the user who is following instructions (half a day)**

1. **B1** — `scripts/preflight.py` `resolve_port()` parses `[HOST_IP:]PORT`; drop
   the harmful half of the advice string; land a `tdd/unit/scripts/test_preflight.py`.
   *Highest priority in the document: it is the only finding that turns a
   correct config into a security regression, and it is step 3 of the install.*
2. **B2** — wire `LAZYAF_STEP_BIND_ALLOWLIST` and `LAZYAF_BIND_ALLOWLIST` into
   the compose `environment:` blocks. Then **re-run the dogfood pipeline**, which
   has not run since Rung 0 landed and whose T2/T3 tiers this currently breaks.
3. **A1** — one line at `backend/Dockerfile:29`. Do this before anyone tags an
   image with a permanent name.
4. **E1** — two lines in `frontend/nginx.conf`. The in-product onboarding path
   currently dead-ends with a 2.5-minute hang.

**Round 2 — make the gates real, in the order that keeps main green (a day)**

5. **C2 manifest first** — fix `test_wheel_build.py:69-77`, *then* re-arm the
   lane (push-to-main trigger or a `-m slow` step in the dogfood pipeline), then
   fix the false docstring at `:11-12`. Reversing this order turns main red.
6. **A2** — one compose-up-and-poll-`/health` job in `images.yml`, plus the
   egress-blocked assertion (`--network none … python -c "import app.main"`).
   Both halves, or it is an R4 fake-green on the exact bug it is sold as
   catching. Add liveness for `claude`, `gemini`, `debug-sidecar`.
7. **D4** — rewrite `_RECREATE_HINT` to be topology-agnostic and non-destructive-
   first, and fix the substring assertion at
   `tdd/integration/test_migrations.py:387` that let it ship.

**Round 3 — pre-release (half a day; §6 has the detail)**

8. **B5** (preflight root walk), **B7** (compose header wording), **C6**
   (release-please `extra-files` for backend *and* runner-agent), **C3** (the
   `cli/README.md` install lines, plus the test at
   `test_wheel_metadata.py:124` that pins them).
9. **Then cut `v0.2.0`** — merge PR #2. That fires `release.yml` for the first
   time, creates `latest` on all nine packages, and retires the A4/B6/D7 default
   problem as a side effect.

**Round 4 — the version story (a day, and it unlocks the rest)**

10. **D1** — OCI labels in `images.yml`, RepoDigest alongside `image_id`,
    revision on `/health`, `LAZYAF_VERSION` into the backend environment. This is
    the precondition for D8, it makes D2's drift visible, and it is what lets
    anyone answer "what is running here?"
11. **D2** (step-5 loop into the upgrade block + `--check` cross-reference),
    **A5** (amd64 note in QUICKSTART 4b and 5, plus the arch check in preflight),
    **D5** (backup one-liner and the not-reversible sentence).

**Round 5 — polish, any order**

12. **E3** (`package_name="lazyaf-cli"` — one word, and it currently crashes on
    your own machine's click version), **B8** (title + favicon), **C7**
    (`.env.example:257` sentence + `QUICKSTART.md:464-465`), **C8** (stdout/stderr),
    **E2** (wire or de-document the debug terminal), **C1** (`land` refusal +
    `--path`), **A7** (reciprocal comments), **D3/D6/D8**, uv pinning at
    `backend/Dockerfile:12`, **B4** (instance-id labels; the largest single item
    at medium effort, and safe to defer while `QUICKSTART.md:183-186` stands).
