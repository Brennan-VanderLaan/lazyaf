# LazyAF Quick Start

## Prerequisites
- Docker & Docker Compose
- Anthropic API key

## 1. Start the Stack

```bash
# Set your API key
echo "ANTHROPIC_API_KEY=your_key_here" > .env

# Start backend + frontend
docker compose up -d

# Verify
curl http://localhost:8000/health
```

Frontend: http://localhost:80
Backend: http://localhost:8000

## 2. Start a Runner

```bash
# Build runner image
docker build -t lazyaf-runner -f backend/runner/Dockerfile backend/runner

# Start runner (connects to backend)
docker run --rm \
  -e BACKEND_URL=http://host.docker.internal:8000 \
  -e ANTHROPIC_API_KEY=your_key_here \
  --add-host=host.docker.internal:host-gateway \
  lazyaf-runner
```

## 3. Install CLI

```bash
cd cli
pip install -e .
```

## 4. Ingest a Repo

```bash
# Basic ingest
lazyaf ingest /path/to/your/repo --name my-project

# Ingest specific branch
lazyaf ingest /path/to/your/repo --name my-project --branch main

# Ingest all branches
lazyaf ingest /path/to/your/repo --name my-project --all-branches
```

## 5. Use the UI

1. Open http://localhost:80
2. Create a card with your feature request
3. Click "Start" - runner picks it up
4. Watch real-time progress
5. Review the branch when complete
6. Use `lazyaf land` to push to your real remote

## Troubleshooting

**Runner can't connect to backend:**
```bash
# Check backend is running
docker compose ps

# Check runner logs
docker logs <runner-container-id>
```

**CLI can't connect:**
```bash
# Verify backend is up
curl http://localhost:8000/api/repos

# Set custom server
export LAZYAF_SERVER=http://localhost:8000
```

See `PLAN.md` for architecture details.
