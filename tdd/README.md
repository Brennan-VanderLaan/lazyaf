# LazyAF Test Suite

This directory contains the complete test suite for LazyAF, organized by test type following TDD best practices.

## Directory Structure

```
tdd/
├── unit/                    # Fast, isolated tests
│   ├── models/              # SQLAlchemy model tests
│   ├── schemas/             # Pydantic schema tests
│   ├── execution/           # Dormant 12.6 contract suites ("12.6-dormant:" skips)
│   ├── scripts/             # Tests for repo-level scripts (e.g. ci_gate.py)
│   └── helpers/             # Unit test utilities
├── integration/             # API and database tests
│   ├── api/                 # FastAPI endpoint tests
│   ├── services/execution/  # Docker-dependent executor + chaos tests (tier T2)
│   ├── fixtures/            # Test data
│   └── setup/               # Test configuration
├── e2e/                     # End-to-end tests (quick tier + slow full-stack)
├── demos/                   # Smoke tests and demos
│   ├── scenarios/           # Workflow demonstrations
│   └── scripts/             # Demo runners
├── shared/                  # Cross-cutting utilities
│   ├── factories/           # Test data factories
│   ├── mocks/               # Mock implementations
│   └── assertions/          # Custom assertions
├── config/                  # Test configuration
├── skip_baseline.json       # Allowlisted skip-reason prefixes (R4 gate)
├── tier_floors.json         # Committed executed-count floors per CI tier (R4 gate)
└── conftest.py              # Shared pytest fixtures
```

## Running Tests

### Prerequisites

Install test dependencies:

```bash
cd backend
uv pip install -e ".[test]"
```

### Run All Tests

```bash
cd backend
uv run pytest ../tdd
```

### Run by Test Type

```bash
# Unit tests only (fast)
uv run pytest ../tdd/unit -v

# Integration tests only
uv run pytest ../tdd/integration -v

# Demo/smoke tests
uv run pytest ../tdd/demos -v -s
```

### Run with Markers

```bash
# Only unit tests
uv run pytest ../tdd -m unit

# Only integration tests
uv run pytest ../tdd -m integration

# Only demo tests
uv run pytest ../tdd -m demo

# Exclude slow tests
uv run pytest ../tdd -m "not slow"
```

### Run with Coverage

```bash
uv run pytest ../tdd --cov=app --cov-report=html --cov-report=term-missing
```

Coverage report will be generated in `backend/htmlcov/`.

## Test Types

### Unit Tests (`tdd/unit/`)

- **Purpose**: Test individual functions and classes in isolation
- **Speed**: Milliseconds per test
- **Dependencies**: Mocked (no database, no network)
- **When to run**: Every commit, every file save
- **Example**: Testing Pydantic schema validation, model field defaults

### Integration Tests (`tdd/integration/`)

- **Purpose**: Test API endpoints with real database
- **Speed**: Seconds per test
- **Dependencies**: In-memory SQLite database
- **When to run**: Every PR, before merge
- **Example**: Testing full CRUD operations on repos, cards, jobs

### Demo Tests (`tdd/demos/`)

- **Purpose**: Demonstrate complete workflows, serve as living documentation
- **Speed**: Seconds to minutes
- **Dependencies**: Full application stack
- **When to run**: Manually, scheduled CI, before releases
- **Example**: Complete card lifecycle from creation to approval

## Writing Tests

### Test Naming

- Files: `test_<subject>.py` or `<subject>_test.py`
- Classes: `Test<Subject>`
- Methods: `test_<behavior_being_tested>`

### Using Factories

```python
from tdd.shared.factories import RepoFactory, CardFactory

# Create model instance
repo = RepoFactory.build(name="MyRepo")

# Create with specific traits
card = CardFactory.build(in_progress=True)

# Create API payload
payload = repo_create_payload(name="TestRepo")
```

### Using Assertions

```python
from tdd.shared.assertions import (
    assert_created_response,
    assert_not_found,
    assert_json_contains,
)

# Assert API responses
assert_created_response(response, {"name": "MyRepo"})
assert_not_found(response, "Repo")
assert_json_contains(response, {"status": "ok"})
```

### Async Tests

All tests use pytest-asyncio with auto mode:

```python
async def test_create_repo(self, client):
    response = await client.post("/api/repos", json={...})
    assert response.status_code == 201
```

## Fixtures

Key fixtures available in all tests:

- `client`: AsyncClient for making HTTP requests to the API
- `db_session`: AsyncSession for direct database access
- `repo`: Pre-created Repo (in card/job tests)
- `card`: Pre-created Card (in job tests)

## CI/CD Integration (dogfood CI)

There is no external CI and never will be — **LazyAF gates LazyAF**. The
pipeline definition lives in `.lazyaf/pipelines/test-suite.yaml`, is re-synced
from the pushed commit on every push to the default branch, and is bound to a
`push` trigger on `main`. A push to the internal remote runs the suite as
**three tiers** (one pipeline step each) inside a runner container, and each
tier is gated by `scripts/ci_gate.py` (standing rule R4: no fake green).

### The tiers (single-sourced in `scripts/run_tier.py`)

The exact pytest selection, junitxml artifact, and `scripts/ci_gate.py`
invocation for every tier live in **one place: `scripts/run_tier.py`**
(stdlib-only, runs on bare `python3` on Linux and Windows). The pipeline
steps, the `scripts/test.sh` / `scripts/test.ps1` `tier` and `all` lanes, and
local runs all invoke it — never a hand-copied pytest command:

| Tier | Command | Covers | Needs |
|------|---------|--------|-------|
| T1 | `python3 scripts/run_tier.py T1` | unit + demos + non-Docker integration | nothing beyond the venv |
| T2 | `python3 scripts/run_tier.py T2` | `tdd/integration/services` (real-Docker executor, 12.2-INT workspace/local-execution suites, 12.3 HOME-persistence + base-image contracts) | a Docker socket **and** fresh `lazyaf-*:dev` images — preflighted via `build_images.py --check`, see below |
| T3 | `python3 scripts/run_tier.py T3` | e2e quick tier (`tdd/e2e`, not slow) | nothing (spawns its own uvicorn on localhost) |

Convention: any new Docker-dependent integration test goes under
`tdd/integration/services/` so it lands in T2 — T1 must stay
runnable with no Docker socket.

### Step images (Phase 12.3 build prerequisite)

The dogfood pipeline steps and the control-layer/HOME-persistence tests run
on the locally-built step images `lazyaf-base:dev` / `lazyaf-test-runner:dev`
(`lazyaf-claude:dev` for agent work). **`:dev` tags are built, never
pulled** — build them once (and after editing `images/**`) with:

```bash
python scripts/build_images.py        # builds base -> claude -> test-runner
python scripts/build_images.py --check   # exits nonzero listing missing/stale
./scripts/test.sh images              # same, via the test-script lane
```

The build script skips fresh images via their `lazyaf.content-hash` label.
If an image is missing, a pipeline step fails loudly with
`Image not found: lazyaf-base:dev` (the backend never auto-builds) and the
control-layer e2e tests fail with the build hint. **`scripts/run_tier.py T2`
preflights `python scripts/build_images.py --check`** before pytest: a
missing/stale image is a loud tier failure printing the exact rebuild
command — never a skip, so there is no `12.3-images:` entry in
`tdd/skip_baseline.json` and the T2 floor counts every image-dependent test.
Host devs running bare pytest without the images hit the same loud story
(a failure or an *unbaselined* skip pointing at `build_images.py`).

T2 runs on runner services with `/var/run/docker.sock` mounted (deliberate
interim Docker-outside-of-Docker, see `docker-compose.yml`; retired at Phase
12.4 when step containers get a socket option). Docker being unreachable
fails loudly in the shared `docker_client` fixture
(`tdd/integration/conftest.py` — `from_env` + `ping`, never a skip). That
conftest also owns the DooD-safe addressing helpers: a test that hosts a
server (uvicorn, stub backend) binds it on `0.0.0.0:<free port>` and
advertises the address a **sibling** container can reach — the test
container's own IP when the suite runs inside a container (the CI path), or
`host.docker.internal` on the host (Linux-Engine hosts may need
`--add-host host.docker.internal:host-gateway`).

### Known exclusion: the slow e2e tests run in NO tier (stated per R4)

The `@pytest.mark.slow` e2e tests (control layer, real card execution,
graph pipeline full-stack in `tdd/e2e/`) are **not run by any tier today** —
this is a stated exclusion under standing rule R4, not a silent cap. They
need the compose e2e stack (`backend-e2e` + `runner-mock-e2e`), which the
legacy runner hosting dogfood CI cannot start. They remain runnable on the
host via the `scripts/test.ps1 slow` / `scripts/test.sh slow` lane (which
brings the stack up, runs `pytest /tdd/e2e -m slow` inside the backend-e2e
container, and tears the stack down), and they enter dogfood CI at Phase
12.4/12.5 when ephemeral execution can host the stack. The control-layer
e2e tests (`tdd/e2e/test_control_layer.py`) additionally require
`lazyaf-base:dev` on the daemon (see "Step images" above) and FAIL loudly —
never skip — when it is missing or unlabeled.

### The gate (`scripts/ci_gate.py`)

After each tier's pytest writes `--junitxml`, the gate parses it and **fails** if:

1. any skipped test's reason does not start with an allowlisted
   `reason_prefix` in `tdd/skip_baseline.json`, or
2. the executed count (passed + failed) is below the tier's committed floor
   in `tdd/tier_floors.json`.

`scripts/run_tier.py` runs the gate automatically after a green pytest (a red
pytest exits red before the gate). Run any tier locally exactly as CI does:

```bash
python3 scripts/run_tier.py T3          # one tier
python3 scripts/run_tier.py T1 T3      # the no-Docker tiers (= scripts/test all)
python3 scripts/run_tier.py T1 -- -x   # args after -- go to pytest verbatim
```

### The ratchet rules

- **Adding a skip**: baseline its reason prefix (with a note) in
  `tdd/skip_baseline.json` *in the same commit*. Prefer `xfail(strict=True)`
  when the target is known-missing — xfails are not gated.
- **Un-skipping**: shrink the baseline in the same commit. The `12.6-dormant:`
  prefix (the ported 12.6 contract suites in `tdd/unit/execution/`) must hit
  zero skips by 12.6's exit gate.
- **Adding tests**: raise the tier's floor in `tdd/tier_floors.json`
  (measured executed count minus ~2% slack). Floors only ratchet up;
  lowering one requires a PLAN.md note explaining why.
- **Floors are per-tier**, so moving tests between tiers means re-measuring
  both floors in the same commit.

## Adding New Tests

1. Identify the test type (unit/integration/demo)
2. Create test file in appropriate directory
3. Use factories for test data
4. Use assertions for validation
5. Add appropriate markers if needed
6. Run the test locally before committing

## Troubleshooting

### Tests not discovered

Ensure files match pattern `test_*.py` and classes match `Test*`.

### Import errors

Run from the `backend` directory, or ensure paths are configured:

```bash
cd backend
uv run pytest ../tdd
```

### Async errors

Ensure `asyncio_mode = auto` is set and test methods are `async def`.
