# LazyAF Quick Start

## 1. Start everything

```bash
cp .env.example .env
# Edit .env with your API keys (Anthropic and/or Gemini)

docker compose up -d
```

Open http://localhost:5173

## 2. Import a repo

```bash
pip install -e cli/
lazyaf ingest /path/to/your/repo --name my-project
```

## 3. Create and run a card

1. Select your repo in the UI
2. Click "New Card"
3. Title: "Document the project"
4. Description: "Create a file called rocks-that-think.txt explaining what this project does"
5. Click "Create", then "Start"
6. Watch the agent work in real-time
7. Review the diff when done, then Approve or Reject

## 4. Sync changes

The CLI adds `lazyaf` as a git remote. Use standard git commands:

```bash
# Push local changes to lazyaf
git push lazyaf main

# Pull agent changes back to local
git fetch lazyaf
git merge lazyaf/card-123-feature-name

# Or cherry-pick specific commits
git cherry-pick <commit-sha>
```

## Extras

**Playground** - Test prompts without creating cards

**Pipelines** - Automate multi-step workflows

**Scale runners** - Set `CLAUDE_RUNNERS=3` or `GEMINI_RUNNERS=2` in `.env`

**Just one runner type** - `docker compose up -d backend frontend runner-claude`

See `PLAN.md` for architecture details.
