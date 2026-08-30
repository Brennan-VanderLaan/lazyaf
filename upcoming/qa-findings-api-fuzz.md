# QA-1 — API fuzzing and input abuse

**Target:** isolated QA stack, `http://localhost:8790` (compose project `lazyaf-qa`)
**Date:** 2026-08-30
**Scope:** the whole REST surface enumerated from `GET /api/openapi.json` (117 operations across
`/api/*` and `/git/*`), hammered with malformed, hostile and stupid input.
**Regression tests:** `C:\projects\lazyaf\tdd\qa\test_api_fuzz_findings.py`
(83 tests: 46 guards that pass today, 37 `xfail(strict=True)` that encode the findings below).

Run them with:

```
python -m pytest -c tdd/qa/pytest.ini tdd/qa/test_api_fuzz_findings.py
LAZYAF_QA_BASE_URL=http://localhost:8790 python -m pytest -c tdd/qa/pytest.ini tdd/qa
```

> **Caveat on shared state.** The QA stack was being used concurrently by at least one other
> QA agent throughout this run (`/api/test/reset` fired several times mid-probe, and a parallel
> pipeline-run load generator was active). Every finding below was re-confirmed in isolation
> with a dedicated reproduction after the fact. Where I could *not* reproduce something on
> demand I say so explicitly rather than reporting it as solid.

---

## Ranked findings

| ID | Sev | One line |
|----|-----|----------|
| QA-API-01 | **BLOCKER** | `approve` marks a never-started card `done` with no merge and no state check |
| QA-API-02 | **MAJOR** | `PATCH {"<required field>": null}` → 500 on all 8 patchable entities |
| QA-API-03 | **MAJOR** | Concurrent create of one prompt-template name → 500 for *every* caller |
| QA-API-04 | **MAJOR** | Any integer ≥ 2^63 → 500 `OverflowError` from the SQLite driver |
| QA-API-05 | **MAJOR** | JSON `NaN`/`Infinity` literal turns any validation error into a plain-text 500 |
| QA-API-06 | **MAJOR** | Pipeline name goes raw into `Content-Disposition`: 500 on non-Latin-1, dropped connection on CR/LF/NUL |
| QA-API-07 | **MAJOR** | YAML export silently drops step `timeout`/`on_success`/`on_failure` and **all** triggers |
| QA-API-08 | **MAJOR** | Ingested repo reports `default_branch: main` while its git HEAD is `master` |
| QA-API-09 | **MAJOR** | `git-upload-pack` leaks raw Python exception text, with a 404 for a malformed body |
| QA-API-10 | **MAJOR** | Usage manifest accepts negative tokens, negative wall-clock and `gpu_fraction: 99999` |
| QA-API-16 | **MAJOR** | `sqlite3.OperationalError: database is locked` surfaces as a bare 500 under contention *(not deterministically reproducible)* |
| QA-API-11 | MINOR | Playground `internal/*` endpoints are unauthenticated and answer `{"ok":true}` for ghost sessions |
| QA-API-12 | MINOR | `PATCH /api/user-stories/{id}` silently ignores `feature_id` — 200, nothing happens |
| QA-API-13 | MINOR | No length / whitespace / control-char validation on any name or title anywhere |
| QA-API-17 | MINOR | `reject` has no state guard and destroys `branch_name` from any state |
| QA-API-18 | MINOR | Step-auth JWT secret defaults to a constant published in the repo, with no startup warning |
| QA-API-19 | MINOR | SQLAlchemy `echo=True` is hardcoded on the production engine |
| QA-API-14 | POLISH | Duplicate-name conflicts answer 400 in one router and 409 in another |
| QA-API-15 | POLISH | `GET /api/repos/{id}/commits?limit=` has no bound, unlike every other `limit` |
| QA-API-20 | POLISH | `Content-Disposition` filename is unquoted; `;`, `"` and `../` land in it verbatim |
| QA-API-21 | POLISH | `priority: true` is silently coerced to `1`; unvalidated `on_success`/trigger `type` strings stored verbatim |

---

## QA-API-01 — BLOCKER — `approve` promotes a card that never ran to `done`

**Repro**

```bash
R=$(curl -s -XPOST localhost:8790/api/repos/ingest -H 'Content-Type: application/json' \
      -d '{"name":"demo"}' | jq -r .id)
C=$(curl -s -XPOST localhost:8790/api/repos/$R/cards -H 'Content-Type: application/json' \
      -d '{"title":"never started"}' | jq -r .id)
curl -s localhost:8790/api/cards/$C | jq .status          # -> "todo",  branch_name: null
curl -s -XPOST localhost:8790/api/cards/$C/approve -H 'Content-Type: application/json' -d '{}' \
  | jq '{status: .card.status, merge: .merge_result}'
```

**What happened**

```json
{ "status": "done", "merge": null }
```

HTTP 200. The card moves `todo → done`. No branch existed, no merge ran, no agent ever
touched it. Repeated approves keep returning 200.

**What should happen**

`400`, the way `retry` already does it (`"Can only retry cards in 'failed' or 'in_review'
status, current: todo"`). Approving work that does not exist is not a valid transition.

**Root cause** — `backend/app/routers/cards.py:435-462`

```python
    # Only merge if card has a branch
    if card.branch_name and repo.is_ingested:
        ...                      # merge, conflict handling, error handling
    # Update card status to done only if merge succeeded
    old_status = card.status
    card.status = "done"
```

The comment on the last line is wrong: the merge block is entirely skipped for a branchless
card, and `card.status = "done"` runs unconditionally outside it. There is no check of
`card.status` anywhere in the handler.

**Why it is a BLOCKER for a demo:** the board is the demo. One stray click on a `todo` card's
approve button — or one replayed request — silently shows Done for work that was never done,
and there is no way to tell from the UI that nothing merged.

**Test:** `test_approve_rejects_a_card_that_never_ran` (xfail), plus
`test_approve_on_todo_card_is_currently_a_silent_done` which pins the current behaviour so the
blast radius stays visible until the guard lands.

---

## QA-API-02 — MAJOR — explicit `null` on a required field 500s on every entity

**Repro**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -XPATCH localhost:8790/api/cards/$C \
     -H 'Content-Type: application/json' -d '{"title": null}'
# 500
```

Confirmed on all eight patchable entities:

| endpoint | field | result |
|---|---|---|
| `PATCH /api/cards/{id}` | `title` | 500 |
| `PATCH /api/features/{id}` | `title` | 500 |
| `PATCH /api/user-stories/{id}` | `title` | 500 |
| `PATCH /api/criteria/{id}` | `text` | 500 |
| `PATCH /api/agent-files/{id}` | `name` | 500 |
| `PATCH /api/agent-files/{id}` | `content` | 500 |
| `PATCH /api/prompt-templates/{id}` | `name` | 500 |
| `PATCH /api/pipelines/{id}` | `name` | 500 |

Body is the bare string `Internal Server Error` — not even JSON, so a frontend that does
`res.json()` on the error path throws a second time.

**What should happen** — 422 with a field-level message. The row does survive (the transaction
rolls back), so this is a UX/robustness defect rather than corruption.

**Root cause** — every `*Update` schema types its required column as optional, e.g.
`backend/app/schemas/card.py:23`:

```python
class CardUpdate(BaseModel):
    title: str | None = None
```

Handlers then do `update.model_dump(exclude_unset=True)`, which correctly distinguishes
"absent" from "explicit null" — but nothing rejects the explicit null, so it reaches the DB:

```
sqlite3.IntegrityError: NOT NULL constraint failed: cards.title
```

**Fix sketch:** either keep the field non-nullable in the update model, or add a
`field_validator` that rejects `None` for the columns that are `NOT NULL`. A blanket
`IntegrityError → 409/422` handler would also stop the bare 500, and would cover QA-API-03.

**Test:** `test_patch_explicit_null_on_required_field_is_a_4xx` (xfail),
`test_patch_explicit_null_leaves_the_row_intact` (guard — the rollback is correct, keep it).

---

## QA-API-03 — MAJOR — concurrent duplicate prompt-template name → 500 for everyone

**Repro** — 20 threads POSTing the same name:

```python
name = "race-pt-" + uuid.uuid4().hex[:8]
# 20 x requests.post(BASE + "/api/prompt-templates", json={"name": name})
```

**What happened** — three consecutive trials:

```
trial 0: Counter({500: 20})                  # nobody won; the template was never created
trial 1: Counter({409: 16, 500: 3, 201: 1})
trial 2: Counter({500: 10, 409: 9, 201: 1})
```

Trial 0 is the bad one: **all twenty callers got a 500 and no row was created.**

**What should happen** — exactly one 201, nineteen 409s, zero 5xx.

**Root cause** — `backend/app/routers/spec.py:776-788` is check-then-insert against a column
that carries a real `UNIQUE` constraint, with no handler for the losing race:

```python
async def create_prompt_template(template, db):
    await _ensure_template_name_unique(db, template.name)   # SELECT
    db_template = PromptTemplate(...)
    db.add(db_template)
    await db.commit()                                        # INSERT -> IntegrityError
```

Backend log:

```
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: prompt_templates.name
[SQL: INSERT INTO prompt_templates (id, name, description, content, created_at, updated_at) ...]
```

**Note:** `POST /api/agent-files` has the *same* check-then-insert shape
(`backend/app/routers/agent_files.py:22-31`) but its column has no unique constraint, so it
survives the race by serialising on SQLite's write lock instead. On a database with real
concurrency it would produce **duplicate rows**, and `GET /api/agent-files/by-name/{name}`
uses `scalar_one_or_none()` — which raises `MultipleResultsFound` — so that lookup would
start 500ing permanently. I could not produce duplicates on SQLite and am **not** reporting it
as a confirmed finding, but the shape is worth fixing alongside QA-API-03.

**Test:** `test_concurrent_duplicate_prompt_template_name_never_500s` (xfail).

---

## QA-API-04 — MAJOR — integers ≥ 2^63 crash the driver with a 500

**Repro**

```bash
curl -s -w '\n%{http_code}\n' -XPOST localhost:8790/api/user-stories \
  -H 'Content-Type: application/json' \
  -d '{"feature_id":"'$F'","title":"ovf","priority":9223372036854775808}'
# Internal Server Error
# 500
```

`2^63 - 1` (9223372036854775807) works and stores fine; `2^63`, `10^30` and
`-(2^63) - 1` all 500.

**What should happen** — 422 (`Input should be less than or equal to …`).

**Root cause** — `backend/app/schemas/spec.py:57`, `priority: int | None = None` with no
bound; the value passes Pydantic and reaches the driver:

```
OverflowError: Python int too large to convert to SQLite INTEGER
```

This is not specific to `priority` — it is the generic shape of every unbounded `int` field
that reaches the DB (`UsageManifest.output_tokens` has the same exposure, see QA-API-10).

**Test:** `test_out_of_range_integer_is_a_4xx_not_a_500` (xfail, 3 params) plus
`test_largest_safe_integer_still_works` (guard for the boundary).

---

## QA-API-05 — MAJOR — `NaN` / `Infinity` turns any 422 into a plain-text 500

This one is **universal**: it applies to every endpoint on the service, not one route.

**Repro**

```bash
curl -s -w '\n%{http_code}\n' -XPOST localhost:8790/api/agent-files \
  -H 'Content-Type: application/json' -d '{"name":"x","content":NaN}'
# Internal Server Error
# 500
```

Same for `Infinity` and `-Infinity`.

**What should happen** — 400 or 422.

**Root cause** — a three-step trap:

1. Python's `json` module accepts the non-standard `NaN`/`Infinity` literals, so the body parses.
2. `content` fails validation (it wants a string), and FastAPI's default
   `request_validation_exception_handler` **echoes the offending value back** in the 422 body.
3. `JSONResponse.render()` calls `json.dumps(..., allow_nan=False)`, which refuses:

```
File ".../fastapi/exception_handlers.py", line 23, in request_validation_exception_handler
    return JSONResponse(
File ".../starlette/responses.py", line 192, in render
    return json.dumps(
ValueError: Out of range float values are not JSON compliant: nan
```

The exception escapes the handler, so the client gets uvicorn's fallback: HTTP 500 with the
literal body `Internal Server Error` (not JSON), and uvicorn then closes the connection —
which makes the *next* request on a keep-alive pool fail with a bare connection reset.

**Fix sketch:** install a `RequestValidationError` handler that scrubs non-finite floats out of
`exc.errors()` before rendering, or parse bodies with `parse_constant` rejecting these literals.

**Test:** `test_nonfinite_json_literal_is_a_4xx_not_a_500` (xfail, 3 params) and
`test_nonfinite_literal_500_is_bare_text_not_json` (pins the not-even-JSON detail).

---

## QA-API-06 — MAJOR — pipeline name lands raw in `Content-Disposition`

`GET /api/pipelines/{id}/export/yaml` builds a response header out of the user-supplied name.

**Repro**

```python
pid = POST /api/repos/{R}/pipelines  {"name": "中文-pipeline", "steps": []}
GET  /api/pipelines/{pid}/export/yaml
```

**What happened**

| pipeline name | export result |
|---|---|
| `Déploiement` | 200 (Latin-1 fits) |
| `中文-pipeline` | **500** `Internal Server Error` |
| `ship-it-🚀` | **500** |
| `Проверка` | **500** |
| `evil\r\nX-Injected: yes` | **connection aborted, no response at all** |
| `line1\nline2` | **connection aborted** |
| `nul\x00name` | **connection aborted** |

**What should happen** — 200 with an RFC 6266 `filename*=UTF-8''…` parameter and a sanitised
ASCII `filename` fallback.

**Root cause** — `backend/app/routers/pipelines.py:569`

```python
headers={"Content-Disposition": f"attachment; filename={pipeline.name.replace(' ', '_')}.yaml"}
```

Starlette latin-1 encodes every header value (`starlette/responses.py:62`), so anything above
U+00FF raises:

```
UnicodeEncodeError: 'latin-1' codec can't encode character '\U0001f680' in position 29
```

CR/LF/NUL get further than that and are rejected by h11 at the wire level, so uvicorn drops
the connection with no status line — the worst possible failure mode, because the client sees
a network error rather than an HTTP error.

**Demo relevance:** name a pipeline "Déploiement 🚀" and the Export button produces a 500 toast.
This is exactly the sort of name a demo audience types.

**Test:** `test_export_yaml_survives_a_non_latin1_pipeline_name` (xfail, 3 params),
`test_export_yaml_never_drops_the_connection` (xfail, 3 params),
`test_latin1_pipeline_name_still_exports` (guard).

---

## QA-API-07 — MAJOR — YAML export is lossy: step settings and all triggers vanish

**Repro**

```bash
curl -s -XPOST localhost:8790/api/repos/$R/pipelines -H 'Content-Type: application/json' -d '{
  "name":"roundtrip-demo",
  "steps":[{"name":"build","type":"script","config":{"script":"make"},
            "timeout":7200,"on_success":"deploy","on_failure":"rollback",
            "continue_in_context":true},
           {"name":"deploy","type":"script","config":{"script":"ship"},"timeout":60}],
  "triggers":[{"type":"push","config":{"branch":"main"},"enabled":true,
               "on_pass":"merge","on_fail":"revert"}]}'
curl -s localhost:8790/api/pipelines/$PID/export/yaml
```

**What happened**

```yaml
name: roundtrip-demo
description: null
version: 2
steps:
- name: build
  type: script
  config:
    script: make
- name: deploy
  type: script
  config:
    script: ship
```

Gone: `timeout: 7200`, `on_success: deploy`, `on_failure: rollback`,
`continue_in_context: true`, `timeout: 60`, and the entire `triggers` block. HTTP 200, no
warning. An operator who exports a pipeline, commits the YAML to `.lazyaf/pipelines/` and
re-imports it gets a **different pipeline**: no push trigger, default 300 s timeouts, and
failure routing reset to `stop`.

**What should happen** — either a faithful round-trip, or an explicit error/warning naming
what cannot be represented.

**Root cause** — `backend/app/routers/pipelines.py:551-561` (legacy branch) rebuilds each step
from `name`/`type`/`config` only:

```python
        for step in steps:
            step_export = {
                "name": step.get("name", "Unnamed"),
                "type": step.get("type", "script"),
            }
            if step.get("config"):
                step_export["config"] = step["config"]
            export_data["steps"].append(step_export)
```

and `export_data` (lines 499-503) never contains a `triggers` key in **either** the graph or
the legacy branch.

**Test:** `test_yaml_export_round_trips_step_settings_and_triggers` (xfail).

---

## QA-API-08 — MAJOR — ingested repo says `main`, its git HEAD says `master`

**Repro**

```bash
R=$(curl -s -XPOST localhost:8790/api/repos/ingest -H 'Content-Type: application/json' \
      -d '{"name":"demo","default_branch":"main"}' | jq -r .id)
curl -s localhost:8790/api/repos/$R           | jq -r .default_branch   # main
curl -s localhost:8790/api/repos/$R/branches  | jq -r .default_branch   # master
curl -s localhost:8790/git/$R.git/HEAD                                  # ref: refs/heads/master
```

The mismatch persists when the caller explicitly requests `main`, and equally for any other
requested name (`trunk`).

**What should happen** — one answer. The bare repo's HEAD should be set to the repo's
`default_branch`.

**Root cause** — `backend/app/services/git_server.py:59`

```python
        DulwichRepo.init_bare(str(repo_path))
```

`init_bare` writes `ref: refs/heads/master` into HEAD and is never told otherwise, while the
`Repo` row keeps `RepoCreate.default_branch`'s `"main"` default
(`backend/app/schemas/…RepoCreate`). Two sources of truth, silently disagreeing.

**Consequences:** the UI shows `main`; a `git clone` of the clone-url lands on `master`; the
first push creates `refs/heads/main` alongside a dangling HEAD; `/branches` and `/commits`
resolve the default differently from the repo detail panel.

**Test:** `test_ingested_repo_git_head_matches_its_default_branch` (xfail, 2 params).

---

## QA-API-09 — MAJOR — raw Python exception text leaks out of the git endpoint

**Repro**

```bash
curl -s -w '\n%{http_code}\n' -XPOST localhost:8790/git/$R.git/git-upload-pack --data-binary 'zzzz'
```

**What happened**

```json
{"detail":"invalid literal for int() with base 16: b'zzzz'"}
```

HTTP **404**.

Two defects in one: an internal `ValueError` string is handed to the client verbatim, and a
malformed request body is reported as "not found".

**What should happen** — `400 {"detail": "malformed pack protocol request"}`, with the real
exception logged server-side only.

**Root cause** — `backend/app/routers/git.py:99-104`

```python
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Git error: {e}")
```

Both arms interpolate the exception into the response body. The `Git error: {e}` arm is the
more dangerous of the two — a dulwich or filesystem exception will carry server paths.

**Test:** `test_malformed_git_upload_pack_does_not_leak_internals` (xfail, 2 params).

---

## QA-API-10 — MAJOR — the usage manifest accepts impossible accounting

`POST /api/steps/{id}/usage` validates its body before checking the `Authorization` header, so
a 401 response proves the body passed schema validation. All of these reach 401:

| field | value sent | verdict |
|---|---|---|
| `input_tokens` | `-5` | accepted |
| `output_tokens` | `-1` | accepted |
| `output_tokens` | `10**30` | accepted by schema → will 500 at insert (QA-API-04) |
| `wall_clock_ms` | `-99999` | accepted |
| `container_seconds` | `-1.0` | accepted |
| `gpu_fraction` | `99999.0` | accepted (`1.0` is documented as "exclusive") |
| `cost_usd` | `-1000000` | accepted |

**What should happen** — `ge=0` on the counters and the durations, `0 < gpu_fraction <= 1`,
`ge=0` on cost.

**Root cause** — `backend/app/schemas/usage.py:56-73`: every numeric field is a bare
`int | None` / `float | None` with no `Field(ge=…)`.

**Demo relevance:** the run usage rollup (`GET /api/pipeline-runs/{id}/usage`) is a dashboard.
One misbehaving or malicious runner reports `wall_clock_ms: -99999` and the panel shows a
negative duration; `-1000000` in `cost_usd` shows a negative spend that a viewer cannot
explain. `RunUsageRollup.build` sums whatever it is given.

**Credit where due:** `cost_usd` *is* correctly guarded against non-finite Decimals —
`"NaN"`, `"Infinity"` and `"-Infinity"` are all rejected with `finite_number`. That is the
right instinct applied to exactly one field.

**Test:** `test_usage_manifest_rejects_impossible_accounting` (xfail, 5 params) and
`test_usage_manifest_rejects_nonfinite_cost` (guard for the part that is already right).

---

## QA-API-16 — MAJOR — `database is locked` surfaces as a bare 500  *(not reliably reproducible)*

**Observed** — during a full test run under concurrent load from another agent,
`POST /api/repos/ingest` returned 500. Backend log:

```
sqlite3.OperationalError: database is locked
sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) database is locked
  File ".../sqlalchemy/dialects/sqlite/aiosqlite.py", line 313, in commit
```

**Honesty note:** I could **not** reproduce this on demand. 40 concurrent
`POST /api/features` and 25 concurrent `POST /api/repos/ingest` both came back 100 % 201. It
took genuinely mixed load (my 20-thread duplicate-name race running alongside another agent's
pipeline executions) to trigger it. I am reporting it because the log evidence is unambiguous
and the configuration explains it — **not** because I have a one-liner repro.

**Root cause** — `backend/app/database.py:15`

```python
engine = create_async_engine(settings.database_url, echo=True)
```

No `connect_args={"timeout": …}` (SQLite's `busy_timeout` defaults to 0, so a contended writer
fails immediately instead of waiting) and no WAL journal mode. Any write contention becomes a
500 rather than a short wait.

**No regression test written** — a test that only fails under unrelated load is worse than no
test. The `repo` fixture in the QA suite retries ingest three times and cites this finding.

---

## QA-API-11 — MINOR — playground `internal/*` endpoints: no auth, `ok:true` for ghosts

**Repro**

```bash
curl -s -XPOST localhost:8790/api/playground/no-such-session/internal/status \
     -H 'Content-Type: application/json' -d '{"status":"completed"}'
# {"ok":true}
curl -s -XPOST localhost:8790/api/playground/no-such-session/internal/log \
     -H 'Content-Type: application/json' -d '{"lines":["injected"]}'
# {"ok":true}
curl -s localhost:8790/api/playground/no-such-session/status
# {"detail":"Session not found"}   <-- the read side gets it right
```

Also: `{"status":"totally-made-up"}` is accepted — the status string is unvalidated.

**What should happen** — 404 for an unknown session, and an `Authorization` check to match
`/api/steps/*`.

**Root cause** — `backend/app/routers/playground.py:226-257`. None of the four `internal/*`
handlers takes an `authorization` header, and `playground_service.update_status` /
`append_logs` are no-ops for an unknown id.

Contrast with `backend/app/routers/steps.py`, whose module docstring explicitly documents
"zombie-token hardening (12.3 adversarial review)" and answers 409 on terminal executions. The
playground path never got the same treatment: it is the same class of container→backend
callback with none of the checks.

**Test:** `test_playground_internal_write_to_unknown_session_is_rejected` (xfail, 2 params),
`test_playground_reads_do_404_on_unknown_session` (guard).

---

## QA-API-12 — MINOR — re-parenting a user story silently does nothing

**Repro**

```bash
# story currently under feature $SRC
curl -s -XPATCH localhost:8790/api/user-stories/$S -H 'Content-Type: application/json' \
     -d '{"feature_id":"'$DST'"}' | jq -r .feature_id
# prints $SRC — unchanged. HTTP 200.
```

A *valid* destination feature id is ignored just as silently as a garbage one.

**What should happen** — either perform the move, or 422 "feature_id is not updatable".

**Root cause** — `backend/app/schemas/spec.py:64-68`: `UserStoryUpdate` has no `feature_id`
field, and Pydantic's default `extra='ignore'` drops unknown keys without complaint. The same
applies to every PATCH on the service: unknown fields are always silently discarded with a 200.

**Test:** `test_reparenting_a_story_either_works_or_fails_loudly` (xfail).

---

## QA-API-13 — MINOR — no length, whitespace or control-character validation anywhere

Every one of these was stored verbatim and returned by the list endpoints (HTTP 201):

| payload | field tested |
|---|---|
| `""` | agent-file `name`, repo `name`, card `title`, feature `title`, prompt-template `name` |
| `"   "`, `"\t\n  "` | same |
| `"abc\x00def"` (NUL) | same — round-trips through `GET` and even through `by-name` |
| `"a\x01\x02\x07\x08\x1bb"` | control characters |
| `"A" * 1_000_000` | 1 MB name |
| `"...hidden"`, `"name...   "` | leading dots, trailing dots + spaces |
| `"../../../etc/passwd"`, `"..\\..\\windows"`, `"/etc/passwd"`, `"~/.ssh/id_rsa"` | traversal |
| `"; rm -rf / #"`, `"$(id)`whoami`"` | shell metacharacters |
| `"'; DROP TABLE repos;--"` | SQL |
| `"<img src=x onerror=alert(1)><script>alert('xss')</script>"` | XSS |
| `"\u202Etxet_desrever\u202C"` | RTL override |
| `"a" + "\u0301"*300` | combining-mark stack |
| `"a\u200b\u200c\u200d\ufeffb"` | zero-width characters |

**Assessed severity: MINOR, not MAJOR.** I chased the traversal and injection payloads to see
whether any of them reach a filesystem path or a shell, and they do not:

* Agent-file names go into a `claude --agents` **JSON value**, not a filename
  (`backend/app/services/pipeline_executor.py:2454-2497`).
* Repo storage is keyed by the generated UUID, never by the name
  (`backend/app/services/git_server.py:47-60`).
* Branch names reach dulwich as ref strings, not as CLI arguments —
  `DELETE …/branches/--all` returns a clean `400 Branch '--all' does not exist`.

What is left is real but bounded: unbounded storage, and a set of strings that will look
broken or overflow their container the moment the UI renders them. The empty agent-file name
in particular makes `GET /api/agent-files/by-name/` unreachable (it 404s on the route), so
that record can never be fetched by name again. The stored XSS payloads are a latent risk that
QA-2/QA-3 should confirm against the actual frontend — at the API level they are simply
echoed as data.

**Fix sketch:** `Field(min_length=1, max_length=…)` plus a strip/`\x00`-reject validator on
the shared name/title base models.

**Test:** `test_blank_and_control_char_names_are_rejected` (xfail, 4 params),
`test_absurdly_long_name_is_rejected` (xfail),
`test_nul_byte_in_a_name_round_trips_verbatim` (pins current behaviour).

---

## QA-API-17 — MINOR — `reject` has no state guard and destroys the branch pointer

`POST /api/cards/{id}/reject` on a `todo` card returns 200. On a `done` card it also returns
200, sets `status = "todo"`, and nulls `branch_name` and `pr_url`
(`backend/app/routers/cards.py:478-495`) — irreversibly losing the only link from the card to
the work an agent produced. There is no confirmation, no state check, and no way to undo it.

Same missing-guard family as QA-API-01; listed separately because the consequence is data loss
rather than a false status.

---

## QA-API-18 — MINOR — step-auth JWT secret defaults to a published constant

`backend/app/services/control_layer/auth.py:14`

```python
_SECRET_KEY = "lazyaf-step-auth-secret-key-change-in-production"
```

`backend/app/config.py:110` makes it overridable via `LAZYAF_STEP_AUTH_SECRET`, and
`backend/app/main.py:43-46` wires it at startup — so this **is** configurable and I am not
calling it a MAJOR. The gap is that nothing warns when the default is still in use. A
self-hosted deployment that never sets the variable will accept step tokens minted from a
constant that is in the public source tree, letting anyone who can reach the backend write
logs, status, usage and test results into any step. `LAZYAF_RUNNER_AUTH_SECRET` has the same
shape (`config.py:113`).

**Fix sketch:** log a startup WARNING (or refuse to start outside test mode) when
`settings.step_auth_secret` still equals the module default — the same treatment
`WEB_CONCURRENCY > 1` already gets a few lines below in `main.py`.

---

## QA-API-19 — MINOR — `echo=True` hardcoded on the production engine

`backend/app/database.py:15` sets `echo=True` unconditionally, so every SQL statement is
written to the log twice (once by SQLAlchemy's logger, once by the echo handler). During this
session `docker logs` for the QA backend was almost entirely SELECT statements; finding an
actual traceback required filtering thousands of lines. That is a real cost during a live
demo or an incident, and a measurable throughput cost besides.

---

## QA-API-14 — POLISH — duplicate-name conflicts use two different status codes

`POST /api/agent-files` with an existing name → **400**
(`backend/app/routers/agent_files.py:26`).
`POST /api/prompt-templates` with an existing name → **409**
(`backend/app/routers/spec.py:777`).

Same class of conflict, two codes. A client cannot write one handler.

**Test:** `test_duplicate_name_conflicts_use_409_everywhere` (xfail).

---

## QA-API-15 — POLISH — `commits?limit` is the only unbounded `limit`

`GET /api/repos/{id}/commits?limit=-1`, `limit=0` and `limit=1000000000000` are all accepted
(200). Every sibling bounds it: `/api/pipeline-runs` (`ge=1, le=100`),
`/api/pipelines/{id}/runs` (`ge=1, le=100`), `/api/test-refs` (`ge=1, le=1000`),
`/api/criteria/{id}/history` (`ge=1, le=500`).

**Test:** `test_commits_limit_is_bounded` (xfail, 3 params).

---

## QA-API-20 — POLISH — `Content-Disposition` filename is unquoted

Same line as QA-API-06 (`pipelines.py:569`), but a separate defect: the filename is not
quoted, so caller-controlled `;`, `"` and `/` land in the header structure:

| pipeline name | resulting header |
|---|---|
| `x; filename=other.sh` | `attachment; filename=x;_filename=other.sh.yaml` |
| `has"quote.yaml` | `attachment; filename=has"quote.yaml.yaml` |
| `../../../../etc/cron.d/evil` | `attachment; filename=../../../../etc/cron.d/evil.yaml` |
| 1 KB of `N` | a 1 KB header value |

Browsers strip path components from a download filename, so this is not a write-anywhere
primitive — hence POLISH. It is still a malformed header that any strict client may reject.

---

## QA-API-21 — POLISH — silent type coercion and unvalidated enum-ish strings

* `{"priority": true}` is stored as `1`; `{"priority": "42"}` is stored as `42`. Pydantic's
  lax mode, but surprising on a field a UI will render.
* Step `on_success` / `on_failure` accept any string —
  `"SELF_DESTRUCT"` and `"../../etc"` are stored verbatim and only discovered at run time.
* Trigger `type` accepts any string, including `""` and `"summon_demon"` (201).
* Step `timeout` accepts `-1`, `0` and `10**30`.
* Duplicate JSON keys silently take the last value (`{"name":"dup1","name":"dup2"}` → `dup2`).
* A pipeline may be created with duplicate step names, an empty step name, or a `script` step
  whose `config` has no `script` key — all 201, all fail only when someone presses Run.

---

# Verified NOT a bug

These were probed deliberately and behaved correctly. Guard tests exist for most of them in
`tdd/qa/test_api_fuzz_findings.py` so they stay correct.

**Body and content-type handling** — clean 422 for: no body at all; empty body; truncated JSON
(`{`); a JSON array, scalar or `null` where an object is required; `Content-Type: text/plain`,
`application/xml` or `application/x-www-form-urlencoded` carrying JSON; and a missing
`Content-Type`. Invalid UTF-8 bytes give a clean `400 "There was an error parsing the body"`.

**Type confusion** — string/int/float/bool/array/object/null in a string field all give a
precise 422 naming the field. Missing required fields likewise. Extra unexpected fields
(including `id`, `created_at`, `__proto__`) are ignored and cannot overwrite server-assigned
columns.

**Deeply nested JSON** — 100 / 300 / 500 / 900 / 1500 / 3000 levels of nesting all handled
without a recursion error; `/health` stays 200 after each. (My first pass logged this as a
crash; it was a poisoned keep-alive connection left behind by the QA-API-05 500, not a
nesting failure. Corrected here rather than reported.)

**Large payloads** — 1 MB and 10 MB strings, a 200 000-element array and a 2 MB nested object
are all accepted and stored without error or timeout. Error responses do **not** amplify: a
5 MB body that fails validation produces a 111-byte 422, because only the failing field is
echoed.

**Path parameters** — malformed UUIDs, `' OR '1'='1`, 5 000-character ids, `%00`, `%20` and
wrong-entity UUIDs (a repo id passed as a card id, etc.) all give a clean, correctly-worded
404 across `/api/cards`, `/api/repos`, `/api/pipelines`, `/api/step-runs`, `/api/jobs`,
`/api/runners`, `/api/criteria/{id}/history`, `/api/features/{id}/stories` and
`/api/pipelines/{id}/export/yaml`. URL-encoded traversal in a path segment
(`/api/agent-files/by-name/..%2F..%2Fhealth`,
`/api/repos/{id}/lazyaf/agents/..%2F..%2F..%2Fetc%2Fpasswd`) does not escape the route.

**No SQL injection found.** Every injection string reached the database as a bound parameter.
The routers use SQLAlchemy `select()` throughout; I found no string-built SQL.

**No shell injection found.** `; rm -rf /`, `$(id)`, backticks, `--all`, `-D`,
`--upload-pack=id` and `--output=/tmp/pwn` were pushed through repo names, branch names,
`commits?branch=`, `diff?base=&head=` and `DELETE /branches/{name}`. Git work goes through
dulwich (in-process, no shell), so these are treated as ref strings and rejected as
non-existent refs.

**`POST /api/repos/ingest` with a server-side `path`** — `/etc`, `/`, `/app`, `/root`,
`/proc/self`, `~`, `../../../`, `C:\Windows` all give `400 "Local path is not a git
repository"`. No local-file disclosure.

**Git smart-HTTP** — `?service=` is whitelisted (`400 Invalid service` for anything else),
missing `service` is a 422, an unknown repo id is a 404, and a traversal repo id does not
match the route.

**Query-parameter bounds** — `/api/test-refs?limit=0` and `?offset=-1`,
`/api/pipeline-runs?limit=101` and `/api/models?refresh=yesplease` all give a clean 422 with
the constraint spelled out. `/api/test-refs?status=<script>` gives
`400 "Invalid status '<script>': valid values are active, orphan"`.

**Referential integrity on create** — creating a user story under an unknown feature, a
criterion under an unknown story, or a feature naming unknown `repo_ids` are all rejected
(404 / 404 / `400 "Unknown repo IDs: …"`).

**Cascade deletes** — deleting a feature removes its stories and their criteria; deleting a
repo removes its cards and pipelines; the deleted children are then 404 rather than dangling.
Acting on a deleted card (`approve`, `delete` again) is a clean 404.

**Pipeline graph validation** — `steps_graph` is properly validated: `"Pipeline must have at
least one entry point"` for an empty graph, `"Entry point 'ghost' references non-existent
step"` for a dangling entry point, and a required `id` on every node and edge. Enum fields
(`step.type`, `card.runner_type`, `card.step_type`, usage `provider` / `cost_source` /
`version`) all reject unknown values with a 422 that lists the valid ones.

**Usage manifest, non-finite cost** — `cost_usd: "NaN"` / `"Infinity"` / `"-Infinity"`
correctly rejected with `finite_number`.

**Step callback auth** — all five `/api/steps/{id}/*` endpoints require an `Authorization`
header and answer `401 "Missing Authorization header"` without it.

**Card `retry` guards its state machine** — `400 "Can only retry cards in 'failed' or
'in_review' status, current: todo"`. This is exactly the guard QA-API-01 and QA-API-17 are
missing, and it proves the pattern is understood elsewhere in the same file.

**`POST /api/features/seed-milestone12` is idempotent** — calling it twice returns the same
feature id and does not duplicate rows.

**`GET /api/models`** — returns a static catalogue in ~0 s with and without `?refresh=true`,
with no API keys configured. No hang, no upstream error leak.

**Concurrent creation is mostly safe** — 40 simultaneous `POST /api/features` and 25
simultaneous `POST /api/repos/ingest` all returned 201 with no duplicates or errors. 16
simultaneous creates of the same agent-file name produced exactly one 201 and fifteen clean
400s with a single row stored.

---

# Notes for whoever fixes these

Two shared fixes cover six findings:

1. **A global `IntegrityError` / `OperationalError` handler** that maps to 409 / 503 instead of
   letting a bare 500 escape would fix QA-API-03 and take the sting out of QA-API-02 and
   QA-API-16.
2. **A `RequestValidationError` handler that scrubs non-finite floats** fixes QA-API-05 across
   the entire service in one place.

Two findings are one-line fixes with outsized demo value: QA-API-06 (quote and RFC-6266-encode
the filename) and QA-API-08 (set HEAD after `init_bare`).

QA-API-01 is the one I would fix first regardless of effort: it is the only finding that puts a
wrong fact on the board a demo audience is looking at.
