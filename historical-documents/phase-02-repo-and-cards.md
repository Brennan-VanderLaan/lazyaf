# Phase 2: Repo & Card Management

> **Status**: COMPLETE
> **Goal**: Can attach repos and create/view cards

## Completed Tasks

- [x] Implement Repo CRUD endpoints
- [x] Implement Card CRUD endpoints
- [x] Build RepoSelector component
- [x] Build Board + Column + Card components
- [x] Card drag-and-drop between columns
- [x] CardModal for create/edit

## Deliverable

Can create cards on a board for an attached repo

## Key Components

### Data Models

```python
class CardStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"

class Card:
    id: UUID
    repo_id: UUID
    title: str
    description: str
    status: CardStatus
    branch_name: str | None
    pr_url: str | None
    job_id: UUID | None
    created_at: datetime
    updated_at: datetime
```

### API Endpoints

```
GET    /api/repos/{repo_id}/cards    # List cards for repo
POST   /api/repos/{repo_id}/cards    # Create card
GET    /api/cards/{id}               # Get card details
PATCH  /api/cards/{id}               # Update card
DELETE /api/cards/{id}               # Delete card
POST   /api/cards/{id}/start         # Trigger agent work
POST   /api/cards/{id}/approve       # Approve PR, move to done
POST   /api/cards/{id}/reject        # Reject, back to todo
```
