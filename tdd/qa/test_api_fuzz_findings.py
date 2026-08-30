"""Regression tests for QA-1 (API fuzzing / input abuse) findings.

Report: upcoming/qa-findings-api-fuzz.md

Tests that ENCODE A BUG are marked `xfail(strict=True)`: they fail today, and
the moment the bug is fixed pytest reports XPASS -> failure, so the finding
cannot be silently closed. Tests that assert already-correct behaviour are
plain passing tests and act as guards against regressions.

Run:  pytest tdd/qa/test_api_fuzz_findings.py
      LAZYAF_QA_BASE_URL=http://localhost:8790 pytest tdd/qa
"""
import json
import os
import threading
import time
import uuid
from collections import Counter

import pytest
import requests

# (no module-level marker: tdd/qa/pytest.ini is shared and its marker list
# is owned by other lanes)

# Self-contained on purpose: tdd/qa/conftest.py is shared with other QA lanes
# and its `repo` / `base_url` fixtures have a different shape (a dict, not an
# id). Module-level fixtures override conftest ones for this file only, so the
# lanes cannot collide and this module imports nothing from conftest.
BASE_URL = (
    os.environ.get("LAZYAF_QA_BASE_URL")
    or os.environ.get("QA_BASE_URL")
    or "http://localhost:8790"
).rstrip("/")

TIMEOUT = float(os.environ.get("LAZYAF_QA_TIMEOUT", "90"))


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class _Api:
    """Thin HTTP client bound to the live QA backend.

    `Connection: close` on purpose: several findings here make uvicorn drop the
    connection mid-response, which poisons a keep-alive pool and turns one real
    failure into a cascade of confusing ones.
    """

    def __init__(self, base):
        self.base = base
        self._s = requests.Session()
        self._s.headers.update({"Connection": "close"})

    def _req(self, method, path, **kw):
        kw.setdefault("timeout", TIMEOUT)
        return self._s.request(method, self.base + path, **kw)

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, **kw):
        return self._req("POST", path, **kw)

    def patch(self, path, **kw):
        return self._req("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self._req("DELETE", path, **kw)

    def close(self):
        self._s.close()


@pytest.fixture(scope="module", autouse=True)
def _qa1_require_live_backend():
    try:
        requests.get(f"{BASE_URL}/health", timeout=15).raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure means "no target"
        pytest.skip(f"QA backend not reachable at {BASE_URL}: {exc}",
                    allow_module_level=True)


@pytest.fixture
def base_url():
    return BASE_URL


@pytest.fixture
def api():
    client = _Api(BASE_URL)
    yield client
    client.close()


@pytest.fixture
def repo(api):
    """A freshly ingested repo id (a real bare git repo sits behind it).

    Retries: `POST /api/repos/ingest` has been observed answering 500
    (`sqlite3.OperationalError: database is locked`) under write contention on
    the shared stack - that is QA-API-16, not something these tests
    should trip over.
    """
    last = None
    for _ in range(3):
        last = api.post("/api/repos/ingest", json={"name": unique("qa1-fuzz")})
        if last.status_code == 201:
            return last.json()["id"]
        time.sleep(1)
    pytest.fail(f"could not ingest a repo: {last.status_code} {last.text[:200]}")


@pytest.fixture
def plain_repo(api):
    """A repo record with no git initialisation - cheap, for CRUD-only tests."""
    r = api.post("/api/repos", json={"name": unique("qa1-plain")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def feature(api):
    r = api.post("/api/features", json={"title": unique("qa1-feat")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# =============================================================================
# QA-API-01  BLOCKER - approve has no state machine guard
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-01: POST /api/cards/{id}/approve marks a 'todo' "
           "card with no branch as 'done' without merging anything "
           "(backend/app/routers/cards.py:435-462)",
)
def test_approve_rejects_a_card_that_never_ran(api, repo):
    card = api.post(f"/api/repos/{repo}/cards", json={"title": "never-started"}).json()
    assert card["status"] == "todo"
    assert card.get("branch_name") in (None, "")

    r = api.post(f"/api/cards/{card['id']}/approve", json={})

    # Approving work that does not exist must be refused, and the card must
    # not silently land in 'done'.
    assert r.status_code == 400, (
        f"approve on a todo/branchless card returned {r.status_code}: {r.text[:300]}"
    )
    after = api.get(f"/api/cards/{card['id']}").json()
    assert after["status"] == "todo"


def test_approve_on_todo_card_is_currently_a_silent_done(api, repo):
    """Documents the CURRENT (wrong) behaviour so the blast radius is explicit.

    This test passes today. It is the mirror of the xfail above: when the guard
    lands, this one must be deleted along with it.
    """
    card = api.post(f"/api/repos/{repo}/cards", json={"title": "silent-done"}).json()
    r = api.post(f"/api/cards/{card['id']}/approve", json={})
    assert r.status_code == 200
    assert r.json()["card"]["status"] == "done"
    assert r.json().get("merge_result") is None, "nothing was merged, yet the card is done"


# =============================================================================
# QA-API-02  MAJOR - PATCH <required field>: null -> 500 on every entity
# =============================================================================

def _patch_null_targets(api, repo, feature):
    card = api.post(f"/api/repos/{repo}/cards", json={"title": "nullme"}).json()["id"]
    story = api.post(
        "/api/user-stories", json={"feature_id": feature, "title": "nullme"}
    ).json()["id"]
    crit = api.post(
        "/api/criteria", json={"user_story_id": story, "text": "nullme"}
    ).json()["id"]
    af = api.post(
        "/api/agent-files", json={"name": unique("nullme-af"), "content": "x"}
    ).json()["id"]
    tpl = api.post("/api/prompt-templates", json={"name": unique("nullme-pt")}).json()["id"]
    pipe = api.post(f"/api/repos/{repo}/pipelines", json={"name": "nullme"}).json()["id"]
    return [
        (f"/api/cards/{card}", "title"),
        (f"/api/features/{feature}", "title"),
        (f"/api/user-stories/{story}", "title"),
        (f"/api/criteria/{crit}", "text"),
        (f"/api/agent-files/{af}", "name"),
        (f"/api/agent-files/{af}", "content"),
        (f"/api/prompt-templates/{tpl}", "name"),
        (f"/api/pipelines/{pipe}", "name"),
    ]


@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-02: every *Update schema types its required "
           "column as `str | None = None`, so an explicit JSON null passes "
           "validation and hits NOT NULL at commit -> 500 "
           "(e.g. backend/app/schemas/card.py:23)",
)
def test_patch_explicit_null_on_required_field_is_a_4xx(api, repo, feature):
    offenders = []
    for path, field in _patch_null_targets(api, repo, feature):
        r = api.patch(path, json={field: None})
        if r.status_code >= 500:
            offenders.append(f"{path} {{{field}: null}} -> {r.status_code}")
    assert not offenders, "explicit null produced a 5xx on:\n  " + "\n  ".join(offenders)


def test_patch_explicit_null_leaves_the_row_intact(api, repo, feature):
    """The 500 is ugly, but at least the rollback holds - assert that it does."""
    card = api.post(f"/api/repos/{repo}/cards", json={"title": "survivor"}).json()["id"]
    api.patch(f"/api/cards/{card}", json={"title": None})
    after = api.get(f"/api/cards/{card}")
    assert after.status_code == 200
    assert after.json()["title"] == "survivor"


# =============================================================================
# QA-API-03  MAJOR - concurrent create of the same prompt-template name -> 500
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-03: create_prompt_template does check-then-insert "
           "with no IntegrityError handler, so concurrent creates of one name "
           "return 500 instead of 409 (backend/app/routers/spec.py:776-788)",
)
def test_concurrent_duplicate_prompt_template_name_never_500s(api, base_url):
    name = unique("race-pt")
    codes = []
    lock = threading.Lock()

    def go():
        try:
            r = requests.post(
                f"{base_url}/api/prompt-templates", json={"name": name}, timeout=60
            )
            code = r.status_code
        except Exception as exc:  # connection aborted counts as a failure too
            code = f"EXC:{type(exc).__name__}"
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = Counter(codes)
    bad = [c for c in codes if not isinstance(c, int) or c >= 500]
    assert not bad, f"concurrent duplicate creates produced server errors: {counts}"
    assert counts.get(201, 0) == 1, f"exactly one create should win: {counts}"


# =============================================================================
# QA-API-04  MAJOR - integers >= 2**63 crash the SQLite driver with a 500
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-04: an int field with no upper bound reaches "
           "sqlite3 and raises OverflowError -> 500 "
           "(UserStoryCreate.priority, backend/app/schemas/spec.py:57)",
)
@pytest.mark.parametrize("priority", [2 ** 63, 10 ** 30, -(2 ** 63) - 1])
def test_out_of_range_integer_is_a_4xx_not_a_500(api, feature, priority):
    r = api.post(
        "/api/user-stories",
        json={"feature_id": feature, "title": "overflow", "priority": priority},
    )
    assert r.status_code < 500, f"priority={priority} -> {r.status_code} {r.text[:200]}"


def test_largest_safe_integer_still_works(api, feature):
    """2**63-1 is the boundary that must keep working once bounds are added."""
    r = api.post(
        "/api/user-stories",
        json={"feature_id": feature, "title": "boundary", "priority": 2 ** 63 - 1},
    )
    assert r.status_code == 201, r.text
    assert r.json()["priority"] == 2 ** 63 - 1


# =============================================================================
# QA-API-05  MAJOR - JSON NaN/Infinity literals turn any 422 into a 500
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-05: python's json parser accepts the non-standard "
           "NaN/Infinity literals; FastAPI echoes the offending value into the "
           "422 body and JSONResponse.render() refuses to serialise it, so the "
           "response becomes a plain-text 500",
)
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_json_literal_is_a_4xx_not_a_500(api, literal):
    body = '{"name":"nonfinite-probe","content":%s}' % literal
    r = api.post(
        "/api/agent-files", data=body, headers={"Content-Type": "application/json"}
    )
    assert r.status_code < 500, f"{literal} -> {r.status_code} {r.text[:200]}"


def test_nonfinite_literal_500_is_bare_text_not_json(api):
    """Demo risk: the 500 body is not even JSON, so a UI toast shows raw text."""
    r = api.post(
        "/api/agent-files",
        data='{"name":"nf","content":NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 500
    with pytest.raises(ValueError):
        r.json()


# =============================================================================
# QA-API-06  MAJOR - pipeline name lands raw in Content-Disposition
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-06: backend/app/routers/pipelines.py:569 "
           "interpolates the pipeline name into Content-Disposition; starlette "
           "latin-1 encodes headers, so any codepoint > U+00FF raises "
           "UnicodeEncodeError -> 500",
)
@pytest.mark.parametrize(
    "name",
    ["中文-pipeline", "ship-it-\U0001F680", "Проверка"],
)
def test_export_yaml_survives_a_non_latin1_pipeline_name(api, plain_repo, name):
    pid = api.post(f"/api/repos/{plain_repo}/pipelines", json={"name": name}).json()["id"]
    r = api.get(f"/api/pipelines/{pid}/export/yaml")
    assert r.status_code == 200, f"name={name!r} -> {r.status_code} {r.text[:200]}"


@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-06b: a pipeline name containing CR/LF/NUL makes "
           "h11 refuse to serialise the Content-Disposition header; uvicorn "
           "drops the connection with no response at all",
)
@pytest.mark.parametrize("name", ["evil\r\nX-Injected: yes", "line1\nline2", "nul\x00name"])
def test_export_yaml_never_drops_the_connection(api, plain_repo, name):
    pid = api.post(f"/api/repos/{plain_repo}/pipelines", json={"name": name}).json()["id"]
    try:
        r = api.get(f"/api/pipelines/{pid}/export/yaml")
    except requests.exceptions.ConnectionError as exc:
        pytest.fail(f"name={name!r}: server closed the connection: {exc}")
    assert r.status_code < 500
    # and nothing the caller supplied may become a real response header
    assert "X-Injected" not in r.headers


def test_latin1_pipeline_name_still_exports(api, plain_repo):
    """Guard: the ASCII/latin-1 path must keep working after the fix."""
    pid = api.post(
        f"/api/repos/{plain_repo}/pipelines", json={"name": "Déploiement"}
    ).json()["id"]
    r = api.get(f"/api/pipelines/{pid}/export/yaml")
    assert r.status_code == 200


# =============================================================================
# QA-API-07  MAJOR - YAML export silently drops step settings and all triggers
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-07: export_pipeline_yaml rebuilds each step from "
           "name/type/config only and never emits triggers, so an exported "
           "pipeline re-imports as a DIFFERENT pipeline "
           "(backend/app/routers/pipelines.py:499-503, 551-561)",
)
def test_yaml_export_round_trips_step_settings_and_triggers(api, plain_repo):
    steps = [
        {
            "name": "build",
            "type": "script",
            "config": {"script": "make"},
            "timeout": 7200,
            "on_success": "deploy",
            "on_failure": "rollback",
            "continue_in_context": True,
        }
    ]
    triggers = [
        {"type": "push", "config": {"branch": "main"}, "enabled": True,
         "on_pass": "merge", "on_fail": "revert"}
    ]
    pid = api.post(
        f"/api/repos/{plain_repo}/pipelines",
        json={"name": unique("roundtrip"), "steps": steps, "triggers": triggers},
    ).json()["id"]

    text = api.get(f"/api/pipelines/{pid}/export/yaml").text
    missing = [tok for tok in ("timeout", "7200", "rollback", "continue_in_context",
                               "triggers", "push") if tok not in text]
    assert not missing, f"export lost {missing}; got:\n{text}"


# =============================================================================
# QA-API-08  MAJOR - ingest reports default_branch 'main' but git HEAD is master
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-08: GitRepoManager.create_bare_repo calls "
           "dulwich init_bare, which sets HEAD to refs/heads/master, while the "
           "Repo row keeps RepoCreate's 'main' default "
           "(backend/app/services/git_server.py:59)",
)
@pytest.mark.parametrize("requested", ["main", "trunk"])
def test_ingested_repo_git_head_matches_its_default_branch(api, requested):
    rid = api.post(
        "/api/repos/ingest", json={"name": unique("qa-head"), "default_branch": requested}
    ).json()["id"]

    record = api.get(f"/api/repos/{rid}").json()
    branches = api.get(f"/api/repos/{rid}/branches").json()
    head = api.get(f"/git/{rid}.git/HEAD").text.strip()

    assert record["default_branch"] == requested
    assert branches["default_branch"] == requested, (
        f"repo record says {record['default_branch']!r} but /branches says "
        f"{branches['default_branch']!r}"
    )
    assert head == f"ref: refs/heads/{requested}", f"git HEAD is {head!r}"


# =============================================================================
# QA-API-09  MAJOR - raw Python exception text leaks out of the git endpoint
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-09: backend/app/routers/git.py:99-104 returns "
           "str(exc) as the HTTP detail (and picks 404 for a malformed body), "
           "leaking internal exception text to the client",
)
@pytest.mark.parametrize("body", [b"garbage" * 100, b"zzzz"])
def test_malformed_git_upload_pack_does_not_leak_internals(api, repo, body):
    r = api.post(f"/git/{repo}.git/git-upload-pack", data=body)
    assert r.status_code == 400, f"malformed pack body -> {r.status_code}"
    detail = json.dumps(r.json())
    for leak in ("invalid literal", "base 16", "Traceback", "b'"):
        assert leak not in detail, f"internal detail leaked: {detail[:200]}"


# =============================================================================
# QA-API-10  MAJOR - usage manifest accepts negative / absurd accounting values
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-10: UsageManifest declares plain int/float for "
           "token counts, wall_clock_ms, container_seconds and gpu_fraction "
           "with no ge=0 bound, so a runner can poison the cost dashboard "
           "(backend/app/schemas/usage.py:56-73)",
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("input_tokens", -5),
        ("output_tokens", -1),
        ("wall_clock_ms", -99999),
        ("container_seconds", -1.0),
        ("gpu_fraction", 99999.0),
    ],
)
def test_usage_manifest_rejects_impossible_accounting(api, field, value):
    manifest = {
        "version": 1,
        "provider": "anthropic",
        "cost_source": "cli-reported",
        "wall_clock_ms": 100,
        field: value,
    }
    # No Authorization header: a 401 means the body already passed validation,
    # which is exactly the gap. A 422 means the schema caught it (desired).
    r = api.post("/api/steps/probe-step/usage", json=manifest)
    assert r.status_code == 422, (
        f"{field}={value} was accepted by the schema (got {r.status_code})"
    )


def test_usage_manifest_rejects_nonfinite_cost(api):
    """Guard: the Decimal cost field IS correctly bounded today - keep it so."""
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = api.post(
            "/api/steps/probe-step/usage",
            json={
                "version": 1,
                "provider": "anthropic",
                "cost_source": "cli-reported",
                "wall_clock_ms": 1,
                "cost_usd": bad,
            },
        )
        assert r.status_code == 422, f"cost_usd={bad} slipped through"


# =============================================================================
# QA-API-11  MINOR - playground internal endpoints: no auth, ok:true on ghosts
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-11: /api/playground/{id}/internal/* take no "
           "Authorization header and answer {'ok': true} for a session that "
           "does not exist, while the matching GETs 404 "
           "(backend/app/routers/playground.py:226-257)",
)
@pytest.mark.parametrize(
    "suffix,body",
    [
        ("internal/status", {"status": "completed"}),
        ("internal/log", {"lines": ["injected"]}),
    ],
)
def test_playground_internal_write_to_unknown_session_is_rejected(api, suffix, body):
    ghost = f"no-such-session-{uuid.uuid4().hex}"
    r = api.post(f"/api/playground/{ghost}/{suffix}", json=body)
    assert r.status_code in (401, 403, 404), (
        f"unauthenticated write to a ghost session returned {r.status_code} {r.text[:120]}"
    )


def test_playground_reads_do_404_on_unknown_session(api):
    """The read side is correct - guard it."""
    ghost = f"no-such-session-{uuid.uuid4().hex}"
    assert api.get(f"/api/playground/{ghost}/status").status_code == 404
    assert api.get(f"/api/playground/{ghost}/result").status_code == 404


# =============================================================================
# QA-API-12  MINOR - PATCH silently ignores feature_id on a user story
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-12: UserStoryUpdate has no feature_id field "
           "(backend/app/schemas/spec.py:64-68), so re-parenting a story "
           "returns 200 and changes nothing",
)
def test_reparenting_a_story_either_works_or_fails_loudly(api):
    src = api.post("/api/features", json={"title": unique("src")}).json()["id"]
    dst = api.post("/api/features", json={"title": unique("dst")}).json()["id"]
    story = api.post(
        "/api/user-stories", json={"feature_id": src, "title": "movable"}
    ).json()["id"]

    r = api.patch(f"/api/user-stories/{story}", json={"feature_id": dst})
    if r.status_code == 200:
        assert r.json()["feature_id"] == dst, (
            "PATCH returned 200 but the story is still under the old feature"
        )
    else:
        assert r.status_code == 422


# =============================================================================
# QA-API-13  MINOR - no length/whitespace validation on any name or title
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-13: no name/title field on any entity has a "
           "min_length, a strip, or a max_length, so empty strings, "
           "whitespace-only names, NUL bytes and 1 MB blobs are all stored",
)
@pytest.mark.parametrize("bad_name", ["", "   ", "\t\n ", "nul\x00name"])
def test_blank_and_control_char_names_are_rejected(api, bad_name):
    r = api.post("/api/agent-files", json={"name": bad_name, "content": "x"})
    assert r.status_code == 422, f"name={bad_name!r} accepted with {r.status_code}"


@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-13b: a 1 MB name is stored verbatim and then "
           "rendered by every list view",
)
def test_absurdly_long_name_is_rejected(api):
    r = api.post("/api/agent-files", json={"name": "A" * 1_000_000, "content": "x"})
    assert r.status_code == 422, f"1 MB name accepted with {r.status_code}"


def test_nul_byte_in_a_name_round_trips_verbatim(api):
    """Documents the current storage behaviour that QA-API-13 will change."""
    name = f"nul\x00{uuid.uuid4().hex[:6]}"
    r = api.post("/api/agent-files", json={"name": name, "content": "c\x00d"})
    assert r.status_code == 201
    got = api.get(f"/api/agent-files/{r.json()['id']}").json()
    assert got["name"] == name and "\x00" in got["content"]


# =============================================================================
# QA-API-14  POLISH - duplicate-name conflicts use inconsistent status codes
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-14: agent-files answers 400 for a duplicate name "
           "while prompt-templates answers 409 for the same class of conflict "
           "(backend/app/routers/agent_files.py:26)",
)
def test_duplicate_name_conflicts_use_409_everywhere(api):
    af = {"name": unique("dupe-af"), "content": "x"}
    assert api.post("/api/agent-files", json=af).status_code == 201
    assert api.post("/api/agent-files", json=af).status_code == 409

    pt = {"name": unique("dupe-pt")}
    assert api.post("/api/prompt-templates", json=pt).status_code == 201
    assert api.post("/api/prompt-templates", json=pt).status_code == 409


# =============================================================================
# QA-API-15  POLISH - commits?limit has no bound (every other limit does)
# =============================================================================

@pytest.mark.xfail(
    strict=True,
    reason="QA finding QA-API-15: GET /api/repos/{id}/commits declares "
           "limit: int = 20 with no ge/le, unlike /api/pipeline-runs and "
           "/api/test-refs which both bound it",
)
@pytest.mark.parametrize("limit", [-1, 0, 10 ** 12])
def test_commits_limit_is_bounded(api, repo, limit):
    r = api.get(f"/api/repos/{repo}/commits", params={"limit": limit})
    assert r.status_code == 422, f"limit={limit} accepted with {r.status_code}"


# =============================================================================
# Guards: behaviour that is ALREADY correct and must stay that way
# =============================================================================

@pytest.mark.parametrize(
    "payload,ctype",
    [
        (None, "application/json"),
        ("", "application/json"),
        ("{", "application/json"),
        ("[1,2,3]", "application/json"),
        ('"scalar"', "application/json"),
        ("null", "application/json"),
        ('{"name":"x"}', "text/plain"),
        ("name=x", "application/x-www-form-urlencoded"),
        ("<a/>", "application/xml"),
    ],
)
def test_malformed_bodies_and_content_types_are_clean_422s(api, payload, ctype):
    r = api.post("/api/agent-files", data=payload, headers={"Content-Type": ctype})
    assert r.status_code == 422, f"{ctype} {payload!r} -> {r.status_code} {r.text[:150]}"


@pytest.mark.parametrize("value", [12345, None, ["a"], {"a": 1}, True, 1.5])
def test_wrong_types_are_clean_422s(api, value):
    r = api.post("/api/agent-files", json={"name": value, "content": "x"})
    assert r.status_code == 422


@pytest.mark.parametrize("depth", [100, 1500, 3000])
def test_deeply_nested_json_does_not_crash_the_worker(api, depth):
    body = ('{"name":"deep-%d-%s","content":"x","extra":' % (depth, uuid.uuid4().hex[:6])
            + "[" * depth + "]" * depth + "}")
    r = api.post("/api/agent-files", data=body, headers={"Content-Type": "application/json"})
    assert r.status_code < 500
    assert api.get("/health").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/cards/not-a-uuid",
        "/api/cards/' OR '1'='1",
        "/api/cards/" + "a" * 5000,
        "/api/repos/deadbeef/branches",
        "/api/pipelines/deadbeef/export/yaml",
        "/api/criteria/deadbeef/history",
        "/api/step-runs/deadbeef",
        "/api/jobs/deadbeef/logs",
        "/api/pipeline-runs/deadbeef/usage",
    ],
)
def test_malformed_and_unknown_ids_are_clean_404s(api, path):
    r = api.get(path)
    assert r.status_code == 404, f"{path} -> {r.status_code} {r.text[:150]}"


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/test-refs", {"limit": 0}),
        ("/api/test-refs", {"offset": -1}),
        ("/api/pipeline-runs", {"limit": 101}),
        ("/api/models", {"refresh": "yesplease"}),
    ],
)
def test_bounded_query_params_reject_out_of_range(api, path, params):
    assert api.get(path, params=params).status_code == 422


def test_child_creation_validates_its_parent(api):
    ghost = uuid.uuid4().hex
    assert api.post(
        "/api/user-stories", json={"feature_id": ghost, "title": "orphan"}
    ).status_code == 404
    assert api.post(
        "/api/criteria", json={"user_story_id": ghost, "text": "orphan"}
    ).status_code == 404
    assert api.post(
        "/api/features", json={"title": "ghosty", "repo_ids": [ghost]}
    ).status_code == 400


def test_deleting_a_feature_cascades_to_stories_and_criteria(api):
    f = api.post("/api/features", json={"title": unique("casc")}).json()["id"]
    s = api.post("/api/user-stories", json={"feature_id": f, "title": "s"}).json()["id"]
    c = api.post("/api/criteria", json={"user_story_id": s, "text": "c"}).json()["id"]
    assert api.delete(f"/api/features/{f}").status_code == 204
    assert api.get(f"/api/user-stories/{s}").status_code == 404
    assert api.get(f"/api/criteria/{c}").status_code == 404


def test_deleting_a_repo_cascades_to_cards_and_pipelines(api, plain_repo):
    c = api.post(f"/api/repos/{plain_repo}/cards", json={"title": "doomed"}).json()["id"]
    p = api.post(f"/api/repos/{plain_repo}/pipelines", json={"name": "doomed"}).json()["id"]
    assert api.delete(f"/api/repos/{plain_repo}").status_code == 204
    assert api.get(f"/api/cards/{c}").status_code == 404
    assert api.get(f"/api/pipelines/{p}").status_code == 404


def test_ingest_refuses_arbitrary_server_side_paths(api):
    for path in ["/etc", "/", "/app", "/root", "~", "/proc/self", "../../../"]:
        r = api.post("/api/repos/ingest", json={"name": unique("qa-lfi"), "path": path})
        assert r.status_code == 400, f"path={path!r} -> {r.status_code} {r.text[:150]}"


def test_git_smart_http_validates_the_service_parameter(api, repo):
    assert api.get(f"/git/{repo}.git/info/refs", params={"service": "rm -rf /"}).status_code == 400
    assert api.get(f"/git/{repo}.git/info/refs").status_code == 422
    assert api.get("/git/deadbeef.git/info/refs",
                   params={"service": "git-upload-pack"}).status_code == 404


def test_seed_milestone12_is_idempotent(api):
    first = api.post("/api/features/seed-milestone12")
    assert first.status_code == 200
    before = len(api.get("/api/features").json())
    second = api.post("/api/features/seed-milestone12")
    assert second.status_code == 200
    assert second.json()["feature"]["id"] == first.json()["feature"]["id"]
    assert len(api.get("/api/features").json()) == before


def test_retry_does_guard_its_state_machine(api, repo):
    """retry gets this right; approve/reject do not. Keep retry honest."""
    card = api.post(f"/api/repos/{repo}/cards", json={"title": "guarded"}).json()["id"]
    r = api.post(f"/api/cards/{card}/retry", json={})
    assert r.status_code == 400
    assert "todo" in r.text
