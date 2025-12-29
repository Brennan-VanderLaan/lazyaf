# LazyAF Test Suite

This directory contains the complete test suite for LazyAF, organized by test type following TDD best practices.

## Directory Structure

```
tdd/
├── unit/                    # Fast, isolated tests
│   ├── models/              # SQLAlchemy model tests
│   ├── schemas/             # Pydantic schema tests
│   └── helpers/             # Unit test utilities
├── integration/             # API and database tests
│   ├── api/                 # FastAPI endpoint tests
│   ├── fixtures/            # Test data
│   └── setup/               # Test configuration
├── demos/                   # Smoke tests and demos
│   ├── scenarios/           # Workflow demonstrations
│   └── scripts/             # Demo runners
├── shared/                  # Cross-cutting utilities
│   ├── factories/           # Test data factories
│   ├── mocks/               # Mock implementations
│   └── assertions/          # Custom assertions
├── config/                  # Test configuration
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

## CI/CD Integration

Tests run automatically in GitHub Actions:

1. **lint-and-unit**: Runs on every push (<2 min)
2. **integration**: Runs on PRs (<10 min)
3. **coverage**: Generates coverage reports
4. **demo**: Runs on main branch or manual trigger

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
